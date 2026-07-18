"""Tests for toto_gateway.gateway.Gateway — dispatch, accounting, trace invariants.

All tests use the fake lane (FakeRunner) — no network, no secrets required.
"""

from __future__ import annotations

import pytest

from toto_gateway.catalog import Catalog, UnknownModelError
from toto_gateway.gateway import Gateway
from toto_gateway.runners.fake import FakeRunner
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Message,
    StreamOptions,
)
from toto_gateway.trace import MemoryTraceWriter


def _gw(catalog: Catalog, writer: MemoryTraceWriter | None = None) -> tuple[Gateway, MemoryTraceWriter]:
    if writer is None:
        writer = MemoryTraceWriter()
    registry = RunnerRegistry(factory=lambda entry: FakeRunner(entry))
    gw = Gateway(catalog=catalog, registry=registry, writer=writer)
    return gw, writer


def _req(model: str = "echo-local", content: str = "hello", *, stream: bool = False,
         include_usage: bool = False) -> ChatCompletionRequest:
    req = ChatCompletionRequest(model=model, messages=[Message(role="user", content=content)])
    req.stream = stream
    if include_usage:
        req.stream_options = StreamOptions(include_usage=True)
    return req


# --- non-streaming complete() ---


@pytest.mark.asyncio
async def test_complete_returns_response(catalog: Catalog):
    gw, _ = _gw(catalog)
    result = await gw.complete(_req("echo-local", "hi there"))
    assert result.response is not None
    assert isinstance(result.response, ChatCompletionResponse)
    assert result.response.choices


@pytest.mark.asyncio
async def test_complete_response_content_contains_echo(catalog: Catalog):
    """FakeRunner echoes the prompt — content must be non-empty."""
    gw, _ = _gw(catalog)
    result = await gw.complete(_req("echo-local", "the quick brown fox"))
    content = result.response.choices[0].message.content
    assert content and "the quick brown fox" in content


@pytest.mark.asyncio
async def test_complete_usage_populated(catalog: Catalog):
    """Non-streaming call: usage must have non-zero tokens (FakeRunner always reports usage)."""
    gw, _ = _gw(catalog)
    result = await gw.complete(_req("echo-local", "test token counting"))
    usage = result.response.usage
    assert usage.prompt_tokens > 0
    assert usage.completion_tokens > 0
    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens


@pytest.mark.asyncio
async def test_complete_cost_estimated_false(catalog: Catalog):
    """FakeRunner always reports usage, so cost_estimated must be False."""
    gw, writer = _gw(catalog)
    await gw.complete(_req("echo-local", "check estimated flag"))
    trace = writer.records[-1]
    assert trace.cost_estimated is False


@pytest.mark.asyncio
async def test_complete_residency_in_perimeter(catalog: Catalog):
    """echo-local is in_perimeter — trace must reflect this."""
    gw, writer = _gw(catalog)
    await gw.complete(_req("echo-local", "residency check"))
    trace = writer.records[-1]
    assert trace.residency_class == "in_perimeter"


@pytest.mark.asyncio
async def test_complete_latency_fields_populated(catalog: Catalog):
    """latency_ms_total and latency_ms_gateway_overhead must be >= 0 after a call."""
    gw, writer = _gw(catalog)
    await gw.complete(_req("echo-local", "latency test"))
    trace = writer.records[-1]
    assert trace.latency_ms_total is not None
    assert trace.latency_ms_gateway_overhead is not None
    assert trace.latency_ms_total >= 0
    assert trace.latency_ms_gateway_overhead >= 0


@pytest.mark.asyncio
async def test_complete_gateway_overhead_lte_total(catalog: Catalog):
    """Gateway overhead must be <= total latency (overhead is total minus upstream)."""
    gw, writer = _gw(catalog)
    await gw.complete(_req("echo-local", "overhead sanity"))
    trace = writer.records[-1]
    assert trace.latency_ms_gateway_overhead <= trace.latency_ms_total


@pytest.mark.asyncio
async def test_complete_frontier_baseline_set(catalog: Catalog):
    """frontier_baseline_usd is set (catalog has a frontier-residency entry)."""
    gw, writer = _gw(catalog)
    await gw.complete(_req("echo-local", "baseline test"))
    trace = writer.records[-1]
    assert trace.frontier_baseline_usd is not None


@pytest.mark.asyncio
async def test_complete_trace_written_once(catalog: Catalog):
    """Exactly one trace record per complete() call."""
    gw, writer = _gw(catalog)
    assert len(writer.records) == 0
    await gw.complete(_req("echo-local", "trace count"))
    assert len(writer.records) == 1


@pytest.mark.asyncio
async def test_complete_local_zero_cost(catalog: Catalog):
    """echo-local has $0 pricing — cost_usd must be 0.0."""
    gw, writer = _gw(catalog)
    await gw.complete(_req("echo-local", "zero cost local lane"))
    assert writer.records[-1].cost_usd == 0.0


@pytest.mark.asyncio
async def test_complete_unknown_model_raises(catalog: Catalog):
    """complete() with an unknown model raises UnknownModelError."""
    gw, _ = _gw(catalog)
    with pytest.raises(UnknownModelError):
        await gw.complete(_req("ghost-model-xyz", "unknown"))


