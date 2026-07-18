"""Stickiness policy ladder (S2/S3/S4): the successive `assess` bodies wired onto the S1 seam.

Each section is offline and deterministic — no network, no real redis, no sleeps. The S1 seam
mechanics (memo hold, label_metadata["stick"] observability, expiry) are proven in
tests/test_stickiness.py; here we prove the POLICY decisions themselves plus the declared-session
flow through the gateway and the optional Redis L2.
"""

from __future__ import annotations

import types

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.routing import smart
from toto_gateway.routing.labels import LabelBindings
from toto_gateway.routing.smart import (
    DECLARED_TTL_S,
    DeclaredSession,
    LabelAwareTTL,
    StickCtx,
    is_declared,
    smart_route,
)

_LABEL_TTL_S = 900.0

# Offline smart_route harness (mirrors tests/test_stickiness.py) — a stub classifier + a fake clock.
_RAW = {"labels": {
    "code_generation": {"model": "or-qwen3-coder-flash", "desc": "write or debug code"},
    "other": {"model": None, "desc": "none of the above"},
}}
_REPLY = '{"label": "code_generation", "metadata": {"component": "auth"}, "reason": "r"}'


def _ctx(label="code_generation", *, policy=None, conv="c1"):
    return StickCtx(conversation_key=conv, label=label, vocab={}, require_tools=False, policy=policy)


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


async def _route(classify_fn, *, stick=None, conv="c1", memo_redis=None):
    return await smart_route(
        "write a sort function", catalog=_catalog(), labels=LabelBindings(_raw=_RAW),
        benchmarks=None, classifier_model="or-haiku-4.5", policy=None,
        classify_fn=classify_fn, timeout_s=1.0, conversation_key=conv, stick=stick,
        memo_redis=memo_redis)


