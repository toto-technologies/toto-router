"""POST /v1/chat/completions — the data plane (streaming + non-streaming)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..catalog import UnknownModelError
from ..gateway import GatewayDegradedError, Gateway, StreamStallError
from ..pipeline import BlockedError, DataPolicyDeniedError, ModelNotPermittedError

from ..gateway import BudgetExceededError, GatewayDegradedError, Gateway, StreamStallError
from ..pipeline import BlockedError, ModelNotPermittedError
from ..routing.smart import is_smart
from ..routing.candidates import CandidateIneligibleError
from ..schemas import ChatCompletionRequest
from .deps import Identity, require_auth

log = logging.getLogger("toto_gateway.chat")
router = APIRouter()


def _error(status: int, message: str, err_type: str, code: str | None = None,
           headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": code}},
        headers=headers,
    )


# W1-C3 org allowlist denial: the exact body the design mandates — invalid_request_error +
# model_not_permitted + an ask-your-admin message (distinct from the C2 per-team deny, which keeps
# its policy_violation shape). Kept as one string so the route and the SSE path agree.
_ALLOWLIST_MESSAGE = ("This model is not in your organization's approved list. Ask your admin to "
                      "add it to the catalog allowlist.")


def _model_not_permitted_body(exc: ModelNotPermittedError) -> dict:
    if exc.allowlist:
        return {"error": {"type": "invalid_request_error", "code": "model_not_permitted",
                          "message": _ALLOWLIST_MESSAGE}}
    return {"error": {"type": "policy_violation", "code": "model_not_permitted",
                      "message": str(exc)}}


async def _audit_model_denied(request: Request, identity: Identity, model: str) -> None:
    """Best-effort C3 denial audit — one append-only row (actor, org, model asked for) per allowlist
    denial, so the Governance panel's audit feed + denied-count include it. Never raises (audit.record
    swallows failures); only emitted for the org allowlist gate, not the C2 per-team deny."""
    from .. import audit

    xff = request.headers.get("x-forwarded-for", "")
    await audit.record(getattr(request.app.state, "auth", None), "catalog.model_denied",
                       actor_user_id=identity.user_id, org_id=identity.org_id,
                       target_type="model", target_id=model, meta={"reason": "allowlist"},
                       ip=(xff.split(",")[0].strip() or None),
                       request_id=request.headers.get("x-request-id"))


def _data_policy_denied_body() -> dict:
    """W2-C7 data-policy denial: the exact body the design mandates. The message is deliberately
    generic (no classification leaked to the caller) — the trace carries the data_label for audit."""
    return {"error": {"type": "invalid_request_error", "code": "data_policy_denied",
                      "message": "This request was blocked by your organization's data-handling "
                                 "policy."}}

def _budget_body(decision) -> dict:
    """The W2-C5 over-budget 402 payload (action=reject). Machine-readable: type=budget_exceeded
    plus the scope, spend, budget, and pct so a client can render "you're over your monthly cap"."""
    return {"error": {"type": "budget_exceeded", "code": "budget_exceeded",
                      "scope": decision.scope, "spend_usd": round(decision.spend, 4),
                      "budget_usd": decision.monthly_usd, "pct": round(decision.pct, 4),
                      "message": (f"This {decision.scope}'s monthly budget of "
                                  f"${decision.monthly_usd:.2f} is exhausted "
                                  f"(${decision.spend:.2f} spent) and its over-budget policy is "
                                  "'reject'; the request was refused.")}}


def _degraded_body(reason: str) -> dict:
    """The W1-C1 fail-closed error payload. Distinct shape (carries `reason`) so a client can tell a
    gateway degradation (routing intelligence unavailable + org fails closed) apart from an upstream
    5xx and surface the specific reason (classify_failed | policy_error | breaker_open)."""
    return {"error": {"type": "gateway_degraded", "reason": reason,
                      "message": "Gateway routing intelligence degraded and this org's fail policy "
                                 "is closed; the request was rejected rather than served by a "
                                 "fallback. Reason: " + reason + "."}}


def _retry_after_header(exc: Exception) -> str | None:
    """The upstream's raw Retry-After header off a status error, to pass through unchanged."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get("retry-after")
    except Exception:
        return None


