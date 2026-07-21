"""GET /v1/models — the catalog, OpenAI-compatible, with Toto lane/residency extensions."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..catalog import CatalogEntry
from ..schemas import Model, ModelsResponse
from .deps import _resolve_identity, require_auth

router = APIRouter()


def _provider_of(entry: CatalogEntry) -> str:
    """Clean provider label for the console. Known cloud providers first (the api_key_env /
    base_url is what actually distinguishes OpenAI-compatible hosts), then fake, then a local box.
    So or-* entries (endpoint=openai + OPENROUTER_API_KEY) read as 'openrouter', not 'openai'."""
    if entry.provider:
        return entry.provider
    key = (entry.api_key_env or "").upper()
    base = (entry.base_url or "").lower()
    ep = (entry.endpoint or "").lower()
    if "OPENROUTER" in key or "openrouter" in base:
        return "openrouter"
    if "FIREWORKS" in key or "fireworks" in base:
        return "fireworks"
    if "CLOUDFLARE" in key or "cloudflare" in base:
        return "cloudflare"
    if ep in ("anthropic", "openai"):
        return ep
    if ep == "fake":
        return "fake"
    # endpoint is a base URL (local mlx box) or otherwise in-perimeter → a local model.
    return "local"


# Virtual model (SR1): `smart` isn't a catalog upstream — the gateway classifies the request and
# routes it to a real model per the team's policy. Listed so OpenAI clients (pi) can select it;
# no lane/price/provider because it has no fixed upstream. Requires the classifier model in the
# catalog to actually classify (else it degrades to the benchmark default — see gateway.smart_enabled).
_SMART_MODEL = Model(id="smart", owned_by="toto", provider="toto", via="toto")


@router.get("/v1/models", response_model=ModelsResponse)
async def list_models(request: Request) -> ModelsResponse:
    gw = request.app.state.gateway
    # Resolve identity whenever credentials are present, independent of auth_enabled: a tokenless
    # local deploy still has cookie-authed users who can adopt, and the list is the CALLER's EFFECTIVE
    # catalog (base + their adoptions). _resolve_identity never raises → ANONYMOUS when no credential.
    # Login-required (auth_enabled) still 401s an anonymous caller, matching require_auth.
    identity = await _resolve_identity(request)
    if request.app.state.settings.auth_enabled and not identity.authenticated:
        await require_auth(request)  # raises the canonical 401
    catalog = gw.catalog_for(identity)
    dynamic_entries = gw.candidates.entries_for(catalog, identity)
    entries = (*catalog.models, *dynamic_entries)
    # W1-C3 org allowlist: in allowlist mode the list is the org's approved set only (its allow list
    # + adoptions, resolved onto identity.org_allowlist at auth) — so a caller doesn't see models the
    # resolution path would 403. None = allow_all → the full effective catalog, unchanged. The smart
    # sentinel stays: it resolves THROUGH the gate to an approved model.
    allow_ids = identity.org_allowlist
    if allow_ids is not None:
        entries = tuple(e for e in entries if e.id in allow_ids)
    data = [
        _SMART_MODEL,
        *(Model(id=e.id, owned_by=f"toto-{e.lane}", lane=e.lane, residency_class=e.residency_class,
              residency=e.residency_class,
              upstream_model=e.effective_upstream_model, provider=_provider_of(e),
              # in-perimeter entries dispatch to a local box; frontier/cloud carry a provider keyword.
              via="local" if e.residency_class == "in_perimeter" else e.endpoint,
              price_in=e.price_usd_per_1k.prompt, price_out=e.price_usd_per_1k.completion,
              context_window=e.context_window,
              identity_id=e.identity_id, offer_id=e.offer_id,
              credential_scope=e.credential_scope.kind if e.credential_scope is not None else None,
              modalities=e.modalities, supported_parameters=e.supported_parameters)
          for e in entries),
    ]
    return ModelsResponse(data=data)
