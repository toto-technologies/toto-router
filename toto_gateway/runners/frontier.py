"""Frontier lane adapter — Anthropic Messages API ⇄ OpenAI Chat Completions.

Translates ChatCompletionRequest → Anthropic kwargs and Anthropic responses/events →
ChatCompletionChunk/ChatCompletionResponse so the gateway pipeline stays OpenAI-shaped
throughout. All mapping is in pure module-level functions; the runner is a thin shell.

Streaming event order from the Anthropic SDK:
  message_start           → carries usage.input_tokens
  content_block_start     → (type: "text")
  content_block_delta*    → delta.type == "text_delta", delta.text
  content_block_stop
  message_delta           → carries usage.output_tokens + stop_reason
  message_stop

We always emit a trailing usage chunk (base.py streaming contract).
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from ..catalog import CatalogEntry
from ..config import get_settings
from ..schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChunkChoice,
    ChunkDelta,
    Model,
    Usage,
)
from .base import CartridgeManifest, NotImplementedInPhase0, Telemetry, auto_cache_prefs

_DEFAULT_MAX_TOKENS = 4096

# --- Stop reason mapping -------------------------------------------------------

_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


def _cache_tok(usage: Any, field: str) -> int:
    """Anthropic usage cache field as an int — 0 when absent, None, or a test double's auto-attr
    (MagicMock fabricates truthy attributes; only a real int counts)."""
    v = getattr(usage, field, 0)
    return v if isinstance(v, int) else 0


def _map_stop_reason(anthropic_reason: str | None) -> str:
    if anthropic_reason is None:
        return "stop"
    return _STOP_REASON_MAP.get(anthropic_reason, "stop")


# --- Pure mapping functions ----------------------------------------------------


def _args_dict(arguments: Any) -> dict:
    """OpenAI tool_call arguments (a JSON string) → Anthropic input (a dict). Fail-safe: an
    unparseable argument string becomes {} rather than a 400 on the wire."""
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _text_blocks(msg: Any) -> list[dict[str, Any]]:
    """Text content → Anthropic text blocks, preserving any `cache_control` breakpoint carried on
    a structured content part (context-caching plan Decision 2 — Anthropic caching still requires
    explicit breakpoints through the gateway). A plain-string message flattens to one bare text
    block, so the lone-text collapse downstream stays byte-identical for unmarked conversations."""
    if isinstance(msg.content, list):
        out: list[dict[str, Any]] = []
        for part in msg.content:
            if not (isinstance(part, dict) and part.get("type") in (None, "text")):
                continue
            text = part.get("text", "")
            if not text:
                continue
            block: dict[str, Any] = {"type": "text", "text": text}
            if part.get("cache_control"):
                block["cache_control"] = part["cache_control"]
            out.append(block)
        return out
    text = msg.text()
    return [{"type": "text", "text": text}] if text else []


def _message_blocks(msg: Any) -> tuple[str, list[dict[str, Any]]]:
    """One OpenAI message → (anthropic role, content blocks). Tool traffic maps natively:
    assistant tool_calls → tool_use blocks; role:"tool" → a user message with a tool_result
    block (Anthropic's shape for tool outputs)."""
    if msg.role == "tool":
        return "user", [{
            "type": "tool_result",
            "tool_use_id": getattr(msg, "tool_call_id", None) or "",
            "content": msg.text(),
        }]
    blocks: list[dict[str, Any]] = _text_blocks(msg)
    for t in (getattr(msg, "tool_calls", None) or []):
        fn = (t.get("function") or {}) if isinstance(t, dict) else {}
        blocks.append({
            "type": "tool_use",
            "id": (t.get("id") if isinstance(t, dict) else None) or "",
            "name": fn.get("name", ""),
            "input": _args_dict(fn.get("arguments")),
        })
    role = "assistant" if msg.role == "assistant" else "user"
    return role, blocks


def _anthropic_tools(req: ChatCompletionRequest) -> list[dict[str, Any]]:
    """OpenAI `tools` (function specs) → Anthropic tool specs. Non-function entries are skipped."""
    out = []
    for t in (getattr(req, "tools", None) or []):
        if not isinstance(t, dict) or t.get("type") not in (None, "function"):
            continue
        fn = t.get("function") or {}
        if not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _anthropic_tool_choice(tool_choice: Any) -> dict[str, Any] | None:
    """OpenAI tool_choice → Anthropic tool_choice. "none" is handled by the caller (drops tools
    entirely — Anthropic has no "none")."""
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return {"type": "tool", "name": name}
    return None  # "auto"/absent → Anthropic's default (auto)


def _has_cache_breakpoint(kwargs: dict[str, Any]) -> bool:
    """True if the request already carries an explicit Anthropic cache_control breakpoint anywhere
    (a marked system/message/tool block). Client-sent breakpoints always win, so auto-inject skips
    entirely when one is present. Scans the translated kwargs — the one shape where every breakpoint
    the client sent (system parts, message parts) has already surfaced as a block."""
    system = kwargs.get("system")
    if isinstance(system, list) and any(
        isinstance(b, dict) and "cache_control" in b for b in system
    ):
        return True
    for msg in kwargs.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and "cache_control" in b for b in content
        ):
            return True
    return any(
        isinstance(t, dict) and "cache_control" in t for t in kwargs.get("tools", [])
    )


