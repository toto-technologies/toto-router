"""Per-user API tokens — mint/list/revoke, the credential behind the API/MCP/CLI user surface.

A logged-in user (session cookie) mints a bearer here; that bearer then authenticates every
/v1 route as that user — same Identity, same strict scoping as the cookie (docs/api.md).
The secret is returned exactly ONCE at mint; only its sha256 digest is stored (AuthStore
auth_tokens, purpose 'api'). No expiry v1 — revocation is the lever.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .auth import _audit, _error
from .deps import Identity, require_auth

router = APIRouter(tags=["tokens"])


class MintToken(BaseModel):
    label: str = Field(min_length=1, max_length=100,
                       description="What this token is for (e.g. 'laptop CLI', 'mcp')")
    org_id: str | None = Field(default=None,
                               description="Bind this token to one of your orgs (W2-C1). Omit → "
                                           "resolves to your default (oldest) membership.")
    expires_in_days: float | None = Field(default=None, gt=0,
                                           description="Requested lifetime in days (W2-C3). Clamped "
                                                       "DOWN to the org's max-token-lifetime cap; "
                                                       "omit for no expiry (subject to the cap).")


def _user_or_error(identity: Identity):
    """API tokens belong to a user. The operator is unscoped — it has every route already and
    an operator-owned user token would be a privilege-laundering hole. Fail closed."""
    if identity.user_id is None:
        return _error(403, "user credential required — the operator token cannot own API tokens",
                      "authentication_error", "operator_cannot_own_tokens")
    return None


@router.post("/v1/tokens", status_code=201)
async def mint_token(body: MintToken, request: Request,
                     identity: Identity = Depends(require_auth)):
    """Mint an API token. The `token` field is shown ONCE — store it; we keep only a hash."""
    err = _user_or_error(identity)
    if err is not None:
        return err
    auth = request.app.state.auth
    # W2-C1: an org-bound token must be one of the caller's memberships — a foreign org_id is 403
    # (no minting a credential into an org you don't belong to).
    if body.org_id is not None and await auth.get_membership_in(identity.user_id, body.org_id) is None:
        return _error(403, "not a member of that org", "authorization_error", "not_a_member")
    raw, token_id = await auth.mint_api_token(identity.user_id, body.label.strip(),
                                              org_id=body.org_id,
                                              expires_in_days=body.expires_in_days)
    await _audit(request, "token_mint", identity.user_id)
    return {"token": raw, "token_id": token_id, "label": body.label.strip(), "org_id": body.org_id}


@router.post("/v1/tokens/{token_id}/rotate", status_code=201)
async def rotate_token(token_id: str, request: Request,
                       identity: Identity = Depends(require_auth)):
    """Rotate one of YOUR tokens: returns a NEW `token` (shown ONCE) with the same label + org
    binding; the old secret keeps working for the org's rotation-grace window, then dies. Someone
    else's token_id is a 404 (indistinguishable from absent)."""
    err = _user_or_error(identity)
    if err is not None:
        return err
    result = await request.app.state.auth.rotate_api_token(identity.user_id, token_id)
    if result is None:
        return _error(404, "no such token", "invalid_request_error", "token_not_found")
    new_raw, new_id, old_expires_at = result
    await _audit(request, "token_rotate", identity.user_id)
    return {"token": new_raw, "token_id": new_id, "old_token_id": token_id,
            "old_expires_at": old_expires_at}


@router.get("/v1/tokens")
async def list_tokens(request: Request, identity: Identity = Depends(require_auth)):
    """This user's tokens — labels + created/last_used timestamps, never the secret."""
    err = _user_or_error(identity)
    if err is not None:
        return err
    return {"tokens": await request.app.state.auth.list_api_tokens(identity.user_id)}


@router.delete("/v1/tokens/{token_id}", status_code=204)
async def revoke_token(token_id: str, request: Request,
                       identity: Identity = Depends(require_auth)):
    """Revoke one of YOUR tokens. Someone else's token_id is indistinguishable from absent."""
    err = _user_or_error(identity)
    if err is not None:
        return err
    if not await request.app.state.auth.revoke_api_token(identity.user_id, token_id):
        return _error(404, "no such token", "invalid_request_error", "token_not_found")
    await _audit(request, "token_revoke", identity.user_id)
