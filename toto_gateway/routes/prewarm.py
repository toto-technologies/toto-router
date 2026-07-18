"""POST /v1/prewarm — warm a conversation's provider prompt cache ahead of the first real turn.

A latency tool, not a cost tool: the first real request pays the same cache-write a pre-warm does
(~8% extra input for a faster first token). So it's gated behind a per-org toggle, default OFF
(the org-default routing policy's `prewarm` flag — see INTEGRATION-prewarm.md). When ON, we send a
minimal (max_tokens=1) request through the SAME Gateway.complete path as /v1/chat/completions, so
all provenance/tracing/affinity/cache_control passthrough apply and the provider warms the prefix.
When OFF: 200 {"status": "disabled"} and NO upstream call.

The client decides WHEN to call this (on session open, say) — the gateway ships the capability and
the toggle only; no scheduling/cron here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from ..gateway import _conversation_key
from ..schemas import ChatCompletionRequest, Message
from .deps import Identity, require_auth

router = APIRouter()


class PrewarmRequest(BaseModel):
    # extra="allow" so a client may pass cache_control breakpoints / provider knobs verbatim — they
    # ride through ChatCompletionRequest's own passthrough to the runner, same as a real turn.
    model_config = ConfigDict(extra="allow")
    model: str = "smart"  # smart sentinel allowed — resolved through the normal classify path
    messages: list[Message]


def _prewarm_enabled(identity: Identity) -> bool:
    """The per-org toggle, read off the caller's resolved routing overlay (C6). Default OFF: no
    policy, or the flag unset/false → disabled. Follows the org-default routing-policy mechanism
    (PR #45) — the flag lives in the same blob as bindings/optimize, resolved server-side at auth."""
    return bool((identity.routing_policy or {}).get("prewarm"))


@router.post("/v1/prewarm")
async def prewarm(
    body: PrewarmRequest, request: Request, identity: Identity = Depends(require_auth)
):
    if not _prewarm_enabled(identity):
        # No upstream call — cache-warming is off for this org.
        return {"status": "disabled", "model": body.model,
                "conversation_key": _conversation_key(body.messages)}

    gateway = request.app.state.gateway
    warm_req = ChatCompletionRequest(model=body.model, messages=body.messages, max_tokens=1)
    result = await gateway.complete(
        warm_req, harness=request.headers.get("x-toto-harness"), identity=identity,
    )
    t = result.trace
    return {
        "status": "warmed",
        "model": t.model,  # the RESOLVED model (smart → real id), what actually got warmed
        "conversation_key": t.conversation_key,
        "tokens_cached_write_hint": {
            "tokens_prompt": t.tokens_prompt,
            "tokens_completion": t.tokens_completion,
            "tokens_cached": t.tokens_cached,
        },
    }
