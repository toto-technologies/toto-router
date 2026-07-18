"""Memory — the companion's RECALL plane, in-Postgres (replaces the old gbrain subprocess).

This is a THIN adapter over the content plane (toto_gateway/content.py). It keeps the exact seam
the rest of the app already speaks — recall / capture / list_documents / get_document /
delete_document — so companion/core.py, the recall tool, routes/companion.py's brain endpoints,
and the /brain page keep working unchanged. All the storage + retrieval lives in ContentStore:

  - capture(text)  → a `documents` row (namespace 'conversation'/'session'), durable + tenant-
    scoped, embedded on write via the ContentIndexer seam (no separate plane, no double-storage).
  - recall(query)  → hybrid pgvector-cosine + tsvector-keyword search over doc_embeddings, which
    indexes the WHOLE corpus (brain files, note bodies, and captures) for this (tenant, user).
  - list/get/delete → the auto-captured conversation/session docs for the Brain UI's recall pane.

Per-user isolation is the content plane's own fail-closed (tenant, user) scoping (CRIT-1) — two
users share nothing, same guarantee as user_memory. DEGRADE-TO-OFF stays LAW: a missing embedding
key drops recall to keyword-only, and every method here swallows failure (empty/None/False) so a
memory hiccup never breaks a wake.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time

from .content import CAPTURE_NAMESPACES, ContentResolver

log = logging.getLogger("toto_gateway.memory")


class LLMReranker:
    """The default rerank stage: one batched call to OUR gateway (economy model) reorders the
    fused candidates by relevance and cuts to k. Swappable — any object with the same `rerank`
    method drops in. DEGRADE-TO-OFF is LAW: a timeout, a bad response, or any error falls back to
    the fused order (candidates[:k]) — recall NEVER fails or hangs on the reranker."""

    def __init__(self, llm_fn, model: str, budget_s: float = 0.6, max_candidates: int = 20) -> None:
        self._llm = llm_fn                      # driver._llm(model, messages, name=, max_tokens=)
        self._model = model
        self._budget = budget_s
        self._max = max_candidates

    async def rerank(self, query: str, candidates: list[dict], k: int) -> list[dict]:
        cands = candidates[:self._max]
        if len(cands) <= 1:
            return cands[:k]
        listing = "\n".join(f"{i + 1}. {' '.join((c.get('text') or '').split())[:300]}"
                            for i, c in enumerate(cands))
        prompt = (f"Rank these memory snippets by how well they help answer the query. "
                  f"Return ONLY a JSON array of the {min(k, len(cands))} most relevant snippet "
                  f"numbers, most relevant first (e.g. [3,1,7]).\n\n"
                  f"Query: {query}\n\nSnippets:\n{listing}")
        try:
            ex = await asyncio.wait_for(
                self._llm(self._model, [{"role": "user", "content": prompt}],
                          name="memory.rerank", max_tokens=64),
                timeout=self._budget)
            order = _parse_order(ex.text, len(cands))
        except Exception:
            log.debug("rerank degraded to fused order", exc_info=True)
            return cands[:k]
        if not order:
            return cands[:k]
        ranked = [cands[i] for i in order]
        ranked += [c for i, c in enumerate(cands) if i not in set(order)]  # stable tail
        return ranked[:k]


def _parse_order(text: str, n: int) -> list[int]:
    """Parse the model's JSON array of 1-based snippet numbers into distinct 0-based indices in
    range. Tolerant: grabs the first bracketed list of ints; drops anything out of range/dupes."""
    m = re.search(r"\[[\d,\s]*\]", text or "")
    if not m:
        return []
    try:
        nums = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    seen, out = set(), []
    for x in nums:
        i = int(x) - 1 if isinstance(x, (int, float)) else -1
        if 0 <= i < n and i not in seen:
            seen.add(i)
            out.append(i)
    return out


class Memory:
    """One adapter per gateway process; per-user scope selected by user_id at call time."""

    def __init__(self, resolver: ContentResolver, embedder=None, *, recall_k: int = 5,
                 vec_weight: float = 0.7, kw_weight: float = 0.3,
                 reranker=None, candidates: int = 20) -> None:
        self._resolver = resolver
        self._embedder = embedder
        self._recall_k = recall_k
        self._vec_w = vec_weight
        self._kw_w = kw_weight
        self._reranker = reranker      # rerank stage (LLMReranker or None → fused order)
        self._candidates = candidates  # top-N fused candidates fetched before rerank

    # tenant resolution is the ONE convention (routes.deps); v1: tenant_id == user_id.
    def _scope(self, user_id: str | None):
        from .routes.deps import _resolve_tenant

        tenant_id = _resolve_tenant(user_id) if user_id else None
        if not tenant_id or not user_id:
            return None, None
        return tenant_id, user_id

    @property
    def mode(self) -> str:
        """'on' | 'keyword-only' | 'python' — what the underlying store can do (for /readyz)."""
        try:
            return self._resolver.shared_store().memory_mode
        except Exception:
            return "off"

    # --- the RECALL plane -----------------------------------------------------

    async def recall(self, user_id: str | None, query: str, k: int | None = None,
                     rerank: bool = True) -> list[dict]:
        """Top-k hits for `query` from this user's whole corpus: RRF-fused hybrid retrieval, then
        the (optional) rerank stage reorders + cuts to k. [] on any failure. Returns [{slug, text,
        score}] best-first, one hit per document. `rerank=False` skips the LLM rerank stage and
        returns fused order — the voice-turn fast path (voice-agent plan): recall stays, the
        reranker's ~600ms round-trip doesn't ride the speech-to-first-word latency budget."""
        query = (query or "").strip()
        tenant_id, uid = self._scope(user_id)
        if not query or tenant_id is None:
            return []
        k = k or self._recall_k
        try:
            store = self._resolver.resolve(tenant_id)
            qvec = None
            if self._embedder is not None:
                qvec = await self._embedder.embed_one(query)
            candidates = await store.search(tenant_id, uid, query, qvec, top_n=self._candidates,
                                            vec_w=self._vec_w, kw_w=self._kw_w)
        except Exception:
            log.debug("recall failed (degrade-to-off)", exc_info=True)
            return []
        # search() is already doc-deduped + best-first; map to the {slug, text} hit shape first so
        # the reranker (and its fused-order fallback) work on the public shape.
        hits = [{"slug": r["doc_id"], "text": (r.get("chunk_text") or "").strip(),
                 "score": r.get("score")} for r in candidates]
        if rerank and self._reranker is not None and len(hits) > 1:
            hits = await self._reranker.rerank(query, hits, k)
        return hits[:k]

    async def capture(self, user_id: str | None, text: str, meta: dict | None = None) -> str | None:
        """Fire-and-forget write of durable content (a chat turn, a session outcome) into the
        recall plane as a content-plane document. Returns the new doc_id, or None on failure.
        Safe to create_task and forget — it never raises."""
        # Zero-retention (W1-C4, widened 2026-07-13): the AUTOMATIC capture lane persists verbatim
        # turn text the user never asked to keep, so it gates like the telemetry sinks. Explicit
        # user-invoked writes (memory_write tool / REST) and chat history are product data and stay.
        # Identity rides the request contextvar into this fire-and-forget task (asyncio.create_task
        # copies context); no identity (operator/tests) → False → unchanged.
        from .routes.deps import current_identity

        if getattr(current_identity(), "zero_retention", False):
            return None
        text = (text or "").strip()
        tenant_id, uid = self._scope(user_id)
        if not text or tenant_id is None:
            return None
        kind = ((meta or {}).get("type") or "conversation")
        namespace = kind if kind in CAPTURE_NAMESPACES else "conversation"
        # A generated slug: date folder + short content hash. Deterministic per (text, time) so a
        # retried capture of the same turn upserts rather than duplicating within the same second.
        digest = hashlib.sha256(f"{uid}\x00{text}".encode()).hexdigest()[:12]
        doc_id = f"{time.strftime('%Y-%m-%d')}/{digest}"
        title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")[:80]
        try:
            await self._resolver.resolve(tenant_id).put_document(
                tenant_id, uid, namespace, doc_id, title=title, body=text,
                frontmatter={"type": namespace})
        except Exception:
            log.debug("capture failed (degrade-to-off)", exc_info=True)
            return None
        return doc_id

    # --- the BROWSE surface (Brain UI recall pane) ----------------------------

    async def list_documents(self, user_id: str | None, limit: int = 50,
                             offset: int = 0, kind: str | None = None) -> list[dict]:
        """Enumerate this user's auto-captured recall docs (conversations + sessions), newest-
        first. [{slug, title, kind, updated_at, deleted_at}]."""
        tenant_id, uid = self._scope(user_id)
        if tenant_id is None:
            return []
        namespaces = (kind,) if kind in CAPTURE_NAMESPACES else CAPTURE_NAMESPACES
        docs: list[dict] = []
        try:
            store = self._resolver.resolve(tenant_id)
            for ns in namespaces:
                for d in await store.list_documents(tenant_id, uid, namespace=ns,
                                                    limit=offset + limit):
                    docs.append({"slug": d["doc_id"], "title": d["title"] or d["doc_id"],
                                 "kind": ns, "updated_at": d["updated_at"], "deleted_at": None})
        except Exception:
            log.debug("list_documents failed (degrade-to-off)", exc_info=True)
            return []
        docs.sort(key=lambda d: d["updated_at"] or 0, reverse=True)
        return docs[offset:offset + limit]

    async def get_document(self, user_id: str | None, doc_id: str) -> dict | None:
        """One recall doc's full content for the Brain UI. None if unknown/failed."""
        doc_id = (doc_id or "").strip()
        tenant_id, uid = self._scope(user_id)
        if not doc_id or tenant_id is None:
            return None
        try:
            store = self._resolver.resolve(tenant_id)
            for ns in CAPTURE_NAMESPACES:
                d = await store.get_document(tenant_id, uid, ns, doc_id)
                if d is not None:
                    return {"slug": d["doc_id"], "title": d["title"] or d["doc_id"], "kind": ns,
                            "markdown": d["body"], "tags": [], "frontmatter": d["frontmatter"],
                            "created_at": d["created_at"], "updated_at": d["updated_at"]}
        except Exception:
            log.debug("get_document failed (degrade-to-off)", exc_info=True)
        return None

    async def delete_document(self, user_id: str | None, doc_id: str) -> bool:
        """Erase one recall doc (soft delete, 72h window — same as any content doc). True on hit."""
        doc_id = (doc_id or "").strip()
        tenant_id, uid = self._scope(user_id)
        if not doc_id or tenant_id is None:
            return False
        try:
            store = self._resolver.resolve(tenant_id)
            for ns in CAPTURE_NAMESPACES:
                if await store.get_document(tenant_id, uid, ns, doc_id) is not None:
                    await store.delete_document(tenant_id, uid, ns, doc_id)
                    return True
        except Exception:
            log.debug("delete_document failed (degrade-to-off)", exc_info=True)
        return False