# --- streaming stream() ---


async def _collect_stream(gw: Gateway, req: ChatCompletionRequest) -> list[ChatCompletionChunk]:
    chunks: list[ChatCompletionChunk] = []
    async for chunk in gw.stream(req):
        chunks.append(chunk)
    return chunks


@pytest.mark.asyncio
async def test_stream_chunk_ordering(catalog: Catalog):
    """Chunks arrive: role → content(s) → stop. No usage chunk when include_usage=False."""
    gw, _ = _gw(catalog)
    req = _req("echo-cloud", "stream order check", stream=True, include_usage=False)
    chunks = await _collect_stream(gw, req)

    # No usage chunk should be forwarded when include_usage=False
    usage_chunks = [c for c in chunks if c.usage is not None]
    assert usage_chunks == [], "no usage chunk should be forwarded when include_usage=False"

    # First non-empty chunk has a role delta
    role_chunks = [c for c in chunks if c.choices and c.choices[0].delta.role == "assistant"]
    assert len(role_chunks) >= 1, "first chunk must carry role='assistant'"

    # At least one content chunk
    content_chunks = [
        c for c in chunks if c.choices and c.choices[0].delta.content is not None
    ]
    assert len(content_chunks) >= 1, "must have content chunks"

    # Last non-[DONE] chunk has finish_reason
    stop_chunks = [
        c for c in chunks if c.choices and c.choices[0].finish_reason is not None
    ]
    assert len(stop_chunks) >= 1, "must have a stop chunk"


@pytest.mark.asyncio
async def test_stream_with_include_usage_delivers_usage_chunk(catalog: Catalog):
    """When include_usage=True, a usage chunk IS forwarded to the client."""
    gw, writer = _gw(catalog)
    req = _req("echo-cloud", "include usage test", stream=True, include_usage=True)
    chunks = await _collect_stream(gw, req)

    usage_chunks = [c for c in chunks if c.usage is not None]
    assert len(usage_chunks) >= 1, "usage chunk must be forwarded when include_usage=True"


@pytest.mark.asyncio
async def test_stream_with_include_usage_trace_has_exact_usage(catalog: Catalog):
    """With include_usage=True, trace has exact (not estimated) usage and cost_estimated=False."""
    gw, writer = _gw(catalog)
    req = _req("echo-cloud", "trace exact usage", stream=True, include_usage=True)
    await _collect_stream(gw, req)

    assert len(writer.records) == 1
    trace = writer.records[0]
    assert trace.cost_estimated is False
    assert trace.tokens_prompt > 0
    assert trace.tokens_completion > 0


@pytest.mark.asyncio
async def test_stream_without_include_usage_trace_still_has_usage(catalog: Catalog):
    """Critical invariant: even without include_usage, trace has usage with cost_estimated=False.

    The FakeRunner always emits a trailing usage chunk. The gateway MUST consume it for
    accounting even when stripping it from the client-visible stream.
    """
    gw, writer = _gw(catalog)
    req = _req("echo-cloud", "accounting without forwarding", stream=True, include_usage=False)
    chunks = await _collect_stream(gw, req)

    # Assert no usage chunk was forwarded to client
    client_usage_chunks = [c for c in chunks if c.usage is not None]
    assert client_usage_chunks == [], "usage chunk must NOT be forwarded to client"

    # Assert trace still has exact usage (FakeRunner always emits trailing usage)
    assert len(writer.records) == 1
    trace = writer.records[0]
    assert trace.cost_estimated is False, (
        "trace must have exact usage even when usage chunk was not forwarded to client"
    )
    assert trace.tokens_prompt > 0
    assert trace.tokens_completion > 0


@pytest.mark.asyncio
async def test_stream_frontier_residency(catalog: Catalog):
    """echo-cloud trace reflects frontier residency."""
    gw, writer = _gw(catalog)
    req = _req("echo-cloud", "residency", stream=True)
    await _collect_stream(gw, req)
    trace = writer.records[-1]
    assert trace.residency_class == "cloud"


@pytest.mark.asyncio
async def test_stream_frontier_has_positive_cost(catalog: Catalog):
    """echo-cloud has non-zero pricing — streaming call must produce cost_usd > 0."""
    gw, writer = _gw(catalog)
    req = _req("echo-cloud", "cost check on frontier stream", stream=True)
    await _collect_stream(gw, req)
    trace = writer.records[-1]
    assert trace.cost_usd > 0.0


@pytest.mark.asyncio
async def test_stream_latency_populated(catalog: Catalog):
    """Streaming trace has populated latency fields."""
    gw, writer = _gw(catalog)
    req = _req("echo-local", "latency stream", stream=True)
    await _collect_stream(gw, req)
    trace = writer.records[-1]
    assert trace.latency_ms_total is not None and trace.latency_ms_total >= 0
    assert trace.latency_ms_gateway_overhead is not None and trace.latency_ms_gateway_overhead >= 0


