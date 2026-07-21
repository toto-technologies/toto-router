"""Catalog freshness: the daily discovery-snapshot pipeline behind the Model Library's "new"
surfacing, the "no longer listed upstream" flag, and the price-drift flag on adopted models.

Two halves, kept apart so the interesting logic is testable without the network:
  - `snapshot_diff` (pure) — diff a fetched discovery list against the stored snapshot rows.
  - `refresh_provider` / `run_freshness` (async) — fetch each keyed provider, persist the snapshot,
    optionally auto-adopt, and hand back the diff. A failed provider degrades to its stale snapshot.

Snapshots are PLATFORM-wide (openrouter is keyless, fireworks/cloudflare use the platform key), so
they carry no scope; adoptions the auto-adopt path writes land in the single-tenant local scope.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger("toto_gateway.freshness")


@dataclass(frozen=True)
class ProviderDiff:
    provider: str
    checked_at: float
    added: list[str]      # slugs first seen this tick
    removed: list[str]    # slugs in the snapshot no longer returned upstream
    priced: list[str]     # slugs whose upstream price changed since last snapshot
    error: str | None = None


def snapshot_diff(existing: dict[str, dict], fetched: list[dict], now: float) -> ProviderDiff:
    """Diff a provider's fetched discovery list against its stored snapshot rows (keyed by slug).
    Pure: returns the ProviderDiff; the caller persists. `existing[slug]` carries price_in/price_out;
    `fetched` items carry slug + optional price_in/price_out. Price change ignores None↔None and
    treats a missing fetched price as "unknown, not a change"."""
    fetched_by = {m["slug"]: m for m in fetched if m.get("slug")}
    added, priced = [], []
    for slug, m in fetched_by.items():
        prior = existing.get(slug)
        if prior is None:
            added.append(slug)
            continue
        pin, pout = m.get("price_in"), m.get("price_out")
        if (pin is not None and pin != prior.get("price_in")) or \
           (pout is not None and pout != prior.get("price_out")):
            priced.append(slug)
    removed = [slug for slug in existing if slug not in fetched_by]
    return ProviderDiff(provider="", checked_at=now, added=sorted(added),
                        removed=sorted(removed), priced=sorted(priced))


def is_new(first_seen: float | None, now: float, window_days: float) -> bool:
    """A model is "new" while it's within window_days of first being seen. None → never new."""
    return first_seen is not None and (now - first_seen) <= window_days * 86400


def price_drift(entry_price_in: float, entry_price_out: float, snap: dict | None) -> dict | None:
    """The upstream price change for an ADOPTED model: its stored (accepted) price vs the latest
    snapshot price. None when no snapshot, the snapshot carries no price, or nothing changed. Prices
    are per-1k (the catalog's unit); the console renders per-Mtok."""
    if snap is None:
        return None
    new_in, new_out = snap.get("price_in"), snap.get("price_out")
    if new_in is None and new_out is None:
        return None
    if (new_in or 0.0) == entry_price_in and (new_out or 0.0) == entry_price_out:
        return None
    return {"old_in": entry_price_in, "old_out": entry_price_out,
            "new_in": new_in, "new_out": new_out}


def provider_checked_at(snap_rows: list[dict]) -> float | None:
    """When a provider's snapshot was last refreshed = the newest last_seen across its rows. A row
    behind this is no longer being returned upstream. None when the provider has no snapshot yet."""
    return max((r["last_seen"] for r in snap_rows), default=None)


def adopted_flags(upstream_model: str, price_in: float, price_out: float,
                  snap_map: dict[str, dict], checked_at: float | None) -> dict:
    """Freshness flags for ONE adopted model: `upstream_removed` (its slug is in the snapshot but no
    longer returned upstream) and `price_drift` (upstream price moved vs the adopted price). A model
    the snapshot has never covered gets neither flag — unknown is not a warning."""
    snap = snap_map.get(upstream_model)
    removed = bool(snap and checked_at is not None and snap["last_seen"] < checked_at)
    drift = None if removed else price_drift(price_in, price_out, snap)
    return {"upstream_removed": removed, "price_drift": drift}


# --- fetch dispatch (reuses the discovery fetchers; env creds, matching the discovery routes) ---


async def _fetch_provider(provider: str) -> dict:
    """{models, error} for one provider's discovery list. openrouter is keyless; fireworks/cloudflare
    need their env keys (same source the discovery routes read). A provider with no usable key yields
    an empty, non-error result (nothing to snapshot, not a failure)."""
    from .catalog_sync import fetch_cloudflare_library, fetch_fireworks_library, fetch_openrouter

    if provider == "openrouter":
        return await fetch_openrouter(os.environ.get("OPENROUTER_API_KEY"))
    if provider == "fireworks":
        key = os.environ.get("FIREWORKS_API_KEY")
        return await fetch_fireworks_library(key) if key else {"models": [], "error": None}
    if provider == "cloudflare":
        token, acct = os.environ.get("CLOUDFLARE_API_TOKEN"), os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        return await fetch_cloudflare_library(token, acct) if (token and acct) \
            else {"models": [], "error": None}
    return {"models": [], "error": None}