def _upstream_error(exc: Exception, model: str) -> JSONResponse:
    """Map a terminal upstream failure to the client. A 429/503 keeps its status AND its
    Retry-After (competing with OpenRouter/Fireworks means honest backpressure, not a blanket
    502); a tripped circuit breaker is a fast 503; everything else stays a generic 502 with
    detail server-side only."""
    from ..breaker import CircuitOpen

    if isinstance(exc, CircuitOpen):  # provider breaker OPEN — fast 503, no wire was touched.
        return _error(503, "Upstream provider temporarily unavailable (circuit open).",
                      "service_unavailable", "circuit_open")
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status in (429, 503):
        ra = _retry_after_header(exc)
        hdrs = {"Retry-After": ra} if ra else None
        typ = "rate_limit_error" if status == 429 else "service_unavailable"
        return _error(status, "Upstream rate limited." if status == 429 else "Upstream unavailable.",
                      typ, None, headers=hdrs)
    log.exception("upstream failure completing chat request (model=%s)", model)
    return _error(502, "Upstream provider error.", "upstream_error", "bad_gateway")


def _classified_as(route_reason: str | None) -> str | None:
    """The label a smart request classified as, recovered from route_reason. Only smart routing
    stamps a "label:<l>…" reason on the passthrough plane, so this is None otherwise."""
    if route_reason and route_reason.startswith("label:"):
        return route_reason.split(":")[1]
    return None


def _declared_session(request: Request, req: ChatCompletionRequest) -> str | None:
    """A client-declared session identity (S3), precedence: x-session-id header > body session_id >
    body prompt_cache_key. Names the conversation anchor so all its turns stay on one model with a
    long hold. Body session_id/prompt_cache_key are only READ here — they stay in the body so the
    runner's cache-affinity hints still send the client's value upstream (client wins there)."""
    return (
        request.headers.get("x-session-id")
        or getattr(req, "session_id", None)
        or getattr(req, "prompt_cache_key", None)
    )


def _harness(request: Request) -> str | None:
    h = request.headers.get("x-toto-harness")
    if h:
        return h
    ua = request.headers.get("user-agent", "").lower()
    for name in ("pi", "opencode"):
        if name in ua:
            return name
    return None


