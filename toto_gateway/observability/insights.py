"""Pure insight derivation over normalized provider data — no HTTP, no DB, no clock.

`build_insights` takes the per-provider bundles the route already fetched (usage buckets, cost
buckets, members, and name-resolution maps) plus the catalog pricing table, and returns exactly
the insights JSON shape in the module contract. Everything here is deterministic and unit-testable
with fixture dataclasses; the route owns the fetch, the cache, and per-provider error isolation.

Money is float USD (connectors already normalized). Savings candidates are labeled estimates: a
model whose observed blended $/Mtok materially exceeds the cheapest catalog alternative is flagged
at a conservative assumed-30%-shiftable share — the router's value prop, quantified honestly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date, timedelta

from .schema import CostBucket, OrgMember, UsageBucket

_TOP_N = 10
_MATERIAL_CHEAPER = 0.70   # a catalog option must be <=70% of the observed rate to be "materially" cheaper
_SHIFTABLE_SHARE = 0.30    # conservative fraction of a model's spend assumed movable
_MIN_SAVINGS_USD = 0.01    # drop sub-cent estimates as noise


def _day(iso: str) -> str:
    return iso[:10]


def _fill_days(starting_at: str, ending_at: str) -> list[str]:
    """Every YYYY-MM-DD in [start, end) so the spend timeline has no gaps. A window ending
    mid-day (the live route ends at now) includes that partial day — otherwise today's spend
    would count in the summary total but vanish from the chart."""
    start, end = date.fromisoformat(_day(starting_at)), date.fromisoformat(_day(ending_at))
    if ending_at[11:19] not in ("", "00:00:00"):
        end += timedelta(days=1)
    out, d = [], start
    while d < end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _blended_rate_per_mtok(prompt_per_1k: float, completion_per_1k: float,
                           in_frac: float, out_frac: float) -> float:
    """A catalog entry's $/Mtok at the observed input/output mix (price is per-1k → ×1000)."""
    return (in_frac * prompt_per_1k + out_frac * completion_per_1k) * 1000.0


def _savings_for_model(model: str, spend_usd: float, input_tokens: int, output_tokens: int,
                       days: int, pricing: list[dict]) -> dict | None:
    """Cheapest materially-cheaper catalog alternative for one model's traffic, or None."""
    tokens = input_tokens + output_tokens
    if spend_usd <= 0 or tokens <= 0 or not pricing:
        return None
    observed_rate = spend_usd / (tokens / 1e6)
    in_frac, out_frac = input_tokens / tokens, output_tokens / tokens
    best = None
    for p in pricing:
        if (p["id"] or "").lower() == (model or "").lower():
            continue  # never suggest a model onto itself
        rate = _blended_rate_per_mtok(p.get("prompt_per_1k", 0.0), p.get("completion_per_1k", 0.0),
                                      in_frac, out_frac)
        if best is None or rate < best[1]:
            best = (p["id"], rate)
    if best is None or best[1] >= observed_rate * _MATERIAL_CHEAPER:
        return None
    cheaper_id, cheaper_rate = best
    savings_fraction = (observed_rate - cheaper_rate) / observed_rate
    monthly_spend = spend_usd * (30.0 / days) if days else spend_usd
    est = monthly_spend * _SHIFTABLE_SHARE * savings_fraction
    if est < _MIN_SAVINGS_USD:
        return None
    return {
        "model": model,
        "spend_usd": round(spend_usd, 4),
        "suggestion": (f"~{round(_SHIFTABLE_SHARE * 100)}% of {model} traffic could move to "
                       f"{cheaper_id} (~{round(savings_fraction * 100)}% cheaper per token)"),
        "est_monthly_savings_usd": round(est, 2),
        "basis": "estimate",
    }


