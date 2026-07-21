"""OpenAI lane adapter — native OpenAI Chat Completions.

OpenAI IS the gateway's native shape (OpenAI-in, OpenAI-out), so this runner
is near-passthrough — no translation layer needed. We still re-stamp model=entry.id
so callers always see the catalog alias, never the upstream model string.

Streaming contract (base.py §7.3): always emit a trailing usage chunk when the
upstream reports usage (stream_options.include_usage=True on the upstream call).
The gateway layer decides whether to forward it to the client.
"""

from __future__ import annotations

import os
import time
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


def _wire_content(m: Any) -> Any:
    """Message content for the upstream wire: a parts list passes through AS-IS (preserving
    cache_control breakpoints etc. — context-caching plan Decision 2); everything else flattens
    via m.text(), byte-identical to a plain string's content."""
    return m.content if isinstance(m.content, list) else m.text()


def _textified_calls(tc: list[dict]) -> str:
    """A text rendering of assistant tool_calls for providers that can't speak them natively —
    the model still sees WHAT it called so the following [tool result] text makes sense."""
    lines = []
    for t in tc:
        fn = t.get("function") or {}
        lines.append(f"[called {fn.get('name', '?')}({fn.get('arguments', '')})]")
    return "\n".join(lines)


def _wire_messages(messages: list, *, native_tools: bool = True) -> list[dict]:
    """Serialize request messages for the upstream wire.

    native_tools=True (the default — catalog entry `tools: true`): the agentic shape is preserved
    verbatim — assistant `tool_calls` and the tool message's `tool_call_id` round-trip, so a
    harness tool loop (pi, OpenCode) reaches the upstream model with full fidelity. Dropping these
    (the pre-SR2 behavior) made models re-issue calls and lose the thread mid-loop.

    native_tools=False (catalog entry `tools: false`): coerce tool traffic to labeled text.
    Why: some providers ABORT the stream on a `tool` role — gemini-2.5-flash via OpenRouter
    returns an empty stream with no finish_reason (verified 2026-07-08). The tool output still
    reaches the model, just as user text, and assistant tool_calls are textified so the
    conversation stays coherent."""
    out = []
    for m in messages:
        if m.role == "tool":
            if native_tools:
                d = {"role": "tool", "content": _wire_content(m)}
                tcid = getattr(m, "tool_call_id", None)
                if tcid:
                    d["tool_call_id"] = tcid
                out.append(d)
            else:
                out.append({"role": "user", "content": f"[tool result]\n{m.text()}"})
            continue
        d = {"role": m.role, "content": _wire_content(m)}
        if m.name:
            d["name"] = m.name
        tc = getattr(m, "tool_calls", None)
        if tc:
            if native_tools:
                d["tool_calls"] = tc
                if m.content is None:  # null content is legal (and expected) beside tool_calls
                    d["content"] = None
            else:
                d["content"] = (m.text() + "\n" + _textified_calls(tc)).strip()
        out.append(d)
    return out


def _strip_tool_params(params: dict) -> dict:
    """Remove tool-calling knobs for a `tools: false` entry — a provider that chokes on the tool
    role may also 400 on (or hallucinate against) the tools param, and we've already textified
    the loop."""
    for k in ("tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"):
        params.pop(k, None)
    return params


def _str_or_none(v: Any) -> str | None:
    """Only actual non-empty strings survive — anything else (None, a MagicMock in tests, a
    number) reads as None. Keeps the schema's str|None fields clean on the shared prod path."""
    return v if isinstance(v, str) and v else None


def _upstream_provenance(resp: Any) -> tuple[str | None, str | None, str | None]:
    """(served_model, provider, generation_id) off an upstream response, defensively. resp.model
    is the model the upstream actually served; resp.id is the generation id; provider is
    OpenRouter's non-standard body field (lands in pydantic model_extra). Absent → None (fakes,
    providers that don't return them). Never raises on the shared prod path."""
    provider = _str_or_none(getattr(resp, "provider", None))
    if provider is None:  # OpenRouter puts it in the raw body → pydantic model_extra
        extra = getattr(resp, "model_extra", None)
        provider = _str_or_none(extra.get("provider")) if isinstance(extra, dict) else None
    return (_str_or_none(getattr(resp, "model", None)),
            provider,
            _str_or_none(getattr(resp, "id", None)))


