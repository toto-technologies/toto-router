"""Anthropic Messages wire-format boundary for the /v1/messages surface.

Everything inside the gateway stays OpenAI-shaped (schemas.ChatCompletion*); this module is
the only place Anthropic bodies exist. Conversion is delegated to nemo-switchyard's Rust
translation engine (Apache-2.0), which owns the streaming state machine (content-block
open/close, tool-call argument deltas, stop-reason mapping) in both directions.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from switchyard_rust.translation import TranslationEngine

from .schemas import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse

_ANTHROPIC = "anthropic_messages"
_OPENAI = "openai_chat"


def _strip_output_config_format(body: dict[str, Any]) -> None:
    """Claude Code 2.1.1x sends output_config.format, which upstreams reject with a 400.
    output_config.effort is fine, so only the format key goes; an emptied output_config
    goes with it."""
    oc = body.get("output_config")
    if isinstance(oc, dict) and "format" in oc:
        oc.pop("format", None)
        if not oc:
            body.pop("output_config", None)


def to_chat_request(body: dict[str, Any]) -> ChatCompletionRequest:
    """Anthropic Messages request body -> the gateway's internal OpenAI-shaped request."""
    body = dict(body)
    _strip_output_config_format(body)
    translated = TranslationEngine().translate_request(_ANTHROPIC, _OPENAI, body)
    # stream is routing-relevant; make it explicit rather than trusting extra-passthrough.
    translated["stream"] = bool(body.get("stream"))
    return ChatCompletionRequest.model_validate(translated)


def to_anthropic_response(resp: ChatCompletionResponse) -> dict[str, Any]:
    """Gateway response -> Anthropic Message body."""
    return TranslationEngine().translate_response(
        _OPENAI, _ANTHROPIC, resp.model_dump(exclude_none=True)
    )


def anthropic_error(err_type: str, message: str) -> dict[str, Any]:
    """The Anthropic error envelope — the /v1/messages equivalent of routes/chat._error."""
    return {"type": "error", "error": {"type": err_type, "message": message}}


async def stream_events(
    chunks: AsyncIterator[ChatCompletionChunk], *, model: str
) -> AsyncIterator[dict[str, Any]]:
    """OpenAI chunk stream -> Anthropic named-event stream (message_start ... message_stop).

    translate_stream consumes an async iterable of source-format events and yields the
    target-format events, carrying the block-index state machine and flushing the terminal
    events (content_block_stop, message_delta, message_stop) once the source stream closes.
    """
    async def _bodies() -> AsyncIterator[dict[str, Any]]:
        async for chunk in chunks:
            yield chunk.model_dump(exclude_none=True)

    async for event in TranslationEngine().translate_stream(
        _OPENAI, _ANTHROPIC, _bodies(), model=model
    ):
        yield event
