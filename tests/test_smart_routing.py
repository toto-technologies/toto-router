"""Smart auto-routing (SR1): the `smart` sentinel on /v1/chat/completions classifies the request
and picks a model per the team's label bindings — the driver-plane routing, now reachable by
OpenAI clients (pi -m toto/smart).

All offline: a fake runner returns a JSON label for the classifier call and echoes everything
else. Proves classify -> route, the team override, guard downgrade + allow/deny still apply to
the resolved model, graceful degrade when the classifier is unavailable, streaming, and the
/v1/models listing.
"""

from __future__ import annotations

import json
import types
from typing import AsyncIterator

import pytest

from harness.appharness import default_settings, in_process_app
from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.gateway import Gateway
from toto_gateway.pipeline import ModelNotPermittedError
from toto_gateway.routing.labels import LabelBindings
from toto_gateway.runners.fake import FakeRunner
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.schemas import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse, Message
from toto_gateway.signals.extractor import HeuristicExtractor
from toto_gateway.signals.guards import RuleGuard
from toto_gateway.trace import MemoryTraceWriter

_RAW = {"labels": {
    "code_generation": {"model": "or-qwen3-coder-flash", "desc": "write or debug code"},
    "brainstorming": {"model": "or-sonnet-4.6", "desc": "open-ended ideas"},
    "other": {"model": None, "desc": "none of the above"},
}}


def _catalog(in_perimeter: bool = False) -> Catalog:
    models = [
        CatalogEntry(id="or-haiku-4.5", lane="economy", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="or-qwen3-coder-flash", lane="economy", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="fake", residency_class="cloud"),
    ]
    if in_perimeter:
        models.append(CatalogEntry(id="local-box", lane="economy", endpoint="http://localhost:1",
                                   residency_class="in_perimeter"))
    return Catalog(models=models)


class _LabelRunner:
    """Returns a fixed JSON label for the classifier call (system prompt = LABEL_PROMPT), echoes
    everything else via a real FakeRunner (so the resolved model's completion/stream still works)."""

    def __init__(self, entry: CatalogEntry, label_reply: str | None) -> None:
        self.entry = entry
        self.runner_id = f"label-{entry.id}"
        self._fake = FakeRunner(entry)
        self._reply = label_reply

    def _is_label_call(self, req: ChatCompletionRequest) -> bool:
        sys = req.messages[0].text() if req.messages and req.messages[0].role == "system" else ""
        return "label one piece of work" in sys

    async def chat(self, req, entry) -> ChatCompletionResponse:
        if self._is_label_call(req):
            return ChatCompletionResponse.simple(
                model=entry.id, content=self._reply or "", usage=self._fake._usage(req, ""))
        return await self._fake.chat(req, entry)

    def stream(self, req, entry) -> AsyncIterator[ChatCompletionChunk]:
        return self._fake.stream(req, entry)


def _gw(*, catalog=None, label_reply='{"label": "code_generation", "reason": "r"}',
        labels=_RAW, classifier="or-haiku-4.5", guard=None, router=None, extractor=None):
    catalog = catalog or _catalog()
    writer = MemoryTraceWriter()
    registry = RunnerRegistry(factory=lambda e: _LabelRunner(e, label_reply))
    gw = Gateway(
        catalog=catalog, registry=registry, writer=writer,
        guard=guard, router=router, extractor=extractor,
        labels=LabelBindings(_raw=labels) if labels else None,
        benchmarks=None, classifier_model=classifier, label_timeout_ms=200,
    )
    return gw, writer


def _req(content="write a python function to sort a list", *, model="smart", stream=False):
    return ChatCompletionRequest(
        model=model, messages=[Message(role="user", content=content)], stream=stream)


def _identity(catalog_policy=None, routing_policy=None):
    return types.SimpleNamespace(catalog_policy=catalog_policy, routing_policy=routing_policy,
                                 org_id=None, team_id=None)


# --- classify -> route (global binding) ------------------------------------------------

@pytest.mark.asyncio
async def test_smart_classifies_and_routes_to_global_binding():
    gw, _ = _gw()
    res = await gw.complete(_req())
    assert res.trace.model == "or-qwen3-coder-flash"                 # code_generation -> or-qwen3-coder-flash (labels.yaml)
    assert res.trace.route_reason == "label:code_generation"
    # the served response is the resolved model's, NOT a classifier turn
    assert res.response.model == "or-qwen3-coder-flash"


