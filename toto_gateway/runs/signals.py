"""Routing signals: per-user preferences, feedback verdicts, and the task-embedding corpus.

Feedback + embeddings are the labeled data future routing learns from; preferences are the
user's explicit overrides. All strictly per-user scoped.
"""

from __future__ import annotations

import json
import time

from .scoping import _scope


class SignalsMixin:
    """Preferences/feedback/embeddings surface of RunStore."""

    # --- preferences ------------------------------------------------------------

    async def get_preferences(self, user_id: str | None = None) -> dict:
        """{"optimize": str|None, "pins": {skill: model_id}, "label_models": {label: model_id}}
        — absent keys mean defaults. Per-user: a logged-in caller reads only their own rows
        (strict isolation)."""
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT key, value FROM preferences" + (f" WHERE {clause}" if clause else ""), params)
        out = {r["key"]: json.loads(r["value"]) for r in rows}
        return {"optimize": out.get("optimize"), "pins": out.get("pins") or {},
                "label_models": out.get("label_models") or {}}

    async def set_preferences(self, prefs: dict, user_id: str | None = None) -> None:
        for key in ("optimize", "pins", "label_models"):
            if key in prefs:
                await self._exec(
                    "INSERT INTO preferences (user_id, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                    (user_id, key, json.dumps(prefs[key])),
                )

    # --- feedback ---------------------------------------------------------------

    async def set_feedback(self, run_id: str, task_id: str, model_id: str, skill: str,
                     verdict: str, user_id: str | None = None) -> None:
        """Idempotent per (run, task): a re-tap flips the verdict. Fail-closed ownership guard on
        the upsert — a re-tap only flips a row the caller owns; a cross-tenant (run,task) collision
        DO-UPDATEs nothing rather than overwriting another user's verdict. Null-safe so operator
        (user_id NULL) can still flip operator-written rows."""
        # SQLite: `IS` is null-safe equality; PG: `IS NOT DISTINCT FROM`. Both compare NULL=NULL true.
        guard = "feedback.user_id IS NOT DISTINCT FROM ?" if self._pg else "feedback.user_id IS ?"
        await self._exec(
            "INSERT INTO feedback (run_id, task_id, model_id, skill, verdict, created_at, "
            "user_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, task_id) DO UPDATE SET "
            "model_id = excluded.model_id, skill = excluded.skill, "
            "verdict = excluded.verdict, created_at = excluded.created_at "
            "WHERE " + guard,
            (run_id, task_id, model_id, skill, verdict, time.time(), user_id, user_id),
        )

    async def feedback_for_run(self, run_id: str, user_id: str | None = None) -> list[dict]:
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT task_id, model_id, skill, verdict FROM feedback WHERE run_id = ?"
            + (f" AND {clause}" if clause else ""),
            (run_id, *params),
        )
        return [dict(r) for r in rows]

    async def feedback_summary(self, user_id: str | None = None) -> list[dict]:
        """Labeled-data aggregates: per (model, skill), how often the user affirmed/rejected."""
        clause, params = _scope(user_id)
        # PG has no SUM(boolean); COUNT(*) FILTER is the portable count-of-matches (sqlite 3.30+
        # supports FILTER too, but SUM(bool) is the established sqlite form — keep both explicit).
        agg = ("COUNT(*) FILTER (WHERE verdict = 'up') AS up, "
               "COUNT(*) FILTER (WHERE verdict = 'down') AS down") if self._pg else \
              "SUM(verdict = 'up') AS up, SUM(verdict = 'down') AS down"
        rows = await self._all(
            "SELECT model_id, skill, " + agg + " FROM feedback "
            + (f"WHERE {clause} " if clause else "")
            + "GROUP BY model_id, skill ORDER BY model_id, skill",
            params,
        )
        return [dict(r) for r in rows]

    # --- embeddings (routing corpus + cache) -----------------------------------

    async def write_task_embedding(self, run_id: str, task_id: str, text: str, vector: list[float],
                             skill: str = "", model_id: str = "", outcome: str = "",
                             cost_usd: float | None = None, latency_ms: int | None = None,
                             user_id: str | None = None) -> None:
        """Persist one dispatched task's embedding + provenance — the experience corpus for
        future kNN routing. text is task+description only; the answer is never stored."""
        await self._exec(
            "INSERT INTO task_embeddings (run_id, task_id, text, vector, skill, model_id, "
            "outcome, cost_usd, latency_ms, user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, task_id) DO UPDATE SET "
            "text=excluded.text, vector=excluded.vector, skill=excluded.skill, "
            "model_id=excluded.model_id, outcome=excluded.outcome, cost_usd=excluded.cost_usd, "
            "latency_ms=excluded.latency_ms",
            (run_id, task_id, text, json.dumps(vector), skill, model_id, outcome, cost_usd,
             latency_ms, user_id, time.time()),
        )

    async def experience_rows(self, limit: int = 5000) -> list[dict]:
        """The kNN corpus: each dispatched task's embedding + provenance + its feedback verdict
        (LEFT JOIN → verdict None when unlabeled). Newest-first, capped. vector stays a JSON string
        (the caller parses; keeps this a plain read). ponytail: full-table scan client-side is fine
        at thousands of rows — the LIMIT bounds it; add a vector index only when it measurably hurts."""
        rows = await self._all(
            "SELECT te.vector, te.model_id, te.skill, te.outcome, te.cost_usd, f.verdict "
            "FROM task_embeddings te "
            "LEFT JOIN feedback f ON f.run_id = te.run_id AND f.task_id = te.task_id "
            "WHERE te.model_id != '' ORDER BY te.created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def get_cached_embedding(self, hash: str) -> list[float] | None:
        row = await self._one("SELECT vector FROM embedding_cache WHERE hash = ?", (hash,))
        return json.loads(row["vector"]) if row else None

    async def put_cached_embedding(self, hash: str, vector: list[float]) -> None:
        await self._exec(
            "INSERT INTO embedding_cache (hash, vector, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(hash) DO NOTHING",
            (hash, json.dumps(vector), time.time()),
        )
