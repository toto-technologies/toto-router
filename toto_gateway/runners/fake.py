"""Deterministic fake runner — the offline spine.

Runs anywhere with no secrets and no GPU. Echoes a deterministic, useful completion so the
entire pipeline (ingest -> resolve -> dispatch -> stream tee -> trace) is exercisable offline,
in tests, and in the Phase-0 exit-criterion demo. Reports exact usage so cost is never estimated.
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator

from ..catalog import CatalogEntry
from ..schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Model,
    Usage,
)
from ..tokens import estimate_prompt_tokens, estimate_tokens
from .base import CartridgeManifest, NotImplementedInPhase0, Telemetry


def _completion_text(req: ChatCompletionRequest, entry: CatalogEntry) -> str:
    last_user = next((m for m in reversed(req.messages) if m.role == "user"), None)
    prompt = last_user.text() if last_user else ""
    return f"[{entry.lane}:{entry.id}] received {len(prompt)} chars. echo: {prompt}".strip()


class FakeRunner:
    """A self-contained OpenAI-compatible echo runner."""

    def __init__(self, entry: CatalogEntry) -> None:
        self.entry = entry
        self.runner_id = f"fake-{entry.id}"

    def _usage(self, req: ChatCompletionRequest, text: str) -> Usage:
        return Usage.of(estimate_prompt_tokens(req.messages), estimate_tokens(text))

    async def chat(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> ChatCompletionResponse:
        text = _completion_text(req, entry)
        return ChatCompletionResponse.simple(
            model=entry.id, content=text, usage=self._usage(req, text)
        )

    async def stream(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> AsyncIterator[ChatCompletionChunk]:
        text = _completion_text(req, entry)
        cid = f"chatcmpl-fake-{abs(hash((entry.id, text))) % (10**12)}"
        yield ChatCompletionChunk.role_chunk(id=cid, model=entry.id)
        # ponytail: dev-only streaming-QA lever. TOTO_GW_FAKE_DELAY_MS spaces the word chunks over
        # real time so the driver's time-based delta flush emits several answer_delta events and an
        # interrupt can land mid-stream. 0 (default, unset) = today's instant single-flush behavior.
        delay = float(os.environ.get("TOTO_GW_FAKE_DELAY_MS") or 0) / 1000.0
        for word in text.split(" "):
            if delay:
                await asyncio.sleep(delay)
            yield ChatCompletionChunk.content_chunk(id=cid, model=entry.id, text=word + " ")
        yield ChatCompletionChunk.stop_chunk(id=cid, model=entry.id)
        # Always emit a trailing usage chunk so the gateway can account EXACTLY. The gateway
        # forwards it to the client only when stream_options.include_usage was requested.
        yield ChatCompletionChunk.usage_chunk(id=cid, model=entry.id, usage=self._usage(req, text))

    def models(self) -> list[Model]:
        return [
            Model(
                id=self.entry.id,
                owned_by="toto-fake",
                lane=self.entry.lane,
                residency_class=self.entry.residency_class,
            )
        ]

    # --- stubbed contract ----------------------------------------------------

    def cartridge_manifest(self) -> CartridgeManifest:
        return CartridgeManifest(base_model=self.entry.id)

    async def load(self, cartridge_ref: str) -> None:
        raise NotImplementedInPhase0("cartridge load is Phase 2")

    async def unload(self, cartridge_ref: str) -> None:
        raise NotImplementedInPhase0("cartridge unload is Phase 2")

    def health(self) -> Telemetry:
        return Telemetry(healthy=True)