# Providers with a discovery source (matches the discovery routes). openrouter always runs (keyless);
# the others only produce models when their key is present.
FRESHNESS_PROVIDERS = ("openrouter", "fireworks", "cloudflare")


async def refresh_provider(app, provider: str, now: float, *, window_days: float) -> ProviderDiff:
    """Fetch one provider, diff against the stored snapshot, persist the new snapshot, and (when the
    provider's auto-adopt toggle is on) adopt the newly-seen models. Never raises — a fetch error
    degrades to the stale snapshot with the error attached (honesty §5)."""
    store = app.state.auth
    fetched = await _fetch_provider(provider)
    if fetched["error"]:
        return ProviderDiff(provider, now, [], [], [], error=fetched["error"])
    models = fetched["models"]
    if not models:  # no key / empty library — leave the snapshot untouched, not an error
        return ProviderDiff(provider, now, [], [], [], error=None)

    existing = {r["slug"]: r for r in await store.snapshot_rows(provider)}
    diff = snapshot_diff(existing, models, now)
    for m in models:  # persist the latest snapshot (first_seen preserved on conflict)
        await store.upsert_snapshot(provider, m["slug"], m.get("price_in"), m.get("price_out"), now)

    d = ProviderDiff(provider, now, diff.added, diff.removed, diff.priced, error=None)
    if diff.added and await store.get_auto_adopt(provider):
        await _auto_adopt(app, provider, [m for m in models if m["slug"] in set(diff.added)])
    return d


async def _auto_adopt(app, provider: str, new_models: list[dict]) -> None:
    """Opt-in path: adopt each newly-seen model into the single-tenant scope, marked auto in its
    provenance (created_by="auto:freshness") so Activity/audit can tell it apart from a click.
    Reuses the route's id-suggestion + tier-word guard + materialize; a model whose id would be
    invalid is skipped (logged), never crashes the tick.
    ponytail: minimal re-use of the adopt validation inline — the full route handler carries HTTP
    idempotency/audit we don't need here; the naming guard (the safety-critical bit) is reused."""
    from .catalog import LEGACY_MODEL_IDS, id_tier_words
    from .routes.admin_catalog_adoptions import _PROVIDERS, _materialize, _suggested_id
    from .routes.deps import OSS_LOCAL_ORG

    store = app.state.auth
    cfg = _PROVIDERS.get(provider)
    if cfg is None:
        return
    base_ids = {e.id for e in app.state.gateway.catalog.models}
    existing = await store.list_adoptions(OSS_LOCAL_ORG)
    taken = base_ids | {r["id"] for r in existing}
    adopted_slugs = {r["upstream_model"] for r in existing}
    for m in new_models:
        slug = m["slug"]
        if slug in adopted_slugs:
            continue
        id = _suggested_id(slug, taken, cfg["prefix"])
        if id in taken or id in LEGACY_MODEL_IDS or id_tier_words(id) \
                or not id.startswith(f"{cfg['prefix']}-"):
            log.info("auto-adopt skipped %s: id %r fails the naming guard", slug, id)
            continue
        entry = _materialize(provider, m, id)
        await store.add_adoption(OSS_LOCAL_ORG, id, entry_json=entry.model_dump_json(),
                                 upstream_model=slug, provider=provider, created_by="auto:freshness")
        taken.add(id)
        log.info("auto-adopted %s as %s (provider=%s)", slug, id, provider)


async def run_freshness(app, *, window_days: float) -> dict:
    """One refresh pass over every freshness provider. Returns {checked_at, providers: {provider:
    {checked_at, added, removed, priced, error}}} — stored on app.state for the console's "last
    checked" / degrade note; the durable per-model snapshot lives in the DB."""
    now = time.time()
    out: dict[str, dict] = {}
    for provider in FRESHNESS_PROVIDERS:
        try:
            d = await refresh_provider(app, provider, now, window_days=window_days)
        except Exception as e:  # noqa: BLE001 — one provider never sinks the pass
            log.warning("freshness refresh failed for %s: %s", provider, e)
            d = ProviderDiff(provider, now, [], [], [], error=f"{type(e).__name__}: {e}")
        out[provider] = {"checked_at": d.checked_at, "added": d.added, "removed": d.removed,
                         "priced": d.priced, "error": d.error}
    return {"checked_at": now, "providers": out}
