"""Context-caching P0 (docs/plans/2026-07-04-conversation-context-caching.md).

Four planes, all offline:
  1. get_history block eviction with hysteresis — the rendered prefix is a byte-superset
     of the previous turn's between evictions (never slides one turn per call).
  2. OpenAIRunner — plain-string content serializes byte-identically to before; a parts
     list passes through AS-IS; usage accounting is requested via extra_body; cached
     tokens + upstream cost are captured (chat + stream).
  3. Breakpoint placement — ≤2 cache_control breakpoints on driver-model builders,
     none on triage.
  4. End-to-end plumbing — a fake usage payload with prompt_tokens_details flows
     runner → gateway trace → build_driver's Exec → the answer span + execution dict.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry, Price
from toto_gateway import persona
from toto_gateway.driver import prompts
from toto_gateway.gateway import Gateway
from toto_gateway.runners.openai import OpenAIRunner
from toto_gateway.runs import RunStore
from toto_gateway.schemas import ChatCompletionRequest, Message
from toto_gateway.trace import MemoryTraceWriter

# --- fakes -------------------------------------------------------------------


def _entry() -> CatalogEntry:
    # An OpenRouter entry: usage accounting (extra_body usage:{include:true}) is OpenRouter-specific,
    # so this test — which asserts that request extra + the OpenRouter usage shape — must use an
    # OpenRouter base_url (strict providers like Fireworks reject the extra; see _usage_extra).
    return CatalogEntry(
        id="gpt-4o", lane="frontier", endpoint="openai", residency_class="cloud",
        base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
        price_usd_per_1k=Price(prompt=2.5, completion=10.0), upstream_model="gpt-4o",
    )


def _fake_client(content: str = "A response", *, prompt: int = 100, completion: int = 10,
                 cached: int = 80, cache_write: int = 0, cost: float | None = 0.00123):
    """Minimal fake AsyncOpenAI client whose usage carries prompt_tokens_details + cost
    (the OpenRouter usage-accounting shape). `cache_write` mirrors OpenRouter's Anthropic-family
    passthrough of cache_creation_input_tokens. Returns (client, captured_kwargs)."""
    usage = NS(prompt_tokens=prompt, completion_tokens=completion,
               prompt_tokens_details=NS(cached_tokens=cached),
               cache_creation_input_tokens=cache_write, cost=cost)
    resp = NS(choices=[NS(message=NS(content=content), finish_reason="stop")], usage=usage)
    captured: dict = {}

    async def create(**kw):
        captured.clear()
        captured.update(kw)
        if kw.get("stream"):
            async def gen():
                yield NS(choices=[NS(delta=NS(content=content, role=None), finish_reason=None)],
                         usage=None)
                yield NS(choices=[NS(delta=NS(content=None, role=None), finish_reason="stop")],
                         usage=None)
                yield NS(choices=[], usage=usage)
            return gen()
        return resp

    return NS(chat=NS(completions=NS(create=create))), captured


# --- 1. block eviction with hysteresis ----------------------------------------


async def test_history_prefix_is_byte_superset_between_evictions():
    """20-turn conversation: the serialized history prefix for turn N+1 must EXTEND turn N's
    byte-for-byte except at eviction points — and evictions must be blocks (down to half the
    cap), not a per-turn slide."""
    s = RunStore(":memory:")
    for i in range(1, 21):
        rid = f"r{i}"
        await s.create(rid, f"question {i} " + "q" * 188, conv_id="r1" if i > 1 else None, turn=i)
        await s.finish(rid, status="done", answer=f"answer {i} " + "a" * 291)  # ~500 chars/turn

    cap = 4000  # fits ~8 turns; half-cap ~4 turns
    prev_bytes, prev_first = None, None
    evictions, superset_steps = 0, 0
    for turn in range(2, 22):
        hist = await s.get_history("r1", before_turn=turn, max_chars=cap)
        assert sum(len(p["query"]) + len(p["answer"]) for p in hist) <= cap
        rendered = "".join(
            json.dumps(m, sort_keys=True) for m in prompts._history_messages(hist))
        first = hist[0]["query"] if hist else ""
        if prev_bytes is not None:
            if first == prev_first:  # no eviction → strict byte-superset
                assert rendered.startswith(prev_bytes), f"prefix churned at turn {turn}"
                superset_steps += 1
            else:  # eviction → must have dropped a BLOCK, to at most half the cap
                evictions += 1
                assert sum(len(p["query"]) + len(p["answer"]) for p in hist) <= cap // 2
        prev_bytes, prev_first = rendered, first
    assert evictions >= 1            # the cap was actually exercised
    assert superset_steps >= 3 * evictions  # stable runs between evictions, not per-turn slides


async def test_history_under_cap_returns_everything():
    s = RunStore(":memory:")
    for i in range(1, 4):
        rid = f"r{i}"
        await s.create(rid, f"q{i}", conv_id="r1" if i > 1 else None, turn=i)
        await s.finish(rid, status="done", answer=f"a{i}")
    assert len(await s.get_history("r1", before_turn=4, max_chars=16000)) == 3


# --- 2. runner: serialization + capture ----------------------------------------


@pytest.mark.asyncio
async def test_string_content_serializes_byte_identically():
    """Plain-string messages must hit the wire exactly as before: {"role", "content": str}."""
    entry = _entry()
    client, captured = _fake_client()
    req = ChatCompletionRequest(model="gpt-4o", messages=[
        Message(role="system", content="be brief"), Message(role="user", content="Hello")])
    await OpenAIRunner(entry, client=client).chat(req, entry)
    assert captured["messages"] == [
        {"role": "system", "content": "be brief"}, {"role": "user", "content": "Hello"}]
    assert all(isinstance(m["content"], str) for m in captured["messages"])


@pytest.mark.asyncio
async def test_parts_content_passes_through_as_is():
    parts = [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    entry = _entry()
    client, captured = _fake_client()
    req = ChatCompletionRequest(model="gpt-4o", messages=[
        Message(role="system", content=parts), Message(role="user", content="hi")])
    await OpenAIRunner(entry, client=client).chat(req, entry)
    assert captured["messages"][0] == {"role": "system", "content": parts}
    assert captured["messages"][1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_tool_role_is_coerced_to_labeled_user_message():
    """On a `tools: false` catalog entry, a `role:"tool"` message must not reach the wire as a
    `tool` role: gemini-2.5-flash via OpenRouter aborts the stream on it (empty, no finish_reason
    → "Stream ended without finish_reason"). It's coerced to a labeled user message, while other
    roles pass through unchanged. Covers chat + stream (both build via _wire_messages).
    Entries WITH native tools (the default) preserve the tool role — see test_agentic_loop.py."""
    entry = _entry().model_copy(update={"tools": False})
    req = ChatCompletionRequest(model="gpt-4o", messages=[
        Message(role="user", content="write a merge fn"),
        Message(role="assistant", content="done, wrote it"),
        Message(role="tool", content="Successfully wrote 1718 bytes to merge.py"),
        Message(role="user", content="now summarize the file"),
    ])

    client, captured = _fake_client()
    await OpenAIRunner(entry, client=client).chat(req, entry)
    assert [m["role"] for m in captured["messages"]] == ["user", "assistant", "user", "user"]
    assert captured["messages"][2] == {
        "role": "user", "content": "[tool result]\nSuccessfully wrote 1718 bytes to merge.py"}
    assert not any(m["role"] == "tool" for m in captured["messages"])

    # same coercion on the streaming path
    client_s, captured_s = _fake_client()
    async for _ in OpenAIRunner(entry, client=client_s).stream(req, entry):
        pass
    assert not any(m["role"] == "tool" for m in captured_s["messages"])
    assert captured_s["messages"][2]["content"].startswith("[tool result]\n")


@pytest.mark.asyncio
async def test_chat_requests_usage_accounting_and_captures_cached():
    entry = _entry()
    client, captured = _fake_client(cached=80, cost=0.00123)
    req = ChatCompletionRequest(model="gpt-4o",
                                messages=[Message(role="user", content="Hello")])
    resp = await OpenAIRunner(entry, client=client).chat(req, entry)
    assert captured["extra_body"] == {"usage": {"include": True}}
    assert resp.usage.tokens_cached == 80
    assert resp.usage.cost_upstream == pytest.approx(0.00123)
    assert resp.usage.prompt_tokens == 100 and resp.usage.total_tokens == 110


@pytest.mark.asyncio
async def test_stream_trailing_usage_carries_cached_and_cost():
    entry = _entry()
    client, captured = _fake_client(content="foo bar", cached=64, cost=0.002)
    req = ChatCompletionRequest(model="gpt-4o",
                                messages=[Message(role="user", content="Hello")])
    chunks = [c async for c in OpenAIRunner(entry, client=client).stream(req, entry)]
    assert captured["extra_body"] == {"usage": {"include": True}}
    last = chunks[-1]
    assert last.usage is not None and last.usage.tokens_cached == 64
    assert last.usage.cost_upstream == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_missing_details_read_as_zero():
    """Providers without prompt_tokens_details (or fakes) → tokens_cached 0, cost None."""
    entry = _entry()
    usage = NS(prompt_tokens=10, completion_tokens=5)  # no details, no cost
    bare = NS(choices=[NS(message=NS(content="ok"), finish_reason="stop")], usage=usage)

    async def create(**kw):
        return bare

    client = NS(chat=NS(completions=NS(create=create)))
    req = ChatCompletionRequest(model="gpt-4o",
                                messages=[Message(role="user", content="x")])
    out = await OpenAIRunner(entry, client=client).chat(req, entry)
    assert out.usage.tokens_cached == 0 and out.usage.cost_upstream is None


# --- 3. breakpoint placement ----------------------------------------------------


def _n_breakpoints(msgs: list[dict]) -> int:
    return sum(1 for m in msgs
               for p in (m["content"] if isinstance(m["content"], list) else [])
               if isinstance(p, dict) and "cache_control" in p)


_HIST = [{"query": f"q{i}", "answer": f"a{i}"} for i in range(3)]


def test_direct_builder_two_breakpoints_with_history():
    msgs = persona.build_direct_messages("now shorter", history=_HIST)
    assert _n_breakpoints(msgs) == 2
    # (a) end of system block
    assert msgs[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[0]["content"][0]["text"] == persona.DIRECT_ANSWER_PROMPT
    # (b) last message of the stable prefix = final history message
    assert msgs[-2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[-2]["content"][0]["text"] == "a2"
    # the volatile current turn stays a plain string
    assert msgs[-1] == {"role": "user", "content": "now shorter"}


def test_direct_builder_single_breakpoint_without_history():
    msgs = persona.build_direct_messages("q")
    assert _n_breakpoints(msgs) == 1
    assert msgs[1] == {"role": "user", "content": "q"}


def test_synthesize_and_decompose_builders_marked():
    for msgs in (persona.build_synthesize_messages("q", [{"task": "t", "result": "r"}],
                                                   history=_HIST),
                 prompts.build_decompose_messages("q", history=_HIST),
                 prompts.build_decompose_retry_messages("q", "bad", history=_HIST)):
        assert 1 <= _n_breakpoints(msgs) <= 2


def test_triage_builder_unmarked():
    msgs = prompts.build_triage_messages("q", history=_HIST)
    assert _n_breakpoints(msgs) == 0
    assert all(isinstance(m["content"], str) for m in msgs)


def test_add_cache_breakpoints_idempotent():
    once = prompts.add_cache_breakpoints([{"role": "system", "content": "s"},
                                          {"role": "user", "content": "u"}])
    assert prompts.add_cache_breakpoints(once) == once  # never double-wraps


# --- 4. plumbing: fake usage payload → trace → Exec → span/execution -------------


def _gateway(client) -> tuple[Gateway, MemoryTraceWriter]:
    from toto_gateway.runners.registry import RunnerRegistry

    writer = MemoryTraceWriter()
    catalog = Catalog(models=[_entry()])
    registry = RunnerRegistry(factory=lambda e: OpenAIRunner(e, client=client))
    return Gateway(catalog=catalog, registry=registry, writer=writer), writer


@pytest.mark.asyncio
async def test_cached_tokens_reach_gateway_trace():
    client, _ = _fake_client(cached=80)
    gw, writer = _gateway(client)
    req = ChatCompletionRequest(model="gpt-4o",
                                messages=[Message(role="user", content="Hello")])
    await gw.complete(req)
    assert writer.records[0].tokens_cached == 80
    assert writer.records[0].tokens_prompt == 100


@pytest.mark.asyncio
async def test_cache_write_tokens_reach_gateway_trace():
    """OpenRouter's cache_creation_input_tokens (Anthropic-family passthrough) maps onto Usage and
    rides through the gateway's _account choke point onto the trace's write-ledger column."""
    client, _ = _fake_client(cached=0, cache_write=48)
    gw, writer = _gateway(client)
    req = ChatCompletionRequest(model="gpt-4o",
                                messages=[Message(role="user", content="Hello")])
    resp = await gw.complete(req)
    assert resp.response.usage.tokens_cache_write == 48
    assert writer.records[0].tokens_cache_write == 48


@pytest.mark.asyncio
async def test_cached_tokens_reach_span_and_execution(tmp_path):
    """The full driver wire: runner usage details → gateway trace → app.build_driver's
    Exec → the answer_trivial span + the task execution dict."""
    from toto_gateway.app import build_driver
    from toto_gateway.config import Settings

    triage_json = json.dumps({"kind": "trivial", "reason": "t"})
    client, _ = _fake_client(content=triage_json, cached=96, prompt=128)
    gw, _w = _gateway(client)
    settings = Settings(catalog="catalog.yaml", trace_jsonl="", trace_db="", trace_stdout=False,
                        driver=True, fake_exec=True, db=":memory:", toto_token="",
                        driver_model="gpt-4o", triage_model="gpt-4o",
                        driver_spans_jsonl=str(tmp_path / "spans.jsonl"))
    driver = build_driver(settings, gw)
    result = await driver.run("hello there")

    spans = {s["node"]: s for s in result.spans}
    assert spans["triage"]["tokens_cached"] == 96
    assert spans["triage"]["tokens_prompt"] == 128
    assert spans["answer_trivial"]["tokens_cached"] == 96
    assert result.tasks[0]["execution"]["tokens_cached"] == 96
