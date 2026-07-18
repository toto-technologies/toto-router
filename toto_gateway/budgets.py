"""Team/org monthly budgets (enterprise-readiness W2-C5) — the over-budget decision + threshold alerts.

One object, `BudgetEnforcer`, that the gateway consults once at the top of complete()/stream(). It
owns everything so the gateway stays lean and identity-thin callers (operator, driver-internal) pay
nothing:

  * CONFIG — the caller's effective budget: the team's row, falling back to the org-default sentinel
    row (team_id == org_id), the same team->org fallback the routing overlay uses. No row / 0 budget
    → `decide()` returns None → today's behavior, unchanged.
  * SPEND — the calendar-month SUM(cost_usd) of status='ok' traces (`metering.current_month_spend`),
    org- or team-scoped to match the budget's scope.
  * THRESHOLD ALERTS — crossing 50/80/100% writes ONE `budget:threshold` audit row per threshold per
    month per scope (deduped by a small `budget_alerts` table).

Ponytail: the whole DECISION (config + spend + which thresholds fired) is cached in-process behind a
short TTL keyed by scope, so the hot path runs zero SQL on a cache hit — a budget decision therefore
LAGS real spend AND a just-saved config change by up to the TTL (documented, acceptable: budgets are
soft guardrails, not a real-time paywall). Threshold audits fire only on a cache MISS (a fresh spend
read), so they cost one INSERT-OR-IGNORE per scope per TTL, not per request. Fail-OPEN everywhere: a
broken budget row or an unreachable DB must never 402 a request it can't prove is over budget —
`decide()` swallows every error and returns None.

# ponytail: unbounded in-process dict, one entry per (org, team) seen. Scope count is bounded by the
# tenant count (tiny); add LRU eviction only if a deploy ever has millions of live scopes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from .metering import current_month_spend

# The action at 100% of budget. `observe` is the default (serve + stamp); `downgrade` forces the
# cheapest eligible model; `reject` 402s. Fail-closed at write time (the admin route validates).
BUDGET_ACTIONS = ("observe", "downgrade", "reject")

# Default alert thresholds (fractions of the monthly budget) — 50/80/100%. A budget row may override.
DEFAULT_THRESHOLDS = (0.5, 0.8, 1.0)


def member_budget_key(org_id: str, user_id: str) -> str:
    """The `budgets.team_id` PK value a per-member cap is stored under. A member cap reuses the same
    table as team/org (sentinel-row pattern: org-default already parks an org_id in the team_id PK
    column) — this just parks a namespaced `member:<org>:<user>` value there. The prefix can't collide
    with a real team_id or org_id (those are bare uuids), so no schema change is needed.
    # ponytail: string-key overload, not a scope column. Add a `scope` column only if a member ever
    # needs to also hold a team cap under the same user_id (they can't today — one cap per member)."""
    return f"member:{org_id}:{user_id}"


@dataclass(frozen=True)
class BudgetDecision:
    """The resolved budget posture for one request. `over` is pct >= 1.0; `budget_state` is the
    INTENDED trace stamp when over ("over"|"downgraded"|"rejected"), else None. The gateway acts on
    `action`/`over` (raise on reject, rewrite model on downgrade) and stamps `budget_state`."""

    action: str
    over: bool
    budget_state: str | None
    pct: float
    spend: float
    monthly_usd: float
    scope: str            # "member" | "team" | "org"
    scope_key: str        # member key, team_id, or org_id — what spend was summed over


