"""StickinessPolicy seam (S1): the label memo's per-conversation hold becomes an explicit,
per-entry decision instead of a flat 900s constant. These prove the four seam invariants:

  (a) stick=None → today's behavior, byte-for-byte (memo pins the label as before);
  (b) a policy's hold_ttl actually governs expiry — a 1s hold lapses, a 900s one persists;
  (c) the decision rides label_metadata under "stick" on the put AND is re-flagged hit=True on the
      memo hit (the observability contract; route_reason's label:<l> grammar is untouched);
  (d) SlidingTTL (policy #1) returns the 900s default.

All offline: a stub classify_fn returns a JSON label; a monkeypatched module clock makes expiry
deterministic without real sleeps.
"""

from __future__ import annotations

import types

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.routing import smart
from toto_gateway.routing.labels import LabelBindings
from toto_gateway.routing.smart import SlidingTTL, StickCtx, StickDecision, smart_route

_RAW = {"labels": {
    "code_generation": {"model": "or-qwen3-coder-flash", "desc": "write or debug code"},
    "other": {"model": None, "desc": "none of the above"},
}}
_REPLY = '{"label": "code_generation", "metadata": {"component": "auth"}, "reason": "r"}'


def _catalog() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id="or-haiku-4.5", lane="economy", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="or-qwen3-coder-flash", lane="economy", endpoint="fake", residency_class="cloud"),
    ])


def _classify_fn(reply=_REPLY):
    calls = {"n": 0}

    async def fn(messages, model_id):
        calls["n"] += 1
        return reply

    fn.calls = calls
    return fn


async def _route(classify_fn, *, stick=None, conv="c1"):
    return await smart_route(
        "write a sort function", catalog=_catalog(), labels=LabelBindings(_raw=_RAW),
        benchmarks=None, classifier_model="or-haiku-4.5", policy=None,
        classify_fn=classify_fn, timeout_s=1.0, conversation_key=conv, stick=stick)


@pytest.fixture(autouse=True)
def _clear_memo():
    smart._label_cache.clear()
    yield
    smart._label_cache.clear()


@pytest.fixture
def clock(monkeypatch):
    """A controllable module clock so ttl expiry is deterministic (no real sleeps)."""
    now = {"t": 1000.0}
    monkeypatch.setattr(smart, "time", types.SimpleNamespace(
        monotonic=lambda: now["t"], perf_counter=lambda: 0.0))
    return now


# (a) default None → memo pins the label with zero re-classification, exactly as before.
async def test_none_policy_memoizes_as_before(clock):
    fn = _classify_fn()
    first = await _route(fn)
    second = await _route(fn)  # same conversation_key → memo hit
    assert first.model_id == second.model_id == "or-qwen3-coder-flash"
    assert fn.calls["n"] == 1  # classified once, then sticky
    assert second.classify_ms is None  # the memo-hit tell
    assert second.label_metadata.get("stick") is None  # no policy → no stick record


# (b) the policy's hold_ttl governs expiry: a 1s hold lapses, a 900s one survives the same jump.
async def test_hold_ttl_drives_expiry(clock):
    short = types.SimpleNamespace(assess=lambda ctx: StickDecision(hold_ttl=1.0, reason="short"))
    await _route(_classify_fn(), stick=short, conv="short")
    long = types.SimpleNamespace(assess=lambda ctx: StickDecision(hold_ttl=900.0, reason="long"))
    await _route(_classify_fn(), stick=long, conv="long")

    clock["t"] += 2.0  # past the 1s hold, well inside the 900s one
    short_fn, long_fn = _classify_fn(), _classify_fn()
    await _route(short_fn, stick=short, conv="short")
    await _route(long_fn, stick=long, conv="long")
    assert short_fn.calls["n"] == 1  # expired → re-classified
    assert long_fn.calls["n"] == 0   # still held → memo hit


# (c) the decision rides label_metadata["stick"] on the put, re-flagged hit=True on the memo hit;
#     route_reason keeps its label:<l> grammar.
async def test_stick_record_on_put_and_hit(clock):
    policy = types.SimpleNamespace(assess=lambda ctx: StickDecision(hold_ttl=900.0, reason="sliding_ttl"))
    fn = _classify_fn()
    put = await _route(fn, stick=policy)
    assert put.route_reason == "label:code_generation"
    assert put.label_metadata["component"] == "auth"  # classify block preserved alongside
    assert put.label_metadata["stick"] == {"reason": "sliding_ttl", "hold_ttl": 900.0, "hit": False}

    hit = await _route(fn, stick=policy)
    assert fn.calls["n"] == 1  # not re-classified
    assert hit.label_metadata["stick"] == {"reason": "sliding_ttl", "hold_ttl": 900.0, "hit": True}
    assert hit.route_reason == "label:code_generation"


# (c') assess sees the in-memory ctx (conversation_key, label, vocab, require_tools, policy).
async def test_assess_receives_stickctx(clock):
    seen = {}

    def assess(ctx: StickCtx) -> StickDecision:
        seen["ctx"] = ctx
        return StickDecision(hold_ttl=42.0, reason="r")

    await _route(_classify_fn(), stick=types.SimpleNamespace(assess=assess), conv="k")
    assert seen["ctx"].conversation_key == "k"
    assert seen["ctx"].label == "code_generation"
    assert "code_generation" in seen["ctx"].vocab


# (d) SlidingTTL is policy #1: the flat 900s default.
def test_sliding_ttl_returns_default():
    dec = SlidingTTL().assess(StickCtx(conversation_key="c", label="x", vocab={}, require_tools=False, policy=None))
    assert dec.hold_ttl == smart._LABEL_TTL_S == 900.0
    assert dec.reason == "sliding_ttl"
    assert dec.strength == 1.0
