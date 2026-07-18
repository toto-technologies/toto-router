"""GET /v1/admin/catalog/sync/fireworks — live drift check between the catalog and the Fireworks
account (fine-tune models + on-demand deployments). A live read, not stored history. Global (not
org-scoped), require_read_role("member") — same read bar as the catalog detail API.

Never 500s the console: a missing key or a provider hiccup returns 200 with `error` set.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, Request

from ..catalog_sync import (
    fetch_fireworks,
    fetch_fireworks_library,
    fetch_openrouter,
    probe_availability,
    reconcile,
    reconcile_fireworks_library,
    reconcile_openrouter,
)
from .deps import Identity, require_read_role, require_role

router = APIRouter(tags=["admin"])

_EMPTY = {"account_models": [], "deployments": [], "catalog_entries": [], "drift": [], "ok": []}


@router.get("/v1/admin/catalog/sync/fireworks")
async def sync_fireworks(request: Request,
                         identity: Identity = Depends(require_read_role("member"))):
    key = os.environ.get("FIREWORKS_API_KEY")
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
    key = os.environ.get("OPENROUTER_API_KEY")
    fetched = await fetch_openrouter(key)
    models = reconcile_openrouter(
        request.app.state.gateway.catalog_for(identity).models, fetched["models"])
    return {"provider": "openrouter", "key_present": bool(key), "checked_at": time.time(),
            "error": fetched["error"], "total": len(models), "models": models}


@router.get("/v1/admin/catalog/discovery/fireworks")
async def discover_fireworks(request: Request,
                             identity: Identity = Depends(require_read_role("member"))):
    """Explore the whole Fireworks model library (the `fireworks` platform account), each flagged
    cataloged/catalog_id. Needs the key (unlike OpenRouter). `filtered_out` = deprecated/embedding/
    non-READY entries hidden."""
    key = os.environ.get("FIREWORKS_API_KEY")
    base = {"provider": "fireworks", "checked_at": time.time()}
    if not key:
        return {**base, "key_present": False, "error": "FIREWORKS_API_KEY not configured",
                "total": 0, "filtered_out": 0, "models": []}
    fetched = await fetch_fireworks_library(key)
    models = reconcile_fireworks_library(
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