def _period(now: float) -> str:
    """The current calendar month as "YYYY-MM" (UTC), the budget window key."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m")


def _intended_state(action: str, over: bool) -> str | None:
    if not over:
        return None
    return {"observe": "over", "downgrade": "downgraded", "reject": "rejected"}.get(action, "over")


class BudgetEnforcer:
    def __init__(self, auth: Any, engine_fn: Callable[[], Any], *, ttl: float = 60.0,
                 now: Callable[[], float] = time.time) -> None:
        self._auth = auth
        self._engine_fn = engine_fn          # returns the trace engine, or None (no trace DB)
        self._ttl = ttl
        self._now = now
        self._cache: dict[tuple[str, str | None, str | None],
                          tuple[float, BudgetDecision | None]] = {}

    async def decide(self, org_id: str | None, team_id: str | None,
                     user_id: str | None = None) -> BudgetDecision | None:
        """The budget posture for a request, or None (no org, no budget, or any failure → don't
        enforce). Never raises — fail-open is the whole safety contract of a budget switch. The cache
        keys on user_id too: a member cap resolves per-user, so a member's cached decision must never
        serve another user (the cross-user leak class from the long-horizon money bug)."""
        if not org_id:
            return None
        key = (org_id, team_id, user_id)
        now = self._now()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        try:
            decision = await self._compute(org_id, team_id, user_id, now)
        except Exception:
            decision = None  # fail-open: a broken row / unreachable DB never blocks the request
        self._cache[key] = (now + self._ttl, decision)
        return decision

    async def _compute(self, org_id: str, team_id: str | None, user_id: str | None,
                       now: float) -> BudgetDecision | None:
        cfg = await self._resolve_config(org_id, team_id, user_id)
        if cfg is None:
            return None
        monthly = float(cfg.get("monthly_usd") or 0.0)
        if monthly <= 0:
            return None
        scope, scope_key = cfg["_scope"], cfg["_scope_key"]
        engine = self._engine_fn()
        if engine is None:
            return None  # no trace DB → no spend to measure → don't enforce
        period = _period(now)
        spend = current_month_spend(
            engine, org_id=org_id,
            team_id=scope_key if scope == "team" else None,
            user_id=cfg.get("_user_id") if scope == "member" else None, period=period)
        pct = spend / monthly if monthly else 0.0
        thresholds = cfg.get("thresholds") or list(DEFAULT_THRESHOLDS)
        await self._fire_thresholds(scope_key, period, pct, thresholds, spend, monthly, org_id)
        action = cfg.get("action") or "observe"
        over = pct >= 1.0
        return BudgetDecision(action=action, over=over,
                              budget_state=_intended_state(action, over),
                              pct=pct, spend=spend, monthly_usd=monthly,
                              scope=scope, scope_key=scope_key)

    async def _resolve_config(self, org_id: str, team_id: str | None,
                              user_id: str | None) -> dict | None:
        """The caller's effective budget by first-match fallback member -> team -> org-default sentinel
        (team_id == org_id) — the SAME precedence team/org use today (a more specific row short-circuits
        the broader one; there is no strictest-of-both blend). A member with no cap therefore inherits
        the team's cap, then the org-default. Mirrors deps._resolve_routing_policy's team->org fallback.
        Tags the result with the scope + key it applies to (`_scope`/`_scope_key`, plus `_user_id` for a
        member) so spend sums the right rows."""
        if user_id:
            mkey = member_budget_key(org_id, user_id)
            row = await self._auth.get_budget(mkey)
            if row is not None:
                return {**row, "_scope": "member", "_scope_key": mkey, "_user_id": user_id}
        if team_id:
            row = await self._auth.get_budget(team_id)
            if row is not None:
                return {**row, "_scope": "team", "_scope_key": team_id}
        row = await self._auth.get_budget(org_id)  # org-default sentinel
        if row is not None:
            return {**row, "_scope": "org", "_scope_key": org_id}
        return None

    async def _fire_thresholds(self, scope_key: str, period: str, pct: float,
                               thresholds: list, spend: float, monthly: float,
                               org_id: str) -> None:
        """Write a `budget:threshold` audit row the FIRST time this scope crosses each threshold in
        this month. `budget_alert_fire_once` is the dedupe (INSERT-OR-IGNORE on the alerts table),
        so re-checking every TTL never double-alerts. Best-effort: an audit failure never blocks."""
        for t in sorted(float(x) for x in thresholds):
            if pct < t:
                break  # sorted ascending — nothing higher is crossed either
            try:
                if await self._auth.budget_alert_fire_once(scope_key, period, t):
                    import json

                    await self._auth.write_audit(
                        "budget:threshold", org_id=org_id, target_type="budget", target_id=scope_key,
                        metadata=json.dumps({"threshold": t, "pct": round(pct, 4),
                                             "spend_usd": round(spend, 4), "budget_usd": monthly,
                                             "period": period}))
            except Exception:
                pass