def build_insights(window: dict, days: int, providers: dict[str, dict],
                   pricing: list[dict] | None = None) -> dict:
    """Merge per-provider bundles into the contract insights shape.

    `providers` maps provider -> bundle:
      {"configured": bool, "org_name": str|None, "error": str|None,
       "usage": list[UsageBucket], "costs": list[CostBucket], "members": list[OrgMember],
       "scope_names": {scope_id: name}, "actor_names": {actor_id: name}}
    A provider with an error (or unconfigured) contributes no data — its status still appears under
    `providers` so the caller sees why it's absent. `pricing` is [{"id","prompt_per_1k",
    "completion_per_1k"}] from the catalog; empty → no savings candidates.
    """
    pricing = pricing or []
    prov_status = {p: {"configured": b.get("configured", False),
                       "org_name": b.get("org_name"),
                       "error": b.get("error")}
                   for p, b in providers.items()}

    # Only providers that actually returned data feed the aggregates.
    live = {p: b for p, b in providers.items() if not b.get("error")}

    usage: list[UsageBucket] = [u for b in live.values() for u in b.get("usage", [])]
    costs: list[CostBucket] = [c for b in live.values() for c in b.get("costs", [])]
    members: list[OrgMember] = [m for b in live.values() for m in b.get("members", [])]
    scope_names = {(p, sid): name for p, b in live.items()
                   for sid, name in b.get("scope_names", {}).items()}
    actor_names = {(p, aid): name for p, b in live.items()
                   for aid, name in b.get("actor_names", {}).items()}

    # --- summary + cache efficiency ---
    total_spend = sum(c.amount_usd for c in costs)
    input_tokens = sum(u.input_tokens for u in usage)
    cached_tokens = sum(u.cached_input_tokens for u in usage)
    output_tokens = sum(u.output_tokens for u in usage)
    requests = sum(u.requests for u in usage if u.requests is not None)
    presented = input_tokens + cached_tokens
    cache_rate = (cached_tokens / presented) if presented else 0.0

    # --- spend by day (gap-filled) ---
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"anthropic": 0.0, "openai": 0.0})
    for c in costs:
        by_day[_day(c.starting_at)][c.provider] = (
            by_day[_day(c.starting_at)].get(c.provider, 0.0) + c.amount_usd)
    spend_by_day = []
    for d in _fill_days(window["starting_at"], window["ending_at"]):
        a, o = by_day[d].get("anthropic", 0.0), by_day[d].get("openai", 0.0)
        spend_by_day.append({"date": d, "anthropic_usd": round(a, 4), "openai_usd": round(o, 4),
                             "total_usd": round(a + o, 4)})

    # --- spend by model (join cost + tokens on provider+model) ---
    model_spend: dict[tuple, float] = defaultdict(float)
    model_tok: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])  # [input, output]
    for c in costs:
        if c.model:
            model_spend[(c.provider, c.model)] += c.amount_usd
    for u in usage:
        if u.model:
            model_tok[(u.provider, u.model)][0] += u.input_tokens
            model_tok[(u.provider, u.model)][1] += u.output_tokens
    spend_by_model = []
    for key in sorted(set(model_spend) | set(model_tok),
                      key=lambda k: model_spend.get(k, 0.0), reverse=True):
        provider, model = key
        spend = model_spend.get(key, 0.0)
        tin, tout = model_tok.get(key, [0, 0])
        spend_by_model.append({
            "provider": provider, "model": model, "spend_usd": round(spend, 4),
            "input_tokens": tin, "output_tokens": tout,
            "share": round(spend / total_spend, 4) if total_spend else 0.0,
        })

    # --- top scopes (workspaces/projects by spend) ---
    scope_spend: dict[tuple, float] = defaultdict(float)
    for c in costs:
        if c.scope_id:
            scope_spend[(c.provider, c.scope_id)] += c.amount_usd
    top_scopes = [
        {"provider": p, "scope_id": sid, "name": scope_names.get((p, sid)),
         "spend_usd": round(amt, 4)}
        for (p, sid), amt in sorted(scope_spend.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_N]
    ]

    # --- top actors (api keys/users by tokens) ---
    actor_tok: dict[tuple, list] = defaultdict(lambda: [0, 0, None])  # [input, output, requests]
    for u in usage:
        if u.actor_id:
            a = actor_tok[(u.provider, u.actor_id)]
            a[0] += u.input_tokens
            a[1] += u.output_tokens
            if u.requests is not None:
                a[2] = (a[2] or 0) + u.requests
    top_actors = [
        {"provider": p, "actor_id": aid, "name": actor_names.get((p, aid)),
         "input_tokens": v[0], "output_tokens": v[1], "requests": v[2]}
        for (p, aid), v in sorted(actor_tok.items(), key=lambda kv: kv[1][0] + kv[1][1],
                                  reverse=True)[:_TOP_N]
    ]

    # --- savings candidates ---
    savings = []
    for row in spend_by_model:
        cand = _savings_for_model(row["model"], row["spend_usd"], row["input_tokens"],
                                  row["output_tokens"], days, pricing)
        if cand:
            savings.append({"provider": row["provider"], **cand})
    savings.sort(key=lambda s: s["est_monthly_savings_usd"], reverse=True)

    return {
        "window": window,
        "providers": prov_status,
        "summary": {
            "total_spend_usd": round(total_spend, 4),
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "output_tokens": output_tokens,
            "requests": requests,
            "members": len(members),
            "cache_read_rate": round(cache_rate, 4),
        },
        "spend_by_day": spend_by_day,
        "spend_by_model": spend_by_model,
        "top_scopes": top_scopes,
        "top_actors": top_actors,
        "members": [_member_row(m) for m in members],
        "cache_efficiency": {"cached_input_tokens": cached_tokens, "input_tokens": input_tokens,
                             "rate": round(cache_rate, 4)},
        "savings_candidates": savings,
    }


def _member_row(m: OrgMember) -> dict:
    d = asdict(m)
    return {"provider": d["provider"], "email": d["email"], "name": d["name"],
            "role": d["role"], "added_at": d["added_at"]}
