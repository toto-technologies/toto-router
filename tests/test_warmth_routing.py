"""TTL-aware incumbent hold (chunk B): the smart route keeps a conversation's warm model over a
freshly-resolved model while its upstream prefix cache is still warm.

Offline and deterministic — a stub classifier + a fake module clock, no network, no sleeps. The
memo/stickiness mechanics live in tests/test_stickiness*.py; here we prove _warm_hold's decision:
held within window, freed once cold, and every escape valve (tools guard, incumbent absent from
catalog, policy deny, kill-switch off).
"""

from __future__ import annotations

import types

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.pricing import compute_cost_usd
from toto_gateway.routing import smart
from toto_gateway.routing.labels import LabelBindings
from toto_gateway.routing.smart import cache_ttl_s, smart_route
from toto_gateway.schemas import Usage
from toto_gateway.trace import _label_from_reason

# code_generation is UNBOUND (model: None) with no category, so it resolves via the fallback ladder
# to model-a (the first cloud entry = frontier_reference). That's a DERIVED pick — the drift path the
# warm-hold protects. A hold routes away from model-a to the warm incumbent. (Bound picks are an
# operator decision and beat warmth — proven separately below with _BOUND_RAW.)
_RAW = {"labels": {
    "code_generation": {"model": None, "desc": "write or debug code"},
    "other": {"model": None, "desc": "none of the above"},
}}
# Same vocab, but code_generation is now EXPLICITLY bound to model-a — the rebind case.
_BOUND_RAW = {"labels": {
    "code_generation": {"model": "model-a", "desc": "write or debug code"},
    "other": {"model": None, "desc": "none of the above"},
}}
_REPLY = '{"label": "code_generation", "reason": "r"}'


def _catalog() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id="model-a", lane="economy", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="model-b", lane="economy", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="notools", lane="economy", endpoint="fake", residency_class="cloud",
                     tools=False),
    ])


async def fn(messages, model_id):
    return _REPLY


async def _route(*, warmth_routing=True, require_tools=False, policy=None, conv="c1", raw=_RAW):
    return await smart_route(
        "write a sort function", catalog=_catalog(), labels=LabelBindings(_raw=raw),
        benchmarks=None, classifier_model="model-a", policy=policy, classify_fn=fn,
        timeout_s=1.0, require_tools=require_tools, conversation_key=conv,
        warmth_routing=warmth_routing)


@pytest.fixture(autouse=True)
def _clear():
    smart._label_cache.clear()
    smart._warmth.clear()
    yield
    smart._label_cache.clear()
    smart._warmth.clear()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(smart, "time", types.SimpleNamespace(
        monotonic=lambda: now["t"], perf_counter=lambda: 0.0))
    return now


# ---- the hold -------------------------------------------------------------------------------


async def test_incumbent_held_within_window(clock):
    # Conversation was served model-b with a live prefix cache one second ago; a fresh classify would
    # rebind to model-a, but the warm incumbent is kept.
    smart.record_warmth("c1", tokens_cached=512, model="model-b")
    clock["t"] += 1.0
    res = await _route()
    assert res.model_id == "model-b"
    assert res.route_reason == "label:code_generation:warm-hold"
    assert res.label_metadata["warm_hold"] == {
        "kept": "model-b", "over": "model-a", "window_left_s": 299.0}


async def test_swap_allowed_once_cold(clock):
    # Same warm incumbent, but the provider cache TTL (model-b family default 300s) has elapsed → the
    # fresh pick wins freely.
    smart.record_warmth("c1", tokens_cached=512, model="model-b")
    clock["t"] += 301.0
    res = await _route()
    assert res.model_id == "model-a"
    assert res.route_reason == "label:code_generation:fallback"


async def test_no_cache_hit_swaps_freely(clock):
    # Incumbent within the time window but last turn had NO cache hit → nothing warm to protect.
    smart.record_warmth("c1", tokens_cached=0, model="model-b")
    clock["t"] += 1.0
    res = await _route()
    assert res.model_id == "model-a"


async def test_tools_turn_escapes_to_tools_capable_model(clock):
    # The warm incumbent can't speak native tools; a tools-bearing turn must escape to model-a.
    smart.record_warmth("c1", tokens_cached=512, model="notools")
    clock["t"] += 1.0
    res = await _route(require_tools=True)
    assert res.model_id == "model-a"


async def test_incumbent_absent_from_catalog_takes_fresh(clock):
    smart.record_warmth("c1", tokens_cached=512, model="ghost-model")
    clock["t"] += 1.0
    res = await _route()
    assert res.model_id == "model-a"


async def test_policy_deny_of_incumbent_takes_fresh(clock):
    # model-b is warm but the caller's policy no longer permits it → fresh.
    policy = types.SimpleNamespace(
        label_bindings={}, custom_labels=[], optimize=None,
        permits=lambda e: e.id != "model-b")
    smart.record_warmth("c1", tokens_cached=512, model="model-b")
    clock["t"] += 1.0
    res = await _route(policy=policy)
    assert res.model_id == "model-a"


async def test_kill_switch_off_always_fresh(clock):
    smart.record_warmth("c1", tokens_cached=512, model="model-b")
    clock["t"] += 1.0
    res = await _route(warmth_routing=False)
    assert res.model_id == "model-a"
    assert res.route_reason == "label:code_generation:fallback"


async def test_incumbent_equal_to_fresh_is_noop(clock):
    # Warm model IS the fresh pick → no hold, plain derived reason (no warm-hold suffix).
    smart.record_warmth("c1", tokens_cached=512, model="model-a")
    clock["t"] += 1.0
    res = await _route()
    assert res.model_id == "model-a"
    assert res.route_reason == "label:code_generation:fallback"


async def test_explicit_binding_beats_warmth(clock):
    # code_generation is now EXPLICITLY bound to model-a (an operator rebind). Even with model-b warm
    # inside its window, the binding wins immediately — warmth only holds against benchmark drift.
    smart.record_warmth("c1", tokens_cached=512, model="model-b")
    clock["t"] += 1.0
    res = await _route(raw=_BOUND_RAW)
    assert res.model_id == "model-a"
    assert res.route_reason == "label:code_generation"


# ---- observability + supporting math --------------------------------------------------------


def test_warm_hold_reason_parses_to_label():
    # The :warm-hold suffix must survive _label_from_reason so analytics keep the task type.
    assert _label_from_reason("label:code_generation:warm-hold") == "code_generation"


def test_cache_ttl_family_defaults():
    def e(mid, upstream=None, ttl=None):
        return CatalogEntry(id=mid, lane="economy", endpoint="fake", residency_class="cloud",
                            upstream_model=upstream, cache_ttl_s=ttl)
    assert cache_ttl_s(e("or-sonnet-4.6", "anthropic/claude-sonnet-4.6")) == 300
    assert cache_ttl_s(e("gpt-4o")) == 1800
    assert cache_ttl_s(e("fw-deepseek-v4-pro", "deepseek/deepseek-v4")) == 86400
    assert cache_ttl_s(e("model-x")) == 300
    assert cache_ttl_s(e("model-x", ttl=42)) == 42  # catalog field wins over the heuristic
    assert cache_ttl_s(None) == 300


def test_cached_tokens_discounted_in_cost():
    entry = CatalogEntry(id="p", lane="economy", endpoint="fake", residency_class="cloud")
    entry.price_usd_per_1k.prompt = 3.0
    # 1000 prompt, 800 cached @ default 0.1x: 200*3/1k + 800*3/1k*0.1 = 0.6 + 0.24
    assert compute_cost_usd(entry, Usage.of(1000, 0, cached=800)) == pytest.approx(0.84)
