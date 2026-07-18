"""ContentStore — the content-plane database for authored markdown AND the memory recall plane.

The content plane holds every authored markdown body in one `documents` table, keyed (tenant_id,
user_id, namespace, doc_id), plus a `doc_embeddings` side-table (per-chunk vectors + tsvector)
that IS the recall index — hybrid pgvector-cosine + tsvector-keyword retrieval, all in the same
Postgres. Conversation/session captures are just documents rows (namespace 'conversation' /
'session'), so long-term memory is durable and tenant-scoped with no second storage engine.

Connection: same dual-mode seam as runs.py (db.connect + make_async_pool). In prod this rides
the primary Postgres under a `content` schema (or a dedicated CONTENT_DATABASE_URL); SQLite is
dev-only. pgvector is created at init inside a try — unavailable → recall degrades to keyword-
only (parity with the old BM25 fallback); SQLite has neither, so it scores in Python.

Scoping is fail-CLOSED and does NOT reuse runs._scope (CRIT-1): every method requires a
non-empty (tenant_id, user_id) or raises ContentScopeError — there is no operator/unscoped
query path into the content plane, and the schema's NOT NULLs mean no NULL-owner rows can
exist at all. doc_ids are validated at this boundary (MED-8): brain slugs are strict
lowercase pseudo-paths, note ids are single tokens — a doc_id is a PK value, never a path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import time

from . import db as _db_mod

log = logging.getLogger("toto_gateway.content")

# Namespaces the memory recall plane auto-captures (vs. user-authored brain files / note bodies).
CAPTURE_NAMESPACES = ("conversation", "session")


def chunk_text(text: str, size: int = 800, max_chunks: int = 64) -> list[str]:
    """Paragraph-aware chunker for the embed-on-write path. Packs paragraphs up to `size`, hard-
    splits any single oversized paragraph, caps total chunks. ponytail: fixed-size packing, no
    overlap/sentence-splitting — upgrade only if recall quality on long docs demands it."""
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if cur and len(cur) + len(para) + 2 > size:
            chunks.append(cur)
            cur = ""
        while len(para) > size:                # a lone paragraph bigger than one chunk
            chunks.append(para[:size])
            para = para[size:]
        cur = f"{cur}\n\n{para}" if cur else para
        if len(chunks) >= max_chunks:
            break
    if cur and len(chunks) < max_chunks:
        chunks.append(cur)
    return chunks[:max_chunks]


def _pgvec(vec: list[float]) -> str:
    """A pgvector literal: '[0.1,0.2,...]'. Cast to ::vector in SQL — no pgvector Python dep."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _kw_score(query: str, text: str) -> float:
    """Python keyword overlap (SQLite / no-FTS fallback for ts_rank): fraction of distinct query
    tokens present in the chunk. Cheap, good enough at per-user corpus scale."""
    q = {w for w in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(w) > 1}
    if not q:
        return 0.0
    t = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return len(q & t) / len(q)


def token_sim(a: str, b: str) -> float:
    """Symmetric token overlap in [0,1]: |A∩B| / max(|A|,|B|) over distinct word tokens. Symmetric
    (unlike _kw_score) so a short distilled fact vs a long raw turn scores LOW — that's what lets
    the extraction dedupe reject a re-distilled fact without rejecting a fresh one buried in a long
    capture (memory_extract), and lets dreams cluster two same-length near-duplicate captures
    (dreams). ponytail: bag-of-words, no stemming — enough at per-user corpus scale."""
    ta = {w for w in re.findall(r"[a-z0-9]+", (a or "").lower()) if len(w) > 1}
    tb = {w for w in re.findall(r"[a-z0-9]+", (b or "").lower()) if len(w) > 1}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb + 1e-9)


