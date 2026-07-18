"""Per-user BYOK credential management (docs: fireworks-byok).

Logged-in users store their own OpenRouter / Fireworks API key; the runner uses it at run time
(fallback: the platform key). Keys are encrypted at rest and NEVER returned — only configured +
last4. Anonymous/operator callers have no per-user store → 401.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..benchmarking.platform import InventoryRefreshIntent, PlatformActor
from ..credentials import PROVIDERS, credentials_secret, encrypt, last4
from .deps import Identity, require_auth

log = logging.getLogger(__name__)

router = APIRouter()


async def refresh_scoped_inventory(request: Request, identity: Identity, provider: str,
                                   org_id: str | None = None) -> None:
    """Best-effort inventory refresh after a key save, so the caller's scoped models (e.g. their
    Fireworks account fine-tunes) appear without waiting for the platform cron. The saved key
    changes what a scope resolves to, so compile re-resolves and refreshes that partition.

    `org_id` set → warm the ORGANIZATION partition (an org owner saved the org-wide key); otherwise
    warm the saver's `effective` scope. Failures are logged and swallowed — storing the key must
    never depend on this."""
    platform = getattr(request.app.state, "benchmark_platform", None)
    if platform is None:
        return
    if getattr(request.app.state.settings, "fake_exec", False):
        return  # test/demo mode never reaches real provider HTTP; inventory discovery included
    try:
        # Providers without an inventory adapter fail InventoryRefreshIntent validation → no-op.
        if org_id is not None:
            intent = InventoryRefreshIntent(
                providers=(provider,), scope="organization", org_id=org_id
            )
        else:
            intent = InventoryRefreshIntent(
                providers=(provider,), scope="effective", user_id=identity.user_id
            )
        actor = PlatformActor.from_identity(identity)
        plan = await platform.compile(intent, actor)
        await platform.submit(plan, actor, idempotency_key=f"key-save-{uuid.uuid4().hex}")
    except Exception:  # noqa: BLE001 - fail-soft by contract
        log.warning("scoped inventory refresh after %s key save failed", provider, exc_info=True)


def _error(status: int, message: str, err_type: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": err_type}})


class KeyBody(BaseModel):
    key: str = ""


@router.get("/v1/credentials")
async def list_credentials(request: Request, identity: Identity = Depends(require_auth)):
    if identity.user_id is None:
        return _error(401, "login required", "authentication_error")
    store = request.app.state.auth
    configured = {r["provider"]: r["last4"] for r in await store.list_provider_keys(identity.user_id)}
    return {"credentials": [
        {"provider": slug, "label": p.label,
         "configured": slug in configured, "last4": configured.get(slug),
         "powers": p.powers}
        for slug, p in PROVIDERS.items()]}


@router.put("/v1/credentials/{provider}")
async def put_credential(provider: str, body: KeyBody, request: Request,
                         identity: Identity = Depends(require_auth)):
    if identity.user_id is None:
        return _error(401, "login required", "authentication_error")
    if provider not in PROVIDERS:
        return _error(400, f"unknown provider {provider!r}", "invalid_request_error")
    key = body.key.strip()
    if not key:
        return _error(400, "key must be non-empty", "invalid_request_error")
    if len(key) > 1024:  # real provider keys are ~40-200 chars; bound the encrypt+store work
        return _error(400, "key too long", "invalid_request_error")
    store = request.app.state.auth
    secret = credentials_secret(request.app.state.settings)
    if not secret:  # fail closed — never store plaintext
        return _error(503, "credential storage not configured", "config_error")
    l4 = last4(key)
    await store.set_provider_key(identity.user_id, provider, encrypt(secret, key), l4)
    await refresh_scoped_inventory(request, identity, provider)
    return {"provider": provider, "configured": True, "last4": l4}


@router.delete("/v1/credentials/{provider}")
async def delete_credential(provider: str, request: Request,
                            identity: Identity = Depends(require_auth)):
    if identity.user_id is None:
        return _error(401, "login required", "authentication_error")
    if provider not in PROVIDERS:
        return _error(400, f"unknown provider {provider!r}", "invalid_request_error")
    store = request.app.state.auth
    await store.delete_provider_key(identity.user_id, provider)
    return {"provider": provider, "configured": False, "last4": None}