def _dump_tool_calls(tc: Any) -> list[dict] | None:
    """Upstream tool_calls → plain dicts for opaque passthrough. SDK objects dump via
    model_dump; already-dict shapes pass as-is; absent/empty → None (field omitted)."""
    if not tc:
        return None
    return [t.model_dump(exclude_none=True) if hasattr(t, "model_dump") else t for t in tc]


def _int_attr(obj: Any, name: str) -> int:
    """A non-negative int attribute off `obj`, or 0 (absent / MagicMock auto-attr / non-int)."""
    v = getattr(obj, name, None)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _cache_details(usage: Any) -> tuple[int, int, float | None]:
    """(cached_tokens, cache_write_tokens, upstream cost) off an upstream usage object, defensively —
    absent or non-numeric fields (fakes, providers without details) read as (0, 0, None).

    Cache WRITES: OpenAI does NOT report them (caching is automatic; writes bill at the base — or on
    GPT-5.6+ a 1.25x — rate but are never itemized in the usage object), so this stays 0 there. Only
    OpenRouter's Anthropic-family passthrough surfaces cache creation, via `cache_creation_input_tokens`
    on either the usage object or its prompt_tokens_details (usage accounting shape); read both,
    default 0. Anything real gets priced with the entry's cache_write_multiplier in compute_cost_usd."""
    details = getattr(usage, "prompt_tokens_details", None)
    cached = _int_attr(details, "cached_tokens")
    cache_write = (_int_attr(usage, "cache_creation_input_tokens")
                   or _int_attr(details, "cache_creation_input_tokens"))
    cost = getattr(usage, "cost", None)  # OpenRouter usage accounting (usage: {include: true})
    return (cached, cache_write,
            float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)


def _usage_extra(entry: CatalogEntry) -> dict:
    """OpenRouter's `usage: {include: true}` accounting extension — reports cached_tokens + real
    cost. It is OpenRouter-SPECIFIC: strict OpenAI-compatible providers (Fireworks) 400 with
    "Extra inputs are not permitted, field: 'usage'". So send it only to OpenRouter; everyone else
    gets a clean OpenAI body. Caught by a live Fireworks BYOK call 2026-07-04."""
    base = entry.base_url or ""
    return {"extra_body": {"usage": {"include": True}}} if "openrouter.ai" in base else {}


def _cache_affinity(entry: CatalogEntry, req: ChatCompletionRequest, params: dict) -> dict:
    """Outbound cache-affinity hints (context-caching plan P2): steer a conversation's turns to the
    same upstream prompt cache. OpenAI-native gets `prompt_cache_key` (a native param that combines
    with OpenAI's first-~256-token routing hash); OpenRouter gets `session_id` (activates its
    cache-preserving sticky routing). Client-sent values win over the gateway's conversation_key.
    Each hint goes ONLY to the endpoint that understands it — a strict OpenAI-compatible provider
    (Fireworks) 400s on unknown fields — so we pop BOTH off params up front and re-add only the one
    that fits. `session_id` rides in extra_body (not a native OpenAI param)."""
    conv = getattr(req, "conversation_key", None)
    client_pck = params.pop("prompt_cache_key", None)
    client_sid = params.pop("session_id", None)
    base = entry.base_url or ""
    if "openrouter.ai" in base:
        sid = client_sid or conv
        return {"extra_body": {"session_id": sid}} if sid else {}
    if not base or "api.openai.com" in base:
        pck = client_pck or conv
        return {"prompt_cache_key": pck} if pck else {}
    return {}


def _provider_extra(entry: CatalogEntry, req: ChatCompletionRequest, params: dict) -> dict:
    """Non-standard outbound kwargs: OpenRouter usage accounting + cache-affinity hints, with both
    extra_body contributions merged into one (dict() forbids a duplicate `extra_body` keyword).
    Mutates `params`, popping any client-sent prompt_cache_key/session_id (see _cache_affinity)."""
    usage = _usage_extra(entry)
    affinity = _cache_affinity(entry, req, params)
    extra_body = {**usage.get("extra_body", {}), **affinity.pop("extra_body", {})}
    out = dict(affinity)  # prompt_cache_key, if any
    if extra_body:
        out["extra_body"] = extra_body
    return out


def _is_anthropic_family(model: str) -> bool:
    """Heuristic: does this upstream model id name an Anthropic-family or Qwen model? Those two are
    the providers OpenRouter does NOT cache automatically — they need explicit `cache_control`
    breakpoints passed through (multi-model-caching plan). Everyone else caches with no markers.
    ponytail: string-match heuristic; the catalog is the source of truth the day it grows a
    provider-family field — replace this then."""
    m = (model or "").lower()
    return "claude" in m or "anthropic/" in m or "qwen" in m


