"""Usage / metering taxonomy (control-plane C4) — a read-only rollup layer over `gateway_events`.

House discipline (Ponytail): the trace table (`gateway_events`, written by `SqlModelTraceWriter`)
IS the metering substrate — F put `org_id`/`team_id` on it next to the cost/token fields that
`_account` already writes. This module is a QUERY layer over that one table: aggregate by any
grounded dimension, no parallel event stream, no new storage.

Two products:
  - `rollup_usage(engine, ...)`  → per-dimension usage rows (requests, tokens, cost, savings).
  - `export_billing_records(...)` → Stripe-SHAPED billing records (the export SEAM only — NO
    Stripe SDK, NO invoicing; a future billing job consumes this list).

Dual-dialect by construction: SQLAlchemy Core over `TraceRow.__table__`, and time bucketing uses
`substr()` on the ISO-8601 `ts_start` TEXT column — ISO-8601 sorts lexically, so a date prefix IS
the day/hour bucket and a string range IS the time window. No date functions → identical SQL on
SQLite and Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import Integer, case, cast, func, select

from .trace import TraceRow

# Dimension name → the `gateway_events` column it groups by. Only columns the trace ACTUALLY
# carries are here — that is the whole allowlist (also the SQL-injection guard: group_by comes
# from a query param, so an unknown name is rejected, never interpolated).
# ponytail: `tool` is NOT in the trace yet; add one line here when the column lands, no other change.
_DIMENSIONS: dict[str, str] = {
    "org": "org_id",
    "team": "team_id",
    "model": "model",
    "provider": "runner_id",   # runner_id → the box/provider that answered
    "lane": "lane",            # economy | frontier | fake
    "residency": "residency_class",
    "label": "label",          # task type (analytics A1) — NULL = unattributed
    "user": "user_id",         # who called (analytics A1)
}

_GRANULARITY_LEN = {"day": 10, "hour": 13}  # ISO prefix length: "2026-07-08" / "2026-07-08T14"

_C = TraceRow.__table__.c


def _bucket_expr(granularity: str | None):
    n = _GRANULARITY_LEN.get(granularity or "")
    return func.substr(_C.ts_start, 1, n).label("bucket") if n else None


def rollup_usage(
    engine: Any,
    *,
    org_id: str,
    group_by: Iterable[str] = (),
    start: str | None = None,
    end: str | None = None,
    granularity: str | None = None,
    status: str | None = "ok",
) -> list[dict]:
    """Aggregate `gateway_events` for ONE org into usage rows.

    `org_id` is mandatory and always filtered — the rollup is org-scoped at the SQL floor, so a
    caller can never sum another org's traffic. `group_by` is any subset of `_DIMENSIONS`;
    `granularity` ("day"|"hour") adds a time bucket. `start`/`end` are ISO-8601 strings compared
    lexically against `ts_start` (half-open [start, end)). Returns one dict per group with the
    group keys plus: requests, tokens_prompt, tokens_completion, tokens_cached, tokens, cost_usd,
    frontier_baseline_usd, savings_usd, escalations (count of escalated_from-tagged requests).
    """
    dims = list(group_by)
    unknown = [d for d in dims if d not in _DIMENSIONS]
    if unknown:
        raise ValueError(f"unknown group_by dimension(s): {unknown}")

    group_cols = [_C[_DIMENSIONS[d]].label(d) for d in dims]
    bucket = _bucket_expr(granularity)
    if bucket is not None:
        group_cols.append(bucket)

    cost = func.coalesce(func.sum(_C.cost_usd), 0.0)
    baseline = func.coalesce(func.sum(_C.frontier_baseline_usd), 0.0)
    # W3-C3: how many of these requests were escalations (retried onto another model). One dual-dialect
    # CASE sum on the additive escalated_from column — rides every group_by (per-label, per-model, …).
    escalations = func.coalesce(
        func.sum(case((_C.escalated_from.isnot(None), 1), else_=0)), 0)
    stmt = select(
        *group_cols,
        func.count().label("requests"),
        func.coalesce(func.sum(_C.tokens_prompt), 0).label("tokens_prompt"),
        func.coalesce(func.sum(_C.tokens_completion), 0).label("tokens_completion"),
        func.coalesce(func.sum(_C.tokens_cached), 0).label("tokens_cached"),
        cost.label("cost_usd"),
        baseline.label("frontier_baseline_usd"),
        escalations.label("escalations"),
    ).where(_C.org_id == org_id)

    if status is not None:
        stmt = stmt.where(_C.status == status)
    if start is not None:
        stmt = stmt.where(_C.ts_start >= start)
    if end is not None:
        stmt = stmt.where(_C.ts_start < end)
    if group_cols:
        stmt = stmt.group_by(*group_cols).order_by(*group_cols)

    from sqlmodel import Session

    rows: list[dict] = []
    with Session(engine) as s:
        for r in s.execute(stmt):
            m = r._mapping
            tp, tc = m["tokens_prompt"], m["tokens_completion"]
            cost_usd, base = float(m["cost_usd"]), float(m["frontier_baseline_usd"])
            row = {d: m[d] for d in dims}
            if bucket is not None:
                row["bucket"] = m["bucket"]
            row.update(
                requests=m["requests"],
                tokens_prompt=tp,
                tokens_completion=tc,
                tokens_cached=m["tokens_cached"],
                tokens=tp + tc,
                cost_usd=cost_usd,
                frontier_baseline_usd=base,
                savings_usd=max(base - cost_usd, 0.0),
                escalations=m["escalations"],  # W3-C3: escalated requests in this group
            )
            rows.append(row)
    return rows


# --- Budget spend (W2-C5) — the calendar-month cost sum a budget decision reads ------------------


def current_month_spend(engine: Any, *, org_id: str, team_id: str | None, period: str,
                        user_id: str | None = None) -> float:
    """SUM(cost_usd) of status='ok' traces for one org (optionally one team or one user) over the
    calendar month `period` ("YYYY-MM"). Org-scoped at the SQL floor exactly like rollup_usage
    (mandatory org filter — a budget can never sum another org's traffic); a team budget adds
    `team_id`, a member budget adds `user_id` (so one member's spend never counts against another's
    cap). Half-open [month_start, next_month) via the same lexical ts_start prefix trick as
    export_billing_records. Returns 0.0 for an empty window. The BudgetEnforcer caches this behind a
    short TTL, so the hot path never runs it per request."""
    start, end = _period_window(period)
    stmt = select(func.coalesce(func.sum(_C.cost_usd), 0.0)).where(
        _C.org_id == org_id, _C.status == "ok", _C.ts_start >= start, _C.ts_start < end)
    if team_id is not None:
        stmt = stmt.where(_C.team_id == team_id)
    if user_id is not None:
        stmt = stmt.where(_C.user_id == user_id)

    from sqlmodel import Session

    with Session(engine) as s:
        return float(s.execute(stmt).scalar_one() or 0.0)


# --- Cache P&L (multi-model-caching plan §6) — an honest savings rollup over gateway_events ------


def cache_savings(
    engine: Any,
    *,
    catalog: Any,
    org_id: str | None = None,
    user_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """The caching profit-and-loss for a scope + window, per MODEL (the provider-ish dimension the
    trace carries) with the lane alongside.

    For each model row: read_savings_usd = tokens_cached * prompt_price * (1 - cache_read_multiplier)
    (what the discounted read slice would have cost at full input price, minus what it did cost) and
    write_premium_usd = tokens_cache_write * prompt_price * (cache_write_multiplier - 1) (the extra a
    provider charges to WRITE the cache). net_usd = read_savings - write_premium — the honest number
    behind "caching saved your org $X". Prices come from the live `catalog` at compute time (a
    row's model may have been re-priced since the call); an unknown/retired model prices at 0, so it
    contributes 0 savings rather than guessing. We derive from token COUNTS × the price table, never
    from the stored cost_usd (which already reflects the read discount — deriving savings off it
    would double-count).

    Scope is the CALLER's job (routes/admin_usage): pass `org_id`/`user_id` exactly as list_requests
    — the same IDOR floor. `start`/`end` are ISO-8601, lexical against ts_start (inclusive [start,
    end], matching the activity list). Returns {"total": {...}, "models": [...], "from", "to"}."""
    from .catalog import normalize_legacy_id

    stmt = select(
        _C.model.label("model"),
        _C.lane.label("lane"),
        func.count().label("requests"),
        func.coalesce(func.sum(_C.tokens_cached), 0).label("tokens_cached"),
        func.coalesce(func.sum(_C.tokens_cache_write), 0).label("tokens_cache_write"),
    ).where(_C.status == "ok")
    if org_id is not None:
        stmt = stmt.where(_C.org_id == org_id)
    if user_id is not None:
        stmt = stmt.where(_C.user_id == user_id)
    if start is not None:
        stmt = stmt.where(_C.ts_start >= start)
    if end is not None:
        stmt = stmt.where(_C.ts_start <= end)
    stmt = stmt.group_by(_C.model, _C.lane).order_by(_C.model, _C.lane)

    from sqlmodel import Session

    models: list[dict] = []
    tot = {"net_usd": 0.0, "read_savings_usd": 0.0, "write_premium_usd": 0.0,
           "tokens_cached": 0, "tokens_cache_write": 0}
    with Session(engine) as s:
        for r in s.execute(stmt):
            m = r._mapping
            cached, written = int(m["tokens_cached"]), int(m["tokens_cache_write"])
            entry = catalog.get(m["model"]) or catalog.get(normalize_legacy_id(m["model"]))
            price = entry.price_usd_per_1k if entry else None
            prompt = price.prompt if price else 0.0
            read_mult = price.cache_read_multiplier if price else 0.1
            write_mult = price.cache_write_multiplier if price else 1.0
            read_savings = round((cached / 1000.0) * prompt * (1.0 - read_mult), 6)
            write_premium = round((written / 1000.0) * prompt * (write_mult - 1.0), 6)
            net = round(read_savings - write_premium, 6)
            models.append({
                "model": m["model"],
                # ids are routing handles; dashboards name the ACTUAL model (same rule as rollup)
                "model_name": entry.effective_upstream_model if entry else m["model"],
                "lane": m["lane"], "requests": m["requests"],
                "tokens_cached": cached, "tokens_cache_write": written,
                "read_savings_usd": read_savings, "write_premium_usd": write_premium,
                "net_usd": net,
            })
            tot["read_savings_usd"] += read_savings
            tot["write_premium_usd"] += write_premium
            tot["net_usd"] += net
            tot["tokens_cached"] += cached
            tot["tokens_cache_write"] += written
    for k in ("net_usd", "read_savings_usd", "write_premium_usd"):
        tot[k] = round(tot[k], 6)
    return {"total": tot, "models": models, "from": start, "to": end}


# --- Cache-health time series (A8) — the console's observability pane -------------------------


def cache_health(
    engine: Any,
    *,
    org_id: str,
    start: str | None = None,
    end: str | None = None,
    granularity: str = "day",
) -> list[dict]:
    """A caching-health time series for ONE org: per time bucket, how well the prefix cache is
    working. Org-scoped at the SQL floor exactly like rollup_usage (the org filter is mandatory, so
    a caller can never sum another org's traffic). `granularity` day|hour buckets `ts_start` by its
    ISO prefix; `start`/`end` are ISO-8601, lexical against ts_start (inclusive [start, end], same as
    cache_savings). Only status='ok' rows count (a failed turn cached nothing).

    Each bucket: requests, tokens_prompt, tokens_cached, tokens_cache_write, warm_hold_requests
    (turns the TTL-aware incumbent hold kept warm — route_reason ends `:warm-hold`), and hit_rate
    (tokens_cached / tokens_prompt, computed in Python so an empty bucket reads 0.0 rather than
    dividing by zero)."""
    bucket = _bucket_expr(granularity)
    if bucket is None:  # unknown granularity from a direct caller → default to day (identity check,
        bucket = _bucket_expr("day")  # never truthiness — a SQL clause has no boolean value)
    warm_hold = func.sum(case((_C.route_reason.like("%:warm-hold"), 1), else_=0))
    stmt = select(
        bucket,
        func.count().label("requests"),
        func.coalesce(func.sum(_C.tokens_prompt), 0).label("tokens_prompt"),
        func.coalesce(func.sum(_C.tokens_cached), 0).label("tokens_cached"),
        func.coalesce(func.sum(_C.tokens_cache_write), 0).label("tokens_cache_write"),
        func.coalesce(warm_hold, 0).label("warm_hold_requests"),
    ).where(_C.org_id == org_id).where(_C.status == "ok")
    if start is not None:
        stmt = stmt.where(_C.ts_start >= start)
    if end is not None:
        stmt = stmt.where(_C.ts_start <= end)
    stmt = stmt.group_by(bucket).order_by(bucket)

    from sqlmodel import Session

    rows: list[dict] = []
    with Session(engine) as s:
        for r in s.execute(stmt):
            m = r._mapping
            prompt, cached = int(m["tokens_prompt"]), int(m["tokens_cached"])
            rows.append({
                "bucket": m["bucket"],
                "requests": m["requests"],
                "tokens_prompt": prompt,
                "tokens_cached": cached,
                "tokens_cache_write": int(m["tokens_cache_write"]),
                "warm_hold_requests": int(m["warm_hold_requests"]),
                "hit_rate": round(cached / prompt, 6) if prompt else 0.0,
            })
    return rows


# --- Per-stage latency summary (W1-C2) — the console overhead panel over gateway_events ---------


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100) over `values`, or None when empty. Nearest-rank (not
    interpolation) so a test with a handful of rows asserts an exact observed value; the window is
    modest, so the Python sort is cheaper than dialect-specific percentile SQL (SQLite has none)."""
    if not values:
        return None
    import math

    s = sorted(values)
    idx = min(len(s) - 1, max(0, math.ceil(q / 100.0 * len(s)) - 1))
    return s[idx]


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def latency_summary(engine: Any, *, org_id: str, start: str | None = None,
                    end: str | None = None) -> dict:
    """Per-stage latency + fast-path summary for ONE org over [start, end).

    Org-scoped at the SQL floor (mandatory org filter, same as rollup_usage), status='ok' only (an
    errored turn has no meaningful upstream/overhead). Returns p50/p95 of gateway overhead, avg+p95
    of each stage (classify/plan/upstream, NULLs skipped so a fast-path row doesn't drag classify
    toward zero), the request count, and the FAST-PATH share — requests that ran no classifier
    (classify_ms NULL or 0). Percentiles computed in Python (see `_percentile`).
    """
    cols = ("latency_ms_gateway_overhead", "classify_ms", "plan_ms", "upstream_ms")
    stmt = (select(*[_C[c].label(c) for c in cols])
            .where(_C.org_id == org_id).where(_C.status == "ok"))
    if start is not None:
        stmt = stmt.where(_C.ts_start >= start)
    if end is not None:
        stmt = stmt.where(_C.ts_start < end)

    from sqlmodel import Session

    overhead: list[float] = []
    stages: dict[str, list[float]] = {"classify_ms": [], "plan_ms": [], "upstream_ms": []}
    total = fast = 0
    with Session(engine) as s:
        for r in s.execute(stmt):
            m = r._mapping
            total += 1
            oh = m["latency_ms_gateway_overhead"]
            if oh is not None:
                overhead.append(float(oh))
            for k in stages:
                v = m[k]
                if v is None:
                    continue
                if k == "classify_ms" and not v:  # 0 classify == no classifier ran (like NULL),
                    continue                       # not a classify-latency data point
                stages[k].append(float(v))
            if not m["classify_ms"]:  # NULL or 0 → no classifier call this request (fast path)
                fast += 1
    return {
        "requests": total,
        "overhead_ms": {"p50": _percentile(overhead, 50), "p95": _percentile(overhead, 95)},
        "stages": {k: {"avg": _avg(v), "p95": _percentile(v, 95)} for k, v in stages.items()},
        "fast_path": {"requests": fast, "share": round(fast / total, 4) if total else 0.0},
    }


# --- Per-request activity log (analytics A2) — the decision-trail list over gateway_events ------
# METADATA ONLY: `gateway_events` stores no prompt/response content, so this query CANNOT expose
# any — it selects the routing-decision columns and nothing else (Toto data-boundary principle).

# The decision-trail projection: DB column -> response key. This IS the response shape (one place),
# and — like `_DIMENSIONS` — the allowlist: only these columns are ever read/returned, no content.
_REQUEST_COLS: dict[str, str] = {
    "id": "id",                          # stable per-row id — the key the detail endpoint opens
    "conversation_key": "conversation_key",  # multi-turn grouping key (also filterable)
    "ts_start": "ts",
    "model": "model",
    "label": "classified_as",           # task type (A1) — already derived at finalize
    "route_reason": "route_reason",
    "lane": "lane",
    "residency_class": "residency",
    "tokens_prompt": "tokens_prompt",
    "tokens_cached": "tokens_cached",   # warm-prefix reads — the cache-health signal per turn
    "tokens_cache_write": "tokens_cache_write",  # warm-prefix writes — the P&L write-ledger per turn
    "tokens_completion": "tokens_completion",
    "cost_usd": "cost_usd",
    "cost_estimated": "cost_estimated",
    "frontier_baseline_usd": "frontier_baseline_usd",
    "latency_ms_total": "latency_ms",
    "guard_action": "guard_action",
    "status": "status",
    "user_id": "user_id",
    "team_id": "team_id",
}


def list_requests(
    engine: Any,
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    model: str | None = None,
    label: str | None = None,
    conversation_key: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Recent `gateway_events` rows as decision-trail objects, newest first (metadata only).

    Scoping is the CALLER's job (routes/admin_requests): pass `org_id` to pin one org (admin),
    `user_id` to pin one user (a member sees only their own), or neither for the operator's
    unrestricted view. `start`/`end` are ISO-8601 strings compared lexically against `ts_start`
    (inclusive [start, end], same TEXT-column trick as rollup_usage). Indexed on org_id/user_id/
    model/label/ts_start (A1). Returns one dict per row keyed by `_REQUEST_COLS` — NO content.
    """
    stmt = select(*[_C[c].label(c) for c in _REQUEST_COLS])
    if org_id is not None:
        stmt = stmt.where(_C.org_id == org_id)
    if user_id is not None:
        stmt = stmt.where(_C.user_id == user_id)
    if model is not None:
        stmt = stmt.where(_C.model == model)
    if label is not None:
        stmt = stmt.where(_C.label == label)
    if conversation_key is not None:
        stmt = stmt.where(_C.conversation_key == conversation_key)
    if start is not None:
        stmt = stmt.where(_C.ts_start >= start)
    if end is not None:
        stmt = stmt.where(_C.ts_start <= end)
    stmt = stmt.order_by(_C.ts_start.desc()).limit(limit).offset(offset)

    from sqlmodel import Session

    with Session(engine) as s:
        return [{out: r._mapping[col] for col, out in _REQUEST_COLS.items()}
                for r in s.execute(stmt)]


def get_request(engine: Any, request_row_id: int, *, org_id: str | None = None,
                user_id: str | None = None) -> dict | None:
    """One request's decision-trail metadata by its stable row `id`, scoped like list_requests.

    The scope filters (org_id / user_id) are the CALLER's entitlement (routes/admin_requests
    detail): a member passes their own user_id so another user's id resolves to None (a 404 the
    caller can't distinguish from absent — the IDOR floor). Returns the `_REQUEST_COLS` projection
    plus `request_id` (the content lookup key), or None when no row matches within scope."""
    cols = [_C[c].label(c) for c in _REQUEST_COLS]
    stmt = select(*cols, _C.request_id.label("request_id")).where(_C.id == request_row_id)
    if org_id is not None:
        stmt = stmt.where(_C.org_id == org_id)
    if user_id is not None:
        stmt = stmt.where(_C.user_id == user_id)

    from sqlmodel import Session

    with Session(engine) as s:
        row = s.execute(stmt).first()
        if row is None:
            return None
        out = {o: row._mapping[c] for c, o in _REQUEST_COLS.items()}
        out["request_id"] = row._mapping["request_id"]
        return out


# --- Stripe export seam (C4: the SHAPE + a documented seam, NOT invoicing) --------------------


@dataclass(frozen=True)
class BillingRecord:
    """One billing-ready line item. Stripe-shaped per control-surface §5.8; a future billing job
    maps `quantity_tokens`/`cost_usd` onto Stripe usage records. `estimated` mirrors the engine's
    `cost_estimated` so a bill built off this never silently claims false precision."""

    org_id: str
    period: str            # billing period, "YYYY-MM"
    team_id: str | None
    model: str
    quantity_tokens: int
    cost_usd: float
    estimated: bool
    unit: str = "tokens"


def _period_window(period: str) -> tuple[str, str]:
    """"YYYY-MM" → (start, end) as date-only ISO strings, half-open. Lexical prefixes of ts_start:
    "2026-07-01" <= "2026-07-08T…+00:00" < "2026-08-01"."""
    year, month = (int(p) for p in period.split("-"))
    start = f"{year:04d}-{month:02d}-01"
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return start, f"{ny:04d}-{nm:02d}-01"


def export_billing_records(engine: Any, org_id: str, period: str) -> list[BillingRecord]:
    """Produce Stripe-shaped billing records for one org and billing period.

    The SEAM: emits well-formed `BillingRecord`s (org, period, quantity, unit, cost) grouped by
    (team, model) over the period's traces. A future Stripe job consumes this list. This function
    does NOT touch Stripe and does NOT invoice — that boundary is deliberate (control-surface #7).
    """
    start, end = _period_window(period)
    # Group by (team, model); fold `cost_estimated` per group via MAX(cast bool→int) — portable on
    # both dialects (PG has no MAX(boolean); the int cast sidesteps it).
    estimated = func.max(cast(_C.cost_estimated, Integer)).label("estimated")
    stmt = (
        select(
            _C.team_id.label("team_id"),
            _C.model.label("model"),
            func.coalesce(func.sum(_C.tokens_prompt + _C.tokens_completion), 0).label("tokens"),
            func.coalesce(func.sum(_C.cost_usd), 0.0).label("cost_usd"),
            estimated,
        )
        .where(_C.org_id == org_id, _C.status == "ok", _C.ts_start >= start, _C.ts_start < end)
        .group_by(_C.team_id, _C.model)
        .order_by(_C.team_id, _C.model)
    )
    from sqlmodel import Session

    out: list[BillingRecord] = []
    with Session(engine) as s:
        for r in s.execute(stmt):
            m = r._mapping
            out.append(BillingRecord(
                org_id=org_id,
                period=period,
                team_id=m["team_id"],
                model=m["model"],
                quantity_tokens=int(m["tokens"]),
                cost_usd=float(m["cost_usd"]),
                estimated=bool(m["estimated"]),
            ))
    return out