def build_memory(settings, resolver: ContentResolver, embedder=None, llm_fn=None) -> Memory | None:
    """Construct the recall adapter iff TOTO_GW_MEMORY=1. Off → None → the companion never touches
    it (declared-memory-only, exactly as today). No binary to probe: the plane rides the content
    Postgres, whose own init already logged pgvector availability (memory_mode). The rerank stage
    is on by default (needs an llm_fn — the driver's own complete seam); absent → fused order."""
    if not getattr(settings, "memory", False):
        return None
    reranker = None
    if settings.memory_rerank and llm_fn is not None:
        reranker = LLMReranker(llm_fn, settings.memory_rerank_model or settings.triage_model,
                               budget_s=settings.memory_rerank_budget_ms / 1000.0,
                               max_candidates=settings.memory_rerank_candidates)
    return Memory(resolver, embedder, recall_k=settings.memory_recall_k,
                  vec_weight=settings.memory_vec_weight, kw_weight=settings.memory_kw_weight,
                  reranker=reranker, candidates=settings.memory_rerank_candidates)


if __name__ == "__main__":  # ponytail: one runnable self-check of the pure helpers (no DB)
    from .content import _cosine, _kw_score, _pgvec, _rrf_fuse, chunk_text

    # RRF: a doc ranked #1 in both lists beats one ranked #1 in only one list
    v = [{"namespace": "n", "doc_id": "a", "chunk_text": "A"},
         {"namespace": "n", "doc_id": "b", "chunk_text": "B"}]
    kw = [{"namespace": "n", "doc_id": "a", "chunk_text": "A"},
          {"namespace": "n", "doc_id": "c", "chunk_text": "C"}]
    fused = _rrf_fuse([v, kw], [0.7, 0.3], top_n=5)
    assert fused[0]["doc_id"] == "a"                       # in both lists → top
    assert {f["doc_id"] for f in fused} == {"a", "b", "c"}  # union, deduped
    # rerank order parsing
    assert _parse_order("[3,1,2]", 3) == [2, 0, 1]
    assert _parse_order("here you go: [2, 1]", 3) == [1, 0]
    assert _parse_order("[5,1,1]", 3) == [0]               # out-of-range + dupes dropped
    assert _parse_order("no array", 3) == []

    assert chunk_text("") == [] and chunk_text("   ") == []
    assert chunk_text("one para") == ["one para"]
    big = "x" * 2500
    cs = chunk_text(big, size=800)
    assert len(cs) == 4 and all(len(c) <= 800 for c in cs)  # oversized paragraph hard-split
    two = chunk_text("a\n\n" + "b" * 900, size=800)
    assert two[0] == "a"  # a small para then an oversized one flushes first
    assert _kw_score("tahoe offsite", "the offsite is in tahoe") == 1.0
    assert _kw_score("tahoe", "nothing here") == 0.0
    assert _kw_score("", "anything") == 0.0
    assert abs(_cosine([1, 0], [1, 0]) - 1.0) < 1e-6
    assert abs(_cosine([1, 0], [0, 1])) < 1e-6
    assert _cosine([1, 0], None) == 0.0
    assert _pgvec([0.5, -1.0]) == "[0.5,-1.0]"
    print("memory self-check ok")