def _wire_has_breakpoint(messages_wire: list[dict]) -> bool:
    """True if any wire message already carries a `cache_control` breakpoint (parts-list form).
    Client-sent breakpoints always win — auto-inject skips entirely when one is present."""
    for msg in messages_wire:
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(p, dict) and "cache_control" in p for p in content
        ):
            return True
    return False


def _mark_last_text(msg: dict) -> None:
    """Add a message-level breakpoint by putting cache_control on the final text part of `msg`,
    converting a plain-string content to the parts-list form. No-op on empty/None or text-less
    content (e.g. an assistant message that is pure tool_calls). Copies before mutating so a
    client-supplied parts list (aliased in from req.messages) is never touched in place."""
    content = msg.get("content")
    bp = {"type": "ephemeral"}
    if isinstance(content, str):
        if content:
            msg["content"] = [{"type": "text", "text": content, "cache_control": bp}]
    elif isinstance(content, list):
        idx = next(
            (i for i in reversed(range(len(content)))
             if isinstance(content[i], dict) and content[i].get("type") == "text"),
            None,
        )
        if idx is not None:
            new = list(content)
            new[idx] = {**content[idx], "cache_control": bp}
            msg["content"] = new


def _last_text_bearing(messages_wire: list[dict]) -> dict | None:
    """The last message that can carry a text breakpoint: role != "tool" and has text content.
    Skipping tool-result messages keeps their structure intact and lands the breakpoint on the
    last real user/assistant turn — the standard second breakpoint of the two-breakpoint pattern."""
    for msg in reversed(messages_wire):
        if msg.get("role") == "tool":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content:
            return msg
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") == "text" for p in content
        ):
            return msg
    return None


def _openrouter_cache_inject(
    entry: CatalogEntry, req: ChatCompletionRequest, messages_wire: list[dict]
) -> list[dict]:
    """Inject Anthropic-style `cache_control` breakpoints on the OpenRouter path.

    OpenRouter caches most providers automatically, but Anthropic-family and Qwen upstreams need
    breakpoints forwarded explicitly — and OpenAI-shaped clients (HERMES, pi) send plain-string
    content with none, so a continuing Sonnet-via-OpenRouter session pays full input price every
    turn (a real 9-turn session measured 0% cached reads). When bound for OpenRouter, the upstream
    is Anthropic-family/Qwen, the client sent no breakpoint anywhere, and the request shows
    continuity (same gate as the frontier auto-inject: tools present, or >= min_messages), mark the
    standard two breakpoints — the system message and the last text-bearing turn. OpenRouter
    forwards message-level cache_control to Anthropic; there is no top-level automatic mode here.
    Any non-OpenRouter or non-Anthropic-family entry, or a client that already sent a breakpoint,
    is a no-op."""
    auto_inject, min_messages = auto_cache_prefs(req, get_settings())
    if not auto_inject:
        return messages_wire
    if "openrouter.ai" not in (entry.base_url or ""):
        return messages_wire
    if not _is_anthropic_family(entry.effective_upstream_model):
        return messages_wire
    if _wire_has_breakpoint(messages_wire):
        return messages_wire
    continuing = bool(getattr(req, "tools", None)) or len(req.messages) >= min_messages
    if not continuing:
        return messages_wire
    if messages_wire and messages_wire[0].get("role") == "system":
        _mark_last_text(messages_wire[0])
    target = _last_text_bearing(messages_wire)
    if target is not None:
        _mark_last_text(target)
    return messages_wire


def _stamp_route_provenance(
    chunk: ChatCompletionChunk, entry: CatalogEntry
) -> ChatCompletionChunk:
    chunk.identity_id = entry.identity_id
    chunk.offer_id = entry.offer_id
    chunk.provider = entry.provider
    chunk.credential_scope = entry.credential_scope_label
    return chunk


