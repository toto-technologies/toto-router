"""Server-side catalog adoption API (catalog-adoption, Alex 2026-07-11).

One-click "add this provider-library model to my catalog", per org/user, live without redeploy —
reversing the paste-YAML ruling. The caller's adoptions merge into their EFFECTIVE catalog
(catalog.effective_catalog), so an adopted id is routable by explicit name, bindable by task-type,
and listed at GET /v1/models — for THAT scope only.

Trust boundary: the server derives EVERY fact (price, context, capabilities, upstream pin) from its
OWN provider discovery snapshot — the same `fetch_openrouter` / `fetch_fireworks_library` the
discovery endpoints use. The client sends only {source, slug, id?}; a client-sent price or entry is
never trusted. Naming honesty is enforced at write time (a stored row can't join the frozen id↔
upstream map the YAML entries are pinned by): tier-word ban, dead-legacy-id ban, provider-prefix
required (so an id can't masquerade as a different provider), and no base-catalog collision.

Gated require_role("admin"); scope_key = team_id or org_id (same fallback as routing policy), so a
personal-org owner adopts for their own traffic. DELETE is scope-pinned (404 for another scope's id).
"""

from __future__ import annotations

import math
import os
import re
from types import SimpleNamespace
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..catalog import LEGACY_MODEL_IDS, CatalogEntry, Price, effective_catalog, id_tier_words
from ..catalog_sync import fetch_cloudflare_library, fetch_fireworks_library, fetch_openrouter
from .admin_catalog import _model_row
from .admin_usage import _scope_org
from .deps import Identity, require_read_role, require_role

router = APIRouter(tags=["admin"])


# Per-provider adoption config: the id prefix, the OpenAI-compatible host, the key env, and the
# discovery fetcher. base_url/api_key_env MIRROR the console's orYaml/fwYaml exactly so an adopted
# entry dispatches through the same runner path as a hand-written fragment.
_PROVIDERS = {
    "openrouter": {
        "prefix": "or", "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY", "key_required": False,
    },
    "fireworks": {
        "prefix": "fw", "base_url": "https://api.fireworks.ai/inference/v1",
        "api_key_env": "FIREWORKS_API_KEY", "key_required": True,
    },
    "cloudflare": {
        # base_url mirrors catalog.cloudflare.yaml — the ${CLOUDFLARE_ACCOUNT_ID} template is
        # expanded by the runner (CatalogEntry.resolved_base_url), so an adopted CF entry dispatches
        # through the same path as a shipped one. The account id is a SECOND env var (not the key).
        "prefix": "cf",
        "base_url": "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        "api_key_env": "CLOUDFLARE_API_TOKEN", "key_required": True,
    },
}


