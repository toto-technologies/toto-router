"""Dreams — the nightly memory-consolidation pass (docs/plans/2026-07-05-memory-lifecycle.md, P1).

Once a night, per active tenant, a background pass keeps the capture pile from rotting. Three legs,
each budget-checked and each degrade-to-off:

  - MERGE:  cluster near-duplicate captures (symmetric token overlap ≥ merge_sim), and for each
            cluster of ≥2 a Haiku-class call rewrites them to one canonical body — kept in the
            newest doc's id, the losers SOFT-ARCHIVED (deleted_at, 72h window, never hard-deleted).
  - DECAY:  soft-archive captures older than stale_days. No LLM — always runs, even if the merge
            budget is spent or the model is down.
  - DIGEST: when anything material changed, write one brain doc (namespace 'brain', slug
            digest/<utc-date>) summarising the tidy. It rides the existing /v1/companion/brain
            `files` lane and the companion's sparing next-wake mention (D5) — no new surface.

D2 is law: this job NEVER hard-deletes. Every removal is a reversible soft-archive. The declared
plane (user_memory) is capped + evicted in memory_write already and is deliberately NOT touched
here — it has no soft-delete column, and merging it would risk an irreversible loss of a user's
declared fact (deferred seam, see the plan). Fail-closed per (tenant, user), same as the content
plane's CRIT-1 scoping: dream_tenant is only ever called with a resolved (tenant_id, user_id).
"""

from __future__ import annotations

import logging
import time

from . import tool_scopes
from .content import CAPTURE_NAMESPACES, token_sim
from .schemas import ChatCompletionRequest, Message

log = logging.getLogger("toto_gateway.dreams")

MERGE_SYSTEM = """You are consolidating several near-duplicate memory notes about one user into a
single canonical note. Merge them into ONE short paragraph that preserves every distinct fact and
drops the repetition. Keep the user's own framing. Return ONLY the merged note text — no preamble,
no bullet list unless the originals were lists."""

_MAX_CAPTURES = 500  # per-user snapshot cap for a pass — bounds the O(n²) cluster scan


def _cluster(docs: list[dict], sim: float) -> list[list[dict]]:
    """Greedy single-pass clustering by symmetric body overlap. ponytail: O(n²) over one user's
    captures (dozens, capped at _MAX_CAPTURES) — the same brute-force blessing as the routing kNN;
    swap for a blocked/ANN pass only if a single user's capture count ever makes this bite."""
    clusters: list[list[dict]] = []
    used: set[int] = set()
    for i, d in enumerate(docs):
        if i in used:
            continue
        cluster = [d]
        used.add(i)
        for j in range(i + 1, len(docs)):
            if j not in used and token_sim(d.get("body") or "", docs[j].get("body") or "") >= sim:
                cluster.append(docs[j])
                used.add(j)
        clusters.append(cluster)
    return clusters


