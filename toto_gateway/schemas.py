"""OpenAI-compatible request/response schemas (the lingua franca in AND out).

These mirror the OpenAI Chat Completions API so any OpenAI client — Pi, OpenCode, the
`openai` SDK, plain curl — works against the gateway unchanged. Runner adapters translate
between this shape and whatever the upstream actually speaks (Anthropic Messages, etc.).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _msg_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _now() -> int:
    return int(time.time())


# --- Inbound -----------------------------------------------------------------


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None

    def text(self) -> str:
        """Best-effort flatten of content to a string (handles the parts array form)."""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            return "".join(
                part.get("text", "")
                for part in self.content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            )
        return ""


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    # extra="allow" so we passthrough provider-specific knobs we don't model explicitly.
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[Message]
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: str | list[str] | None = None
    n: int | None = None
    user: str | None = None
    # Internal, never client-echoed: the conversation-stable fingerprint the gateway stamps
    # (Gateway._conversation_key) so runners can emit provider cache-affinity hints
    # (prompt_cache_key / session_id). Stripped in passthrough_params so it never leaks upstream.
    conversation_key: str | None = None
    # Internal, never client-echoed: the resolved cache-behavior knobs the gateway stamps (A8) from
    # the caller's org/team policy + global env defaults, so runners read auto-inject prefs without
    # importing identity/settings resolution. Stripped in passthrough_params so it never hits the wire.
    cache_prefs: dict | None = None

    def passthrough_params(self) -> dict[str, Any]:
        """Modeled + extra knobs, minus routing/control fields, for upstream forwarding."""
        data = self.model_dump(exclude_none=True)
        # Also strip openai SDK request-control kwargs: a tenant must not be able to
        # inject headers/body/query/timeout into the gateway's outbound provider call.
        for k in ("model", "messages", "stream", "stream_options", "conversation_key", "cache_prefs",
                  "extra_headers", "extra_query", "extra_body", "timeout"):
            data.pop(k, None)
        for k in [k for k in data if k.startswith("extra_")]:
            data.pop(k, None)
        return data


# --- Usage / cost ------------------------------------------------------------


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Toto extensions (context-caching plan P0): how much of the prompt the PROVIDER served
    # from cache (prompt_tokens_details.cached_tokens) + the upstream-reported USD cost when
    # the provider bills one (OpenRouter usage accounting). OpenAI clients ignore extras.
    tokens_cached: int = 0
    # Prompt tokens the provider WROTE to cache this turn (Anthropic cache_creation_input_tokens).
    # A disjoint slice of prompt_tokens from tokens_cached: writes cost the base prompt price plus a
    # provider write premium (pricing.cache_write_multiplier). 0 where the provider doesn't report
    # writes (OpenAI). The write-ledger line behind the caching P&L (multi-model-caching plan §6).
    tokens_cache_write: int = 0
    cost_upstream: float | None = None

    @classmethod
    def of(cls, prompt: int, completion: int, cached: int = 0,
           cost_upstream: float | None = None, cache_write: int = 0) -> "Usage":
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            tokens_cached=cached,
            tokens_cache_write=cache_write,
            cost_upstream=cost_upstream,
        )


# --- Outbound (non-streaming) ------------------------------------------------


class ResponseMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str = "assistant"
    content: str | None = None
    # Structured tool calls, passed through opaquely (harnesses like pi bring their own tool
    # loop — the gateway never interprets these). Modeled loosely on purpose: whatever dict
    # shape the upstream returns survives the round trip.
    tool_calls: list[dict[str, Any]] | None = None


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str | None = "stop"


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: _msg_id("chatcmpl"))
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=_now)
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)
    # Toto extension: the provenance/trace for this answer, so a UI can render the routing
    # decision + residency + economics without a second call. OpenAI clients ignore it.
    x_toto: dict | None = None
    # Toto extension: what the UPSTREAM actually served, captured before we re-stamp model=alias.
    # served model string (e.g. "anthropic/claude-sonnet-5"), the provider that answered
    # (OpenRouter's non-standard body field), and the upstream generation id. Absent on fakes/
    # providers that don't return them → None. OpenAI clients ignore extras.
    upstream_model: str | None = None
    provider: str | None = None
    generation_id: str | None = None
    identity_id: str | None = None
    offer_id: str | None = None
    credential_scope: str | None = None

    @classmethod
    def simple(cls, *, model: str, content: str, usage: Usage, finish_reason: str = "stop",
               upstream_model: str | None = None, provider: str | None = None,
               generation_id: str | None = None, tool_calls: list[dict[str, Any]] | None = None,
               identity_id: str | None = None, offer_id: str | None = None,
               credential_scope: str | None = None):
        return cls(
            model=model,
            choices=[Choice(message=ResponseMessage(content=content, tool_calls=tool_calls),
                            finish_reason=finish_reason)],
            usage=usage,
            upstream_model=upstream_model,
            provider=provider,
            generation_id=generation_id,
            identity_id=identity_id,
            offer_id=offer_id,
            credential_scope=credential_scope,
        )


# --- Outbound (streaming chunks) ---------------------------------------------


class ChunkDelta(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str | None = None
    content: str | None = None
    # Streaming tool-call deltas, passed through opaquely (same spirit as ResponseMessage).
    tool_calls: list[dict[str, Any]] | None = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: ChunkDelta = Field(default_factory=ChunkDelta)
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str = Field(default_factory=lambda: _msg_id("chatcmpl"))
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=_now)
    model: str
    choices: list[ChunkChoice] = Field(default_factory=list)
    # Present only on the final chunk when stream_options.include_usage is set.
    usage: Usage | None = None
    identity_id: str | None = None
    offer_id: str | None = None
    provider: str | None = None
    credential_scope: str | None = None

    @classmethod
    def role_chunk(cls, *, id: str, model: str) -> "ChatCompletionChunk":
        return cls(id=id, model=model, choices=[ChunkChoice(delta=ChunkDelta(role="assistant"))])

    @classmethod
    def content_chunk(cls, *, id: str, model: str, text: str) -> "ChatCompletionChunk":
        return cls(id=id, model=model, choices=[ChunkChoice(delta=ChunkDelta(content=text))])

    @classmethod
    def tool_calls_chunk(cls, *, id: str, model: str,
                         tool_calls: list[dict[str, Any]]) -> "ChatCompletionChunk":
        return cls(id=id, model=model,
                   choices=[ChunkChoice(delta=ChunkDelta(tool_calls=tool_calls))])

    @classmethod
    def stop_chunk(cls, *, id: str, model: str, finish_reason: str = "stop") -> "ChatCompletionChunk":
        return cls(id=id, model=model, choices=[ChunkChoice(delta=ChunkDelta(), finish_reason=finish_reason)])

    @classmethod
    def usage_chunk(cls, *, id: str, model: str, usage: Usage) -> "ChatCompletionChunk":
        # OpenAI convention: a trailing chunk with empty choices carrying usage.
        return cls(id=id, model=model, choices=[], usage=usage)


# --- Models endpoint ---------------------------------------------------------


class Model(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=_now)
    owned_by: str = "toto"
    # Toto extension: surface the lane + residency so clients can see the data path.
    lane: str | None = None
    residency_class: str | None = None
    # The concrete upstream model this entry dispatches to (for catalog-join UIs).
    upstream_model: str | None = None
    # A clean provider label (anthropic/openai/openrouter/fireworks/local/fake) derived from the
    # catalog endpoint + api_key_env, so the console shows the REAL model, not the or-* alias.
    provider: str | None = None
    # Catalog-table extensions (admin console): the provider path + price + context window the
    # Catalog & Routing screen renders. Additive — OpenAI clients ignore unknown fields.
    # `residency` is the human-facing alias of residency_class (in_perimeter / cloud).
    via: str | None = None
    residency: str | None = None
    price_in: float | None = None
    price_out: float | None = None
    context_window: int | None = None
    identity_id: str | None = None
    offer_id: str | None = None
    credential_scope: str | None = None
    modalities: tuple[str, ...] = ()
    supported_parameters: tuple[str, ...] = ()


class ModelsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[Model]
