"""Activity analytics (control-plane) — aggregate bundles + governance-grade LLM insights.

Two products, both METADATA ONLY: `gateway_events` stores no prompt/response content, so nothing
here can expose any (Toto data-boundary principle). `activity_bundle` composes `metering.rollup_usage`
into one dashboard payload; `generate_insights` renders that payload's NUMBERS (never content) into a
prompt for a governance summary. No new SQL lives here — every metric is a `rollup_usage` slice.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from .metering import rollup_usage

_TOP_USERS = 25  # ponytail: cap top-user rows in Python; a paged endpoint if orgs outgrow this

# The fields carried per aggregate row — the whole response shape, and the reason no content leaks.
_KEEP = ("requests", "tokens", "cost_usd", "savings_usd")


def _slim(row: dict) -> dict:
    return {k: row[k] for k in _KEEP}


def _label(row: dict) -> str:
    # NULL label = unattributed traffic (old rows / classify-off) — a named bucket, not a hole.
    return row["label"] if row["label"] is not None else "unclassified"


def _by_real_model(rows: list[dict], catalog) -> list[dict]:
    """Re-group per-model rollup rows by the REAL upstream model name, not our catalog ids.

    A catalog id is our routing handle, not a model — the dashboard shows what actually served
    the request. Every id resolves through the catalog to `effective_upstream_model` (e.g.
    `qwen/qwen3-coder-flash`); rows whose ids point at the same real model collapse into one
    honest row. `catalog_ids` keeps the raw stored ids for provenance; ids no longer in the
    catalog (retired entries) pass through unresolved rather than guessing."""
    merged: dict[str, dict] = {}
    for r in rows:
        raw = r["model"]
        entry = catalog.get(raw) if catalog is not None else None
        name = entry.effective_upstream_model if entry is not None else raw
        row = merged.setdefault(name, {"model": name, "catalog_ids": [],
                                       **{k: 0 for k in _KEEP}})
        for k in _KEEP:
            row[k] += r[k] or 0
        if raw not in row["catalog_ids"]:
            row["catalog_ids"].append(raw)
    return sorted(merged.values(), key=lambda r: r["requests"], reverse=True)


# Drill rows carry the full token-type split; the bundle's _KEEP stays slim on purpose.
_DRILL_KEEP = ("requests", "tokens_prompt", "tokens_completion", "tokens_cached",
               "tokens", "cost_usd", "savings_usd")


def model_drilldown(engine: Any, *, org_id: str, model: str, start: str | None,
                    end: str | None, catalog=None) -> dict:
    """Token-type + task-type breakdown for ONE real upstream model over [start, end).

    `model` may be a real upstream name (what the dashboard shows) or a catalog id — both
    resolve through `effective_upstream_model`, the same identity rule as `_by_real_model`,
    so clicking a merged by_model row always finds every catalog id behind it. Returns
    `totals` (requests + prompt/completion/cached/total tokens + cost/savings), `by_label`
    (the same split per task type, NULL folded into "unclassified", with a request `share`),
    and `catalog_ids` for provenance. Org-scoped at the SQL floor by rollup_usage.
    """
    def resolve(mid: str) -> str:
        entry = catalog.get(mid) if catalog is not None else None
        return entry.effective_upstream_model if entry is not None else mid

    target = resolve(model)
    rows = rollup_usage(engine, org_id=org_id, group_by=["model", "label"],
                        start=start, end=end)
    totals = {k: 0 for k in _DRILL_KEEP}
    by_label: dict[str, dict] = {}
    catalog_ids: list[str] = []
    for r in rows:
        if resolve(r["model"]) != target:
            continue
        if r["model"] not in catalog_ids:
            catalog_ids.append(r["model"])
        row = by_label.setdefault(_label(r), {"label": _label(r),
                                              **{k: 0 for k in _DRILL_KEEP}})
        for k in _DRILL_KEEP:
            row[k] += r[k] or 0
            totals[k] += r[k] or 0
    labels = sorted(by_label.values(), key=lambda r: r["requests"], reverse=True)
    for row in labels:
        row["share"] = round(row["requests"] / totals["requests"], 4) if totals["requests"] else 0.0
    return {"model": target, "catalog_ids": catalog_ids, "totals": totals, "by_label": labels}


def escalation_rates(engine: Any, *, org_id: str, start: str | None, end: str | None) -> dict:
    """Per-task-type escalation rate for ONE org over [start, end) — the routing-dissatisfaction
    signal (W3-C3). Reuses the per-label `rollup_usage` slice (no new SQL): each row already carries
    `requests` and `escalations` (escalated_from-tagged), so share = escalations / requests. Mirrors
    the `fast_path` {requests, share} shape (metering.latency_breakdown). NULL label folds into
    "unclassified". `total` is the org-wide {requests, escalations, share} across all labels.
    """
    by_label = [
        {"label": _label(r), "requests": r["requests"], "escalations": r["escalations"],
         "share": round(r["escalations"] / r["requests"], 4) if r["requests"] else 0.0}
        for r in rollup_usage(engine, org_id=org_id, group_by=["label"], start=start, end=end)
    ]
    by_label.sort(key=lambda r: r["escalations"], reverse=True)
    reqs = sum(r["requests"] for r in by_label)
    esc = sum(r["escalations"] for r in by_label)
    return {
        "total": {"requests": reqs, "escalations": esc,
                  "share": round(esc / reqs, 4) if reqs else 0.0},
        "by_label": by_label,
    }


def activity_bundle(engine: Any, *, org_id: str, start: str | None, end: str | None,
                    catalog=None) -> dict:
    """Compose `rollup_usage` slices into one activity payload for ONE org over [start, end).

    Returns: `totals` (window sums), `by_label` (per task-type, NULL folded into "unclassified"),
    `by_label_day` (label x day rows for a stacked trend), `by_model` (grouped by REAL upstream
    model name when a catalog is supplied — see _by_real_model), and `by_user` (top-25 by
    requests, `by_user_truncated` flags the cap). All org-scoped at the SQL floor by rollup_usage.
    """
    q = dict(org_id=org_id, start=start, end=end)
    [total] = rollup_usage(engine, **q)  # no group_by → exactly one (zeroed) total row

    by_label = [{"label": _label(r), **_slim(r)}
                for r in rollup_usage(engine, group_by=["label"], **q)]
    by_label_day = [{"label": _label(r), "day": r["bucket"], **_slim(r)}
                    for r in rollup_usage(engine, group_by=["label"], granularity="day", **q)]
    by_model = _by_real_model(rollup_usage(engine, group_by=["model"], **q), catalog)

    users = sorted(rollup_usage(engine, group_by=["user"], **q),
                   key=lambda r: r["requests"], reverse=True)
    truncated = len(users) > _TOP_USERS
    by_user = [{"user": r["user"], **_slim(r)} for r in users[:_TOP_USERS]]

    return {
        "totals": _slim(total),
        "by_label": by_label,
        "by_label_day": by_label_day,
        "by_model": by_model,
        "by_user": by_user,
        "by_user_truncated": truncated,
    }


# --- LLM insights: aggregate NUMBERS in, governance JSON out (never content) --------------------

_SYSTEM = (
    "You are a governance analyst for an LLM gateway. You are given ONLY aggregate usage numbers "
    "for one organization over a time window — never any prompt or response content. Report on how "
    "the org is working: task-type mix and trends, concentration across users and models, cost "
    "efficiency and routing savings, the unclassified share, and anything anomalous. "
    'Reply with STRICT JSON only, no prose, exactly: '
    '{"headline": str, "insights": [{"finding": str, "evidence": str}], "recommendations": [str]}'
)


async def generate_insights(
    bundle: dict, *, complete_fn: Callable[[list[dict], str, int], Awaitable[str]], model_id: str,
) -> dict:
    """Ask `model_id` (via `complete_fn`) for governance insights over `bundle`'s numbers.

    `complete_fn(messages, model_id, max_tokens) -> str` is the gateway's `_classify_text` seam
    (no trace, no user turn). Returns the parsed {headline, insights, recommendations} dict. Raises
    on an empty/unparseable/misshapen reply — the ROUTE catches and degrades (insights never 500s a
    dashboard). Only `bundle`'s aggregate numbers reach the model; that is a hard boundary.
    """
    from .driver.prompts import _extract_json

    user = "Aggregate usage bundle (numbers only):\n" + json.dumps(bundle, default=str)
    messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
    text = await complete_fn(messages, model_id, 1200)
    data = _extract_json(text)
    if not isinstance(data, dict) or "headline" not in data:
        raise ValueError("insights model returned no parseable JSON object")
    return {
        "headline": str(data.get("headline", "")),
        "insights": [
            {"finding": str(i.get("finding", "")), "evidence": str(i.get("evidence", ""))}
            for i in data.get("insights", []) if isinstance(i, dict)
        ],
        "recommendations": [str(r) for r in data.get("recommendations", [])],
    }
