"""Tests for the ExactCache and OpenAIRunner, plus gateway-integration tests.

Self-contained — no conftest.py dependency. All OpenAI adapter tests use an
injected fake async OpenAI client; no network calls are made.
"""

from __future__ import annotations

import tempfile
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from toto_gateway.cache.exact import ExactCache
from toto_gateway.catalog import Catalog, CatalogEntry, Price
from toto_gateway.gateway import Gateway
from toto_gateway.runners.openai import OpenAIRunner
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Message,
    Usage,
)
from toto_gateway.trace import MemoryTraceWriter

CATALOG_PATH = "catalog.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    model_id: str = "gpt-4o",
    upstream: str = "gpt-4o",
    endpoint: str = "openai",
) -> CatalogEntry:
    return CatalogEntry(
        id=model_id,
        lane="frontier",
        endpoint=endpoint,
        residency_class="cloud",
        price_usd_per_1k=Price(prompt=2.5, completion=10.0),
        context_window=128000,
        upstream_model=upstream,
    )


def _req(
    model: str = "gpt-4o",
    content: str = "Hello",
    tenant: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ChatCompletionRequest:
    extra: dict = {}
    if tenant is not None:
        extra["tenant"] = tenant
    return ChatCompletionRequest(
        model=model,
        messages=[Message(role="user", content=content)],
        temperature=temperature,
        max_tokens=max_tokens,
        **extra,
    )


def _resp(content: str = "A response", model: str = "gpt-4o") -> ChatCompletionResponse:
    return ChatCompletionResponse.simple(
        model=model,
        content=content,
        usage=Usage.of(prompt=10, completion=5),
    )


# ---------------------------------------------------------------------------
# ExactCache — unit tests
# ---------------------------------------------------------------------------


class TestExactCache:
    def test_put_then_get_returns_equal_response(self):
        cache = ExactCache()
        req = _req()
        resp = _resp()
        cache.put(req, resp)
        result = cache.get(req)
        assert result is not None
        assert result.choices[0].message.content == resp.choices[0].message.content
        assert result.usage.prompt_tokens == resp.usage.prompt_tokens

    def test_different_model_is_a_miss(self):
        cache = ExactCache()
        req_a = _req(model="gpt-4o")
        req_b = _req(model="claude-sonnet-4.6")
        cache.put(req_a, _resp(model="gpt-4o"))
        assert cache.get(req_b) is None

    def test_different_messages_is_a_miss(self):
        cache = ExactCache()
        req_a = _req(content="Hello")
        req_b = _req(content="Goodbye")
        cache.put(req_a, _resp())
        assert cache.get(req_b) is None

    def test_different_tenant_is_a_miss(self):
        cache = ExactCache()
        req_a = _req(content="Hello", tenant="alice")
        req_b = _req(content="Hello", tenant="bob")
        cache.put(req_a, _resp())
        assert cache.get(req_b) is None

    def test_per_tenant_isolation_same_prompt(self):
        """Same prompt, different tenant — each tenant must see only their own hit."""
        cache = ExactCache()
        req_alice = _req(content="Ping", tenant="alice")
        req_bob = _req(content="Ping", tenant="bob")

        resp_alice = _resp(content="Alice's reply")
        resp_bob = _resp(content="Bob's reply")

        cache.put(req_alice, resp_alice)
        cache.put(req_bob, resp_bob)

        got_alice = cache.get(req_alice)
        got_bob = cache.get(req_bob)

        assert got_alice is not None
        assert got_bob is not None
        assert got_alice.choices[0].message.content == "Alice's reply"
        assert got_bob.choices[0].message.content == "Bob's reply"

    def test_get_returns_deep_copy(self):
        """Mutating the returned object must not corrupt the cache."""
        cache = ExactCache()
        req = _req()
        resp = _resp(content="Original")
        cache.put(req, resp)

        result = cache.get(req)
        assert result is not None
        # Mutate the returned copy
        result.choices[0].message.content = "Mutated"

        # The cache must still hold the original
        result2 = cache.get(req)
        assert result2 is not None
        assert result2.choices[0].message.content == "Original"

    def test_sqlite_backed_persists_across_instances(self):
        """SQLite-backed cache survives process-restart simulation (two instances same file)."""
        req = _req(content="Persisted?")
        resp = _resp(content="Yes, persisted.")

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        cache1 = ExactCache(sqlite_path=db_path)
        cache1.put(req, resp)

        # New instance pointing at the same file — should find the entry via SQLite.
        cache2 = ExactCache(sqlite_path=db_path)
        result = cache2.get(req)
        assert result is not None
        assert result.choices[0].message.content == "Yes, persisted."

    def test_default_tenant_when_no_tenant_field(self):
        """Requests with no tenant field resolve to 'default' and share a namespace."""
        cache = ExactCache()
        req_a = _req(content="Hello")   # no tenant → "default"
        req_b = _req(content="Hello")   # same → should hit
        cache.put(req_a, _resp())
        assert cache.get(req_b) is not None

    def test_fifo_eviction_respects_max_entries(self):
        """Cache evicts oldest entries when cap is reached."""
        cache = ExactCache(max_entries=3)
        reqs = [_req(content=f"msg-{i}") for i in range(4)]
        resps = [_resp(content=f"reply-{i}") for i in range(4)]

        for r, p in zip(reqs, resps):
            cache.put(r, p)

        # First entry should be evicted
        assert cache.get(reqs[0]) is None
        # Last three entries should still be present
        assert cache.get(reqs[1]) is not None
        assert cache.get(reqs[2]) is not None
        assert cache.get(reqs[3]) is not None


# ---------------------------------------------------------------------------
# OpenAIRunner — fake async OpenAI client
# ---------------------------------------------------------------------------


def _fake_chat_completion(
    content: str = "A response",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    model_id: str = "gpt-4o",
    finish_reason: str = "stop",
) -> Any:
    """Build a minimal fake openai.ChatCompletion object."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens

    message = MagicMock()
    message.content = content

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason

    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    resp.model = model_id
    return resp


async def _fake_stream_chunks(
    content: str = "hello world",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> AsyncIterator[Any]:
    """Yield OpenAI-shaped stream chunks including a trailing usage chunk."""

    def _chunk(content_text: str | None = None, finish_reason: str | None = None, usage=None) -> Any:
        delta = MagicMock()
        delta.content = content_text
        delta.role = None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason

        c = MagicMock()
        c.choices = [choice]
        c.usage = usage
        return c

    # Role chunk (no content, no finish_reason)
    yield _chunk()

    # Content chunks
    for word in content.split():
        yield _chunk(content_text=word + " ")

    # Stop chunk
    yield _chunk(finish_reason="stop")

    # Usage chunk (OpenAI sends this last when stream_options.include_usage=True)
    usage_obj = MagicMock()
    usage_obj.prompt_tokens = prompt_tokens
    usage_obj.completion_tokens = completion_tokens
    usage_chunk = MagicMock()
    usage_chunk.choices = []
    usage_chunk.usage = usage_obj
    yield usage_chunk


def _make_fake_openai_client(
    *,
    chat_content: str = "A response",
    chat_prompt_tokens: int = 10,
    chat_completion_tokens: int = 5,
    stream_content: str = "hello world",
    stream_prompt_tokens: int = 10,
    stream_completion_tokens: int = 5,
) -> Any:
    """Fake AsyncOpenAI client injectable into OpenAIRunner."""
    chat_resp = _fake_chat_completion(
        content=chat_content,
        prompt_tokens=chat_prompt_tokens,
        completion_tokens=chat_completion_tokens,
    )

    completions = AsyncMock()
    completions.create = AsyncMock(return_value=chat_resp)

    # Override for stream: async generator returned directly (not a context manager)
    async def _create_stream(**kwargs):
        async for chunk in _fake_stream_chunks(
            content=stream_content,
            prompt_tokens=stream_prompt_tokens,
            completion_tokens=stream_completion_tokens,
        ):
            yield chunk

    # The stream=True branch uses `async for chunk in await client.chat.completions.create(...)`
    # so create must return an async iterable when stream=True.
    original_create = completions.create

    async def smart_create(**kwargs):
        if kwargs.get("stream"):
            return _create_stream(**kwargs)
        return await original_create(**kwargs)

    completions.create = smart_create

    chat_ns = MagicMock()
    chat_ns.completions = completions

    client = MagicMock()
    client.chat = chat_ns
    return client


class TestOpenAIRunner:
    @pytest.mark.asyncio
    async def test_chat_returns_response(self):
        entry = _entry()
        client = _make_fake_openai_client(chat_content="Hello from GPT", chat_prompt_tokens=8, chat_completion_tokens=4)
        runner = OpenAIRunner(entry, client=client)

        result = await runner.chat(_req(), entry)

        assert result.choices[0].message.content == "Hello from GPT"

    @pytest.mark.asyncio
    async def test_chat_model_restamped_to_catalog_id(self):
        entry = _entry(model_id="gpt-4o", upstream="gpt-4o")
        client = _make_fake_openai_client()
        runner = OpenAIRunner(entry, client=client)

        result = await runner.chat(_req(), entry)

        assert result.model == "gpt-4o"  # catalog alias, not "gpt-4o"

    @pytest.mark.asyncio
    async def test_chat_exact_usage(self):
        entry = _entry()
        client = _make_fake_openai_client(chat_prompt_tokens=20, chat_completion_tokens=7)
        runner = OpenAIRunner(entry, client=client)

        result = await runner.chat(_req(), entry)

        assert result.usage.prompt_tokens == 20
        assert result.usage.completion_tokens == 7
        assert result.usage.total_tokens == 27

    @pytest.mark.asyncio
    async def test_chat_upstream_receives_effective_model(self):
        """The upstream call must use effective_upstream_model, not the catalog alias."""
        entry = _entry(model_id="gpt-4o", upstream="gpt-4o")
        called_with: dict = {}

        async def capture_create(**kwargs):
            called_with.update(kwargs)
            return _fake_chat_completion()

        client = _make_fake_openai_client()
        # Swap in our capturing version
        client.chat.completions.create = capture_create

        runner = OpenAIRunner(entry, client=client)
        await runner.chat(_req(), entry)

        assert called_with.get("model") == "gpt-4o"

    @pytest.mark.asyncio
    async def test_stream_yields_content(self):
        entry = _entry()
        client = _make_fake_openai_client(stream_content="foo bar")
        runner = OpenAIRunner(entry, client=client)

        chunks = [c async for c in runner.stream(_req(), entry)]

        content_chunks = [c for c in chunks if c.choices and c.choices[0].delta.content]
        texts = "".join(c.choices[0].delta.content for c in content_chunks)
        assert "foo" in texts
        assert "bar" in texts

    @pytest.mark.asyncio
    async def test_stream_model_restamped(self):
        entry = _entry(model_id="gpt-4o", upstream="gpt-4o")
        client = _make_fake_openai_client()
        runner = OpenAIRunner(entry, client=client)

        chunks = [c async for c in runner.stream(_req(), entry)]
        assert all(c.model == "gpt-4o" for c in chunks)

    @pytest.mark.asyncio
    async def test_stream_trailing_usage_chunk(self):
        entry = _entry()
        client = _make_fake_openai_client(stream_prompt_tokens=12, stream_completion_tokens=6)
        runner = OpenAIRunner(entry, client=client)

        chunks = [c async for c in runner.stream(_req(), entry)]

        # Last chunk must be the usage chunk with empty choices
        last = chunks[-1]
        assert last.choices == []
        assert last.usage is not None
        assert last.usage.prompt_tokens == 12
        assert last.usage.completion_tokens == 6
        assert last.usage.total_tokens == 18

    @pytest.mark.asyncio
    async def test_chat_captures_upstream_provenance(self):
        """resp.model (served model), resp.id (generation id), and provider are captured onto the
        response even though model is re-stamped to the catalog alias for callers."""
        entry = _entry(model_id="or-sonnet-5", upstream="anthropic/claude-sonnet-5")
        resp = _fake_chat_completion(model_id="anthropic/claude-sonnet-5")
        resp.id = "gen-abc123"
        resp.provider = "Anthropic"

        async def create(**kwargs):
            return resp

        client = _make_fake_openai_client()
        client.chat.completions.create = create
        runner = OpenAIRunner(entry, client=client)

        result = await runner.chat(_req(), entry)

        assert result.model == "or-sonnet-5"  # caller-facing alias unchanged
        assert result.upstream_model == "anthropic/claude-sonnet-5"
        assert result.generation_id == "gen-abc123"
        assert result.provider == "Anthropic"

    @pytest.mark.asyncio
    async def test_chat_provider_from_model_extra(self):
        """OpenRouter's non-standard `provider` lands in pydantic model_extra — read it there."""
        entry = _entry()
        resp = _fake_chat_completion(model_id="anthropic/claude-sonnet-5")
        resp.provider = None  # not a top-level attr
        resp.model_extra = {"provider": "Google AI Studio"}

        async def create(**kwargs):
            return resp

        client = _make_fake_openai_client()
        client.chat.completions.create = create
        runner = OpenAIRunner(entry, client=client)

        result = await runner.chat(_req(), entry)
        assert result.provider == "Google AI Studio"

    @pytest.mark.asyncio
    async def test_chat_provenance_absent_degrades_to_none(self):
        """Providers/fakes that don't return id/provider → None, no crash (MagicMock attrs
        are not strings, so they read as None)."""
        entry = _entry()
        client = _make_fake_openai_client()  # MagicMock resp: .id/.provider auto-vivify, not str
        runner = OpenAIRunner(entry, client=client)

        result = await runner.chat(_req(), entry)
        assert result.provider is None
        assert result.generation_id is None

    def test_runner_id(self):
        entry = _entry(model_id="gpt-4o", upstream="gpt-4o")
        runner = OpenAIRunner(entry)
        assert runner.runner_id == "openai-gpt-4o"

    def test_models(self):
        entry = _entry()
        runner = OpenAIRunner(entry, client=MagicMock())
        models = runner.models()
        assert len(models) == 1
        assert models[0].id == "gpt-4o"
        assert models[0].owned_by == "openai"
        assert models[0].residency_class == "cloud"

    def test_lazy_client_no_key_at_import(self):
        """OpenAIRunner must be constructible with no API key set — client is lazy."""
        entry = _entry()
        runner = OpenAIRunner(entry)
        assert runner._client is None


# ---------------------------------------------------------------------------
# Gateway integration — cache hit
# ---------------------------------------------------------------------------


class TestGatewayCacheIntegration:
    @pytest.mark.asyncio
    async def test_cache_hit_second_call(self):
        """Second call for the same request: trace must report cache_hit=True, cost_usd=0."""
        catalog = Catalog.load(CATALOG_PATH)
        writer = MemoryTraceWriter()
        # Use the fake runner so no network calls are needed
        from toto_gateway.runners.fake import FakeRunner

        registry = RunnerRegistry(factory=lambda e: FakeRunner(e))
        gw = Gateway(catalog=catalog, registry=registry, writer=writer, cache=ExactCache())

        req = ChatCompletionRequest(
            model="echo-local",
            messages=[Message(role="user", content="cached prompt")],
        )

        result1 = await gw.complete(req)
        result2 = await gw.complete(req)

        assert len(writer.records) == 2
        trace2 = writer.records[1]
        assert trace2.cache_hit is True
        assert trace2.route_reason == "cache"
        assert trace2.cost_usd == 0.0
        assert result2.response.choices[0].message.content == result1.response.choices[0].message.content

    @pytest.mark.asyncio
    async def test_cache_miss_first_call(self):
        """First call must NOT be a cache hit."""
        catalog = Catalog.load(CATALOG_PATH)
        writer = MemoryTraceWriter()
        from toto_gateway.runners.fake import FakeRunner

        registry = RunnerRegistry(factory=lambda e: FakeRunner(e))
        gw = Gateway(catalog=catalog, registry=registry, writer=writer, cache=ExactCache())

        req = ChatCompletionRequest(
            model="echo-local",
            messages=[Message(role="user", content="first call")],
        )
        await gw.complete(req)
        assert writer.records[0].cache_hit is False


# ---------------------------------------------------------------------------
# Gateway integration — OpenAI runner
# ---------------------------------------------------------------------------


class TestGatewayOpenAIIntegration:
    def _build_gateway_with_openai_runner(
        self, fake_client: Any
    ) -> tuple[Gateway, MemoryTraceWriter]:
        catalog = Catalog.load(CATALOG_PATH)
        writer = MemoryTraceWriter()

        def factory(entry):
            if entry.lane == "frontier" and entry.endpoint == "openai":
                return OpenAIRunner(entry, client=fake_client)
            from toto_gateway.runners.fake import FakeRunner

            return FakeRunner(entry)

        registry = RunnerRegistry(factory=factory)
        return Gateway(catalog=catalog, registry=registry, writer=writer), writer

    @pytest.mark.asyncio
    async def test_openai_trace_residency_class(self):
        client = _make_fake_openai_client()
        gw, writer = self._build_gateway_with_openai_runner(client)

        req = ChatCompletionRequest(
            model="gpt-4o",
            messages=[Message(role="user", content="Hello")],
        )
        await gw.complete(req)

        trace = writer.records[0]
        assert trace.residency_class == "cloud"

    @pytest.mark.asyncio
    async def test_openai_trace_exact_usage(self):
        client = _make_fake_openai_client(chat_prompt_tokens=30, chat_completion_tokens=12)
        gw, writer = self._build_gateway_with_openai_runner(client)

        req = ChatCompletionRequest(
            model="gpt-4o",
            messages=[Message(role="user", content="Hello")],
        )
        await gw.complete(req)

        trace = writer.records[0]
        assert trace.tokens_prompt == 30
        assert trace.tokens_completion == 12
        assert trace.cost_estimated is False

    @pytest.mark.asyncio
    async def test_openai_trace_cost_not_estimated(self):
        """OpenAI runner reports real usage — cost_estimated must be False."""
        client = _make_fake_openai_client()
        gw, writer = self._build_gateway_with_openai_runner(client)

        req = ChatCompletionRequest(
            model="gpt-4o",
            messages=[Message(role="user", content="Hello")],
        )
        await gw.complete(req)

        assert writer.records[0].cost_estimated is False