async def dream_tenant(tenant_id: str, user_id: str, *, gateway, content, memory, runs,
                       budget_usd: float = 0.10, stale_days: int = 30, merge_sim: float = 0.90,
                       model: str, now: float | None = None) -> dict:
    """One tenant's nightly pass. Returns {merged, archived, cost_usd} for the dream_runs receipt.
    `memory`/`runs` are accepted for the deferred declared-plane leg + symmetry with extraction;
    the P1 legs work entirely on the content plane. Never raises past a leg — a failed merge call
    is skipped, decay still runs."""
    # Scope enforcement (stamped decision 4): dream is content-plane docs only — ZERO canvas/list/
    # memory tools. A loud guard so adding a tool to the dream scope later fails HERE (and in the
    # parity test), not silently in prod. The whole job below touches only put/delete_document.
    if tool_scopes.effective_scope("dream"):
        raise RuntimeError("dream surface must hold no tools (content-plane docs only) — "
                           f"got {sorted(tool_scopes.effective_scope('dream'))}")
    now = now or time.time()
    store = content.resolve(tenant_id)  # CRIT-3: raises on an unresolvable tenant, never a fallback

    caps: list[dict] = []
    for ns in CAPTURE_NAMESPACES:
        caps += await store.list_documents(tenant_id, user_id, namespace=ns, limit=_MAX_CAPTURES)

    spent = 0.0
    merged = archived = 0
    gone: set[tuple] = set()   # (namespace, doc_id) soft-archived this pass — decay skips them
    kept: set[tuple] = set()   # canonical survivors — decay never archives a fresh merge result

    # --- MERGE leg (budgeted, LLM) ------------------------------------------------------------
    for cluster in _cluster(caps, merge_sim):
        if len(cluster) < 2:
            continue
        if spent >= budget_usd:
            break  # partial pass recorded; resumes next night (plan failure mode)
        joined = "\n\n---\n\n".join((c.get("body") or "").strip() for c in cluster)
        try:
            res = await gateway.complete(ChatCompletionRequest(model=model, messages=[
                Message(role="system", content=MERGE_SYSTEM),
                Message(role="user", content=joined[:8000]),
            ]), harness="memory")
            spent += res.trace.cost_usd or 0.0
            canonical = (res.response.choices[0].message.content or "").strip() \
                if res.response.choices else ""
        except Exception:
            log.debug("dream merge call failed (skip cluster)", exc_info=True)
            continue
        if not canonical:
            continue
        keep = cluster[0]  # newest (list_documents is created_at DESC) holds the canonical body
        await store.put_document(
            tenant_id, user_id, keep["namespace"], keep["doc_id"],
            title=keep.get("title") or "", body=canonical,
            frontmatter={**(keep.get("frontmatter") or {}), "type": keep["namespace"],
                         "consolidated": True})
        kept.add((keep["namespace"], keep["doc_id"]))
        for loser in cluster[1:]:
            await store.delete_document(tenant_id, user_id, loser["namespace"], loser["doc_id"])
            gone.add((loser["namespace"], loser["doc_id"]))
            archived += 1
        merged += 1

    # --- DECAY leg (free, always runs) --------------------------------------------------------
    cutoff = now - stale_days * 86400
    for c in caps:
        key = (c["namespace"], c["doc_id"])
        if key in gone or key in kept:
            continue
        if (c.get("updated_at") or c.get("created_at") or now) < cutoff:
            # ponytail: age-only decay. "no recall hits in the window" wants per-doc hit tracking
            # we don't keep — age is the honest proxy at this scale; soft-archive is reversible.
            await store.delete_document(tenant_id, user_id, c["namespace"], c["doc_id"])
            gone.add(key)
            archived += 1

    # --- DIGEST leg (free, only when material) ------------------------------------------------
    if merged or archived:
        date = time.strftime("%Y-%m-%d", time.gmtime(now))
        body = (f"# Memory digest {date}\n\n"
                f"While you were away I tidied your memory: "
                f"merged {merged} cluster{'' if merged == 1 else 's'} of related notes, "
                f"archived {archived} stale or duplicate one{'' if archived == 1 else 's'}.\n")
        try:
            await store.put_document(tenant_id, user_id, "brain", f"digest/{date}",
                                     title=f"Memory digest {date}", body=body,
                                     frontmatter={"type": "digest", "merged": merged,
                                                  "archived": archived})
        except Exception:
            log.debug("dream digest write failed (non-fatal)", exc_info=True)

    return {"merged": merged, "archived": archived, "cost_usd": round(spent, 6)}


if __name__ == "__main__":  # ponytail: pure-logic self-check of the clusterer (no DB, no model)
    docs = [
        {"namespace": "conversation", "doc_id": "a", "body": "the tahoe offsite is in august"},
        {"namespace": "conversation", "doc_id": "b", "body": "tahoe offsite is happening in august"},
        {"namespace": "conversation", "doc_id": "c", "body": "invoice for the coffee machine is due"},
    ]
    cl = _cluster(docs, 0.6)
    assert len(cl) == 2, cl                              # a+b cluster, c alone
    assert {d["doc_id"] for d in cl[0]} == {"a", "b"}    # the near-dups grouped
    assert [d["doc_id"] for d in cl[1]] == ["c"]         # the unrelated one stands alone
    assert _cluster([], 0.9) == []
    assert _cluster([docs[0]], 0.9) == [[docs[0]]]       # single doc → singleton cluster
    print("dreams self-check ok")
