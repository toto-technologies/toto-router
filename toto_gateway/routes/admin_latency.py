"""Admin latency API (W1-C2) — org-scoped per-stage latency + fast-path summary over gateway_events.

`GET /v1/admin/latency/summary` — p50/p95 gateway overhead, per-stage (classify/plan/upstream)
avg+p95, and the fast-path share over a `?days=N` window (default 7). The number a sales call quotes
("our added latency at p95 is X ms"). Gates on `require_read_role("admin")` (auditor-readable) and is org-scoped via the
`admin_usage` helpers (a non-operator admin only ever sees their own org; the operator must name one).
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request

from ..metering import latency_summary
from .admin_usage import _engine_or_error, _scope_org
from .deps import Identity, require_read_role

router = APIRouter(tags=["admin"])


@router.get("/v1/admin/latency/summary")
async def get_latency_summary(
    request: Request,
    days: int = Query(7, ge=1, le=90, description="trailing window in days"),
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """Per-stage latency + fast-path summary for the caller's org over the last `days` days."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err
    start = (date.today() - timedelta(days=days)).isoformat()
    return {"org_id": org, "days": days, "start": start,
            **latency_summary(engine, org_id=org, start=start)}