def openai_to_anthropic(req: ChatCompletionRequest, model: str) -> dict[str, Any]:
    """Translate a ChatCompletionRequest into Anthropic messages.create kwargs.

    Consecutive same-role messages are merged into one (Anthropic requires alternation, and
    parallel tool results MUST land as sibling tool_result blocks of a single user message).
    Empty messages are dropped."""
    system_parts: list[str] = []       # message-level text for the plain-string join (unchanged)
    system_blocks: list[dict[str, Any]] = []  # part-level blocks, used only if a breakpoint exists
    messages: list[dict[str, Any]] = []

    for msg in req.messages:
        if msg.role == "system":
            system_parts.append(msg.text())
            system_blocks.extend(_text_blocks(msg))
            continue
        role, blocks = _message_blocks(msg)
        if not blocks:
            continue
        if messages and messages[-1]["role"] == role:
            prev = messages[-1]
            if isinstance(prev["content"], str):  # re-expand a collapsed text message
                prev["content"] = [{"type": "text", "text": prev["content"]}]
            prev["content"].extend(blocks)
        else:
            # A lone text block collapses to the plain-string form (byte-identical to the
            # pre-tools wire for pure-text conversations) — but NOT when it carries a cache_control
            # breakpoint, which the string form can't express.
            content = blocks[0]["text"] \
                if len(blocks) == 1 and blocks[0]["type"] == "text" \
                and "cache_control" not in blocks[0] else blocks
            messages.append({"role": role, "content": content})

    if not messages:  # Anthropic requires ≥1 message; degenerate all-system request
        messages = [{"role": "user", "content": " "}]

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": req.max_tokens if req.max_tokens is not None else _DEFAULT_MAX_TOKENS,
    }

    tool_choice = getattr(req, "tool_choice", None)
    tools = _anthropic_tools(req) if tool_choice != "none" else []
    if tools:
        kwargs["tools"] = tools
        mapped_choice = _anthropic_tool_choice(tool_choice)
        if mapped_choice is not None:
            kwargs["tool_choice"] = mapped_choice

    if system_parts:
        # A system breakpoint forces the blocks form (Anthropic accepts `system` as a blocks list);
        # otherwise the joined plain string, byte-identical to the pre-caching wire.
        kwargs["system"] = system_blocks \
            if any("cache_control" in b for b in system_blocks) else "\n\n".join(system_parts)

    if req.temperature is not None:
        kwargs["temperature"] = req.temperature

    if req.top_p is not None:
        kwargs["top_p"] = req.top_p

    if req.stop is not None:
        # Anthropic accepts a list only.
        kwargs["stop_sequences"] = [req.stop] if isinstance(req.stop, str) else req.stop

    # Auto-inject prompt caching for continuing conversations that sent no breakpoint of their own
    # (most OpenAI-shaped clients). Anthropic's TOP-LEVEL automatic mode places one breakpoint on the
    # last cacheable block and advances it as the conversation grows. We gate ONLY on continuity —
    # tools present (a tool call structurally guarantees a next request) or a multi-message dialogue —
    # so a one-shot never pays the 1.25x write premium for a cache nothing will read. We deliberately
    # do NOT gate on the per-model minimum cacheable length: that minimum is enforced Anthropic-side,
    # which silently no-ops (no cache_creation/read) below it — detected after the fact via the usage
    # cache fields already recorded as Usage.tokens_cached, not guessed at here.
    auto_inject, min_messages = auto_cache_prefs(req, get_settings())
    if auto_inject and not _has_cache_breakpoint(kwargs):
        continuing = bool(getattr(req, "tools", None)) or len(req.messages) >= min_messages
        if continuing:
            kwargs["cache_control"] = {"type": "ephemeral"}

    return kwargs


