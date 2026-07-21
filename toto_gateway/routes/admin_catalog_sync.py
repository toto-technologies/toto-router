"""GET /v1/admin/catalog/sync/fireworks — live drift check between the catalog and the Fireworks
account (fine-tune models + on-demand deployments). A live read, not stored history. Global (not
org-scoped), require_read_role("member") — same read bar as the catalog detail API.

Never 500s the console: a missing key or a provider hiccup returns 200 with `error` set.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..catalog_sync import (
    fetch_anthropic_library,
    fetch_cloudflare_library,
    fetch_fireworks,
    fetch_fireworks_library,
    fetch_openrouter,
    probe_availability,
    reconcile,
    reconcile_anthropic_library,
    reconcile_cloudflare_library,
    reconcile_fireworks_library,
    reconcile_openrouter,
)
from ..credentials import stored_or_env
from .deps import Identity, require_read_role, require_role

router = APIRouter(tags=["admin"])

_EMPTY = {"account_models": [], "deployments": [], "catalog_entries": [], "drift": [], "ok": []}


async def _enrich_freshness(request: Request, provider: str, models: list[dict]) -> dict:
    """Tag each discovered model with first_seen + is_new from the stored snapshot (mutates in
    place) and return the module-level freshness facts for the source tab: new_count, the snapshot's
    last-checked time, the last refresh error (honest degrade note), and the auto-adopt toggle. A
    model absent from the snapshot (seen live before the first tick recorded it) gets first_seen
    null / is_new false until the next refresh."""
    from ..freshness import is_new

    store = getattr(request.app.state, "auth", None)
    window = request.app.state.settings.catalog_freshness_new_window_days
    now = time.time()
    snap = {r["slug"]: r for r in await store.snapshot_rows(provider)} if store is not None else {}
    checked_at = max((r["last_seen"] for r in snap.values()), default=None)
    new_count = 0
    for m in models:
        first = (snap.get(m["slug"]) or {}).get("first_seen")
        m["first_seen"] = first
        m["is_new"] = is_new(first, now, window) and not m.get("cataloged")
        if m["is_new"]:
            new_count += 1
    fresh = getattr(request.app.state, "catalog_freshness", None) or {}
    perr = (fresh.get("providers", {}).get(provider) or {}).get("error")
    auto = await store.get_auto_adopt(provider) if store is not None else False
    return {"new_count": new_count, "snapshot_checked_at": checked_at,
            "refresh_error": perr, "auto_adopt": auto}


@router.get("/v1/admin/catalog/sync/fireworks")
async def sync_fireworks(request: Request,
                         identity: Identity = Depends(require_read_role("member"))):
    key = stored_or_env("FIREWORKS_API_KEY")
    base = {"provider": "fireworks", "account": None, "checked_at": time.time()}
    if not key:
        return {**base, "key_present": False, "error": "FIREWORKS_API_KEY not configured", **_EMPTY}

    fetched = await fetch_fireworks(key)
    # Effective catalog (catalog-adoption): a model this caller adopted reconciles as `cataloged`.
    entries = request.app.state.gateway.catalog_for(identity).models
    diff = reconcile(entries, fetched["account"], fetched["account_models"], fetched["deployments"])
    return {**base, "key_present": True, "account": fetched["account"], "error": fetched["error"],
            "account_models": fetched["account_models"], "deployments": fetched["deployments"],
            **diff}


@router.get("/v1/admin/catalog/discovery/openrouter")
async def discover_openrouter(request: Request,
                              identity: Identity = Depends(require_read_role("member"))):
    """Explore ALL OpenRouter models (not just the cataloged few), each flagged cataloged/catalog_id.
    Public endpoint — a missing key is not an error (the key is sent when present, for rate limits)."""
    key = stored_or_env("OPENROUTER_API_KEY")
    fetched = await fetch_openrouter(key)
    models = reconcile_openrouter(
        request.app.state.gateway.catalog_for(identity).models, fetched["models"])
    fresh = await _enrich_freshness(request, "openrouter", models)
    return {"provider": "openrouter", "key_present": bool(key), "checked_at": time.time(),
            "error": fetched["error"], "total": len(models), "models": models, **fresh}


@router.get("/v1/admin/catalog/discovery/fireworks")
async def discover_fireworks(request: Request,
                             identity: Identity = Depends(require_read_role("member"))):
    """Explore the whole Fireworks model library (the `fireworks` platform account), each flagged
    cataloged/catalog_id. Needs the key (unlike OpenRouter). `filtered_out` = deprecated/embedding/
    non-READY entries hidden."""
    key = stored_or_env("FIREWORKS_API_KEY")
    base = {"provider": "fireworks", "checked_at": time.time()}
    if not key:
        return {**base, "key_present": False, "error": "FIREWORKS_API_KEY not configured",
                "total": 0, "filtered_out": 0, "models": []}
    fetched = await fetch_fireworks_library(key)
    models = reconcile_fireworks_library(
        request.app.state.gateway.catalog_for(identity).models, fetched["models"])
    fresh = await _enrich_freshness(request, "fireworks", models)
    return {**base, "key_present": True, "error": fetched["error"], "total": len(models),
            "filtered_out": fetched["filtered_out"], "models": models, **fresh}


@router.get("/v1/admin/catalog/discovery/cloudflare")
async def discover_cloudflare(request: Request,
                              identity: Identity = Depends(require_read_role("member"))):
    """Explore the Cloudflare Workers AI text-generation catalog, each flagged cataloged/catalog_id.
    Needs BOTH CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID (the two-part credential); either
    missing → key_present:false, graceful empty (same idiom as Fireworks' no-key state)."""
    token = stored_or_env("CLOUDFLARE_API_TOKEN")
    account = stored_or_env("CLOUDFLARE_ACCOUNT_ID")
    base = {"provider": "cloudflare", "checked_at": time.time()}
    if not token or not account:
        return {**base, "key_present": False,
                "error": "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID not configured",
                "total": 0, "filtered_out": 0, "models": []}
    fetched = await fetch_cloudflare_library(token, account)
    models = reconcile_cloudflare_library(
        request.app.state.gateway.catalog_for(identity).models, fetched["models"])
    fresh = await _enrich_freshness(request, "cloudflare", models)
    return {**base, "key_present": True, "error": fetched["error"], "total": len(models),
            "filtered_out": fetched["filtered_out"], "models": models, **fresh}


@router.put("/v1/admin/catalog/freshness/auto-adopt/{provider}")
async def set_auto_adopt(provider: str, body: dict, request: Request,
                         identity: Identity = Depends(require_role("admin"))):
    """Toggle per-provider auto-adopt (opt-in, default off). When on, the daily refresh adopts newly
    discovered <provider> models into the single-tenant catalog with an auto provenance."""
    from ..freshness import FRESHNESS_PROVIDERS

    if provider not in FRESHNESS_PROVIDERS:
        return JSONResponse(status_code=400, content={"error": {
            "message": f"provider must be one of {FRESHNESS_PROVIDERS}", "type": "invalid_request_error"}})
    enabled = bool(body.get("enabled"))
    await request.app.state.auth.set_auto_adopt(provider, enabled)
    return {"provider": provider, "auto_adopt": enabled}


@router.post("/v1/admin/catalog/freshness/refresh")
async def refresh_freshness_now(request: Request,
                                identity: Identity = Depends(require_role("admin"))):
    """"Check now": run a freshness pass immediately (same code path as the scheduled tick), store
    the result on app.state, and return the per-provider diff. Admin — it fires outbound requests
    and writes app.state, like the availability probe's POST."""
    from ..freshness import run_freshness

    window = request.app.state.settings.catalog_freshness_new_window_days
    request.app.state.catalog_freshness = await run_freshness(request.app, window_days=window)
    return request.app.state.catalog_freshness


@router.get("/v1/admin/catalog/discovery/anthropic")
async def discover_anthropic(request: Request,
                             identity: Identity = Depends(require_read_role("member"))):
    """Explore Anthropic's model list (native x-api-key auth — the generic Bearer availability
    probe can't cover this provider), each flagged cataloged/catalog_id. Needs the key; missing →
    key_present:false, graceful empty (same idiom as Fireworks' no-key state)."""
    key = stored_or_env("ANTHROPIC_API_KEY")
    base = {"provider": "anthropic", "checked_at": time.time()}
    if not key:
        return {**base, "key_present": False, "error": "ANTHROPIC_API_KEY not configured",
                "total": 0, "filtered_out": 0, "models": []}
    fetched = await fetch_anthropic_library(key)
    models = reconcile_anthropic_library(
        request.app.state.gateway.catalog_for(identity).models, fetched["models"])
    return {**base, "key_present": True, "error": fetched["error"], "total": len(models),
            "filtered_out": fetched["filtered_out"], "models": models}


_NO_PROBE = {"checked_at": None, "providers": {}}


@router.get("/v1/admin/catalog/availability")
async def catalog_availability(request: Request,
                               identity: Identity = Depends(require_read_role("member"))):
    """Latest cyclical availability probe: per provider base_url, `vanished` (declared ids gone
    upstream — broken rows) + `undeclared` (live ids we don't declare) + fetch `error`. The probe
    runs on the scheduled-inventory cadence; this returns the in-memory result of the last run
    (empty until the first tick — POST this same path to force a fresh probe)."""
    return getattr(request.app.state, "catalog_availability", None) or _NO_PROBE


@router.post("/v1/admin/catalog/availability")
async def probe_catalog_availability(request: Request,
                                     identity: Identity = Depends(require_role("admin"))):
    """"Check now": probe every keyed provider's /models immediately, store + return the result.
    Static (hand-maintained) catalog rows only — the base catalog, same as the scheduled probe.
    Mutating (fires outbound requests + writes app.state) → admin, not the read bar; the auditor
    conformance walk (test_auditor_role) enforces this."""
    result = await probe_availability(request.app.state.gateway.catalog.models)  # base-catalog-ok: the probe audits the SHIPPED hand-maintained rows; adopted entries are discovery-derived already, and probing per-scope would leak scope contents into shared app.state
    request.app.state.catalog_availability = result
    return result