@pytest.mark.asyncio
async def test_stream_trace_written_exactly_once(catalog: Catalog):
    """Exactly one trace record per stream() call."""
    gw, writer = _gw(catalog)
    req = _req("echo-local", "single trace stream", stream=True)
    await _collect_stream(gw, req)
    assert len(writer.records) == 1


# --- error path ---


@pytest.mark.asyncio
async def test_complete_error_path_trace_records_error(catalog: Catalog):
    """When runner raises mid-call, trace status='error' and error field is populated."""

    class ErrorRunner:
        runner_id = "error-runner"

        async def chat(self, req, entry):
            raise RuntimeError("upstream exploded")

        async def stream(self, req, entry):
            raise RuntimeError("stream exploded")
            yield  # make it a generator

        def models(self):
            return []

    writer = MemoryTraceWriter()
    registry = RunnerRegistry(factory=lambda entry: ErrorRunner())
    gw = Gateway(catalog=catalog, registry=registry, writer=writer)

    with pytest.raises(RuntimeError):
        await gw.complete(_req("echo-local", "error test"))

    assert len(writer.records) == 1
    trace = writer.records[0]
    assert trace.status == "error"
    assert trace.error is not None
    assert "RuntimeError" in trace.error


@pytest.mark.asyncio
async def test_stream_error_path_trace_records_error(catalog: Catalog):
    """When runner raises mid-stream, trace status='error'."""

    class MidStreamErrorRunner:
        runner_id = "mid-stream-error"

        async def chat(self, req, entry):
            raise RuntimeError("nope")

        async def stream(self, req, entry):
            yield ChatCompletionChunk.role_chunk(id="cid", model=entry.id)
            raise RuntimeError("explodes mid-stream")

        def models(self):
            return []

    writer = MemoryTraceWriter()
    registry = RunnerRegistry(factory=lambda entry: MidStreamErrorRunner())
    gw = Gateway(catalog=catalog, registry=registry, writer=writer)
    req = _req("echo-local", "mid-stream error", stream=True)

    with pytest.raises(RuntimeError):
        async for _ in gw.stream(req):
            pass

    assert len(writer.records) == 1
    trace = writer.records[0]
    assert trace.status == "error"


# --- exactly-once trace on early consumer exit ---


@pytest.mark.asyncio
async def test_stream_exactly_once_trace_on_early_exit(catalog: Catalog):
    """Trace is written exactly once even when the consumer breaks out early."""
    gw, writer = _gw(catalog)
    req = _req("echo-local", "early exit test", stream=True)

    gen = gw.stream(req)
    # Consume only the first chunk, then close the generator
    async for _ in gen:
        break  # stop after first chunk
    await gen.aclose()

    # Allow a brief moment for the finally block to flush
    import asyncio
    await asyncio.sleep(0)

    # Exactly one trace record must have been written
    assert len(writer.records) == 1


# --- unknown model ---


@pytest.mark.asyncio
async def test_stream_unknown_model_raises(catalog: Catalog):
    """stream() with an unknown model raises UnknownModelError."""
    gw, _ = _gw(catalog)
    req = _req("ghost-xyz", "unknown model stream", stream=True)
    with pytest.raises(UnknownModelError):
        async for _ in gw.stream(req):
            pass


# --- task_id propagation ---


@pytest.mark.asyncio
async def test_complete_task_id_in_trace(catalog: Catalog):
    """task_id passed to complete() appears in the trace record."""
    gw, writer = _gw(catalog)
    await gw.complete(_req("echo-local"), task_id="task-research-42")
    assert writer.records[-1].task_id == "task-research-42"


@pytest.mark.asyncio
async def test_stream_task_id_in_trace(catalog: Catalog):
    """task_id passed to stream() appears in the trace record."""
    gw, writer = _gw(catalog)
    req = _req("echo-local", stream=True)
    async for _ in gw.stream(req, task_id="task-coding-7"):
        pass
    assert writer.records[-1].task_id == "task-coding-7"


@pytest.mark.asyncio
async def test_byok_request_bypasses_shared_cache(catalog: Catalog):
    """A BYOK-active request must neither read nor write the exact-match cache — its key has no
    per-user identity, so a hit would serve one user's BYOK-funded completion to another
    (sec review finding #1). Same prompt, once without BYOK (populates cache) then with BYOK set:
    the BYOK call must miss (execute the runner), and must not overwrite/serve the shared entry."""
    from toto_gateway.cache.exact import ExactCache
    from toto_gateway.credentials import byok_keys

    registry = RunnerRegistry(factory=lambda entry: FakeRunner(entry))
    gw = Gateway(catalog=catalog, registry=registry, writer=MemoryTraceWriter(), cache=ExactCache())

    # 1) plain request populates the cache; second identical call is a hit.
    await gw.complete(_req("echo-local", content="same"))
    r2 = await gw.complete(_req("echo-local", content="same"))
    assert r2.trace.cache_hit is True

    # 2) identical request with a BYOK key active → must be a MISS (runner ran, not served from cache).
    token = byok_keys.set({"OPENROUTER_API_KEY": "sk-user-byok"})
    try:
        r3 = await gw.complete(_req("echo-local", content="same"))
        assert r3.trace.cache_hit is False
    finally:
        byok_keys.reset(token)