def anthropic_to_openai(resp: Any, model_id: str) -> ChatCompletionResponse:
    """Translate an Anthropic Message object into a ChatCompletionResponse. tool_use blocks
    come back as OpenAI tool_calls (arguments re-serialized to a JSON string)."""
    # A text block is anything with a string .text that isn't a tool_use — tolerant of SDK
    # objects, plain namespaces, and test mocks alike.
    text = "".join(
        block.text for block in resp.content
        if getattr(block, "type", None) != "tool_use"
        and isinstance(getattr(block, "text", None), str)
    )
    tool_calls = [
        {
            "id": block.id,
            "type": "function",
            "function": {"name": block.name, "arguments": json.dumps(block.input or {})},
        }
        for block in resp.content if getattr(block, "type", None) == "tool_use"
    ] or None

    # Anthropic usage semantics differ from OpenAI: input_tokens EXCLUDES cache reads/writes
    # (input + cache_read + cache_creation = the full prompt), while the gateway's Usage treats
    # tokens_cached / tokens_cache_write as disjoint SUBSETS of prompt_tokens (pricing.py discounts
    # the read slice and charges the write premium on the write slice). Rebuild the full prompt and
    # report reads and writes as their respective slices.
    cache_read = _cache_tok(resp.usage, "cache_read_input_tokens")
    cache_write = _cache_tok(resp.usage, "cache_creation_input_tokens")
    usage = Usage.of(
        prompt=resp.usage.input_tokens + cache_read + cache_write,
        completion=resp.usage.output_tokens,
        cached=cache_read,
        cache_write=cache_write,
    )

    finish_reason = _map_stop_reason(resp.stop_reason)

    return ChatCompletionResponse.simple(
        model=model_id,
        content=text,
        usage=usage,
        finish_reason=finish_reason,
        tool_calls=tool_calls,
    )


