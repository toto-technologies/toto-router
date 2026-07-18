"""Per-request activity log — GET /v1/admin/requests (list) + /v1/admin/requests/{id} (detail).

The decision-trail list over `gateway_events`: for each recent request, which model was selected,
what task it classified as, WHY (route_reason), cost, latency, guard action — newest first. This is
the per-prompt audit surface metering (rollup-only) doesn't provide. Each row carries a stable `id`
a client opens for the detail view.

The LIST is metadata-only (routing-decision columns, no content). The DETAIL endpoint adds the
captured prompt (request messages) + response text from the `request_content` sibling table when
observability content-capture (TOTO_GW_LOG_CONTENT) was on for that request — else it reports
`content_available: false`. Content lives in its own table, keyed by request_id, and ages out.

Auth is `require_auth`, NOT admin-only, so an end user can audit THEIR OWN traffic. Role-scoping is
enforced SERVER-SIDE, fail-closed (`_scope`):
  - member            → only their own rows (user_id == identity.user_id); a `user` filter naming
                        someone else is ignored (never a cross-user read — IDOR floor).
  - org admin / owner → the whole org (org_id == identity.org_id); may narrow to one `user`.
  - operator          → unrestricted; may narrow to one `user`.
A non-operator whose org didn't resolve degrades to own-rows-only (fail-closed), never unscoped.
Reads aren't audited (matches the usage/audit read convention).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request

import json

from ..metering import get_request, list_requests
from ..trace import get_request_content, sql_engine
from .auth import _error
from .deps import Identity, require_auth

router = APIRouter(tags=["admin"])


def _scope(identity: Identity, user_param: str | None) -> tuple[str | None, str | None]:
    """Resolve the (org_id, user_id) SQL filters for this caller, fail-closed. Returns exactly the
    scope the caller is entitled to — never wider than their role allows."""
    if identity.is_operator:
        return None, user_param  # unrestricted; may narrow by user
    if identity.role in ("admin", "owner") and identity.org_id:
        return identity.org_id, user_param  # whole org; may narrow by user
    # member (or an admin whose org didn't resolve) → own rows only; `user_param` is ignored.
    return identity.org_id, identity.user_id


def _iso(sec: float | None) -> str | None:
    """Unix seconds → UTC ISO-8601, so it compares lexically against the ts_start TEXT column."""
    return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat() if sec is not None else None


@router.get("/v1/admin/requests")
async def list_activity(
    request: Request,
    identity: Identity = Depends(require_auth),
    model: str | None = Query(None, description="Exact model filter"),
    label: str | None = Query(None, description="Exact classified-as (task type) filter"),
    conversation_key: str | None = Query(None, description="Group one conversation's turns"),
    user: str | None = Query(None, description="Filter to one user_id (admin/operator only)"),
    from_: float | None = Query(None, alias="from", description="Unix-seconds lower bound (inclusive)"),
    to: float | None = Query(None, description="Unix-seconds upper bound (inclusive)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """This caller's per-request decision trail, newest first. Scope is server-side from the verified
    Identity (a member sees only their own rows); NO prompt/response content is ever returned."""
    engine = sql_engine(getattr(request.app.state.gateway, "writer", None))
    if engine is None:
        return _error(503, "the request activity log requires a trace database (trace_db)",
                      "unavailable", "no_trace_db")
    org_id, user_id = _scope(identity, user)
    requests = list_requests(engine, org_id=org_id, user_id=user_id, model=model, label=label,
                             conversation_key=conversation_key,
                             start=_iso(from_), end=_iso(to), limit=limit, offset=offset)
    next_offset = offset + limit if len(requests) == limit else None
    return {"requests": requests, "next_offset": next_offset}


@router.get("/v1/admin/requests/{request_row_id}")
async def get_activity_detail(
    request_row_id: int,
    request: Request,
    identity: Identity = Depends(require_auth),
):
    """One request's detail: its decision-trail metadata PLUS the captured prompt (messages) +
    response text when content-capture was on, or `content_available: false` when the flag was
    off / not captured / aged out.

    SAME scope as the list (server-side, fail-closed): a member sees only their own request; an
    admin/owner the whole org; the operator all. A cross-user / cross-org id resolves to None →
    404, indistinguishable from a genuinely absent id (the IDOR floor — never leak existence)."""
    engine = sql_engine(getattr(request.app.state.gateway, "writer", None))
    if engine is None:
        return _error(503, "the request activity log requires a trace database (trace_db)",
                      "unavailable", "no_trace_db")
    org_id, user_id = _scope(identity, None)  # a member is pinned to their own user_id
    row = get_request(engine, request_row_id, org_id=org_id, user_id=user_id)
    if row is None:
        return _error(404, "no such request", "not_found", "unknown_request")
    req_id = row.pop("request_id")
    content = get_request_content(engine, req_id)
    if content is None:  # flag off, not captured, or aged out
        return {"request": row, "content_available": False}
    return {
        "request": row,
        "content_available": True,
        "prompt": json.loads(content["prompt"]),
        "response": content["response"],
    }
