"""Admin provider-health API — the live source for the console Overview "Provider health" panel.

  GET /v1/admin/providers/health  (admin) — every real provider the gateway routes to: its
  circuit-breaker state (closed | open | half-open, + retry_in seconds while open), recent traffic
  over a lookback window (requests, errors, error rate, p50/p95/avg latency), and which catalog
  model ids map to it ("serves N models").

Providers come from the CATALOG (grouped by provider host), so an untouched provider with zero
traffic still appears (state closed, empty stats). The breaker keys strictly on the base_url host
(`provider_key(base_url)`), so entries with no base_url (OpenAI-default, native Anthropic, a bare
local URL) share ONE breaker key — `breaker_key` is surfaced per provider so that coupling is
visible, never hidden. Breaker state is a per-replica PLATFORM signal (shared across orgs); the
traffic stats are ORG-SCOPED exactly like the sibling /v1/admin/usage endpoints (a non-operator
admin is pinned to their own org). Fake-lane entries (offline demo/tests) are excluded.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlmodel import Session

from ..breaker import provider_key
from ..trace import TraceRow, sql_engine
from .auth import _error
from .deps import Identity, require_read_role

router = APIRouter(tags=["admin"])

_C = TraceRow.__table__.c


def _scope_org(identity: Identity, requested: str | None):
    """Resolve the org to report on, fail-closed (mirrors admin_usage._scope_org). A non-operator is
    pinned to their own org; a differing `org_id` is cross-org access → 403. The OSS operator binds
    to the `local` sentinel org (Identity.org_id) so an org-less console call resolves there instead
    of 400-ing; an enterprise operator (no org_id) must still name one. Returns (org_id, error) —
    exactly one is set."""
    if identity.is_operator:
        org = requested or identity.org_id
        if not org:
            return None, _error(400, "operator must specify ?org_id=", "invalid_request_error",
                                "org_id_required")
        return org, None
    home = identity.org_id
    if home is None:
        return None, _error(403, "caller has no org", "authorization_error", "no_org")
    if requested and requested != home:
        return None, _error(403, "cannot read another org", "authorization_error",
                            "cross_org_denied")
    return home, None


def _pct(sorted_vals: list[int], q: float) -> int | None:
    """Nearest-rank percentile of an ALREADY-SORTED list (q in 0..1), or None when empty."""
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(q * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def _empty_stats() -> dict:
    return {"requests": 0, "errors": 0, "error_rate": 0.0,
            "latency_p50_ms": None, "latency_p95_ms": None, "latency_avg_ms": None}


@router.get("/v1/admin/providers/health")
async def get_provider_health(
    request: Request,
    window: int = Query(3600, ge=60, le=604800, description="Lookback window in seconds (max 7d)"),
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """Live provider health: breaker state + windowed traffic stats + served models, per provider."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err

    catalog = request.app.state.gateway.catalog  # base-catalog-ok: platform provider health, not a caller view
    breaker = request.app.state.gateway._breaker
    snap = breaker.snapshot()  # per-replica, shared across orgs — keyed by provider_key(base_url)

    # Group non-fake catalog entries by display host, remembering each entry's true breaker key so
    # traffic (per model) and circuit state (per base_url host) both attribute correctly.
    providers: dict[str, dict] = {}
    model_to_provider: dict[str, str] = {}
    for e in catalog.models:
        if e.lane == "fake" or e.endpoint == "fake":
            continue
        host = provider_key(e.base_url or e.endpoint)  # a meaningful host even without base_url
        prov = providers.get(host)
        if prov is None:
            prov = providers[host] = {"provider": host, "base_url": e.base_url,
                                      "breaker_key": provider_key(e.base_url), "models": []}
        prov["models"].append(e.id)
        model_to_provider[e.id] = host

    start_iso = (datetime.now(timezone.utc) - timedelta(seconds=window)).isoformat()
    latencies: dict[str, list[int]] = {h: [] for h in providers}
    counts: dict[str, list[int]] = {h: [0, 0] for h in providers}  # [requests, errors]

    engine = sql_engine(getattr(request.app.state.gateway, "writer", None))
    if engine is not None:
        stmt = (select(_C.model, _C.status, _C.latency_ms_total)
                .where(_C.org_id == org).where(_C.ts_start >= start_iso))
        with Session(engine) as s:
            for model, status, lat in s.execute(stmt):
                host = model_to_provider.get(model)
                if host is None:  # a model no longer in the catalog — not attributable to a provider
                    continue
                counts[host][0] += 1
                if status != "ok":
                    counts[host][1] += 1
                if lat is not None:
                    latencies[host].append(lat)

    out = []
    for host, prov in providers.items():
        reqs, errs = counts[host]
        lats = sorted(latencies[host])
        stats = {"requests": reqs, "errors": errs,
                 "error_rate": round(errs / reqs, 4) if reqs else 0.0,
                 "latency_p50_ms": _pct(lats, 0.5), "latency_p95_ms": _pct(lats, 0.95),
                 "latency_avg_ms": round(sum(lats) / len(lats)) if lats else None}
        brk = snap.get(prov["breaker_key"], {"state": "closed", "retry_in": None,
                                             "consecutive_failures": 0})
        out.append({**prov, **brk, "stats": stats})

    out.sort(key=lambda p: (-p["stats"]["requests"], p["provider"]))
    return {"org_id": org, "window_seconds": window, "since": start_iso,
            "trace_db": engine is not None, "providers": out}
