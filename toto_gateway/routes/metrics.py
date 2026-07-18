"""GET /metrics — Prometheus exposition, operator-token-gated like /statusz (sessions.py:560).

Not a public surface: the scraper carries the operator bearer (standard Prometheus scrape configs
support bearer_token). One registry (metrics.METRICS); this endpoint just serializes it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ..metrics import METRICS
from .deps import Identity, require_auth
from .sessions import _error

router = APIRouter()


@router.get("/metrics")
async def metrics(identity: Identity = Depends(require_auth)):
    if not identity.is_operator:
        return _error(403, "operator token required", "authentication_error")
    return Response(generate_latest(METRICS.registry), media_type=CONTENT_TYPE_LATEST)
