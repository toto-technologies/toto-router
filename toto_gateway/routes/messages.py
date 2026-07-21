"""POST /v1/messages — Anthropic Messages surface over the OpenAI-shaped data plane.

Mirrors routes/chat.py's exception ladder with Anthropic error envelopes. The ladder is
deliberately duplicated rather than extracted: chat.py is the money path and the two
surfaces disagree on every envelope shape.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import anthropic_surface as surf
from ..catalog import UnknownModelError
from ..gateway import Gateway, GatewayDegradedError
from ..pipeline import BlockedError, ModelNotPermittedError
from ..routing.candidates import CandidateIneligibleError
from ..routing.smart import is_smart
from .deps import Identity, require_auth

log = logging.getLogger("toto_gateway.messages")
router = APIRouter()


def _err(status: int, err_type: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content=surf.anthropic_error(err_type, message))


def _frame(event: dict) -> str:
    """Anthropic named-event SSE framing: event: <type> line, then the data line."""
    return f"event: {event.get('type', 'message_delta')}\ndata: {json.dumps(event)}\n\n"


async def _sse(gateway: Gateway, req, *, request: Request, identity, declared_session):
    chunks = gateway.stream(req, harness="anthropic-sdk",
                            task_id=request.headers.get("x-toto-task-id"),
                            identity=identity, declared_session=declared_session)
    try:
        async for event in surf.stream_events(chunks, model=req.model):
            yield _frame(event)
    except (BlockedError, ModelNotPermittedError, CandidateIneligibleError) as exc:
        yield _frame(surf.anthropic_error("permission_error", str(exc)))
    except GatewayDegradedError as exc:
        yield _frame(surf.anthropic_error(
            "api_error", f"gateway routing degraded ({exc.reason}); org fails closed"))
    except Exception:
        log.exception("upstream failure mid-stream on /v1/messages (model=%s)", req.model)
        yield _frame(surf.anthropic_error("api_error", "Upstream provider error."))


@router.post("/v1/messages")
async def messages(request: Request, identity: Identity = Depends(require_auth)):
    gateway: Gateway = request.app.state.gateway
    body = await request.json()
    try:
        req = surf.to_chat_request(body)
    except Exception as exc:
        return _err(400, "invalid_request_error", f"unparseable request: {exc}")

    if (req.model or "").strip() and not is_smart(req.model):
        try:
            gateway.resolve(req.model, identity)
        except UnknownModelError as exc:
            return _err(404, "invalid_request_error", str(exc))

    declared_session = request.headers.get("x-session-id")
    if req.stream:
        return StreamingResponse(
            _sse(gateway, req, request=request, identity=identity,
                 declared_session=declared_session),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    try:
        result = await gateway.complete(
            req, harness="anthropic-sdk", task_id=request.headers.get("x-toto-task-id"),
            resilient=True,
            allow_fallback=False if request.headers.get("x-toto-no-fallback") else None,
            identity=identity, declared_session=declared_session,
        )
    except BlockedError as exc:
        return _err(403, "permission_error", str(exc))
    except ModelNotPermittedError as exc:
        return _err(403, "permission_error", str(exc))
    except CandidateIneligibleError as exc:
        return _err(403, "permission_error", str(exc))
    except GatewayDegradedError as exc:
        return _err(503, "api_error", f"gateway routing degraded ({exc.reason}); org fails closed")
    except UnknownModelError as exc:
        return _err(404, "invalid_request_error", str(exc))
    except Exception:
        log.exception("upstream failure completing /v1/messages (model=%s)", req.model)
        return _err(502, "api_error", "Upstream provider error.")

    t = result.trace
    return JSONResponse(
        content=surf.to_anthropic_response(result.response),
        headers={
            "x-toto-request-id": t.request_id, "x-toto-model": t.model,
            "x-toto-lane": t.lane or "",
            "x-toto-cost-usd": "" if t.cost_usd is None else str(t.cost_usd),
        },
    )
