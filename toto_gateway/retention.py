"""Content-plane retention (W3-C6): age-out for the USER-INVOKED PRODUCT-storage sinks that
zero-retention (W1-C4, #111) deliberately EXCLUDES.

Zero-retention gates the TELEMETRY sinks (request_content, exact cache, eval corpus, driver spans,
LangSmith mirror) and #133 added the automatic memory-capture lane (conversation/session documents,
gated at the write). What remains — and what this module prunes — is product data the user asked to
keep:

  documents  — the content plane's `documents` table (brain files, notes, and any captured docs)
               plus their `doc_embeddings` recall rows, which are FK-by-convention and MUST go with
               their parent (the index can never outlive its source).
  memory     — the explicit `user_memory` facts (memory_write tool / REST), in the operational DB.

BYOS buckets (#132 org storage connectors) are OUT: customer-owned storage keeps a customer-owned
lifecycle. This never touches trace metadata (routing/cost/latency provenance) — that is the audit
record, not content.

Coexistence with the global `content_retention_days` (config.py): that setting ages out the
OBSERVABILITY `request_content` capture (a different table, trace.py) and has nothing to do with
these product sinks. The two are disjoint — no row is eligible for both — so there is no double
prune. Per-org retention has NO global default: a sink absent from an org's policy (or set to 0) is
kept forever. Per-org policy is opt-in and, being additive over a different table, simply coexists.

The policy is per-org ({sink: days}); the content/memory rows are per-USER (tenant_id == user_id,
IDOR discipline), so the sweep expands org -> members and prunes each member's rows. One code path
(`prune_org`) is shared by the scheduled sweeper (app.py::_retention_sweeper) and the admin
POST .../retention/run route, so a manual run and a scheduled run behave identically.
"""

from __future__ import annotations

import logging
import time

from . import audit

log = logging.getLogger("toto_gateway.retention")

# Canonical product-storage sink keys. The admin route validates writes against this set; the sweep
# only acts on keys it knows here. Extension = add a key + a branch in `_prune_user_sink`.
RETENTION_SINKS = ("documents", "memory")


async def _prune_user_sink(content, runs, tenant_id: str, user_id: str, sink: str,
                           older_than: float, batch_limit: int) -> dict:
    """Prune one sink for one user, bounded to `batch_limit` rows. Returns partial counts."""
    if sink == "documents":
        docs, embs = await content.resolve(tenant_id).prune_documents(
            tenant_id, user_id, older_than, batch_limit)
        return {"documents": docs, "embeddings": embs}
    if sink == "memory":
        return {"memory": await runs.prune_user_memory(user_id, older_than, batch_limit)}
    return {}


async def prune_org(auth, content, runs, org_id: str, policy: dict, *,
                    batch_limit: int, now: float | None = None) -> dict:
    """Prune every sink in one org's policy, member by member, and write a `retention:pruned` audit
    row per sink that actually deleted rows (SOC2 evidence — counts only, never content). Returns
    {"documents", "embeddings", "memory"} totals for the org. Missing runs/content plane → that
    sink is skipped (no-op), never an error."""
    from .routes.deps import _resolve_tenant  # v1: tenant_id == user_id, resolved in ONE place

    now = time.time() if now is None else now
    members = await auth.list_org_user_ids(org_id)
    totals = {"documents": 0, "embeddings": 0, "memory": 0}
    for sink, days in policy.items():
        if sink not in RETENTION_SINKS or not isinstance(days, int) or days <= 0:
            continue  # unknown sink / keep-forever — defensively skipped (route already validated)
        if sink == "documents" and content is None:
            continue
        if sink == "memory" and runs is None:
            continue
        cutoff = now - days * 86400
        sink_count = 0
        for user_id in members:
            tenant_id = _resolve_tenant(user_id)
            if not tenant_id:
                continue
            counts = await _prune_user_sink(content, runs, tenant_id, user_id, sink,
                                            cutoff, batch_limit)
            for k, v in counts.items():
                totals[k] += v
            sink_count += counts.get(sink, 0)
        if sink_count:
            # One audit row per (org, sink) with the pruned count. Counts only — the audit plane is
            # the record of WHAT was deleted, never the content itself.
            await audit.record(auth, "retention:pruned", org_id=org_id, target_type="org",
                               target_id=org_id,
                               meta={"sink": sink, "retention_days": days, "pruned": sink_count})
            log.info("retention pruned", extra={"org_id": org_id, "sink": sink,
                                                "count": sink_count})
    return totals


async def run_retention_sweep(auth, content, runs, *, batch_limit: int,
                              now: float | None = None) -> dict:
    """Sweep every org that has a retention policy, org by org. Each org's failure is contained
    (logged, the rest continue) so one bad org never stalls the tick. Returns {org_id: totals}."""
    orgs = await auth.list_retention_orgs()
    summary: dict = {}
    for entry in orgs:
        org_id = entry["org_id"]
        try:
            summary[org_id] = await prune_org(auth, content, runs, org_id, entry["policy"],
                                              batch_limit=batch_limit, now=now)
        except Exception:  # noqa: BLE001 — one org's failure never stops the others
            log.exception("retention sweep failed for org", extra={"org_id": org_id})
    return summary