@pytest.mark.asyncio
async def test_smart_run_stamps_label_and_user_on_trace():
    """Analytics A1: a smart run whose route_reason is label:code_generation writes label
    'code_generation' + the resolved identity's user_id onto gateway_events."""
    gw, _ = _gw()
    ident = types.SimpleNamespace(catalog_policy=None, routing_policy=None,
                                  org_id="o1", team_id="t1", user_id="u_alex")
    res = await gw.complete(_req(), identity=ident)
    assert res.trace.route_reason == "label:code_generation"
    assert res.trace.label == "code_generation"   # derived at finalize, from route_reason
    assert res.trace.user_id == "u_alex"           # stamped at the org/team seam
    # classify_failed → NULL label (unclassified). Different text: the SR2 label memo would
    # (correctly) answer for the text the first call already classified.
    gw2, _ = _gw(classifier="not-in-catalog")
    res2 = await gw2.complete(_req(content="something never classified before"))
    assert res2.trace.route_reason == "smart:classify_failed" and res2.trace.label is None


_TOTOSHAPE_REPLY = (
    '{"label": "code_generation", "metadata": {"component": "auth-service", '
    '"keywords": ["login", "jwt"], "scope": "backend", "intent": "add SSO login"}, '
    '"reason": "r"}'
)


@pytest.mark.asyncio
async def test_smart_captures_totoshape_metadata_on_trace():
    """A totoshape-variant classify emits a metadata block; the gateway captures it (JSON) onto
    gateway_events.label_metadata alongside the label — the work-map's substrate."""
    from toto_gateway.routing import smart

    smart._label_cache.clear()
    gw, _ = _gw(label_reply=_TOTOSHAPE_REPLY)
    res = await gw.complete(_req(content="build a login flow with SSO"))
    assert res.trace.route_reason == "label:code_generation"
    md = json.loads(res.trace.label_metadata)
    assert md["component"] == "auth-service"
    assert md["scope"] == "backend"
    assert md["keywords"] == ["login", "jwt"]


@pytest.mark.asyncio
async def test_smart_metadata_survives_the_label_memo_cache_hit():
    """SR2 stickiness: a repeat turn of the same conversation reuses the cached (label, metadata) —
    no second classify call, and label_metadata is still stamped on the trace."""
    from toto_gateway.routing import smart

    smart._label_cache.clear()
    gw, _ = _gw(label_reply=_TOTOSHAPE_REPLY)
    text = "build a login flow with SSO"
    first = await gw.complete(_req(content=text))
    assert first.trace.label_metadata is not None
    # Point the classifier at a reply that would parse DIFFERENTLY — a cache hit must ignore it.
    gw2, _ = _gw(label_reply='{"label": "brainstorming", "reason": "r"}')
    second = await gw2.complete(_req(content=text))
    assert json.loads(second.trace.label_metadata)["component"] == "auth-service"  # cached, not reclassified
    assert second.trace.route_reason == "label:code_generation"


@pytest.mark.asyncio
async def test_smart_accepts_toto_smart_alias_case_insensitive():
    gw, _ = _gw()
    res = await gw.complete(_req(model="TOTO-SMART"))
    assert res.trace.model == "or-qwen3-coder-flash"
    assert res.trace.route_reason == "label:code_generation"


# --- team override ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_team_binding_overrides_global():
    gw, _ = _gw()
    ident = _identity(routing_policy={"bindings": {"code_generation": "or-sonnet-4.6"}})
    res = await gw.complete(_req(), identity=ident)
    assert res.trace.model == "or-sonnet-4.6"                # the team binding, not or-qwen3-coder-flash
    assert res.trace.route_reason == "label:code_generation:team"


