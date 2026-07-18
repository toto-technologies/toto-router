"""Benchmark reference store: multi-source score facts + model-name aliases.

Benchmark data is GLOBAL reference data — there is NO org/tenant scoping here (unlike the
observability/content planes). Same house plumbing though: own connection over the operational
DB via the `AsyncStoreMixin`, dual-dialect (SQLite + Postgres) from db.py.

Two tables, latest-only (no history rows — an upsert on the PK replaces the fact):

  benchmark_scores — one numeric fact per (canonical_id, benchmark_id, source, benchmark_version).
                     `value` is source-native (pct as 88.7, elo as 1423, fraction as 0.95);
                     normalization to a comparable scale is a later chunk's job. `redistributable`
                     0 = internal routing only, NEVER customer-facing display.
  model_aliases    — maps a foreign model name (source, source_model_id) → canonical_id, so an
                     ingest connector's names resolve onto the OpenRouter-slug join key.
"""

from __future__ import annotations

import threading
import time

from .. import db as _db_mod

_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_scores (
  canonical_id      TEXT NOT NULL,
  benchmark_id      TEXT NOT NULL,
  source            TEXT NOT NULL,
  benchmark_version TEXT NOT NULL DEFAULT '',
  value             REAL NOT NULL,
  value_raw         TEXT NOT NULL DEFAULT '',
  unit              TEXT NOT NULL DEFAULT '',
  source_url        TEXT NOT NULL DEFAULT '',
  license           TEXT NOT NULL DEFAULT '',
  redistributable   INTEGER NOT NULL DEFAULT 1,
  retrieved_at      REAL NOT NULL,
  PRIMARY KEY (canonical_id, benchmark_id, source, benchmark_version)
);
CREATE TABLE IF NOT EXISTS model_aliases (
  source          TEXT NOT NULL,
  source_model_id TEXT NOT NULL,
  canonical_id    TEXT NOT NULL,
  hugging_face_id TEXT NOT NULL DEFAULT '',
  confidence      REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (source, source_model_id)
);
"""

# Column order the upserts bind; every write goes through these so defaults land uniformly.
_SCORE_COLS = ("canonical_id", "benchmark_id", "source", "benchmark_version", "value",
               "value_raw", "unit", "source_url", "license", "redistributable", "retrieved_at")
_ALIAS_COLS = ("source", "source_model_id", "canonical_id", "hugging_face_id", "confidence")


class BenchmarkStore(_db_mod.AsyncStoreMixin):
    def __init__(self, path: str = ":memory:", database_url: str = "",
                 pool: dict | None = None) -> None:
        from .. import db as _db

        self._db, self._pg = _db.connect(database_url, path)          # sync conn: init DDL
        self._pool = _db.make_async_pool(database_url, **(pool or {}))  # async pool: runtime queries
        self._lock = threading.Lock()  # guards the shared sqlite conn (SQLite mode)
        self._db.executescript(_SCHEMA)

    # --- scores ----------------------------------------------------------------

    async def upsert_scores(self, rows: list[dict]) -> int:
        """Batch upsert score facts on the PK (latest-only — a re-ingest replaces the fact).
        Missing optional fields take the schema defaults; `retrieved_at` defaults to now.
        Returns the number of rows written."""
        if not rows:
            return 0
        now = time.time()
        params = [(
            r["canonical_id"], r["benchmark_id"], r["source"], r.get("benchmark_version", ""),
            float(r["value"]), r.get("value_raw", ""), r.get("unit", ""), r.get("source_url", ""),
            r.get("license", ""), int(r.get("redistributable", 1)), float(r.get("retrieved_at", now)),
        ) for r in rows]
        await self._many(
            "INSERT INTO benchmark_scores "
            "(canonical_id, benchmark_id, source, benchmark_version, value, value_raw, unit, "
            "source_url, license, redistributable, retrieved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(canonical_id, benchmark_id, source, benchmark_version) DO UPDATE SET "
            "value = excluded.value, value_raw = excluded.value_raw, unit = excluded.unit, "
            "source_url = excluded.source_url, license = excluded.license, "
            "redistributable = excluded.redistributable, retrieved_at = excluded.retrieved_at",
            params,
        )
        return len(rows)

    async def scores(self, canonical_id: str | None = None, benchmark_id: str | None = None,
                     source: str | None = None, redistributable_only: bool = False) -> list[dict]:
        """Score facts, optionally filtered, in a deterministic order. `redistributable_only`
        drops facts flagged internal-routing-only (redistributable = 0)."""
        where, params = [], []
        for col, val in (("canonical_id", canonical_id), ("benchmark_id", benchmark_id),
                         ("source", source)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if redistributable_only:
            where.append("redistributable = 1")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = await self._all(
            "SELECT canonical_id, benchmark_id, source, benchmark_version, value, value_raw, unit, "
            "source_url, license, redistributable, retrieved_at FROM benchmark_scores" + clause +
            " ORDER BY canonical_id, benchmark_id, source, benchmark_version",
            tuple(params),
        )
        return [dict(r) for r in rows]

    async def display_scores_page(
        self,
        *,
        after_canonical_id: str | None = None,
        limit: int = 50,
        benchmark_ids: tuple[str, ...] = (),
        provider: str = "",
        search: str = "",
        catalog_search_ids: tuple[str, ...] = (),
    ) -> tuple[list[dict], str | None]:
        """Return facts for one bounded keyset page of display model IDs."""
        if not 1 <= limit <= 100:
            raise ValueError("display score page limit must be between 1 and 100")
        # Keyset pagination must not inherit the deployment database's locale. SQLite's
        # BINARY collation and Postgres' C collation both compare the UTF-8 bytes, including
        # the case-sensitive tie-break needed when legacy IDs differ only by case.
        canonical_order = (
            'canonical_id COLLATE "C"' if self._pg else "canonical_id COLLATE BINARY"
        )
        where = ["redistributable = 1"]
        params: list = []
        if after_canonical_id is not None:
            where.append(f"{canonical_order} > ?")
            params.append(after_canonical_id)
        if benchmark_ids:
            where.append(
                "benchmark_id IN (" + ",".join("?" for _ in benchmark_ids) + ")"
            )
            params.extend(benchmark_ids)
        if provider:
            escaped = provider.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("LOWER(canonical_id) LIKE ? ESCAPE '\\'")
            params.append(f"{escaped.casefold()}/%")
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            search_clauses = ["LOWER(canonical_id) LIKE ? ESCAPE '\\'"]
            params.append(f"%{escaped.casefold()}%")
            if catalog_search_ids:
                search_clauses.append(
                    "LOWER(canonical_id) IN ("
                    + ",".join("?" for _ in catalog_search_ids)
                    + ")"
                )
                params.extend(canonical_id.casefold() for canonical_id in catalog_search_ids)
            where.append("(" + " OR ".join(search_clauses) + ")")
        id_rows = await self._all(
            f"SELECT DISTINCT {canonical_order} AS canonical_id FROM benchmark_scores WHERE "
            + " AND ".join(where)
            + " ORDER BY canonical_id LIMIT ?",
            (*params, limit + 1),
        )
        canonical_ids = [row["canonical_id"] for row in id_rows]
        selected = canonical_ids[:limit]
        facts = await self.display_scores_for_models(selected, exact=True)
        next_after = selected[-1] if len(canonical_ids) > limit and selected else None
        return facts, next_after

    async def display_scores_for_models(
        self, canonical_ids, *, max_rows: int = 5_000, exact: bool = False,
    ) -> list[dict]:
        """Load display facts for a bounded set of model IDs."""
        names = tuple(dict.fromkeys(
            str(value) if exact else str(value).casefold()
            for value in canonical_ids if value
        ))
        if not names:
            return []
        if len(names) > 500:
            raise ValueError("benchmark display model selection exceeded its bound")
        if not 1 <= max_rows <= 20_000:
            raise ValueError("benchmark display fact bound is invalid")
        placeholders = ",".join("?" for _ in names)
        canonical_order = (
            'canonical_id COLLATE "C"' if self._pg else "canonical_id COLLATE BINARY"
        )
        rows = await self._all(
            "SELECT canonical_id, benchmark_id, source, benchmark_version, value, value_raw, unit, "
            "source_url, license, redistributable, retrieved_at FROM benchmark_scores "
            "WHERE redistributable = 1 AND "
            + ("canonical_id" if exact else "LOWER(canonical_id)")
            + f" IN ({placeholders}) "
            f"ORDER BY {canonical_order}, benchmark_id, source, benchmark_version LIMIT ?",
            (*names, max_rows + 1),
        )
        if len(rows) > max_rows:
            raise ValueError("benchmark display fact page exceeded its row bound")
        return [dict(row) for row in rows]

    async def models(self) -> list[str]:
        """Distinct canonical_ids carrying at least one score fact."""
        rows = await self._all(
            "SELECT DISTINCT canonical_id FROM benchmark_scores ORDER BY canonical_id")
        return [r["canonical_id"] for r in rows]

    async def delete_source(self, source: str) -> int:
        """Wipe one source's score facts (re-ingest hygiene). Returns rows removed."""
        before = (await self._one(
            "SELECT COUNT(*) AS c FROM benchmark_scores WHERE source = ?", (source,)))["c"]
        await self._exec("DELETE FROM benchmark_scores WHERE source = ?", (source,))
        return before

    # --- aliases ---------------------------------------------------------------

    async def upsert_aliases(self, rows: list[dict]) -> int:
        """Batch upsert name→canonical mappings on (source, source_model_id). Returns rows written."""
        if not rows:
            return 0
        params = [(
            r["source"], r["source_model_id"], r["canonical_id"],
            r.get("hugging_face_id", ""), float(r.get("confidence", 1.0)),
        ) for r in rows]
        await self._many(
            "INSERT INTO model_aliases (source, source_model_id, canonical_id, hugging_face_id, "
            "confidence) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(source, source_model_id) DO UPDATE SET "
            "canonical_id = excluded.canonical_id, hugging_face_id = excluded.hugging_face_id, "
            "confidence = excluded.confidence",
            params,
        )
        return len(rows)

    async def resolve(self, source: str, source_model_id: str) -> str | None:
        """The canonical_id a foreign name maps to, or None. Exact canonical_id passthrough is the
        CALLER's concern — this only consults the alias table."""
        row = await self._one(
            "SELECT canonical_id FROM model_aliases WHERE source = ? AND source_model_id = ?",
            (source, source_model_id),
        )
        return row["canonical_id"] if row else None

    async def aliases(self, source: str | None = None,
                      max_confidence: float | None = None) -> list[dict]:
        """Alias rows, optionally filtered. `max_confidence` is the needs-review queue: it returns
        only aliases with confidence STRICTLY below it (pass 0.8 for the <0.8 review set)."""
        where, params = [], []
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if max_confidence is not None:
            where.append("confidence < ?")
            params.append(max_confidence)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = await self._all(
            "SELECT source, source_model_id, canonical_id, hugging_face_id, confidence "
            "FROM model_aliases" + clause + " ORDER BY source, source_model_id",
            tuple(params),
        )
        return [dict(r) for r in rows]

    # --- ops -------------------------------------------------------------------

    async def coverage(self) -> dict:
        """Freshness/ops snapshot: {models, facts, sources: {name: {facts, latest_retrieved_at}}}."""
        tot = await self._one(
            "SELECT COUNT(DISTINCT canonical_id) AS models, COUNT(*) AS facts FROM benchmark_scores")
        per = await self._all(
            "SELECT source, COUNT(*) AS facts, MAX(retrieved_at) AS latest FROM benchmark_scores "
            "GROUP BY source ORDER BY source")
        return {
            "models": tot["models"],
            "facts": tot["facts"],
            "sources": {r["source"]: {"facts": r["facts"], "latest_retrieved_at": r["latest"]}
                        for r in per},
        }
