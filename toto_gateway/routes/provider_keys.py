"""Single-tenant provider key management — the console Settings "Provider connections" API.

The operator pastes their OpenRouter / Fireworks / Cloudflare / OpenAI / Gemini keys here instead
of managing env vars. Keys are Fernet-encrypted at rest in org_provider_keys under the operator's
single-tenant org (the OSS `local` sentinel — Identity.org_id), which the existing dispatch chain
already resolves per request: require_auth → load_byok → byok_keys → runner. So a saved key is
LIVE on the very next request, no restart; a stored key beats a stale env var, env is the
fallback. Reboot-required pieces are the boot-time seams only: the default-catalog pick and
inventory-derived availability (app.factory documents both).

Key material is NEVER returned — masked (last 4) at most. Env-var-provided keys report
source:"environment" as an informational row; they cannot be edited or deleted here (that's the
shell's job), but a stored key may be PUT over one and wins at dispatch.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..credentials import PROVIDERS, credentials_secret, encrypt, last4, pack_provider_key
from .credentials import refresh_scoped_inventory
from .deps import Identity, require_auth

router = APIRouter(tags=["admin"])


def _error(status: int, message: str, err_type: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": err_type}})


def _gate(identity: Identity) -> JSONResponse | None:
    """Operator-only, and only where the operator has a home org to store under (the OSS
    single-tenant sentinel). Non-operator callers manage their own keys at /v1/credentials."""
    if not identity.is_operator:
        return _error(403, "operator credential required", "authorization_error")
    if identity.org_id is None:
        return _error(400, "provider key storage requires the single-tenant (oss) edition",
                      "invalid_request_error")
    return None


def _row(slug: str, stored: dict | None) -> dict:
    definition = PROVIDERS[slug]
    if stored is not None:
        source = "stored"
        masked = stored["last4"] or None
    elif os.environ.get(definition.api_key_env, "").strip():
        source = "environment"
        masked = None  # informational row — env key material never flows into a response
    else:
        source = None
        masked = None
    return {
        "provider": slug, "label": definition.label, "powers": definition.powers,
        "configured": source is not None, "masked": masked, "source": source,
        "env_var": definition.api_key_env,
        # Non-None → the UI renders a second field (cloudflare's account id).
        "account_env": definition.account_env,
    }


class KeyBody(BaseModel):
    key: str = ""
    account_id: str = ""


@router.get("/v1/admin/provider-keys")
async def list_provider_keys(request: Request, identity: Identity = Depends(require_auth)):
    err = _gate(identity)
    if err is not None:
        return err
    store = request.app.state.auth
    stored = {r["provider"]: r for r in await store.list_org_provider_keys(identity.org_id)}
    return {"providers": [_row(slug, stored.get(slug)) for slug in PROVIDERS]}


@router.put("/v1/admin/provider-keys/{provider}")
async def put_provider_key(provider: str, body: KeyBody, request: Request,
                           identity: Identity = Depends(require_auth)):
    err = _gate(identity)
    if err is not None:
        return err
    if provider not in PROVIDERS:
        return _error(400, f"unknown provider {provider!r}", "invalid_request_error")
    key = body.key.strip()
    if not key:
        return _error(400, "key must be non-empty", "invalid_request_error")
    if len(key) > 1024:  # real provider keys are ~40-200 chars; bound the encrypt+store work
        return _error(400, "key too long", "invalid_request_error")
    definition = PROVIDERS[provider]
    account_id = body.account_id.strip()
    if definition.account_env and not account_id:
        return _error(400, f"{provider} needs an account_id as well", "invalid_request_error")
    if len(account_id) > 256:
        return _error(400, "account_id too long", "invalid_request_error")
    secret = credentials_secret(request.app.state.settings)
    if not secret:  # fail closed — never store plaintext
        return _error(503, "credential storage not configured", "config_error")
    store = request.app.state.auth
    await store.set_org_provider_key(
        identity.org_id, provider, encrypt(secret, pack_provider_key(provider, key, account_id)),
        last4(key))
    await refresh_scoped_inventory(request, identity, provider, org_id=identity.org_id)
    return {"provider": provider, "configured": True, "masked": last4(key) or None,
            "source": "stored"}


@router.delete("/v1/admin/provider-keys/{provider}")
async def delete_provider_key(provider: str, request: Request,
                              identity: Identity = Depends(require_auth)):
    err = _gate(identity)
    if err is not None:
        return err
    if provider not in PROVIDERS:
        return _error(400, f"unknown provider {provider!r}", "invalid_request_error")
    await request.app.state.auth.delete_org_provider_key(identity.org_id, provider)
    return _row(provider, None)  # post-delete state: back to environment or unconfigured
