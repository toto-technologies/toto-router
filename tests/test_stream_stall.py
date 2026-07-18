"""P5 — stream stall / first-token timeout (Wave 1 provider-I/O hardening).

A provider that opens the SSE then goes silent would otherwise hold the concurrency slot for the
full read timeout — a slow-drip capacity leak. Gateway.stream now bounds every inter-chunk gap
(including first-token) by stream_stall_timeout: on a stall it closes the upstream and finalizes
the trace as error=stream_stall, keeping whatever partial usage it accounted.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.gateway import Gateway, StreamStallError
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.schemas import ChatCompletionChunk, ChatCompletionRequest, Message
from toto_gateway.trace import MemoryTraceWriter

_M = "wire-a"


def _req() -> ChatCompletionRequest:
    return ChatCompletionRequest(model=_M, messages=[Message(role="user", content="x")])


def _cat() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id=_M, lane="frontier", endpoint="openai", residency_class="cloud")])


class _StallRunner:
    """Yields a role chunk (opens the stream) then hangs forever — the classic silent-after-open
    provider. Records whether the stream was closed (aclose) so we can prove we release upstream."""

    def __init__(self, entry) -> None:
        self.entry = entry
        self.runner_id = "stall"
        self.closed = False

    async def stream(self, req, entry):
        try:
            yield ChatCompletionChunk.role_chunk(id="c", model=entry.id)
            await asyncio.sleep(3600)  # stall past any sane deadline
            yield ChatCompletionChunk.content_chunk(id="c", model=entry.id, text="never")
        finally:
            self.closed = True


def _gw(runner, **kw) -> Gateway:
    reg = RunnerRegistry(factory=lambda e: runner)
    return Gateway(catalog=_cat(), registry=reg, writer=MemoryTraceWriter(), **kw)


async def test_stalled_stream_aborts_within_deadline_and_finalizes_error():
    runner = _StallRunner(_cat().get(_M))
    gw = _gw(runner, stream_stall_timeout=0.05)
    traces = []
    got = []
    t0 = time.perf_counter()
    with pytest.raises(StreamStallError):
        async for ch in gw.stream(_req(), on_trace=traces.append):
            got.append(ch)
    elapsed = time.perf_counter() - t0

    assert elapsed < 1.0, elapsed              # abandoned in ~0.05s, nowhere near a read timeout
    assert len(got) == 1                        # the role chunk arrived before the stall
    assert runner.closed                        # upstream stream was closed (slot released)
    assert traces and traces[0].status == "error" and traces[0].error == "stream_stall"


async def test_healthy_stream_is_unaffected_by_the_deadline():
    """A stream that keeps producing within the budget completes normally — the deadline only
    fires on an actual stall."""

    class _OKRunner:
        runner_id = "ok"

        def __init__(self, entry):
            self.entry = entry

        async def stream(self, req, entry):
            yield ChatCompletionChunk.role_chunk(id="c", model=entry.id)
            yield ChatCompletionChunk.content_chunk(id="c", model=entry.id, text="hello")
            yield ChatCompletionChunk.stop_chunk(id="c", model=entry.id, finish_reason="stop")

    gw = _gw(_OKRunner(_cat().get(_M)), stream_stall_timeout=0.05)
    traces = []
    text = []
    async for ch in gw.stream(_req(), on_trace=traces.append):
        for c in ch.choices:
            if c.delta and c.delta.content:
                text.append(c.delta.content)
    assert "".join(text) == "hello"
    assert traces and traces[0].status == "ok"
