"""Routing-verdict plane (router-eval chunk 5) — the human-feedback loop over real routing decisions.

An admin reviews recent routing decisions (which task type the classifier called a prompt, which
model it bound) and tags each good/bad, optionally supplying the correct label. Verdicts export as
eval-set rows that feed the label classifier's gold set — closing the loop the eval (`eval/labels`)
opens. Four endpoints, all `require_role("admin")` and ORG-SCOPED via `_scope_org` (a non-operator
admin only ever sees/judges their own org; the operator MUST name `?org_id=`):

  GET  /v1/admin/labeling/queue    — recent labeled+captured requests this judge hasn't verdicted
  POST /v1/admin/labeling/verdict  — record (idempotent per request+judge) a good/bad verdict
  GET  /v1/admin/labeling/stats    — rollups for the gamified labeling UI
  GET  /v1/admin/labeling/export   — verdicts as eval-set rows (load through eval.labels.load_set)

The queue's `query_text` is read live from `request_content`; the verdict DENORMALIZES it onto the
verdict row, so exports survive content aging (trace.LabelVerdict). Cross-org access resolves to 404
(never 403 — an admin must not learn another org's request exists; the IDOR floor, matching
admin_requests detail).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from ..routing.labels import LabelBindings
from ..trace import (
    LabelVerdict,
    RequestContent,
    TraceRow,
    get_request_content,
    upsert_verdict,
)
from .admin_usage import _engine_or_error, _scope_org  # reuse: org pinning + trace-engine guard
from .auth import _error
from .deps import Identity, require_read_role, require_role

router = APIRouter(tags=["admin"])


def _judge(identity: Identity) -> str:
    """The judge key for verdict ownership. A logged-in admin is their user_id; the operator (no
    user_id) is one logical judge 'operator'."""
    return identity.user_id or "operator"


def _bindings(request: Request) -> LabelBindings:
    """The shipped global label->model map (routing/labels.yaml or its override) — cheap YAML load,
    same as admin_routing._global_bindings. Supplies the desc + bound model the queue renders and
    the vocab a corrected_label must validate against."""
    settings = request.app.state.settings
    return LabelBindings(getattr(settings, "label_bindings", "") or None)


def _last_user_text(prompt: str, limit: int = 2000) -> str:
    """Last user message's text from a captured prompt (JSON array of messages), truncated. Handles
    both a plain string content and the vision-style list-of-parts; empty string on any malformed
    input (the queue just shows nothing to judge, never errors)."""
    try:
        msgs = json.loads(prompt)
    except (ValueError, TypeError):
        return ""
    text = ""
    for m in msgs if isinstance(msgs, list) else []:
        if not (isinstance(m, dict) and m.get("role") == "user"):
            continue
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):  # [{type:text, text:...}, ...]
            text = "".join(p.get("text", "") for p in content
                           if isinstance(p, dict) and p.get("type") == "text")
    return text[:limit]


@router.get("/v1/admin/labeling/queue")
async def labeling_queue(
    request: Request,
    org_id: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    identity: Identity = Depends(require_read_role("admin")),
):
    """Recent routing decisions this judge can label: `gateway_events` rows that (a) classified as a
    task type (label not NULL), (b) have captured prompt content, and (c) this judge hasn't verdicted
    yet. Newest first. Each item carries the user text + the label's desc and bound model so the UI
    can show what the router decided and why."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err
    judge = _judge(identity)

    from sqlmodel import Session, select

    stmt = (
        select(TraceRow.request_id, TraceRow.ts_start, TraceRow.label, TraceRow.model)
        .where(TraceRow.label.is_not(None), TraceRow.org_id == org)
        .where(TraceRow.request_id.in_(select(RequestContent.request_id)))
        .where(TraceRow.request_id.not_in(
            select(LabelVerdict.request_id).where(LabelVerdict.judge_user_id == judge)))
        .order_by(TraceRow.ts_start.desc())
        .limit(limit)
    )
    bindings = _bindings(request)
    with Session(engine) as s:
        rows = list(s.execute(stmt))

    # ponytail: one gateway_events row per request_id normally; a fallback's duplicate rows collapse
    # to the newest here (may under-fill a page, refreshed on the next fetch — fine for a queue).
    items, seen = [], set()
    for req_id, ts, label, model in rows:
        if req_id in seen:
            continue
        seen.add(req_id)
        content = get_request_content(engine, req_id)
        if content is None:  # aged out between the join and this read — skip
            continue
        items.append({
            "request_id": req_id,
            "ts": ts,
            "label": label,
            "label_desc": (bindings.labels.get(label) or {}).get("desc"),
            "bound_model": bindings.model_for(label),
            "model_served": model,
            "query_text": _last_user_text(content["prompt"]),
        })
    return {"org_id": org, "queue": items}


