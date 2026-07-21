"""Admin usage / metering API (control-plane C4) — the org-scoped read over `gateway_events`.

`GET /v1/admin/usage`          — usage rollup sliced by any grounded dimension + time range.
`GET /v1/admin/usage/export`   — Stripe-shaped billing records for a period (export SEAM, NOT
                                 invoicing — no Stripe SDK, no charge is created).

Both gate on `require_read_role("admin")` and are ORG-SCOPED: a non-operator admin only ever sees
their own org's usage. Asking for another org's `org_id` is refused (403) — the org filter is
enforced twice, at this boundary and again at the SQL floor (`metering.rollup_usage`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..metering import cache_health, cache_savings, export_billing_records, rollup_usage
from ..trace import sql_engine
from .auth import _error
from .deps import Identity, require_read_role

router = APIRouter(tags=["admin"])


def _scope_org(identity: Identity, requested: str | None):
    """Resolve the org to report on, fail-closed. A non-operator admin is pinned to their own org:
    an explicit `org_id` that differs is cross-org access → 403. The operator (no home org) MUST
    name an org explicitly. Returns (org_id, error) — exactly one is set."""
    if identity.is_operator:
        # OSS binds the operator to the `local` sentinel org (Identity.org_id), so an org-less
        # console call from the operator resolves there instead of 400-ing. Enterprise operators
        # carry no org_id, so they must still name one explicitly (multi-tenant safety).
        org = requested or identity.org_id
        if not org:
            return None, _error(400, "operator must specify ?org_id=", "invalid_request_error",
                                "org_id_required")
        return org, None
    home = identity.org_id
    if home is None:
        return None, _error(403, "caller has no org", "authorization_error", "no_org")
    if requested and requested != home:
        return None, _error(403, "cannot read another org's usage", "authorization_error",
                            "cross_org_denied")
    return home, None


def _iso(sec: float | None) -> str | None:
    """Unix seconds → UTC ISO-8601, so it compares lexically against the ts_start TEXT column."""
    return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat() if sec is not None else None


def _engine_or_error(request: Request):
    engine = sql_engine(getattr(request.app.state.gateway, "writer", None))
    if engine is None:
        return None, _error(503, "usage metering requires a trace database (trace_db)",
                            "unavailable", "no_trace_db")
    return engine, None


@router.get("/v1/admin/usage")
async def get_usage(
    request: Request,
    group_by: str = Query("", description="CSV of dimensions: org,team,model,provider,lane,residency,label,user"),
    start: str | None = Query(None, description="ISO-8601 window start (inclusive)"),
    end: str | None = Query(None, description="ISO-8601 window end (exclusive)"),
    granularity: str | None = Query(None, pattern="^(day|hour)$"),
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """Usage rollup for the caller's org: requests, tokens, cost, and savings-vs-frontier, sliced
    by `group_by` dimensions + optional day/hour time buckets over `[start, end)`."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine = sql_engine(getattr(request.app.state.gateway, "writer", None))
    dims = [d.strip() for d in group_by.split(",") if d.strip()]
    if engine is None:
        # No trace DB is a legitimate configuration (TOTO_GW_TRACE_DB=off, :memory:/PG boots),
        # not a server fault — the header spend chip polls this on every page, so answer with an
        # honest empty rollup instead of a 503. trace_db:false lets the console say "tracking is
        # off" rather than "$0.00". The drill-down usage routes keep their informative 503.
        return {"org_id": org, "group_by": dims, "granularity": granularity, "rows": [],
                "trace_db": False}
    try:
        rows = rollup_usage(engine, org_id=org, group_by=dims, start=start, end=end,
                            granularity=granularity)
    except ValueError as exc:  # unknown group_by dimension
        return _error(400, str(exc), "invalid_request_error", "unknown_dimension")
    if "model" in dims:  # ids are routing handles; the dashboard names the ACTUAL model too
        catalog = request.app.state.gateway.catalog  # base-catalog-ok: cost aggregation over trace rows, platform-priced
        for r in rows:
            entry = catalog.get(r.get("model") or "")
            r["model_name"] = entry.effective_upstream_model if entry else r.get("model")
    return {"org_id": org, "group_by": dims, "granularity": granularity, "rows": rows}


@router.get("/v1/admin/usage/cache-savings")
async def get_cache_savings(
    request: Request,
    from_: float | None = Query(None, alias="from", description="Unix-seconds lower bound (inclusive)"),
    to: float | None = Query(None, description="Unix-seconds upper bound (inclusive)"),
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """The caching P&L for the caller's org over [from, to]: per-model read savings, write premium,
    and net, plus a grand total — the "caching saved your org $X this period" surface. Org-scoped
    exactly like the sibling usage endpoints (a non-operator admin is pinned to their own org)."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err
    catalog = request.app.state.gateway.catalog  # base-catalog-ok: cost aggregation over trace rows, platform-priced
    return cache_savings(engine, catalog=catalog, org_id=org,
                         start=_iso(from_), end=_iso(to))


@router.get("/v1/admin/usage/cache-health")
async def get_cache_health(
    request: Request,
    from_: float | None = Query(None, alias="from", description="Unix-seconds lower bound (inclusive)"),
    to: float | None = Query(None, description="Unix-seconds upper bound (inclusive)"),
    granularity: str = Query("day", pattern="^(day|hour)$"),
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """A caching-health time series for the caller's org over [from, to]: per day/hour bucket the
    request count, prompt/cached/write token totals, warm-hold turn count, and cache hit rate — the
    console observability pane. Org-scoped exactly like the sibling usage endpoints."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err
    buckets = cache_health(engine, org_id=org, start=_iso(from_), end=_iso(to),
                           granularity=granularity)
    return {"buckets": buckets, "from": _iso(from_), "to": _iso(to), "granularity": granularity}


@router.get("/v1/admin/usage/export")
async def export_usage(
    request: Request,
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$", description="Billing period, YYYY-MM"),
    format: str = Query("stripe"),
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """Stripe-shaped billing records for one org and period. Export SEAM only — the payload is what
    a future billing job consumes; no invoice is created here (control-surface decision #7)."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err
    records = export_billing_records(engine, org, period)
    return JSONResponse(
        {
            "period": period,
            "org_id": org,
            "format": format,
            "line_items": [vars(r) for r in records],
        },
        # A direct navigation (the console's Download button) saves a real file; XHR ignores it.
        headers={"Content-Disposition": f'attachment; filename="toto-billing-{period}.json"'},
    )