@pytest.mark.asyncio
async def test_org_default_policy_changes_teamless_smart_chat_route():
    """The real gap: console edits were stored, but owner/API-token traffic has no team_id, so
    `toto/smart` saw no routing overlay. Saving the org-default policy must alter that HTTP path."""
    gw, _ = _gw()
    settings = default_settings(auth_token="", driver=False, fake_exec=False)
    async with in_process_app(gateway=gw, settings=settings) as (client, app):
        auth = app.state.auth
        uid = await auth.create_user("owner@acme.com", None, email_verified=True)
        org = (await auth.resolve_membership(uid))["org_id"]  # owner, team_id None
        await auth.set_routing_policy(org, org, bindings={"code_generation": "or-sonnet-4.6"})
        sess = await auth.create_session(uid, 3600)

        resp = await client.post(
            "/v1/chat/completions",
            headers={"authorization": ""},
            cookies={"toto_session": sess},
            json={"model": "smart", "messages": [{"role": "user", "content": "write python"}]},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["model"] == "or-sonnet-4.6"
    assert body["x_toto"]["model"] == "or-sonnet-4.6"
    assert body["x_toto"]["classified_as"] == "code_generation"
    assert body["x_toto"]["route_reason"] == "label:code_generation:team"


@pytest.mark.asyncio
async def test_org_default_policy_applies_to_team_member_bearer_traffic():
    """A caller on a TEAM that has no team routing row must still honor the org-default policy the
    console saved. The overlay lookup used to pick the team key and stop — a team member's
    bearer/API traffic saw NO overlay and routed globally, so console governance edits appeared
    ignored. The fallback is on the ROW: no team policy → the org-default applies."""
    gw, _ = _gw()
    settings = default_settings(auth_token="", driver=False, fake_exec=False)
    async with in_process_app(gateway=gw, settings=settings) as (client, app):
        auth = app.state.auth
        uid = await auth.create_user("eng@acme.com", None, email_verified=True)
        org = await auth.create_org("Acme", org_id="o_acme")
        team = await auth.create_team(org, "Eng", team_id="t_eng")
        await auth.add_membership(org, uid, "admin", team_id=team)  # on a team, no team policy
        await auth.set_routing_policy(org, org, bindings={"code_generation": "or-sonnet-4.6"})
        raw, _ = await auth.mint_api_token(uid, "cli", org_id=org)

        resp = await client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {raw}"},
            json={"model": "smart", "messages": [{"role": "user", "content": "write python"}]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["model"] == "or-sonnet-4.6"  # org-default, not the global binding

        # A team that DOES have its own policy still wins outright (org-default is only the fallback).
        await auth.set_routing_policy(team, org, bindings={"code_generation": "or-haiku-4.5"})
        resp2 = await client.post(
            "/v1/chat/completions",
            headers={"authorization": f"Bearer {raw}"},
            json={"model": "smart", "messages": [{"role": "user", "content": "write more python"}]},
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["model"] == "or-haiku-4.5"  # team overlay beats the org-default


@pytest.mark.asyncio
async def test_team_custom_label_routes_to_its_bound_model():
    gw, _ = _gw(label_reply='{"label": "legal_review"}')   # a label only the team invented
    ident = _identity(routing_policy={"custom_labels": [
        {"name": "legal_review", "desc": "review a contract", "model": "or-sonnet-4.6"}]})
    res = await gw.complete(_req(content="review this NDA"), identity=ident)
    assert res.trace.model == "or-sonnet-4.6"
    assert res.trace.route_reason == "label:legal_review:team"


# --- LangSmith tracing of the smart route (BYO, env-gated) -----------------------------

@pytest.mark.asyncio
async def test_smart_request_posts_a_langsmith_run_when_tracing_on(monkeypatch):
    """End-to-end wiring: a smart-routed complete() emits one `toto/smart` LangSmith run carrying
    the served model + the classification, when tracing is enabled. No-op path is the default."""
    posted = []

    class _FakeRun:
        def __init__(self, **kw):
            self.kw = kw
            self.children = []
            self.ended = None
            posted.append(self)

        def post(self, *a, **k):
            pass

        def patch(self, *a, **k):
            pass

        def create_child(self, name, run_type="chain", **kw):
            c = _FakeRun(name=name, run_type=run_type, **kw)
            self.children.append(c)
            return c

        def end(self, **kw):
            self.ended = kw

    fake_ls = types.ModuleType("langsmith")
    fake_ls.RunTree = _FakeRun
    monkeypatch.setitem(__import__("sys").modules, "langsmith", fake_ls)
    from toto_gateway.routing import smart_trace
    monkeypatch.setattr(smart_trace, "tracing_enabled", lambda: True)

    gw, _ = _gw()
    res = await gw.complete(_req())
    assert res.trace.model == "or-qwen3-coder-flash"

    roots = [r for r in posted if r.kw.get("name") == "toto/smart"]
    assert len(roots) == 1, "exactly one smart run posted"
    root = roots[0]
    assert root.ended["metadata"]["classified_as"] == "code_generation"
    assert root.ended["metadata"]["ls_model_name"] == "or-qwen3-coder-flash"   # the served model
    assert [c.kw["name"] for c in root.children] == ["classify"]


@pytest.mark.asyncio
async def test_no_langsmith_run_for_a_normal_model(monkeypatch):
    """A normally-named request is not smart-routed → smart is None → no LangSmith emit."""
    calls = []
    from toto_gateway.routing import smart_trace
    monkeypatch.setattr(smart_trace, "tracing_enabled", lambda: True)
    monkeypatch.setattr(smart_trace, "emit", lambda **kw: calls.append(kw))

    gw, _ = _gw()
    await gw.complete(_req(model="or-sonnet-4.6"))   # explicit model, not `smart`
    assert calls == []


# --- `other` as the configurable fallback ----------------------------------------------

@pytest.mark.asyncio
async def test_bound_other_is_used_when_classification_fails():
    """A bound `other` is the designated catch-all: when the classifier is unavailable, smart routes
    to it (not benchmark-best), so an owner can say 'send everything unclassified HERE'."""
    gw, _ = _gw(classifier="not-in-catalog")  # classifier absent → classify_failed path
    ident = _identity(routing_policy={"bindings": {"other": "or-sonnet-4.6"}})
    res = await gw.complete(_req(), identity=ident)
    assert res.trace.route_reason == "smart:classify_failed"
    assert res.trace.model == "or-sonnet-4.6"   # the bound `other`, not the benchmark default


@pytest.mark.asyncio
async def test_classified_other_routes_to_its_binding():
    """When the classifier returns `other` and the team bound it, route there (label:other:team)."""
    gw, _ = _gw(label_reply='{"label": "other"}')
    ident = _identity(routing_policy={"bindings": {"other": "or-sonnet-4.6"}})
    res = await gw.complete(_req(content="something ambiguous"), identity=ident)
    assert res.trace.model == "or-sonnet-4.6"
    assert res.trace.route_reason == "label:other:team"


# --- guard + catalog policy still apply to the resolved model --------------------------

@pytest.mark.asyncio
async def test_guard_downgrade_beats_the_smart_pick():
    from toto_gateway.routing.decision import GuardRouter

    gw, writer = _gw(catalog=_catalog(in_perimeter=True),
                     guard=RuleGuard(), router=GuardRouter(), extractor=HeuristicExtractor())
    # sensitive content -> DOWNGRADE_LOCAL; smart picked or-qwen3-coder-flash (cloud), guard forces local.
    res = await gw.complete(_req(content="here is a confidential strategy memo"))
    assert res.trace.model == "local-box"                  # guard override lands on in-perimeter
    assert res.trace.guard_action == "downgrade_local"


@pytest.mark.asyncio
async def test_catalog_deny_policy_blocks_the_smart_resolved_model():
    gw, _ = _gw()
    ident = _identity(catalog_policy={"mode": "deny", "models": ["or-qwen3-coder-flash"]})
    with pytest.raises(ModelNotPermittedError):            # code_generation -> or-qwen3-coder-flash, denied
        await gw.complete(_req(), identity=ident)


# --- graceful degrade ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_classifier_absent_from_catalog_degrades_no_500():
    # classifier id not in the catalog -> no classify call, benchmark default, never raises
    gw, _ = _gw(classifier="not-in-catalog")
    res = await gw.complete(_req())
    assert res.trace.route_reason == "smart:classify_failed"
    assert gw.catalog.get(res.trace.model) is not None     # resolved to a real model


@pytest.mark.asyncio
async def test_unparseable_classification_degrades_no_500():
    gw, _ = _gw(label_reply="not json at all")
    res = await gw.complete(_req())
    assert res.trace.route_reason == "smart:classify_failed"
    assert gw.catalog.get(res.trace.model) is not None


@pytest.mark.asyncio
async def test_labels_off_still_answers_smart():
    gw, _ = _gw(labels=None)                               # label routing soft-disabled
    res = await gw.complete(_req())
    # W2-C7 small-fix 8a: a DISTINCT reason for the labels-off path (not classify_failed), so
    # _smart_degraded never conflates config with a genuine classifier failure.
    assert res.trace.route_reason == "smart:labels_off"
    assert gw.catalog.get(res.trace.model) is not None


# --- streaming -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_smart_streaming_classifies_before_stream():
    gw, _ = _gw()
    box = {}
    chunks = [c async for c in gw.stream(_req(stream=True), on_trace=lambda tr: box.__setitem__("t", tr))]
    assert chunks                                          # the resolved model streamed
    t = box["t"]
    assert t.model == "or-qwen3-coder-flash"
    assert t.route_reason == "label:code_generation"


# --- /v1/models listing ----------------------------------------------------------------

def test_v1_models_lists_smart(test_client):
    body = test_client.get("/v1/models").json()
    ids = [m["id"] for m in body["data"]]
    assert "smart" in ids
    smart = next(m for m in body["data"] if m["id"] == "smart")
    assert smart["provider"] == "toto"