class _FakeRedis:
    """A minimal async redis stand-in recording get/set; `raising` simulates a Redis outage."""

    def __init__(self, raising: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.raising = raising
        self.gets: list[str] = []
        self.sets: list[tuple] = []

    async def get(self, k):
        self.gets.append(k)
        if self.raising:
            raise RuntimeError("redis down")
        return self.store.get(k)

    async def set(self, k, v, ex=None):
        self.sets.append((k, v, ex))
        if self.raising:
            raise RuntimeError("redis down")
        self.store[k] = v


@pytest.fixture(autouse=True)
def _clear_memo():
    smart._label_cache.clear()
    smart._warmth.clear()
    yield
    smart._label_cache.clear()
    smart._warmth.clear()


@pytest.fixture
def clock(monkeypatch):
    """A controllable module clock so ttl expiry is deterministic (no real sleeps)."""
    now = {"t": 1000.0}
    monkeypatch.setattr(smart, "time", types.SimpleNamespace(
        monotonic=lambda: now["t"], perf_counter=lambda: 0.0))
    return now


# ---- S2: LabelAwareTTL precedence (org map > env default map > flat default) -------------------


def test_label_aware_ttl_falls_to_flat_default_with_no_maps():
    # No env map, no org policy → identical to SlidingTTL (the app wires it unconditionally).
    dec = LabelAwareTTL().assess(_ctx(policy=None))
    assert dec.hold_ttl == _LABEL_TTL_S
    assert dec.reason == "label_ttl"


def test_label_aware_ttl_uses_env_default_map():
    pol = LabelAwareTTL({"code_generation": 3600, "classification": 120})
    assert pol.assess(_ctx("code_generation")).hold_ttl == 3600.0
    assert pol.assess(_ctx("classification")).hold_ttl == 120.0
    assert pol.assess(_ctx("chatbot")).hold_ttl == _LABEL_TTL_S  # unmapped label → flat default


def test_label_aware_ttl_org_map_beats_env_default():
    pol = LabelAwareTTL({"code_generation": 3600})
    org = types.SimpleNamespace(stick_ttls={"code_generation": 300})
    # Org routing-policy stick_ttls (the console table) overrides the global env default per label.
    assert pol.assess(_ctx("code_generation", policy=org)).hold_ttl == 300.0
    # A label the org didn't set still falls to the env default map.
    org2 = types.SimpleNamespace(stick_ttls={"chatbot": 7200})
    assert pol.assess(_ctx("code_generation", policy=org2)).hold_ttl == 3600.0


def test_label_aware_ttl_tolerates_policy_without_stick_ttls():
    pol = LabelAwareTTL({"code_generation": 3600})
    # A catalog-only policy object (no stick_ttls attr) must not raise.
    assert pol.assess(_ctx("code_generation", policy=object())).hold_ttl == 3600.0


def test_stick_ttls_validation_fail_closed():
    # The admin PUT trust boundary: only known labels, positive seconds within the cap.
    from toto_gateway.routes.admin_routing import MAX_STICK_TTL, _validate_stick_ttls

    vocab = {"code_generation", "classification"}
    custom = [{"name": "invoice_parsing"}]
    ok, err = _validate_stick_ttls({"code_generation": 3600, "invoice_parsing": 300}, vocab, custom)
    assert err is None and ok == {"code_generation": 3600.0, "invoice_parsing": 300.0}
    assert _validate_stick_ttls(None, vocab, custom) == ({}, None)  # absent → {}
    assert _validate_stick_ttls({"nope": 60}, vocab, custom)[1] is not None  # unknown label
    assert _validate_stick_ttls({"code_generation": 0}, vocab, custom)[1] is not None  # non-positive
    assert _validate_stick_ttls({"code_generation": -5}, vocab, custom)[1] is not None
    assert _validate_stick_ttls({"code_generation": MAX_STICK_TTL + 1}, vocab, custom)[1] is not None
    assert _validate_stick_ttls({"code_generation": True}, vocab, custom)[1] is not None  # bool≠number
    assert _validate_stick_ttls([("code_generation", 60)], vocab, custom)[1] is not None  # not a dict


# ---- S3: DeclaredSession — anchor override + long eager hold + affinity intact -----------------


def test_declared_key_shape_and_none():
    from toto_gateway.gateway import _declared_key

    assert _declared_key(None) is None
    assert _declared_key("") is None
    k = _declared_key("my-session-123")
    assert k is not None and k.startswith("declared:") and len(k) == len("declared:") + 16
    assert _declared_key("a") == _declared_key("a")  # stable
    assert is_declared(k) and not is_declared("abc0123456789def")


def test_declared_session_extraction_precedence_and_non_mutation():
    from toto_gateway.routes.chat import _declared_session
    from toto_gateway.schemas import ChatCompletionRequest, Message

    msgs = [Message(role="user", content="hi")]
    # header wins over body session_id wins over prompt_cache_key
    req = ChatCompletionRequest(model="m", messages=msgs, session_id="body-sid", prompt_cache_key="pck")
    hdr_req = types.SimpleNamespace(headers={"x-session-id": "hdr-sid"})
    assert _declared_session(hdr_req, req) == "hdr-sid"
    no_hdr = types.SimpleNamespace(headers={})
    assert _declared_session(no_hdr, req) == "body-sid"
    pck_only = ChatCompletionRequest(model="m", messages=msgs, prompt_cache_key="pck")
    assert _declared_session(no_hdr, pck_only) == "pck"
    assert _declared_session(no_hdr, ChatCompletionRequest(model="m", messages=msgs)) is None
    # Reading the declared session must NOT strip the body hints — the runner still sends them
    # upstream (client wins there), so affinity is intact.
    assert req.session_id == "body-sid" and "session_id" in req.passthrough_params()


def test_declared_session_policy_long_hold_else_delegate():
    inner = LabelAwareTTL({"code_generation": 120})
    pol = DeclaredSession(inner)
    dec = pol.assess(_ctx(conv="declared:abc0123456789def"))
    assert dec.hold_ttl == DECLARED_TTL_S and dec.reason == "declared_session"
    # A non-declared conversation falls through to the inner per-task-type hold.
    assert pol.assess(_ctx("code_generation", conv="c1")).hold_ttl == 120.0


async def test_gateway_stamps_declared_conversation_key(gateway):
    from toto_gateway.gateway import _declared_key
    from toto_gateway.schemas import ChatCompletionRequest, Message

    req = ChatCompletionRequest(
        model="echo-local", messages=[Message(role="user", content="hello")])
    res = await gateway.complete(req, declared_session="sess-xyz")
    # The declared session overrides the message fingerprint as the memo/trace anchor.
    assert res.trace.conversation_key == _declared_key("sess-xyz")


async def test_declared_route_pins_label_with_long_hold(clock):
    # A declared conversation_key routes through the memo like any other, but the DeclaredSession
    # policy stamps the long hold + reason on the entry (eager commit on the first turn).
    fn = _classify_fn()
    res = await _route(fn, stick=DeclaredSession(LabelAwareTTL()), conv="declared:deadbeefdeadbeef")
    assert res.label_metadata["stick"] == {
        "reason": "declared_session", "hold_ttl": DECLARED_TTL_S, "hit": False}
    hit = await _route(fn, stick=DeclaredSession(LabelAwareTTL()), conv="declared:deadbeefdeadbeef")
    assert fn.calls["n"] == 1 and hit.label_metadata["stick"]["hit"] is True


# ---- S4: WarmthHold + composite ladder + Redis L2 ----------------------------------------------


def test_warmth_hold_extends_on_warm_conversation():
    from toto_gateway.routing.smart import WarmthHold

    pol = WarmthHold(base=900.0, warm_ttl=3600.0, warm_turns=3)
    # Cold (unseen) conversation → base hold, zero strength.
    cold = pol.assess(_ctx(conv="cold"))
    assert cold.hold_ttl == 900.0 and cold.strength == 0.0
    # A live upstream prefix cache marks it hot immediately (turns aside).
    smart.record_warmth("warm-cache", tokens_cached=512)
    warm = pol.assess(_ctx(conv="warm-cache"))
    assert warm.hold_ttl == 3600.0 and warm.strength == 1.0
    # Turn depth alone crosses the threshold (3 turns).
    for _ in range(3):
        smart.record_warmth("deep", tokens_cached=0)
    deep = pol.assess(_ctx(conv="deep"))
    assert deep.hold_ttl == 3600.0 and deep.strength == 1.0


def test_composite_ladder_precedence():
    from toto_gateway.routing.smart import TotoStickiness

    pol = TotoStickiness({"code_generation": 300})  # short per-task-type hold
    # 1) declared wins outright, regardless of label/warmth.
    d = pol.assess(_ctx("code_generation", conv="declared:abc0123456789def"))
    assert d.reason == "declared_session" and d.hold_ttl == DECLARED_TTL_S
    # 2) cold conversation → the per-task-type label hold.
    cold = pol.assess(_ctx("code_generation", conv="c-cold"))
    assert cold.reason == "label_ttl" and cold.hold_ttl == 300.0
    # 3) warmth is a FLOOR: a hot conversation extends beyond the short label hold, never shortens it.
    smart.record_warmth("c-hot", tokens_cached=256)
    hot = pol.assess(_ctx("code_generation", conv="c-hot"))
    assert hot.reason == "warmth_hold" and hot.hold_ttl == 3600.0
    # A label hold LONGER than the warm floor is kept (max, not replaced).
    pol_long = TotoStickiness({"code_generation": 7200})
    smart.record_warmth("c-hot2", tokens_cached=256)
    keep = pol_long.assess(_ctx("code_generation", conv="c-hot2"))
    assert keep.reason == "label_ttl" and keep.hold_ttl == 7200.0


async def test_redis_l2_shares_classification_across_replicas(clock):
    r = _FakeRedis()
    fn = _classify_fn()
    a = await _route(fn, conv="c1", memo_redis=r)
    assert fn.calls["n"] == 1 and r.sets  # replica A classified once and wrote L2
    # Replica B: empty L1, same L2 → memo hit, no re-classification.
    smart._label_cache.clear()
    fn_b = _classify_fn()
    b = await _route(fn_b, conv="c1", memo_redis=r)
    assert fn_b.calls["n"] == 0  # served from the shared L2
    assert b.model_id == a.model_id == "or-qwen3-coder-flash"


async def test_redis_l2_fail_open_on_errors(clock):
    r = _FakeRedis(raising=True)
    fn = _classify_fn()
    # A Redis that raises on every op must not break routing — it degrades to per-replica L1.
    res = await _route(fn, conv="c1", memo_redis=r)
    assert res.model_id == "or-qwen3-coder-flash" and fn.calls["n"] == 1
    assert r.gets and r.sets  # both were attempted, both swallowed