class OpenAIRunner:
    """OpenAI-backed lane runner. Speaks OpenAI in, OpenAI out (near-passthrough)."""

    def __init__(self, entry: CatalogEntry, client: Any = None) -> None:
        self.entry = entry
        self.runner_id = f"openai-{entry.effective_upstream_model}"
        self._client = client  # injected in tests; lazy-constructed in production

    def _get_client(self, entry: CatalogEntry | None = None) -> Any:
        from ..credentials import (
            PROVIDERS, ProviderCredentialUnavailable, byok_keys, byok_unavailable_envs,
        )

        # Bounded timeout (caps the SDK's 600s read default) + max_retries=0 so OUR layer (the
        # Gateway/Driver resilience wrapper) is the single retry authority — no hidden 3× SDK stack.
        settings = get_settings()
        timeout = settings.provider_timeout()

        # BYOK: if the current request carries the user's own key for this provider, use an
        # EPHEMERAL client with it (never cached — a per-user key must not leak across requests).
        # ponytail: ephemeral client per request on BYOK; pool if p95 shows it.
        active_entry = entry or self.entry
        if active_entry.api_key_env in byok_unavailable_envs.get():
            provider = next((
                name for name, definition in PROVIDERS.items()
                if definition.api_key_env == active_entry.api_key_env
            ), active_entry.api_key_env)
            raise ProviderCredentialUnavailable((provider,), "configured_key_unavailable")
        scope = active_entry.credential_scope
        allow_byok = scope is None or scope.kind in ("user", "organization")
        override = byok_keys.get().get(active_entry.api_key_env) if allow_byok else None
        if override:
            from openai import AsyncOpenAI

            return AsyncOpenAI(
                base_url=active_entry.resolved_base_url,
                api_key=override,
                default_headers={"X-Title": "toto-gateway"},
                timeout=timeout, max_retries=0,
            )
        if self._client is None:
            from openai import AsyncOpenAI  # lazy import — no key needed at import time

            # base_url + api_key_env come from the catalog entry, so any OpenAI-compatible
            # provider (OpenRouter, Together, Fireworks, a direct lab) is a catalog entry, not
            # a code change. base_url=None → OpenAI default.
            self._client = AsyncOpenAI(
                base_url=active_entry.resolved_base_url,
                api_key=os.environ.get(active_entry.api_key_env),
                default_headers={"X-Title": "toto-gateway"},
                timeout=timeout, max_retries=0,
            )
        return self._client

    async def chat(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> ChatCompletionResponse:
        params = req.passthrough_params()
        if not entry.tools:
            params = _strip_tool_params(params)
        # OpenRouter usage accounting + cache-affinity hints (pops any client cache keys off params).
        extra = _provider_extra(entry, req, params)
        client = self._get_client(entry)
        messages_wire = _openrouter_cache_inject(
            entry, req, _wire_messages(req.messages, native_tools=entry.tools)
        )
        body = dict(
            model=entry.effective_upstream_model,
            messages=messages_wire,
            **extra,
            **params,
        )
        # Retry ONLY OpenRouter's empty-body-200 quirk here. Real exceptions (429/5xx/timeouts)
        # propagate immediately — Driver._call is the single retry authority, with backoff;
        # an in-runner blind retry would hammer a rate-limited endpoint before backoff starts.
        resp = None
        for attempt in range(3):
            resp = await client.chat.completions.create(**body)
            # A pure tool-call reply legitimately has empty content — never "empty body".
            if resp.choices and ((resp.choices[0].message.content or "").strip()
                                 or getattr(resp.choices[0].message, "tool_calls", None)):
                break
        # Re-stamp model to the catalog alias (never expose upstream model to callers).
        cached, cache_write, cost = _cache_details(resp.usage) if resp.usage else (0, 0, None)
        usage = Usage.of(
            prompt=resp.usage.prompt_tokens if resp.usage else 0,
            completion=resp.usage.completion_tokens if resp.usage else 0,
            cached=cached, cache_write=cache_write, cost_upstream=cost,
        )
        content = resp.choices[0].message.content or "" if resp.choices else ""
        finish_reason = resp.choices[0].finish_reason or "stop" if resp.choices else "stop"
        tool_calls = _dump_tool_calls(
            getattr(resp.choices[0].message, "tool_calls", None)) if resp.choices else None
        served_model, provider, gid = _upstream_provenance(resp)
        return ChatCompletionResponse.simple(
            model=entry.id,
            content=content,
            usage=usage,
            finish_reason=finish_reason,
            upstream_model=served_model,
            # Actual upstream response provenance. The selected offer transport lives separately
            # in trace/x_toto; never overwrite what the provider reported with routing metadata.
            provider=provider,
            generation_id=gid,
            tool_calls=tool_calls,
            identity_id=entry.identity_id,
            offer_id=entry.offer_id,
            credential_scope=entry.credential_scope_label,
        )

    async def stream(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> AsyncIterator[ChatCompletionChunk]:
        params = req.passthrough_params()
        if not entry.tools:
            params = _strip_tool_params(params)
        extra = _provider_extra(entry, req, params)  # usage accounting + cache affinity
        cid = f"chatcmpl-oai-{entry.effective_upstream_model}"
        messages_wire = _openrouter_cache_inject(
            entry, req, _wire_messages(req.messages, native_tools=entry.tools)
        )
        stream = await self._get_client(entry).chat.completions.create(
            model=entry.effective_upstream_model,
            messages=messages_wire,
            stream=True,
            stream_options={"include_usage": True},
            **extra,
            **params,
        )

        input_tokens: int = 0
        output_tokens: int = 0
        cached_tokens: int = 0
        cache_write_tokens: int = 0
        upstream_cost: float | None = None
        finish_reason: str = "stop"

        yield _stamp_route_provenance(
            ChatCompletionChunk.role_chunk(id=cid, model=entry.id), entry
        )

        # Liveness: a reasoning model can emit non-content deltas (reasoning/thinking) for a long
        # time; if we swallow them silently the gateway's stall timer (and the client's own idle
        # timeout) sees a dead stream and kills a healthy request. Forward reasoning text as a
        # `reasoning` delta extra (OpenRouter convention — clients that render it get it free) and
        # emit a throttled empty-delta keepalive for anything else.
        last_emit = time.monotonic()
        async for chunk in stream:
            # Trailing usage chunk from OpenAI (when stream_options.include_usage=True).
            if chunk.usage is not None:
                input_tokens = chunk.usage.prompt_tokens or 0
                output_tokens = chunk.usage.completion_tokens or 0
                cached_tokens, cache_write_tokens, upstream_cost = _cache_details(chunk.usage)
                continue  # hold — we emit a usage chunk ourselves at the end

            emitted = False
            for choice in chunk.choices:
                delta = choice.delta
                if delta.content:
                    yield _stamp_route_provenance(
                        ChatCompletionChunk.content_chunk(
                            id=cid, model=entry.id, text=delta.content
                        ),
                        entry,
                    )
                    emitted = True
                tool_calls = _dump_tool_calls(getattr(delta, "tool_calls", None))
                if tool_calls:
                    yield _stamp_route_provenance(
                        ChatCompletionChunk.tool_calls_chunk(
                            id=cid, model=entry.id, tool_calls=tool_calls
                        ),
                        entry,
                    )
                    emitted = True
                reasoning = getattr(delta, "reasoning", None) \
                    or getattr(delta, "reasoning_content", None)
                if isinstance(reasoning, str) and reasoning:
                    yield _stamp_route_provenance(
                        ChatCompletionChunk(
                            id=cid,
                            model=entry.id,
                            choices=[ChunkChoice(delta=ChunkDelta(reasoning=reasoning))],
                        ),
                        entry,
                    )
                    emitted = True
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
            if not emitted and time.monotonic() - last_emit >= 1.0:
                yield _stamp_route_provenance(
                    ChatCompletionChunk(
                        id=cid,
                        model=entry.id,
                        choices=[ChunkChoice(delta=ChunkDelta())],
                    ),
                    entry,
                )
                emitted = True
            if emitted:
                last_emit = time.monotonic()

        yield _stamp_route_provenance(
            ChatCompletionChunk.stop_chunk(
                id=cid, model=entry.id, finish_reason=finish_reason
            ),
            entry,
        )
        # Always emit a trailing usage chunk (base.py streaming contract).
        yield _stamp_route_provenance(
            ChatCompletionChunk.usage_chunk(
                id=cid,
                model=entry.id,
                usage=Usage.of(
                    prompt=input_tokens,
                    completion=output_tokens,
                    cached=cached_tokens,
                    cache_write=cache_write_tokens,
                    cost_upstream=upstream_cost,
                ),
            ),
            entry,
        )

    def models(self) -> list[Model]:
        return [
            Model(
                id=self.entry.id,
                owned_by="openai",
                lane=self.entry.lane,
                residency_class=self.entry.residency_class,
                identity_id=self.entry.identity_id,
                offer_id=self.entry.offer_id,
                provider=self.entry.provider,
                credential_scope=self.entry.credential_scope_label,
                modalities=self.entry.modalities,
                supported_parameters=self.entry.supported_parameters,
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