async def anthropic_stream_to_chunks(
    events: AsyncIterator[Any], *, cid: str, model_id: str
) -> AsyncIterator[ChatCompletionChunk]:
    """Consume raw Anthropic stream events; yield OpenAI-compatible chunks.

    Always emits a trailing usage chunk regardless of what the client asked for —
    the gateway layer decides whether to forward it (base.py streaming contract).
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    finish_reason: str = "stop"
    # Anthropic block index → OpenAI tool_calls index, for streamed tool_use blocks.
    tool_index: dict[int, int] = {}

    yield ChatCompletionChunk.role_chunk(id=cid, model=model_id)

    async for event in events:
        event_type = event.type

        if event_type == "message_start":
            if hasattr(event, "message") and hasattr(event.message, "usage"):
                u = event.message.usage
                # Same semantics note as the non-stream path: Anthropic's input_tokens excludes
                # cache reads/writes; rebuild the full prompt, report reads as the cached slice.
                cache_read = _cache_tok(u, "cache_read_input_tokens")
                cache_write = _cache_tok(u, "cache_creation_input_tokens")
                input_tokens = u.input_tokens + cache_read + cache_write
                cached_tokens = cache_read
                cache_write_tokens = cache_write

        elif event_type == "content_block_start":
            block = getattr(event, "content_block", None)
            if getattr(block, "type", None) == "tool_use":
                # OpenAI streaming convention: first delta carries index+id+name+empty arguments,
                # later deltas append argument fragments by index.
                idx = len(tool_index)
                tool_index[event.index] = idx
                yield ChatCompletionChunk.tool_calls_chunk(
                    id=cid, model=model_id,
                    tool_calls=[{"index": idx, "id": block.id, "type": "function",
                                 "function": {"name": block.name, "arguments": ""}}])

        elif event_type == "content_block_delta":
            delta = event.delta
            if getattr(delta, "type", None) == "input_json_delta":
                idx = tool_index.get(event.index)
                if idx is not None and delta.partial_json:
                    yield ChatCompletionChunk.tool_calls_chunk(
                        id=cid, model=model_id,
                        tool_calls=[{"index": idx,
                                     "function": {"arguments": delta.partial_json}}])
            elif getattr(delta, "type", None) == "thinking_delta":
                # Keepalive + fidelity: surface thinking as a `reasoning` delta extra (matching
                # the OpenAI runner) so long thinking never reads as a dead stream.
                thinking = getattr(delta, "thinking", None)
                if thinking:
                    yield ChatCompletionChunk(
                        id=cid, model=model_id,
                        choices=[ChunkChoice(delta=ChunkDelta(reasoning=thinking))])
            elif hasattr(delta, "text") and delta.text:
                yield ChatCompletionChunk.content_chunk(
                    id=cid, model=model_id, text=delta.text
                )

        elif event_type == "message_delta":
            if hasattr(event, "usage") and event.usage is not None:
                output_tokens = event.usage.output_tokens
            if hasattr(event, "delta") and hasattr(event.delta, "stop_reason"):
                finish_reason = _map_stop_reason(event.delta.stop_reason)

    yield ChatCompletionChunk.stop_chunk(id=cid, model=model_id, finish_reason=finish_reason)
    yield ChatCompletionChunk.usage_chunk(
        id=cid,
        model=model_id,
        usage=Usage.of(prompt=input_tokens, completion=output_tokens, cached=cached_tokens,
                       cache_write=cache_write_tokens),
    )


# --- Runner --------------------------------------------------------------------


class FrontierRunner:
    """Anthropic-backed lane runner. Speaks OpenAI in, Anthropic out."""

    def __init__(self, entry: CatalogEntry, client: Any = None) -> None:
        self.entry = entry
        self.runner_id = f"anthropic-{entry.effective_upstream_model}"
        self._client = client  # injected in tests; lazy-constructed in production

    def _get_client(self) -> Any:
        if self._client is None:
            from anthropic import AsyncAnthropic  # lazy import — no key needed at import time

            # Bounded timeout (caps the SDK's 600s read default) + max_retries=0 so our resilience
            # layer is the single retry authority (matches the OpenAI runner).
            self._client = AsyncAnthropic(timeout=get_settings().provider_timeout(), max_retries=0)
        return self._client

    async def chat(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> ChatCompletionResponse:
        kwargs = openai_to_anthropic(req, entry.effective_upstream_model)
        resp = await self._get_client().messages.create(**kwargs)
        return anthropic_to_openai(resp, model_id=entry.id)

    async def stream(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> AsyncIterator[ChatCompletionChunk]:
        kwargs = openai_to_anthropic(req, entry.effective_upstream_model)
        cid = f"chatcmpl-ant-{entry.effective_upstream_model}"

        async with self._get_client().messages.stream(**kwargs) as s:
            async for chunk in anthropic_stream_to_chunks(
                s, cid=cid, model_id=entry.id
            ):
                yield chunk

    def models(self) -> list[Model]:
        return [
            Model(
                id=self.entry.id,
                owned_by="anthropic",
                lane=self.entry.lane,
                residency_class=self.entry.residency_class,
            )
        ]

    # --- stubbed contract (Phase 0) -------------------------------------------

    def cartridge_manifest(self) -> CartridgeManifest:
        return CartridgeManifest(base_model=self.entry.effective_upstream_model)

    async def load(self, cartridge_ref: str) -> None:
        raise NotImplementedInPhase0("cartridge load is Phase 2")

    async def unload(self, cartridge_ref: str) -> None:
        raise NotImplementedInPhase0("cartridge unload is Phase 2")

    def health(self) -> Telemetry:
        return Telemetry(healthy=True)
