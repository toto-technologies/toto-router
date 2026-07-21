"""GET /v1/admin/catalog/models — the raw catalog for the console, plus shared helpers.

Core (edition seam): user-scoped catalog reads only. The org/team catalog-policy admin routes
(team RBAC + org governance) live in admin_tenancy — enterprise-gated, absent in the OSS edition.
_team_in_scope stays HERE because core admin_catalog_adoptions uses it (a scoped team lookup is
not an org admin surface).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..catalog import CatalogEntry
from .deps import Identity, require_read_role
from .models import _provider_of

router = APIRouter()


def _error(status: int, message: str, err_type: str, code: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": err_type, "code": code}})


def is_fine_tuned(upstream_model: str) -> bool:
    """An account-owned Fireworks fine-tune: the upstream ref lives under some `accounts/<acct>/`
    namespace that isn't Fireworks' own serverless catalog (`accounts/fireworks/`)."""
    return upstream_model.startswith("accounts/") and not upstream_model.startswith("accounts/fireworks/")


def _model_row(e: CatalogEntry) -> dict:
    return {"id": e.id, "aliases": e.aliases, "lane": e.lane, "provider": _provider_of(e),
            "endpoint": e.endpoint, "base_url": e.base_url, "api_key_env": e.api_key_env,
            "residency_class": e.residency_class, "upstream_model": e.effective_upstream_model,
            "price_in": e.price_usd_per_1k.prompt, "price_out": e.price_usd_per_1k.completion,
            "context_window": e.context_window, "tools": e.tools,
            "vision": ("image" in e.modalities) if e.modalities else None,
            "fine_tuned": is_fine_tuned(e.effective_upstream_model), "source": e.source,
            "price_source": e.price_source}


@router.get("/v1/admin/catalog/models")
async def list_catalog_models(request: Request,
                              identity: Identity = Depends(require_read_role("member"))):
    """The raw catalog (global, not org-scoped) for the console — every entry with provider,
    fine-tuned flag, and source fragment. Excludes the virtual `smart` model (no fixed upstream)."""
    return {"models": [_model_row(e) for e in request.app.state.gateway.catalog.models]}  # base-catalog-ok: operator manages the SHIPPED catalog itself


async def _team_in_scope(request: Request, identity: Identity, team_id: str):
    """(team_row, error_response) — exactly one is non-None. Fail-closed: an unknown team OR a team
    in another org both return 404 (not 403), so a scoped admin can't probe another org's team ids
    (no cross-org existence leak). The operator is above org scope."""
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        return None, _error(503, "auth store unavailable", "config_error")
    team = await auth.get_team(team_id)
    if team is None or (not identity.is_operator and team["org_id"] != identity.org_id):
        return None, _error(404, "team not found", "invalid_request_error", "team_not_found")
    return team, None
