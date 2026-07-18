"""MLX local lane adapter — OpenAI-compatible HTTP proxy.

Talks to any OpenAI-compatible upstream running locally: mlx_lm.server (Apple Silicon),
LM Studio, Ollama, etc. The upstream is already OpenAI-shaped, so this adapter is mostly
a faithful HTTP proxy with careful SSE parsing and tolerant usage handling.

Key behaviours
--------------
- Re-stamps `model=entry.id` on every response so clients see the catalog alias, not the raw
  upstream model name (which may be a long HuggingFace path or a quant tag).
- Sends `entry.effective_upstream_model` in the upstream request body — the upstream knows its
  own model name.
- Requests a usage chunk via `stream_options={"include_usage": true}` on streamed calls; many
  OpenAI-compatible servers honour it.  When the upstream omits usage entirely, we simply don't
  emit a usage chunk — the gateway estimates from streamed text and flags `cost_estimated=True`
  (Runner contract, base.py module docstring).
- Tolerant SSE parser: blank lines, unexpected prefixes, and malformed JSON are all swallowed;
  the `[DONE]` sentinel terminates the loop cleanly.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from ..catalog import CatalogEntry
from ..config import get_settings
from ..schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Choice,
    Model,
    ResponseMessage,
    Usage,
)
from .base import CartridgeManifest, NotImplementedInPhase0, Telemetry

log = logging.getLogger(__name__)


class MLXRunner:
    """OpenAI-compatible proxy for a local MLX (or similar) inference server."""

    def __init__(
        self,
        entry: CatalogEntry,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.entry = entry
        self.runner_id = f"mlx-{entry.id}"
        # If a client is injected (tests), use it as-is so respx mocking works.
        # Otherwise, build one eagerly bound to the configured base URL. Local generation is
        # genuinely slow, so it uses the longer, independently-tunable local read budget
        # (provider_read_timeout_local) rather than the cloud default.
        self._client = client or httpx.AsyncClient(
            base_url=entry.endpoint,
            timeout=get_settings().provider_timeout(local=True),
        )

    # ------------------------------------------------------------------
    # Core inference methods
    # ------------------------------------------------------------------

    async def chat(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> ChatCompletionResponse:
        """Non-streaming completion — proxy to upstream, re-stamp model id."""
        body = {
            "model": entry.effective_upstream_model,
            "messages": [m.model_dump(exclude_none=True) for m in req.messages],
            **req.passthrough_params(),
        }
        resp = await self._client.post("/chat/completions", json=body)
        resp.raise_for_status()
        raw = resp.json()

        # Parse usage — absent when the upstream omits it; leave at 0 so the gateway
        # knows to estimate (it checks total_tokens == 0 or cost_estimated flag).
        usage = Usage()
        if raw_usage := raw.get("usage"):
            usage = Usage(
                prompt_tokens=raw_usage.get("prompt_tokens", 0),
                completion_tokens=raw_usage.get("completion_tokens", 0),
                total_tokens=raw_usage.get("total_tokens", 0),
            )

        choices = [
            Choice(
                index=c.get("index", i),
                message=ResponseMessage(
                    role=c.get("message", {}).get("role", "assistant"),
                    content=c.get("message", {}).get("content"),
                ),
                finish_reason=c.get("finish_reason", "stop"),
            )
            for i, c in enumerate(raw.get("choices", []))
        ]

        return ChatCompletionResponse(
            id=raw.get("id", f"chatcmpl-mlx-{entry.id}"),
            created=raw.get("created", 0),
            # Re-stamp: client sees our catalog alias, not the upstream name/path.
            model=entry.id,
            choices=choices,
            usage=usage,
        )

    async def stream(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Streaming completion — SSE proxy with tolerant parsing."""
        body = {
            "model": entry.effective_upstream_model,
            "messages": [m.model_dump(exclude_none=True) for m in req.messages],
            "stream": True,
            # Ask the upstream for a trailing usage chunk; many servers honour this.
            "stream_options": {"include_usage": True},
            **req.passthrough_params(),
        }

        cid: str | None = None
        saw_usage = False

        async with self._client.stream("POST", "/chat/completions", json=body) as resp:
            resp.raise_for_status()
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break

                try:
                    chunk_data = json.loads(payload)
                except json.JSONDecodeError:
                    log.debug("mlx-runner: skipping non-JSON SSE line: %r", payload)
                    continue

                # Grab the stable chunk id from the first real chunk.
                if cid is None:
                    cid = chunk_data.get("id") or f"chatcmpl-mlx-{entry.id}"

                choices = chunk_data.get("choices", [])

                # --- usage chunk (trailing) ---
                if not choices and chunk_data.get("usage"):
                    u = chunk_data["usage"]
                    usage = Usage(
                        prompt_tokens=u.get("prompt_tokens", 0),
                        completion_tokens=u.get("completion_tokens", 0),
                        total_tokens=u.get("total_tokens", 0),
                    )
                    yield ChatCompletionChunk.usage_chunk(id=cid, model=entry.id, usage=usage)
                    saw_usage = True
                    continue

                # --- normal delta chunks ---
                for c in choices:
                    delta = c.get("delta", {})
                    finish_reason = c.get("finish_reason")

                    if delta.get("role"):
                        yield ChatCompletionChunk.role_chunk(id=cid, model=entry.id)
                    if delta.get("content"):
                        yield ChatCompletionChunk.content_chunk(
                            id=cid, model=entry.id, text=delta["content"]
                        )
                    if finish_reason:
                        yield ChatCompletionChunk.stop_chunk(
                            id=cid, model=entry.id, finish_reason=finish_reason
                        )

                # Some servers embed usage in the LAST choices-bearing chunk.
                if not saw_usage and chunk_data.get("usage"):
                    u = chunk_data["usage"]
                    usage = Usage(
                        prompt_tokens=u.get("prompt_tokens", 0),
                        completion_tokens=u.get("completion_tokens", 0),
                        total_tokens=u.get("total_tokens", 0),
                    )
                    yield ChatCompletionChunk.usage_chunk(id=cid, model=entry.id, usage=usage)
                    saw_usage = True

        # If the upstream never reported usage, we emit nothing — the gateway will estimate.

    def models(self) -> list[Model]:
        """One model card for the catalog entry this runner serves."""
        return [
            Model(
                id=self.entry.id,
                owned_by="mlx-local",
                lane=self.entry.lane,
                residency_class=self.entry.residency_class,
            )
        ]

    # ------------------------------------------------------------------
    # Appliance-management surface — stubbed in Phase 0
    # ------------------------------------------------------------------

    def cartridge_manifest(self) -> CartridgeManifest:
        return CartridgeManifest(base_model=self.entry.effective_upstream_model)

    async def load(self, cartridge_ref: str) -> None:
        raise NotImplementedInPhase0("cartridge load is Phase 2")

    async def unload(self, cartridge_ref: str) -> None:
        raise NotImplementedInPhase0("cartridge unload is Phase 2")

    def health(self) -> Telemetry:
        return Telemetry(healthy=True)
