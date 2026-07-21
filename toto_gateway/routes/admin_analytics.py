"""Admin activity-analytics API — org-scoped read over `gateway_events` + LLM governance insights.

`GET /v1/admin/analytics/activity`  — aggregate bundle (totals, by task-type/model/user, trend).
`GET /v1/admin/analytics/insights`  — LLM governance summary over those AGGREGATE NUMBERS only.

Both gate on `require_read_role("admin")` and are org-scoped via the `admin_usage` helpers (a non-operator
admin only ever sees their own org; the operator must name one). Insights degrade rather than fail:
a missing model or an LLM/parse error returns HTTP 200 with `insights: null` + an honest `error`
string — observability never breaks a run. Only aggregate numbers ever reach the model.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request

from ..analytics import activity_bundle, generate_insights, model_drilldown
from .admin_usage import _engine_or_error, _scope_org
from .deps import Identity, require_read_role

router = APIRouter(tags=["admin"])

_TTL_S = 15 * 60
# ponytail: per-replica in-process cache — fine for a 15-min governance read; move to Redis only if
# multi-replica cache consistency ever matters. Keyed (org_id, start, end).
_CACHE: dict[tuple, dict] = {}


def _default_window(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    """Absent start AND end → last 30 days (date-only ISO, lexical prefix of ts_start; open upper
    bound includes 'now'). A caller-supplied bound is left exactly as given."""
    if start is None and end is None:
        start = (date.today() - timedelta(days=30)).isoformat()
    return start, end


@router.get("/v1/admin/analytics/activity")
async def get_activity(
    request: Request,
    start: str | None = Query(None, description="ISO date/datetime window start (inclusive)"),
    end: str | None = Query(None, description="ISO date/datetime window end (exclusive)"),
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """Activity aggregate bundle for the caller's org over [start, end) (default: last 30 days)."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err
    start, end = _default_window(start, end)
    bundle = activity_bundle(engine, org_id=org, start=start, end=end,
                             catalog=request.app.state.gateway.catalog)  # base-catalog-ok: cost aggregation over trace rows, platform-priced
    return {"org_id": org, "start": start, "end": end, **bundle}


@router.get("/v1/admin/analytics/model")
async def get_model_drilldown(
    request: Request,
    model: str = Query(..., description="real upstream model name (or a catalog id)"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """Per-model drill-down: token-type split + per-task-type breakdown for ONE real upstream
    model over [start, end) (default: last 30 days). Unknown/quiet models return zeroed totals
    and an empty by_label rather than 404 — an empty window is a fact, not an error."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err
    start, end = _default_window(start, end)
    detail = model_drilldown(engine, org_id=org, model=model, start=start, end=end,
                             catalog=request.app.state.gateway.catalog)  # base-catalog-ok: cost aggregation over trace rows, platform-priced
    return {"org_id": org, "start": start, "end": end, **detail}


@router.get("/v1/admin/analytics/insights")
async def get_insights(
    request: Request,
    start: str | None = Query(None),
    end: str | None = Query(None),
    refresh: bool = Query(False, description="bypass the 15-min cache"),
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """LLM governance insights over the org's aggregate numbers (never content). Cached ~15 min per
    (org, window); `refresh=true` bypasses. A missing model or LLM/parse failure → 200 with
    `insights: null` + `error`, so a broken model never breaks the dashboard."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err
    start, end = _default_window(start, end)
    settings = request.app.state.settings
    gateway = request.app.state.gateway
    model_id = settings.analytics_insights_model or settings.driver_model
    key = (org, start, end)
    now = datetime.now(timezone.utc)

    if not refresh:
        hit = _CACHE.get(key)
        if hit is not None and (now - hit["at"]).total_seconds() < _TTL_S:
            return _envelope(org, model_id, start, end, hit["at"], True,
                             insights=hit["insights"], error=None)

    if gateway.catalog.get(model_id) is None:
        return _envelope(org, model_id, start, end, now, False, insights=None,
                         error=f"insights model {model_id!r} not in catalog")

    try:
        bundle = activity_bundle(engine, org_id=org, start=start, end=end,
                                 catalog=gateway.catalog)
        insights = await generate_insights(bundle, complete_fn=gateway._classify_text,
                                           model_id=model_id)
        error = None
    except Exception as exc:  # noqa: BLE001 — LLM/parse failure degrades, never 500s the dashboard
        insights, error = None, f"insight generation failed: {exc}"

    if insights is not None:  # only cache real results; a transient failure isn't pinned for 15 min
        _CACHE[key] = {"at": now, "insights": insights}
    return _envelope(org, model_id, start, end, now, False, insights=insights, error=error)


def _envelope(org, model_id, start, end, generated_at, cached, *, insights, error) -> dict:
    return {"org_id": org, "model": model_id, "start": start, "end": end,
            "generated_at": generated_at.isoformat(), "cached": cached,
            "insights": insights, "error": error}