def _error(status: int, message: str, err_type: str, code: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": err_type, "code": code}})


def _scope_key(identity: Identity) -> str | None:
    """The caller's adoption scope: team_id or org_id (mirrors deps._resolve_routing_policy). None
    only for the operator (no org) — the operator manages the SHIPPED catalog, not per-scope adoptions."""
    return identity.team_id or identity.org_id


def _last_seg(slug: str) -> str:
    """`anthropic/claude-3.5-sonnet` → `claude-3.5-sonnet` (matches the console's lastSeg)."""
    segs = [p for p in str(slug).split("/") if p]
    return segs[-1] if segs else "model"


def _suggested_id(slug: str, taken: set[str], prefix: str) -> str:
    """`<prefix>-<last segment>`, bumping a numeric suffix on collision — the SAME scheme the console
    computes (catalog.js suggestedId), so the default id matches what the user previewed."""
    base = f"{prefix}-{_last_seg(slug)}"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


async def _discovery_row(source: str, slug: str) -> tuple[dict | None, JSONResponse | None]:
    """(discovery_row, error) for `slug` in the provider's CURRENT snapshot. Exactly one is non-None.
    The row is the trusted fact source (price/context/caps) — never the client body."""
    cfg = _PROVIDERS[source]
    key = os.environ.get(cfg["api_key_env"])
    if cfg["key_required"] and not key:
        return None, _error(503, f"{cfg['api_key_env']} not configured", "config_error")
    if source == "fireworks":
        fetched = await fetch_fireworks_library(key)
    elif source == "cloudflare":
        account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if not account:
            return None, _error(503, "CLOUDFLARE_ACCOUNT_ID not configured", "config_error")
        fetched = await fetch_cloudflare_library(key, account)
    else:
        fetched = await fetch_openrouter(key)
    if fetched["error"]:
        return None, _error(502, fetched["error"], "provider_error")
    row = next((m for m in fetched["models"] if m["slug"] == slug), None)
    if row is None:
        return None, _error(400, f"model {slug!r} not found in the {source} library",
                            "invalid_request_error", "unknown_slug")
    return row, None


def _materialize(source: str, row: dict, id: str) -> CatalogEntry:
    """Build the CatalogEntry from a trusted discovery row. Facts are the provider's, not the
    client's; upstream_model is pinned to the real slug so the id can never lie about what it
    dispatches. Modalities carry image when the provider reports vision (else text-only)."""
    cfg = _PROVIDERS[source]
    modalities = ("text", "image") if row.get("vision") else ("text",)
    supported = ("tools",) if row.get("tools") else ()
    return CatalogEntry(
        id=id,
        lane="economy",  # a cloud provider-library model, same lane the console's *Yaml writes
        endpoint="openai",
        base_url=cfg["base_url"],
        api_key_env=cfg["api_key_env"],
        residency_class="cloud",
        # OpenRouter exposes per-token price; the Fireworks platform library does not → 0 (matches
        # fwYaml's "set the real per-token price" note). An admin can refine later via a fragment.
        price_usd_per_1k=Price(prompt=row.get("price_in", 0.0) or 0.0,
                               completion=row.get("price_out", 0.0) or 0.0),
        context_window=row.get("context_window", 0) or 0,
        upstream_model=row["slug"],  # pinned to the real slug — the id can never lie about upstream
        tools=bool(row.get("tools")),
        provider=source,
        modalities=modalities,
        supported_parameters=supported,
    )


def _view(adoption: dict) -> dict:
    """One adoption → a catalog model row (same shape as /v1/admin/catalog/models) + provenance."""
    return {**_model_row(CatalogEntry.model_validate({**adoption["entry"], "source": "adopted"})),
            "adopted": True, "created_by": adoption["created_by"],
            "updated_at": adoption["updated_at"]}


async def scope_effective_catalog(request: Request, scope_key: str | None):
    """The catalog DISPATCH resolves for callers in `scope_key` (team_id, or org_id for teamless
    callers): shipped base + that scope's server-side adoptions. THE seam for every admin surface
    that validates or lists bindable model ids — validation against anything else drifts from what
    routing actually resolves (the July 2026 adopted-models-unbindable bug). Mirrors
    deps._resolve_adoptions' scope key exactly."""
    gw = getattr(request.app.state, "gateway", None)
    base = getattr(gw, "catalog", None)  # base-catalog-ok: the base half of the effective merge
    auth = getattr(request.app.state, "auth", None)
    if base is None or auth is None or not scope_key:
        return base
    rows = await auth.list_adoptions(scope_key)
    if not rows:
        return base
    adopter = SimpleNamespace(catalog_adoptions=tuple(row["entry"] for row in rows))
    return effective_catalog(base, adopter)


@router.get("/v1/admin/catalog/effective-models")
async def effective_models(request: Request, org_id: str | None = Query(None),
                           team_id: str | None = Query(None),
                           identity: Identity = Depends(require_read_role("admin"))):
    """The EFFECTIVE catalog for the scope being EDITED — shipped base + that scope's adoptions —
    exactly what dispatch resolves for the scope's callers, and therefore the one legitimate source
    for any console picker that writes model ids (routing task-type bindings, governance approvals,
    Settings-style default pickers). /v1/models can't serve this: it's pinned to the CALLER's own
    identity (empty adoptions under the operator credential, blind to the console's org/team
    switcher). Rows are /v1/admin/catalog/models-shaped (adopted rows carry source='adopted').

    Scope resolution mirrors the routing-policy endpoints: team_id (must be in the caller's org
    unless operator) wins over org_id; no params → the caller's own org sentinel."""
    from .admin_catalog import _team_in_scope

    if team_id:
        team, err = await _team_in_scope(request, identity, team_id)
        if err is not None:
            return err
        scope = team_id
    else:
        scope, err = _scope_org(identity, org_id)
        if err is not None:
            return err
    catalog = await scope_effective_catalog(request, scope)
    if catalog is None:
        return _error(503, "catalog unavailable", "config_error")
    return {"scope_key": scope,
            "models": [_model_row(e) for e in catalog.models]}


@router.get("/v1/admin/catalog/adoptions")
async def list_adoptions(request: Request,
                         identity: Identity = Depends(require_read_role("admin"))):
    """The caller scope's adoptions, each a flat catalog model row + provenance (console reads
    `adoptions[i].id`). `source` reads 'adopted'."""
    scope = _scope_key(identity)
    if scope is None:
        return {"adoptions": []}
    return {"adoptions": [_view(r) for r in await request.app.state.auth.list_adoptions(scope)]}


@router.post("/v1/admin/catalog/adoptions")
async def adopt(body: dict, request: Request,
                identity: Identity = Depends(require_role("admin"))):
    """Adopt a provider-library model into the caller scope's catalog. Body {source, slug, id?}.
    Returns 201 with the materialized row; re-adopting the SAME slug under the SAME id is idempotent
    (200). All facts derived server-side from the provider snapshot."""
    scope = _scope_key(identity)
    if scope is None:
        return _error(400, "operator has no adoption scope — adopt as an org admin",
                      "invalid_request_error", "no_scope")
    source = body.get("source")
    slug = (body.get("slug") or "").strip()
    if source not in _PROVIDERS:
        return _error(400, f"source must be one of {tuple(_PROVIDERS)}", "invalid_request_error",
                      "invalid_source")
    if not slug:
        return _error(400, "slug is required", "invalid_request_error", "missing_slug")

    row, err = await _discovery_row(source, slug)
    if err is not None:
        return err

    gw = request.app.state.gateway
    base_ids = {e.id for e in gw.catalog.models}
    existing = await request.app.state.auth.list_adoptions(scope)
    existing_ids = {r["id"] for r in existing}
    cfg = _PROVIDERS[source]
    requested_id = (body.get("id") or "").strip()

    # Idempotency by slug: this scope already adopted this exact upstream → 200 no-op (a double-click
    # or retry), UNLESS the caller forces a different custom id (which then falls through to collide-
    # or-create). Checked BEFORE id computation so the auto-suggestion doesn't bump to a fresh id and
    # silently duplicate the same model.
    same_slug = next((r for r in existing
                      if r["upstream_model"] == slug and r["provider"] == source), None)
    if same_slug is not None and (not requested_id or requested_id == same_slug["id"]):
        return {"entry": _view(same_slug)}  # 200 idempotent; console reads r.entry.id

    # Default id = the console's suggestion; a custom id is validated below.
    id = requested_id or _suggested_id(slug, base_ids | existing_ids, cfg["prefix"])
    prior = next((r for r in existing if r["id"] == id), None)

    # Fail-closed naming validation (a DB row can't join the frozen id↔upstream YAML map).
    if id in base_ids:
        return _error(400, f"id {id!r} collides with a base catalog model", "invalid_request_error",
                      "id_collision")
    if prior is not None:  # same id, DIFFERENT slug — refuse (would silently repoint)
        return _error(400, f"id {id!r} is already adopted to a different model",
                      "invalid_request_error", "id_collision")
    if id in LEGACY_MODEL_IDS:
        return _error(400, f"id {id!r} is a retired tier-word id", "invalid_request_error",
                      "legacy_id")
    tier = id_tier_words(id)
    if tier:
        return _error(400, f"id {id!r} uses banned tier word(s) {sorted(tier)} — a catalog id names "
                      "a real model, not a tier", "invalid_request_error", "tier_word_id")
    if not id.startswith(f"{cfg['prefix']}-"):  # honesty: the prefix must match the real provider
        return _error(400, f"id must start with {cfg['prefix']!r}- so it can't masquerade as another "
                      "provider's model", "invalid_request_error", "prefix_mismatch")

    entry = _materialize(source, row, id)
    stored = await request.app.state.auth.add_adoption(
        scope, id, entry_json=entry.model_dump_json(), upstream_model=slug, provider=source,
        created_by=identity.user_id)

    try:  # best-effort audit under the reserved admin:* namespace, like neighboring admin routes
        await request.app.state.auth.write_audit(
            "admin:catalog_adoption", user_id=identity.user_id, org_id=identity.org_id,
            target_type="catalog_model", target_id=id,
            metadata=f'{{"source":"{source}","slug":"{slug}"}}')
    except Exception:
        pass

    return JSONResponse(status_code=201, content={"entry": _view(stored)})  # console reads r.entry.id


@router.post("/v1/admin/catalog/local-models")
async def add_local_model(body: dict, request: Request,
                          identity: Identity = Depends(require_role("admin"))):
    """Register a locally running OpenAI-compatible server (Ollama, LM Studio, vLLM, mlx_lm) as a
    routing destination. Body {name?, base_url, model}. Unlike provider adoptions there is no
    discovery snapshot to derive facts from — the URL and model name ARE the user's facts (the
    caller is an admin who could equally edit catalog YAML). Persists as an adoption row
    (provider='local'), so it rides every existing seam: effective catalog, task-type binding,
    /v1/models, and the same DELETE. The entry uses the bare-URL local runner (lane=economy +
    endpoint=<url>): no API key, upstream_model sent as the server knows it."""
    scope = _scope_key(identity)
    if scope is None:
        return _error(400, "operator has no adoption scope — add as an org admin",
                      "invalid_request_error", "no_scope")
    base_url = (body.get("base_url") or "").strip().rstrip("/")
    model = (body.get("model") or "").strip()
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return _error(400, "base_url must be an http(s) URL, e.g. http://localhost:11434/v1",
                      "invalid_request_error", "invalid_base_url")
    if not model:
        return _error(400, "model is required — the model name as your server knows it "
                      "(e.g. llama3.1, qwen2.5-coder)", "invalid_request_error", "missing_model")

    gw = request.app.state.gateway
    base_ids = {e.id for e in gw.catalog.models}
    existing = await request.app.state.auth.list_adoptions(scope)
    # id from the display name (or model name): local-<slug>, bumped on collision — same
    # suggestion scheme as provider adoptions, same fail-closed naming checks.
    slug = re.sub(r"[^a-z0-9.]+", "-", (body.get("name") or _last_seg(model)).lower()).strip("-")
    if not slug:
        return _error(400, "name/model must contain some letters or digits",
                      "invalid_request_error", "invalid_name")
    id = _suggested_id(slug, base_ids | {r["id"] for r in existing}, "local")
    tier = id_tier_words(id)
    if tier:
        return _error(400, f"name uses banned tier word(s) {sorted(tier)} — name the real model, "
                      "not a tier", "invalid_request_error", "tier_word_id")

    entry = CatalogEntry(
        id=id, lane="economy", endpoint=base_url, residency_class="in_perimeter",
        upstream_model=model, provider="local",
        price_usd_per_1k=Price(prompt=0.0, completion=0.0),  # local marginal cost ~ electricity
        context_window=int(body.get("context_window") or 0) or 8192,
    )
    stored = await request.app.state.auth.add_adoption(
        scope, id, entry_json=entry.model_dump_json(), upstream_model=model, provider="local",
        created_by=identity.user_id)
    try:
        await request.app.state.auth.write_audit(
            "admin:catalog_adoption", user_id=identity.user_id, org_id=identity.org_id,
            target_type="catalog_model", target_id=id, metadata='{"source":"local"}')
    except Exception:
        pass
    return JSONResponse(status_code=201, content={"entry": _view(stored)})


# --- manual price overrides (catalog-pricing-plane, Alex 2026-07-14) --------------------------
# For entries whose provider publishes no machine-readable price (Groq, direct labs) or whose
# YAML fact rotted. The console speaks per-Mtok (how providers publish); storage and the Price
# model are per-1k — the exact ÷1000 happens HERE and nowhere else. Applied to the caller's
# effective catalog by catalog.effective_catalog via identity.price_overrides (deps), so
# compute_cost_usd and routing inherit an override with zero downstream changes.

_PLATFORM_SCOPE = "platform"  # sentinel scope_key: operator-set, applies to every caller


def _override_scope(identity: Identity) -> str:
    """Team/org admins override for their scope; the operator (no org) sets the PLATFORM rows —
    the one place adoptions and overrides deliberately differ (adoptions refuse the operator)."""
    return _scope_key(identity) or _PLATFORM_SCOPE


def _override_view(row: dict) -> dict:
    """Stored per-1k row → console row carrying BOTH scales (per-Mtok is what humans compare
    against provider pricing pages; per-1k is what dispatch actually uses)."""
    return {"model_id": row["model_id"], "scope_key": row["scope_key"],
            "prompt_usd_per_mtok": row["prompt_usd_per_1k"] * 1000,
            "completion_usd_per_mtok": row["completion_usd_per_1k"] * 1000,
            "prompt_usd_per_1k": row["prompt_usd_per_1k"],
            "completion_usd_per_1k": row["completion_usd_per_1k"],
            "cache_read_multiplier": row["cache_read_multiplier"],
            "updated_by": row["updated_by"], "updated_at": row["updated_at"]}


@router.get("/v1/admin/catalog/price-overrides")
async def list_price_overrides(request: Request,
                               identity: Identity = Depends(require_read_role("admin"))):
    """Platform rows + the caller scope's rows (each tagged with its scope_key so the console can
    show which layer a price comes from; team>org>platform is applied at dispatch, not here)."""
    scope = _override_scope(identity)
    rows = await request.app.state.auth.list_price_overrides(_PLATFORM_SCOPE, scope)
    return {"overrides": [_override_view(r) for r in rows]}


@router.put("/v1/admin/catalog/price-overrides/{model_id}")
async def set_price_override(model_id: str, body: dict, request: Request,
                             identity: Identity = Depends(require_role("admin"))):
    """Upsert a manual price for `model_id` in the caller's scope (operator → platform scope).
    Body {prompt_usd_per_mtok, completion_usd_per_mtok, cache_read_multiplier?, free?}. Both
    prices required, finite, ≥ 0; BOTH zero is refused unless free:true — a genuinely free local
    model is legitimate, a typo'd 0 on a paid model is a silent money bug, so the intent must be
    explicit. An id absent from the caller's effective catalog is stored but inert (known:false
    in the response; the console warns)."""
    raw_p, raw_c = body.get("prompt_usd_per_mtok"), body.get("completion_usd_per_mtok")
    if not isinstance(raw_p, (int, float)) or not isinstance(raw_c, (int, float)) \
            or isinstance(raw_p, bool) or isinstance(raw_c, bool) \
            or not math.isfinite(raw_p) or not math.isfinite(raw_c) or raw_p < 0 or raw_c < 0:
        return _error(400, "prompt_usd_per_mtok and completion_usd_per_mtok must be finite "
                      "non-negative numbers", "invalid_request_error", "invalid_price")
    if raw_p == 0 and raw_c == 0 and body.get("free") is not True:
        return _error(400, "both prices are zero — set free:true if this model is genuinely "
                      "free, otherwise supply the real per-Mtok prices",
                      "invalid_request_error", "zero_price_unconfirmed")
    mult = body.get("cache_read_multiplier")
    if mult is not None and (not isinstance(mult, (int, float)) or isinstance(mult, bool)
                             or not math.isfinite(mult) or mult < 0):
        return _error(400, "cache_read_multiplier must be a finite non-negative number",
                      "invalid_request_error", "invalid_multiplier")

    scope = _override_scope(identity)
    stored = await request.app.state.auth.set_price_override(
        scope, model_id,
        prompt_usd_per_1k=raw_p / 1000, completion_usd_per_1k=raw_c / 1000,
        cache_read_multiplier=None if mult is None else float(mult),
        updated_by=identity.user_id)

    catalog = await scope_effective_catalog(request, _scope_key(identity))
    known = catalog is not None and catalog.get(model_id) is not None

    try:  # best-effort audit, same discipline as the adoption writes
        await request.app.state.auth.write_audit(
            "admin:price_override", user_id=identity.user_id, org_id=identity.org_id,
            target_type="catalog_model", target_id=model_id,
            metadata=f'{{"scope":"{scope}","prompt_usd_per_mtok":{raw_p},'
                     f'"completion_usd_per_mtok":{raw_c}}}')
    except Exception:
        pass
    return {"override": _override_view(stored), "known": known}


@router.delete("/v1/admin/catalog/price-overrides/{model_id}")
async def remove_price_override(model_id: str, request: Request,
                                identity: Identity = Depends(require_role("admin"))):
    """Remove the caller scope's override (operator → platform row). 404 when this scope has no
    override for the id — scope-pinned like unadopt, so scopes can't probe each other."""
    scope = _override_scope(identity)
    removed = await request.app.state.auth.remove_price_override(scope, model_id)
    if not removed:
        return _error(404, "price override not found", "invalid_request_error", "not_found")
    try:
        await request.app.state.auth.write_audit(
            "admin:price_override_removed", user_id=identity.user_id, org_id=identity.org_id,
            target_type="catalog_model", target_id=model_id)
    except Exception:
        pass
    return {"deleted": model_id}


@router.delete("/v1/admin/catalog/adoptions/{id}")
async def unadopt(id: str, request: Request,
                  identity: Identity = Depends(require_role("admin"))):
    """Remove an adoption from the caller scope. 404 when the id isn't adopted in THIS scope (so a
    caller can't probe another scope's adoptions)."""
    scope = _scope_key(identity)
    removed = scope is not None and await request.app.state.auth.remove_adoption(scope, id)
    if not removed:
        return _error(404, "adoption not found", "invalid_request_error", "not_found")
    try:
        await request.app.state.auth.write_audit(
            "admin:catalog_unadoption", user_id=identity.user_id, org_id=identity.org_id,
            target_type="catalog_model", target_id=id)
    except Exception:
        pass
    return {"deleted": id}