class VerdictIn(BaseModel):
    request_id: str
    verdict: str
    corrected_label: str | None = None


@router.post("/v1/admin/labeling/verdict")
async def record_verdict(
    request: Request,
    body: VerdictIn,
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_role("admin")),
):
    """Record this judge's verdict on one request (idempotent per request+judge — re-judging
    overwrites). Validates: verdict is good|bad; a `good` forbids a corrected_label (nothing to
    correct); a corrected_label must be in the label vocab; the request must belong to the scoped
    org (else 404, never revealing another org's request). Denormalizes the user text + predicted
    label + served model onto the verdict so the export survives content aging."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err

    if body.verdict not in ("good", "bad"):
        return _error(400, "verdict must be 'good' or 'bad'", "invalid_request_error",
                      "invalid_verdict")
    if body.verdict == "good" and body.corrected_label:
        return _error(400, "a 'good' verdict cannot carry a corrected_label",
                      "invalid_request_error", "good_forbids_correction")
    if body.corrected_label and body.corrected_label not in _bindings(request).vocab():
        return _error(400, f"corrected_label {body.corrected_label!r} is not in the label vocab",
                      "invalid_request_error", "unknown_label")

    from sqlmodel import Session, select

    # The request must exist WITHIN the scoped org — a cross-org id resolves to None → 404 (the IDOR
    # floor). Newest gateway_events row for the request carries the predicted label + served model.
    with Session(engine) as s:
        row = s.exec(
            select(TraceRow).where(TraceRow.request_id == body.request_id, TraceRow.org_id == org)
            .order_by(TraceRow.id.desc())
        ).first()
    if row is None:
        return _error(404, "no such request", "not_found", "unknown_request")

    content = get_request_content(engine, body.request_id)
    query_text = _last_user_text(content["prompt"]) if content else ""
    stored = upsert_verdict(engine, request_id=body.request_id, judge_user_id=_judge(identity),
                            org_id=org, verdict=body.verdict, corrected_label=body.corrected_label,
                            query_text=query_text, predicted_label=row.label, model_served=row.model)
    return {"verdict": stored}


@router.get("/v1/admin/labeling/stats")
async def labeling_stats(
    request: Request,
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """Rollups for the gamified labeling UI: total judged, judged today (UTC), good/bad split,
    per-label bad counts (the top confusions), and distinct judges. One scan of the org's verdicts —
    they're human-entered and low-volume, so a single fetch + in-Python rollup beats several aggregate
    queries (ponytail: swap for GROUP BY if a firehose of verdicts ever appears)."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err

    from sqlmodel import Session, select

    with Session(engine) as s:
        rows = list(s.exec(select(LabelVerdict).where(LabelVerdict.org_id == org)))

    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()
    bad_by_label: dict[str, int] = {}
    good = judged_today = 0
    for r in rows:
        if r.verdict == "good":
            good += 1
        elif r.predicted_label:  # a bad verdict flags its predicted label as a confusion
            bad_by_label[r.predicted_label] = bad_by_label.get(r.predicted_label, 0) + 1
        if r.created_ts >= today_start:
            judged_today += 1
    return {
        "org_id": org,
        "judged_total": len(rows),
        "judged_today": judged_today,
        "good": good,
        "bad": len(rows) - good,
        "bad_by_label": dict(sorted(bad_by_label.items(), key=lambda kv: -kv[1])),
        "distinct_judges": len({r.judge_user_id for r in rows}),
    }


@router.get("/v1/admin/labeling/export")
async def labeling_export(
    request: Request,
    org_id: str | None = Query(None),
    identity: Identity = Depends(require_read_role("admin")),
):
    """The org's verdicts as eval-set rows (`eval.labels.load_set` shape: id, query, label, note).
    good → the predicted label was right (gold = predicted); bad WITH a correction → gold = the
    corrected label; bad WITHOUT a correction exports NOTHING (it's a flag that the router was wrong,
    not a labeled example). Rows missing a query or a label are dropped (they'd fail load_set). The
    body is a bare JSON array so it round-trips straight through load_set."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    engine, err = _engine_or_error(request)
    if err is not None:
        return err

    from sqlmodel import Session, select

    with Session(engine) as s:
        rows = list(s.exec(select(LabelVerdict).where(LabelVerdict.org_id == org)))

    out = []
    for r in rows:
        if r.verdict == "good":
            label, note = r.predicted_label, "human verdict good"
        elif r.corrected_label:
            label, note = r.corrected_label, f"corrected from {r.predicted_label}"
        else:
            continue  # bad without a correction — a flag, not gold
        if not (label and r.query_text):  # load_set requires a truthy query + label∈vocab
            continue
        out.append({"id": f"verdict-{r.request_id[:8]}", "query": r.query_text,
                    "label": label, "note": note})
    return out