async def _sse(gateway: Gateway, req: ChatCompletionRequest, *, request: Request, harness, task_id,
               identity=None, declared_session=None):
    try:
        async for chunk in gateway.stream(req, harness=harness, task_id=task_id, identity=identity,
                                          declared_session=declared_session):
            yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
    except BlockedError as exc:
        # SSE can't change the HTTP status after it starts; surface the policy block as an
        # error event (the gateway has already written the 'blocked' trace).
        err = {"error": {"message": str(exc), "type": "policy_violation", "code": "mnpi_blocked"}}
        yield f"data: {json.dumps(err)}\n\n"
    except ModelNotPermittedError as exc:
        if exc.allowlist and identity is not None:  # C3 org deny-by-default: audit the denial
            await _audit_model_denied(request, identity, exc.model_id)
        yield f"data: {json.dumps(_model_not_permitted_body(exc))}\n\n"
    except CandidateIneligibleError as exc:
        err = {"error": {"message": str(exc), "type": "policy_violation",
                         "code": "model_ineligible"}}
        yield f"data: {json.dumps(err)}\n\n"
    except DataPolicyDeniedError:  # W2-C7: org data-classification 'deny' — surfaced like a block
        yield f"data: {json.dumps(_data_policy_denied_body())}\n\n"
    except GatewayDegradedError as exc:
        # W1-C1 fail-closed: routing degraded and the org rejects on degradation. SSE can't set a
        # 503 after the stream opens (it hasn't emitted a chunk yet — the check runs before the
        # first token), so surface it as the same error event the gateway already traced.
        yield f"data: {json.dumps(_degraded_body(exc.reason))}\n\n"
    except BudgetExceededError as exc:
        # W2-C5 over-budget reject: SSE can't set a 402 after the stream opens (the check runs before
        # the first token), so surface the same error event the gateway already traced.
        yield f"data: {json.dumps(_budget_body(exc.decision))}\n\n"
    except StreamStallError:
        # Upstream went silent past the stall deadline. Tell the client honestly — a bare EOF
        # (the old behavior for ANY mid-stream failure) reads as a hang to an agentic harness.
        err = {"error": {"message": "Upstream stream stalled; aborted.",
                         "type": "upstream_error", "code": "stream_stall"}}
        yield f"data: {json.dumps(err)}\n\n"
    except Exception:
        # SSE termination contract: EVERY stream ends with an error event and/or [DONE] — never
        # a silent connection drop. Detail stays server-side (same policy as _upstream_error).
        log.exception("upstream failure mid-stream (model=%s)", req.model)
        err = {"error": {"message": "Upstream provider error.",
                         "type": "upstream_error", "code": "stream_error"}}
        yield f"data: {json.dumps(err)}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest, request: Request,
    identity: Identity = Depends(require_auth),
):
    gateway: Gateway = request.app.state.gateway
    harness = _harness(request)
    task_id = request.headers.get("x-toto-task-id")
    declared_session = _declared_session(request, req)

    # Resolve early so an unknown model is a clean 404 before we open a stream. Skipped when the
    # caller omits `model` (empty) — a team catalog policy may substitute a default_model in _plan,
    # so we let the gateway resolve it there — AND for the `smart` sentinel, which is not a catalog
    # entry: the gateway classifies + resolves it to a real model (SR1), so it must NOT 404 here.
    if (req.model or "").strip() and not is_smart(req.model):
        try:
            gateway.resolve(req.model, identity)
        except UnknownModelError as exc:
            return _error(404, str(exc), "invalid_request_error", "model_not_found")

    if req.stream:
        return StreamingResponse(
            _sse(gateway, req, request=request, harness=harness, task_id=task_id, identity=identity,
                 declared_session=declared_session),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    # Passthrough resilience (P3): same-model retry + residency-bounded fallback ON by default.
    # A caller who pinned a model opts out per-request with `x-toto-no-fallback` (same-model retry
    # still applies); the served model is always reported in x_toto.model below.
    allow_fallback = False if request.headers.get("x-toto-no-fallback") else None
    try:
        result = await gateway.complete(req, harness=harness, task_id=task_id,
                                        resilient=True, allow_fallback=allow_fallback,
                                        identity=identity, declared_session=declared_session)
    except BlockedError as exc:
        return _error(403, str(exc), "policy_violation", "mnpi_blocked")
    except ModelNotPermittedError as exc:  # catalog policy forbids it — 403 before the wire
        if exc.allowlist:  # C3 org deny-by-default: audit the denial + ask-your-admin body
            await _audit_model_denied(request, identity, exc.model_id)
        return JSONResponse(status_code=403, content=_model_not_permitted_body(exc))
    except CandidateIneligibleError as exc:
        return _error(403, str(exc), "policy_violation", "model_ineligible")
    except DataPolicyDeniedError:  # W2-C7: org data-classification 'deny' → 403 before the wire
        return JSONResponse(status_code=403, content=_data_policy_denied_body())
    except GatewayDegradedError as exc:  # W1-C1 fail-closed: routing degraded + org rejects → 503
        return JSONResponse(status_code=503, content=_degraded_body(exc.reason))
    except BudgetExceededError as exc:  # W2-C5: over monthly budget + action=reject → 402
        return JSONResponse(status_code=402, content=_budget_body(exc.decision))
    except UnknownModelError as exc:  # e.g. omitted model with no team default_model to substitute
        return _error(404, str(exc), "invalid_request_error", "model_not_found")
    except Exception as exc:  # upstream failure — 429/503 pass through, else generic 502
        return _upstream_error(exc, req.model)
    # Surface the real provenance so a UI can render the routing decision without a 2nd call.
    t = result.trace
    result.response.x_toto = {
        # Correlation keys: request_id joins this response to the gateway trace record + LangSmith
        # run (also the X-Request-ID header); conversation_key groups multi-turn requests.
        "request_id": t.request_id, "conversation_key": t.conversation_key,
        "lane": t.lane, "residency_class": t.residency_class, "model": t.model,
        # Smart routing (SR1): the classified label, so `pi -m toto/smart` sees what it routed as.
        # Derived from route_reason ("label:<l>[:team|:fallback]") — None for a normally-named model.
        "classified_as": _classified_as(t.route_reason),
        "runner_id": t.runner_id, "route_reason": t.route_reason, "guard_action": t.guard_action,
        "signal_intent": t.signal_intent, "cache_hit": t.cache_hit,
        "cost_usd": t.cost_usd, "cost_estimated": t.cost_estimated,
        "frontier_baseline_usd": t.frontier_baseline_usd,
        "tokens_prompt": t.tokens_prompt, "tokens_completion": t.tokens_completion,
        "latency_ms_total": t.latency_ms_total,
        "latency_ms_gateway_overhead": t.latency_ms_gateway_overhead,
        "identity_id": t.identity_id, "offer_id": t.offer_id, "provider": t.provider,
        "upstream_model": t.upstream_model, "credential_scope": t.credential_scope,
        # W2-C5: over-budget disposition on this served request (None = under/no budget).
        "budget_state": t.budget_state,
    }
    return result.response
