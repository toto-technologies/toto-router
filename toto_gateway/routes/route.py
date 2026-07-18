"""POST /v1/route — the driver plane.

Decompose a request into Toto tasks, route each task by its metadata, synthesize an answer.
Accepts the OpenAI ChatCompletions request shape (so the existing UI / any OpenAI client can
target it unchanged) and returns a ChatCompletionResponse whose `x_toto` carries the driver
provenance: the triage kind, the created Toto list, and each task's routing decision + economics.

The raw /v1/chat/completions passthrough is untouched — this is a separate, additive plane.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..benchmarks import OPTIMIZE
from ..schemas import ChatCompletionRequest, ChatCompletionResponse, Usage
from .deps import require_auth

router = APIRouter()


def _last_user(req: ChatCompletionRequest) -> str:
    for m in reversed(req.messages):
        if m.role == "user":
            return m.text()
    return req.messages[-1].text() if req.messages else ""


def _error(status: int, message: str, err_type: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": err_type}})


@router.post("/v1/route")
async def route(
    req: ChatCompletionRequest, request: Request, _auth: None = Depends(require_auth)
):
    driver = getattr(request.app.state, "driver", None)
    if driver is None:
        return _error(503, "driver disabled — start the gateway with TOTO_GW_DRIVER=1", "config_error")

    query = _last_user(req)
    if not query.strip():
        return _error(400, "no user message to route", "invalid_request_error")

    # User knob (OpenAI clients pass it via extra_body): quality | balanced | cost.
    optimize = getattr(req, "optimize", None)
    if optimize is not None and optimize not in OPTIMIZE:
        return _error(400, f"optimize must be one of {OPTIMIZE}", "invalid_request_error")

    try:
        result = await driver.run(query, optimize=optimize)
    except Exception as exc:  # driver/executor failure — surface, don't crash the worker
        return _error(502, f"{type(exc).__name__}: {exc}", "driver_error")

    resp = ChatCompletionResponse.simple(
        model=driver.driver_model, content=result.answer, usage=Usage()
    )
    resp.x_toto = result.provenance()
    return resp