def _rrf_fuse(ranked_lists, weights, top_n: int, c: int = 60) -> list[dict]:
    """Reciprocal-rank fusion: each doc scores Σ weight/(c + rank) across the input lists (each
    already best-first). Robust, scale-free — no cross-signal weight tuning. Fused by (namespace,
    doc_id); the first chunk_text seen represents the doc. Returns fused top-N, best-first."""
    scores: dict = {}
    meta: dict = {}
    for rows, w in zip(ranked_lists, weights):
        for rank, r in enumerate(rows):
            key = (r["namespace"], r["doc_id"])
            scores[key] = scores.get(key, 0.0) + w / (c + rank + 1)
            meta.setdefault(key, r)
    top = sorted(scores, key=lambda k: scores[k], reverse=True)[:top_n]
    return [{"namespace": k[0], "doc_id": k[1], "chunk_text": meta[k]["chunk_text"],
             "score": scores[k]} for k in top]


class ContentScopeError(ValueError):
    """A content-plane call without a non-empty (tenant_id, user_id) — refused, never unscoped."""


class ContentSlugError(ValueError):
    """A doc_id that fails boundary validation (traversal, bad charset, depth/length caps)."""


# Brain slugs: strict lowercase charset per segment, '/'-separated pseudo-folders.
# '.' is not in the charset, so '..' traversal is structurally impossible.
_BRAIN_SEGMENT = re.compile(r"^[a-z0-9_-]+$")
_BRAIN_MAX_DEPTH = 6
_MAX_DOC_ID_LEN = 512
# Note doc_ids are canvas object_ids ('note-8chars' today): one token, no slashes ever.
_NOTE_ID = re.compile(r"^[A-Za-z0-9._:-]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  tenant_id   TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  namespace   TEXT NOT NULL,
  doc_id      TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  body        TEXT NOT NULL DEFAULT '',
  frontmatter TEXT NOT NULL DEFAULT '{}',
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  deleted_at  REAL,
  PRIMARY KEY (tenant_id, user_id, namespace, doc_id)
);
CREATE TABLE IF NOT EXISTS doc_embeddings (
  tenant_id  TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  namespace  TEXT NOT NULL,
  doc_id     TEXT NOT NULL,
  chunk_idx  INTEGER NOT NULL,
  chunk_text TEXT NOT NULL DEFAULT '',
  embedding  TEXT,
  created_at REAL NOT NULL,
  PRIMARY KEY (tenant_id, user_id, namespace, doc_id, chunk_idx)
);
CREATE INDEX IF NOT EXISTS doc_emb_scope ON doc_embeddings (tenant_id, user_id);
"""


def _require_scope(tenant_id, user_id) -> None:
    """CRIT-1: no code path may issue an unscoped content query. Non-empty strings or refuse."""
    for name, value in (("tenant_id", tenant_id), ("user_id", user_id)):
        if not isinstance(value, str) or not value:
            raise ContentScopeError(f"content plane requires a non-empty {name}")


def validate_doc_id(namespace: str, doc_id) -> None:
    """Boundary validation (MED-8). Fail-closed: unknown namespaces are refused until they
    define their own doc_id shape (extension is one branch here, no schema change)."""
    if not isinstance(doc_id, str) or not doc_id or len(doc_id) > _MAX_DOC_ID_LEN:
        raise ContentSlugError(f"invalid doc_id for namespace {namespace!r}")
    if namespace == "brain" or namespace in CAPTURE_NAMESPACES:
        # Capture slugs (conversation/session) share brain's pseudo-path shape — they're generated
        # (date/hash), never user-supplied, but validated here anyway so the boundary is uniform.
        segments = doc_id.split("/")
        # split() makes leading/trailing '/' and 'a//b' produce empty segments → charset-rejected.
        if len(segments) > _BRAIN_MAX_DEPTH or not all(_BRAIN_SEGMENT.match(s) for s in segments):
            raise ContentSlugError(f"invalid {namespace} slug {doc_id!r}")
    elif namespace == "note":
        if not _NOTE_ID.match(doc_id):
            raise ContentSlugError(f"invalid note id {doc_id!r}")
    else:
        raise ContentSlugError(f"unknown content namespace {namespace!r}")


def _like_escape(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_out(row) -> dict:
    d = dict(row)
    fm = d.get("frontmatter")
    # SQLite (and PG jsonb TextLoader) hand back text; a plain PG `json` column would hand back
    # a parsed dict — normalize either way so callers always see a dict.
    d["frontmatter"] = json.loads(fm) if isinstance(fm, str) else (fm or {})
    return d


class ContentStore(_db_mod.AsyncStoreMixin):
    """Mirrors runs.py::RunStore's connection shape: sync conn for DDL, async pool for queries."""

    def __init__(self, path: str = ":memory:", database_url: str = "",
                 indexer=None, schema: str | None = None, pool: dict | None = None) -> None:
        self._db, self._pg = _db_mod.connect(database_url, path, schema)  # sync conn: init DDL
        self._pool = _db_mod.make_async_pool(database_url, schema, **(pool or {}))  # async pool
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()
        # Recall-index feature flags for the doc_embeddings side-table:
        #   _pg_fts  — Postgres tsvector keyword search (a generated column + GIN index)
        #   _vector  — pgvector cosine (the `vector` extension). Either absent → graceful degrade
        # (keyword-only, or Python scoring in SQLite). SQLite has neither: both stay False.
        self._pg_fts = False
        self._vector = False
        if self._pg:
            self._init_pg_index()
        # The embed-on-write seam (ContentIndexer, or None when the memory plane is off). Every
        # namespace flows through here after a successful put/delete — no per-route index code.
        self.indexer = indexer

    def _init_pg_index(self) -> None:
        """Add the Postgres-only recall columns/indexes to doc_embeddings, each inside a try so a
        missing capability degrades instead of failing boot. tsvector is core PG (keyword search);
        pgvector is the optional `vector` extension (cosine). Idempotent (IF NOT EXISTS)."""
        try:
            self._db.execute(
                "ALTER TABLE doc_embeddings ADD COLUMN IF NOT EXISTS tsv tsvector "
                "GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED")
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS doc_emb_tsv ON doc_embeddings USING GIN (tsv)")
            self._pg_fts = True
        except Exception:
            log.warning("content plane: Postgres FTS unavailable — recall scores in Python",
                        exc_info=True)
        try:
            self._db.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self._db.execute("ALTER TABLE doc_embeddings ADD COLUMN IF NOT EXISTS embv vector")
            self._vector = True
        except Exception:
            log.warning("content plane: pgvector unavailable — memory recall is keyword-only",
                        exc_info=True)

    @property
    def memory_mode(self) -> str:
        """What /readyz reports: 'on' (full cosine+keyword recall) or 'keyword-only' (Postgres
        without pgvector — the degraded surface). SQLite dev scores cosine in Python, so it's
        'on' too; only PG-with-the-extension-missing is 'keyword-only'."""
        return "keyword-only" if (self._pg and not self._vector) else "on"

    def close(self) -> None:
        self._db.close()

    def _mirror(self, method: str, *args) -> None:
        """Fire-and-forget into the recall index AFTER a successful content write/delete.
        Degrade-to-off is LAW: indexer missing, broken, or raising → logged no-op. A content
        operation NEVER fails or waits on the index (embedding does network I/O)."""
        if self.indexer is None:
            return
        try:
            getattr(self.indexer, method)(*args)
        except Exception:
            log.debug("index %s write-through failed (degrade-to-off)", method, exc_info=True)

    async def put_document(self, tenant_id: str, user_id: str, namespace: str, doc_id: str,
                           title: str = "", body: str = "",
                           frontmatter: dict | None = None) -> None:
        """Upsert. created_at preserved on update; a put revives a soft-deleted row."""
        _require_scope(tenant_id, user_id)
        validate_doc_id(namespace, doc_id)
        now = time.time()
        await self._exec(
            "INSERT INTO documents (tenant_id, user_id, namespace, doc_id, title, body, "
            "frontmatter, created_at, updated_at, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT (tenant_id, user_id, namespace, doc_id) DO UPDATE SET "
            "title = excluded.title, body = excluded.body, frontmatter = excluded.frontmatter, "
            "updated_at = excluded.updated_at, deleted_at = NULL",
            (tenant_id, user_id, namespace, doc_id, title, body,
             json.dumps(frontmatter or {}), now, now),
        )
        self._mirror("index", user_id, namespace, doc_id, title, body, frontmatter or {})

    async def get_document(self, tenant_id: str, user_id: str, namespace: str,
                           doc_id: str) -> dict | None:
        _require_scope(tenant_id, user_id)
        validate_doc_id(namespace, doc_id)
        row = await self._one(
            "SELECT namespace, doc_id, title, body, frontmatter, created_at, updated_at "
            "FROM documents WHERE tenant_id = ? AND user_id = ? AND namespace = ? "
            "AND doc_id = ? AND deleted_at IS NULL",
            (tenant_id, user_id, namespace, doc_id),
        )
        return _row_out(row) if row else None

    async def list_documents(self, tenant_id: str, user_id: str, namespace: str | None = None,
                             prefix: str | None = None, limit: int = 200,
                             offset: int = 0) -> list[dict]:
        """Newest-first, soft-deleted rows excluded. prefix filters doc_id (pseudo-folders)."""
        _require_scope(tenant_id, user_id)
        wheres = ["tenant_id = ?", "user_id = ?", "deleted_at IS NULL"]
        params: list = [tenant_id, user_id]
        if namespace is not None:
            wheres.append("namespace = ?")
            params.append(namespace)
        if prefix:
            wheres.append("doc_id LIKE ? ESCAPE '\\'")
            params.append(_like_escape(prefix) + "%")  # wildcard added here, never in the SQL text
        rows = await self._all(
            "SELECT namespace, doc_id, title, body, frontmatter, created_at, updated_at "
            "FROM documents WHERE " + " AND ".join(wheres) +
            " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        )
        return [_row_out(r) for r in rows]

    async def delete_document(self, tenant_id: str, user_id: str, namespace: str,
                              doc_id: str) -> None:
        """Soft delete (72h recovery window). Idempotent."""
        _require_scope(tenant_id, user_id)
        validate_doc_id(namespace, doc_id)
        await self._exec(
            "UPDATE documents SET deleted_at = ? WHERE tenant_id = ? AND user_id = ? "
            "AND namespace = ? AND doc_id = ? AND deleted_at IS NULL",
            (time.time(), tenant_id, user_id, namespace, doc_id),
        )
        # HIGH-7 fan-out: the embedding rows go too (fire-and-forget via the indexer).
        self._mirror("unindex", user_id, namespace, doc_id)

    async def hard_delete_document(self, tenant_id: str, user_id: str, namespace: str,
                                   doc_id: str) -> None:
        """Row gone — the compliance path (HIGH-7): bypasses the soft-delete window."""
        _require_scope(tenant_id, user_id)
        validate_doc_id(namespace, doc_id)
        await self._exec(
            "DELETE FROM documents WHERE tenant_id = ? AND user_id = ? "
            "AND namespace = ? AND doc_id = ?",
            (tenant_id, user_id, namespace, doc_id),
        )
        # HIGH-7 fan-out. Index-delete failure logs and never blocks — reconciliation is
        # re-index-from-truth (the boot backfill; the index is derived, the content plane isn't).
        self._mirror("unindex", user_id, namespace, doc_id)

    async def prune_documents(self, tenant_id: str, user_id: str, older_than: float,
                              limit: int) -> tuple[int, int]:
        """W3-C6 retention: HARD-delete this (tenant, user)'s documents last touched before
        `older_than` (updated_at, so an actively-edited doc is never aged out), newest-safe, bounded
        to `limit` rows this call. Each doc's doc_embeddings rows go WITH it (FK-by-convention — the
        recall index must never outlive its parent). Returns (documents_deleted, embeddings_deleted).
        ponytail: select-then-loop, one delete per doc — the composite key makes a set-based bounded
        delete awkward, and a backlog just drains over successive ticks (the caller bounds the tick)."""
        _require_scope(tenant_id, user_id)
        rows = await self._all(
            "SELECT namespace, doc_id FROM documents "
            "WHERE tenant_id = ? AND user_id = ? AND updated_at < ? "
            "ORDER BY updated_at LIMIT ?",
            (tenant_id, user_id, float(older_than), int(limit)))
        docs = embs = 0
        for r in rows:
            ns, doc_id = r["namespace"], r["doc_id"]
            embs += await self._exec_count(
                "DELETE FROM doc_embeddings WHERE tenant_id = ? AND user_id = ? "
                "AND namespace = ? AND doc_id = ?", (tenant_id, user_id, ns, doc_id))
            await self._exec(
                "DELETE FROM documents WHERE tenant_id = ? AND user_id = ? "
                "AND namespace = ? AND doc_id = ?", (tenant_id, user_id, ns, doc_id))
            docs += 1
        return docs, embs

    # --- recall index (doc_embeddings) ----------------------------------------

    async def upsert_embeddings(self, tenant_id: str, user_id: str, namespace: str, doc_id: str,
                                chunks: list[str], vectors: list[list[float]] | None) -> None:
        """Replace this doc's chunk rows (chunk count can change between edits). vectors None →
        keyword-only rows (no embedding). Scoped-write; callers are the indexer + backfill only."""
        _require_scope(tenant_id, user_id)
        await self._exec(
            "DELETE FROM doc_embeddings WHERE tenant_id = ? AND user_id = ? "
            "AND namespace = ? AND doc_id = ?", (tenant_id, user_id, namespace, doc_id))
        if not chunks:
            return
        now = time.time()
        use_embv = self._pg and self._vector
        for i, ch in enumerate(chunks):
            vec = vectors[i] if vectors and i < len(vectors) else None
            emb_json = json.dumps(vec) if vec is not None else None
            if use_embv:
                await self._exec(
                    "INSERT INTO doc_embeddings (tenant_id, user_id, namespace, doc_id, chunk_idx, "
                    "chunk_text, embedding, embv, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, CAST(? AS vector), ?)",
                    (tenant_id, user_id, namespace, doc_id, i, ch, emb_json,
                     _pgvec(vec) if vec is not None else None, now))
            else:
                await self._exec(
                    "INSERT INTO doc_embeddings (tenant_id, user_id, namespace, doc_id, chunk_idx, "
                    "chunk_text, embedding, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (tenant_id, user_id, namespace, doc_id, i, ch, emb_json, now))

    async def delete_embeddings(self, tenant_id: str, user_id: str, namespace: str,
                                doc_id: str) -> None:
        _require_scope(tenant_id, user_id)
        await self._exec(
            "DELETE FROM doc_embeddings WHERE tenant_id = ? AND user_id = ? "
            "AND namespace = ? AND doc_id = ?", (tenant_id, user_id, namespace, doc_id))

    async def search(self, tenant_id: str, user_id: str, query: str,
                     query_vec: list[float] | None, top_n: int = 20,
                     vec_w: float = 0.7, kw_w: float = 0.3) -> list[dict]:
        """Hybrid recall over this user's chunks: separate pgvector-cosine and tsvector-keyword
        result lists fused by reciprocal-rank fusion (RRF). Returns fused top-N candidates
        [{namespace, doc_id, chunk_text, score}], doc-deduped, best-first — the caller reranks/cuts
        to k. Postgres runs two indexed queries; SQLite / no-FTS scores in Python."""
        _require_scope(tenant_id, user_id)
        if not (query or "").strip():
            return []
        top_n = max(int(top_n), 1)
        if self._pg and self._pg_fts:
            vec_hits = kw_hits = []
            if self._vector and query_vec is not None:
                vec_hits = await self._all(
                    "SELECT namespace, doc_id, chunk_text FROM doc_embeddings "
                    "WHERE tenant_id = :t AND user_id = :u AND embv IS NOT NULL "
                    "ORDER BY embv <=> CAST(:qv AS vector) LIMIT :n",
                    {"t": tenant_id, "u": user_id, "qv": _pgvec(query_vec), "n": top_n})
            kw_hits = await self._all(
                "SELECT namespace, doc_id, chunk_text FROM doc_embeddings "
                "WHERE tenant_id = :t AND user_id = :u AND tsv @@ plainto_tsquery('english', :q) "
                "ORDER BY ts_rank(tsv, plainto_tsquery('english', :q)) DESC LIMIT :n",
                {"t": tenant_id, "u": user_id, "q": query, "n": top_n})
            return _rrf_fuse([[dict(r) for r in vec_hits], [dict(r) for r in kw_hits]],
                             [vec_w, kw_w], top_n)
        return await self._search_py(tenant_id, user_id, query, query_vec, top_n, vec_w, kw_w)

    async def _search_py(self, tenant_id, user_id, query, query_vec, top_n, vec_w, kw_w):
        """SQLite / no-FTS fallback: pull this user's chunks, rank each signal in Python, RRF-fuse.
        Corpus is per-user and small (the same brute-force blessing as the routing kNN)."""
        rows = [dict(r) for r in await self._all(
            "SELECT namespace, doc_id, chunk_text, embedding FROM doc_embeddings "
            "WHERE tenant_id = ? AND user_id = ?", (tenant_id, user_id))]
        vec_list, kw_list = [], []
        for r in rows:
            kw = _kw_score(query, r["chunk_text"])
            if kw > 0:
                kw_list.append((kw, r))
            vec = json.loads(r["embedding"]) if r.get("embedding") else None
            if query_vec is not None and vec:
                vec_list.append((_cosine(query_vec, vec), r))
        vec_ranked = [r for _, r in sorted(vec_list, key=lambda x: x[0], reverse=True)][:top_n]
        kw_ranked = [r for _, r in sorted(kw_list, key=lambda x: x[0], reverse=True)][:top_n]
        return _rrf_fuse([vec_ranked, kw_ranked], [vec_w, kw_w], top_n)

    async def documents_missing_embeddings(self, chunk: int, offset: int) -> list[dict]:
        """Live documents with NO embedding rows — the boot backfill's work list (rows written
        while the memory plane was off, or before an embedding key was configured). Embeddings are
        durable now, so this only touches genuinely-unindexed docs, not the whole corpus each boot."""
        rows = await self._all(
            "SELECT d.user_id, d.namespace, d.doc_id, d.title, d.body, d.frontmatter "
            "FROM documents d LEFT JOIN doc_embeddings e "
            "  ON e.tenant_id = d.tenant_id AND e.user_id = d.user_id "
            "  AND e.namespace = d.namespace AND e.doc_id = d.doc_id AND e.chunk_idx = 0 "
            # exclude empty docs: they produce no chunk row, so they'd never drop off the list
            # (a backfill infinite-loop) — and there's nothing to recall from them anyway.
            "WHERE d.deleted_at IS NULL AND e.doc_id IS NULL AND (d.body <> '' OR d.title <> '') "
            "ORDER BY d.tenant_id, d.user_id, d.namespace, d.doc_id LIMIT ? OFFSET ?",
            (chunk, offset))
        return [_row_out(r) for r in rows]


async def merge_note_bodies(objects: list[dict], resolver: "ContentResolver | None",
                            tenant_id: str | None, user_id: str | None) -> list[dict]:
    """Phase-2 read merge, shared by the API route (routes/objects.py:get_objects) and the
    companion's read_canvas: user note bodies live in the content plane — fold each one back
    into its payload so every reader sees the same object shape. No document → payload.body
    serves as-is; that fallback is PERMANENT (it IS the operator path, CRIT-1)."""
    if resolver is None or not tenant_id or not user_id or \
            not any(o["kind"] == "note" for o in objects):
        return objects
    docs = await resolver.resolve(tenant_id).list_documents(
        tenant_id, user_id, namespace="note",
        limit=100_000)  # ponytail: one query for every note body; paginate if canvases grow
    bodies = {d["doc_id"]: d["body"] for d in docs}
    for o in objects:
        if o["kind"] == "note" and o["object_id"] in bodies:
            o["payload"] = {**o["payload"], "body": bodies[o["object_id"]]}
    return objects


async def backfill_note_bodies(runs, resolver: "ContentResolver") -> int:
    """Phase-2 backfill, idempotent so it just runs every boot: any user-owned note row still
    carrying a body in canvas_objects.payload gets the body copied into the content plane
    (skipped if the document already exists), then stripped from the primary payload.
    NULL-owner (operator) notes are left alone — payload.body IS their storage (CRIT-1).
    ponytail: a per-row loop, no batching/journal — Alex 2026-07-04: no real users yet, no
    legacy data worth heavier migration machinery. Returns rows moved.
    """
    from .routes.deps import _resolve_tenant  # v1: tenant_id == user_id, resolved in ONE place

    # Short-circuit a CLEAN table instantly — no full scan every boot (C3: readiness never waits on
    # this). PG uses jsonb_exists(payload,'body') — NOT the `?` operator, which the store's ?→%s
    # placeholder translation (db._PgConn._t) would mangle. SQLite payload is TEXT → substring LIKE.
    # ponytail: LIKE '%"body"%' can false-POSITIVE (a body value mentioning "body"), which only costs
    # one extra scan that then moves 0 rows — never a false-negative, so correctness holds.
    body_test = "jsonb_exists(payload, 'body')" if runs._pg else "payload LIKE '%\"body\"%'"
    if await runs._one(
            f"SELECT 1 FROM canvas_objects WHERE kind = 'note' AND user_id IS NOT NULL "
            f"AND {body_test} LIMIT 1") is None:
        return 0

    rows = await runs._all(
        "SELECT object_id, user_id, payload FROM canvas_objects "
        "WHERE kind = 'note' AND user_id IS NOT NULL")
    cast = "::jsonb" if runs._pg else ""  # payload column is JSONB on PG
    moved = 0
    for r in rows:
        payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
        if "body" not in payload:
            continue  # already flipped (or never had one) — nothing to move
        try:
            validate_doc_id("note", r["object_id"])
        except ContentSlugError:
            continue  # can't live in the content plane; leave the payload as-is (read fallback)
        tenant_id, user_id = _resolve_tenant(r["user_id"]), r["user_id"]
        store = resolver.resolve(tenant_id)
        body = payload.pop("body")
        # Body first, strip after — same two-DB ordering as the write path: if the content put
        # fails we raise here with the primary payload untouched, and the next boot retries.
        if await store.get_document(tenant_id, user_id, "note", r["object_id"]) is None:
            await store.put_document(tenant_id, user_id, "note", r["object_id"],
                                     title=str(payload.get("title") or ""), body=str(body or ""))
        await runs._exec(
            f"UPDATE canvas_objects SET payload = ?{cast} WHERE kind = 'note' AND object_id = ?",
            (json.dumps(payload), r["object_id"]))
        moved += 1
    return moved


class ContentResolver:
    """tenant_id → ContentStore, the routing seam built in v1 (plan: tenancy decision 5).

    Today every valid tenant resolves to the single shared store built from settings; the
    per-tenant cache dict is where dedicated DSNs (tenants registry lookup, epoch-evict) slot
    in later. CRIT-3: an unresolvable tenant raises — this NEVER returns a store as fallback.
    """

    def __init__(self, content_db: str, content_database_url: str = "",
                 indexer=None, schema: str | None = None, pool: dict | None = None) -> None:
        self._path = content_db
        self._url = content_database_url
        self._schema = schema                         # co-located content plane schema (PG)
        self._pool_cfg = pool                         # psycopg pool tunables, forwarded to ContentStore
        self.indexer = indexer                        # embed-on-write seam (ContentIndexer)
        self._shared: ContentStore | None = None      # built lazily on first content touch
        self._stores: dict[str, ContentStore] = {}    # tenant_id → store (CRIT-3: cache key)

    def shared_store(self) -> ContentStore:
        """The v1 shared store, built on demand. Boot maintenance sweeps (backfill) walk it
        directly; per-tenant dedicated stores loop in here when they exist."""
        if self._shared is None:
            self._shared = ContentStore(self._path, self._url, indexer=self.indexer,
                                        schema=self._schema, pool=self._pool_cfg)
        return self._shared

    def resolve(self, tenant_id) -> ContentStore:
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ContentScopeError("content plane requires a tenant — no fallback store")
        store = self._stores.get(tenant_id)
        if store is None:
            store = self._stores[tenant_id] = self.shared_store()
        return store

    async def ping(self) -> None:
        """Readiness probe — raises if the content plane is unreachable. /readyz gates on this on
        Postgres deploys (a replica that can't resolve content silently drops note/recall reads)."""
        await self.shared_store()._one("SELECT 1")

    async def close(self) -> None:
        # ponytail: v1 has exactly one underlying store — close once. Per-tenant stores each
        # get closed here when dedicated DSNs arrive.
        if self._shared is not None:
            await self._shared.close_pool()
            self._shared.close()
            self._shared = None
            self._stores.clear()


class ContentIndexer:
    """The ONE embed-on-write seam: after every successful document put/delete, chunk + embed the
    body into doc_embeddings (the recall index), in the SAME content-plane DB. Repointed from the
    old gbrain subprocess — the index now lives beside its source of truth, so there is no second
    storage engine. DEGRADE-TO-OFF is LAW: everything here is fire-and-forget, catches everything,
    and a content write/delete NEVER fails or waits (embedding does network I/O, deletes don't).
    Embedder absent/failing → rows land with no vector (keyword-only), still recallable."""

    def __init__(self, resolver: "ContentResolver", embedder=None) -> None:
        self._resolver = resolver
        self._embedder = embedder
        self._tasks: set = set()  # strong refs so fire-and-forget tasks aren't GC'd mid-flight

    async def aindex(self, user_id: str | None, namespace: str, doc_id: str,
                     title: str = "", body: str = "", frontmatter: dict | None = None) -> None:
        """Awaitable core (the boot backfill drives this directly). Tenant is resolved from the
        user via the ONE shared resolver — same (tenant, user) scope the content write used."""
        try:
            from .routes.deps import _resolve_tenant  # lazy: one tenant convention

            tenant_id = _resolve_tenant(user_id) if user_id else None
            if not tenant_id or not user_id:
                return  # operator/anon: nothing to index (their content stays in the primary)
            chunks = chunk_text(f"{title}\n\n{body}" if title else (body or ""))
            vectors = None
            if self._embedder is not None and chunks:
                vectors = await self._embedder.embed_texts(chunks)  # None on failure → keyword-only
            await self._resolver.resolve(tenant_id).upsert_embeddings(
                tenant_id, user_id, namespace, doc_id, chunks, vectors)
        except Exception:
            log.debug("embed-on-write index failed (degrade-to-off)", exc_info=True)

    async def aunindex(self, user_id: str | None, namespace: str, doc_id: str) -> None:
        try:
            from .routes.deps import _resolve_tenant

            tenant_id = _resolve_tenant(user_id) if user_id else None
            if not tenant_id or not user_id:
                return
            await self._resolver.resolve(tenant_id).delete_embeddings(
                tenant_id, user_id, namespace, doc_id)
        except Exception:
            log.debug("embed-on-write unindex failed (degrade-to-off)", exc_info=True)

    def index(self, *args, **kwargs) -> None:
        self._spawn(self.aindex(*args, **kwargs))

    def unindex(self, *args, **kwargs) -> None:
        self._spawn(self.aunindex(*args, **kwargs))

    def _spawn(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except Exception:  # no loop / shutdown — drop the mirror write, never the content write
            coro.close()
            return
        # asyncio holds only a WEAK ref to tasks — without this the embed task can be GC'd before
        # it runs. Keep a strong ref until it finishes.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def backfill_embeddings(resolver: ContentResolver, chunk: int = 200) -> int:
    """Boot backfill: embed every LIVE document that has NO embedding rows yet (written while the
    memory plane was off, or before an embedding key existed). Embeddings are durable now, so —
    unlike the old ephemeral re-index — this is a no-op once the corpus is indexed, not a full
    re-embed every boot. Chunked + awaited row by row (off the hot path). Returns docs embedded
    (0 when the memory plane is off)."""
    indexer = resolver.indexer
    if indexer is None:
        return 0
    store = resolver.shared_store()  # v1: the one shared store; dedicated stores loop in later
    n = 0
    seen: set[tuple] = set()  # progress guard: a doc that fails to embed stays "missing" — the
    while True:               # seen-set stops it re-appearing forever (it retries next boot)
        rows = await store.documents_missing_embeddings(chunk, 0)  # embedded rows drop off the list
        fresh = [d for d in rows
                 if (d["user_id"], d["namespace"], d["doc_id"]) not in seen]
        if not fresh:
            return n
        for d in fresh:
            seen.add((d["user_id"], d["namespace"], d["doc_id"]))
            await indexer.aindex(d["user_id"], d["namespace"], d["doc_id"],
                                 d["title"], d["body"], d["frontmatter"])
            n += 1
