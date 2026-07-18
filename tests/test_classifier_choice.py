"""Pluggable in-perimeter classifier (enterprise-readiness W3-C1).

The smart-routing classifier model becomes an ORG routing-policy choice. Before this chunk the
classifier was the global TOTO_GW_LABEL_CLASSIFIER_MODEL — so a Restricted-data org's prompt
egressed to a shared CLOUD classifier BEFORE any local_only/deny constraint could apply. Now:

  * effective_policy unpacks routing_policy.classifier_model (normalized) onto Policy;
  * BOTH classify call sites (smart path + explicit-model data-policy path) use the org's choice;
  * the classify runs against the CALLER'S effective catalog, so an org-adopted model can BE the
    classifier (and an absent id degrades to classify_failed, never 500);
  * admin PUT validation refuses an unknown id, refuses a NON-in-perimeter classifier when the org
    taxonomy carries a local_only/deny constraint, and refuses the field on a TEAM PUT (org-only).

Offline: a fake runner echoes everything and records which model the classifier call ran on.
"""

from __future__ import annotations

import types
from typing import AsyncIterator

import pytest

from harness.appharness import default_settings, in_process_app
from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.gateway import Gateway
from toto_gateway.routing.decision import GuardRouter, effective_policy
from toto_gateway.routing.labels import LabelBindings
from toto_gateway.runners.fake import FakeRunner
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.schemas import (ChatCompletionChunk, ChatCompletionRequest,
                                  ChatCompletionResponse, Message)
from toto_gateway.signals.extractor import HeuristicExtractor
from toto_gateway.signals.guards import RuleGuard
from toto_gateway.trace import MemoryTraceWriter

_RAW = {"labels": {
    "code_generation": {"model": "or-qwen3-coder-flash", "desc": "write or debug code"},
    "other": {"model": None, "desc": "none of the above"},
}}


def _catalog() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id="or-haiku-4.5", lane="economy", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="or-qwen3-coder-flash", lane="economy", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="local-box", lane="economy", endpoint="fake", residency_class="in_perimeter"),
    ])


class _LabelRunner:
    """Echoes everything via a real FakeRunner, but for the classifier call (system prompt = the
    label prompt) returns a fixed reply and RECORDS which catalog entry the call ran on."""

    last_classifier_model: str | None = None

    def __init__(self, entry: CatalogEntry, label_reply: str) -> None:
        self.entry = entry
        self.runner_id = f"label-{entry.id}"
        self._fake = FakeRunner(entry)
        self._reply = label_reply

    def _is_label(self, req: ChatCompletionRequest) -> bool:
        sys = req.messages[0].text() if req.messages and req.messages[0].role == "system" else ""
        return "label one piece of work" in sys

    async def chat(self, req, entry) -> ChatCompletionResponse:
        if self._is_label(req):
            _LabelRunner.last_classifier_model = entry.id
            return ChatCompletionResponse.simple(
                model=entry.id, content=self._reply, usage=self._fake._usage(req, ""))
        return await self._fake.chat(req, entry)

    def stream(self, req, entry) -> AsyncIterator[ChatCompletionChunk]:
        return self._fake.stream(req, entry)


def _gw(*, label_reply='{"label": "code_generation"}', classifier="or-haiku-4.5"):
    registry = RunnerRegistry(factory=lambda e: _LabelRunner(e, label_reply))
    gw = Gateway(
        catalog=_catalog(), registry=registry, writer=MemoryTraceWriter(),
        guard=RuleGuard(), router=GuardRouter(), extractor=HeuristicExtractor(),
        labels=LabelBindings(_raw=_RAW), benchmarks=None, classifier_model=classifier,
        label_timeout_ms=200,
    )
    return gw


def _req(content="write a helper function", *, model="smart"):
    return ChatCompletionRequest(model=model, messages=[Message(role="user", content=content)])


def _ident(*, classifier_model=None, taxonomy=None, adoptions=None):
    routing_policy = {}
    if classifier_model is not None:
        routing_policy["classifier_model"] = classifier_model
    if taxonomy is not None:
        routing_policy["taxonomy"] = taxonomy
    return types.SimpleNamespace(
        catalog_policy=None, routing_policy=routing_policy or None,
        org_id="o_1", team_id=None, catalog_adoptions=tuple(adoptions or ()))


_TAX_LOCAL = {"labels": {"restricted": {"constraint": "local_only", "desc": "keep in perimeter"}}}


# --- Policy unpack (unit) --------------------------------------------------------------

def test_effective_policy_unpacks_classifier_model():
    pol = effective_policy(_ident(classifier_model="or-sonnet-4.6"))
    assert pol.classifier_model == "or-sonnet-4.6"


def test_no_classifier_model_defaults_none():
    pol = effective_policy(_ident(taxonomy=_TAX_LOCAL))
    assert pol.classifier_model is None


# --- both call-site overrides ----------------------------------------------------------

@pytest.mark.asyncio
async def test_smart_path_uses_org_classifier():
    """A smart request classifies on the ORG's chosen model, not the gateway default."""
    gw = _gw(classifier="or-haiku-4.5")
    _LabelRunner.last_classifier_model = None
    await gw.complete(_req(), identity=_ident(classifier_model="or-sonnet-4.6"))
    assert _LabelRunner.last_classifier_model == "or-sonnet-4.6"


@pytest.mark.asyncio
async def test_explicit_data_policy_path_uses_org_classifier():
    """The explicit-model data-policy classify (taxonomy org) also honors the org classifier."""
    gw = _gw(classifier="or-haiku-4.5")
    _LabelRunner.last_classifier_model = None
    await gw.complete(_req(model="or-sonnet-4.6"),
                      identity=_ident(classifier_model="local-box", taxonomy=_TAX_LOCAL))
    assert _LabelRunner.last_classifier_model == "local-box"


@pytest.mark.asyncio
async def test_default_classifier_when_org_sets_none():
    """No org override → the gateway default classifier runs (unchanged behavior)."""
    gw = _gw(classifier="or-haiku-4.5")
    _LabelRunner.last_classifier_model = None
    await gw.complete(_req(), identity=_ident())
    assert _LabelRunner.last_classifier_model == "or-haiku-4.5"


# --- effective-catalog classify (org-adopted classifier) -------------------------------

@pytest.mark.asyncio
async def test_org_adopted_model_can_be_classifier():
    """An org-adopted model absent from the base catalog can be the classifier: _classify_text
    resolves against the caller's EFFECTIVE catalog. Classification succeeds and routes normally."""
    gw = _gw(classifier="or-haiku-4.5")
    adopted = {"id": "in-house-classifier", "lane": "economy", "endpoint": "fake",
               "residency_class": "in_perimeter"}
    _LabelRunner.last_classifier_model = None
    res = await gw.complete(_req(), identity=_ident(
        classifier_model="in-house-classifier", adoptions=[adopted]))
    assert _LabelRunner.last_classifier_model == "in-house-classifier"
    assert res.trace.route_reason == "label:code_generation"   # classified, not degraded
    assert res.trace.model == "or-qwen3-coder-flash"


@pytest.mark.asyncio
async def test_absent_classifier_degrades_not_500():
    """An org classifier id absent from the effective catalog degrades to classify_failed (the
    request still answers on the benchmark/fallback), never a 500."""
    gw = _gw(classifier="or-haiku-4.5")
    res = await gw.complete(_req(), identity=_ident(classifier_model="ghost-model"))
    assert res.trace.route_reason == "smart:classify_failed"


# --- admin PUT validation matrix -------------------------------------------------------

async def _org_client(gw):
    settings = default_settings(auth_token="", driver=False, fake_exec=False)
    ctx = in_process_app(gateway=gw, settings=settings)
    client, app = await ctx.__aenter__()
    auth = app.state.auth
    uid = await auth.create_user("owner@acme.com", None, email_verified=True)
    org = (await auth.resolve_membership(uid))["org_id"]
    sess = await auth.create_session(uid, 3600)
    return ctx, client, {"authorization": ""}, {"toto_session": sess}, org


@pytest.mark.asyncio
async def test_classifier_round_trips_and_rejects():
    gw = _gw()
    ctx, client, H, C, _ = await _org_client(gw)
    try:
        # default is none
        r = await client.get("/v1/admin/org/routing-policy", headers=H, cookies=C)
        assert r.status_code == 200 and r.json()["classifier_model"] is None

        # round-trip a valid catalog id
        r = await client.put("/v1/admin/org/routing-policy",
                             json={"classifier_model": "or-haiku-4.5"}, headers=H, cookies=C)
        assert r.status_code == 200, r.text
        assert r.json()["classifier_model"] == "or-haiku-4.5"
        r = await client.get("/v1/admin/org/routing-policy", headers=H, cookies=C)
        assert r.json()["classifier_model"] == "or-haiku-4.5"

        # unknown id → 422 unknown_model
        r = await client.put("/v1/admin/org/routing-policy",
                             json={"classifier_model": "ghost"}, headers=H, cookies=C)
        assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_model"

        # cloud classifier under a local-required taxonomy → 422 classifier_not_in_perimeter
        r = await client.put("/v1/admin/org/routing-policy",
                             json={"classifier_model": "or-haiku-4.5", "taxonomy": _TAX_LOCAL},
                             headers=H, cookies=C)
        assert r.status_code == 422 and r.json()["error"]["code"] == "classifier_not_in_perimeter"

        # in-perimeter classifier under the same taxonomy → 200
        r = await client.put("/v1/admin/org/routing-policy",
                             json={"classifier_model": "local-box", "taxonomy": _TAX_LOCAL},
                             headers=H, cookies=C)
        assert r.status_code == 200, r.text
        assert r.json()["classifier_model"] == "local-box"

        # omitted on a later PUT → full-replace back to default (none)
        r = await client.put("/v1/admin/org/routing-policy", json={}, headers=H, cookies=C)
        assert r.json()["classifier_model"] is None
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_cloud_classifier_ok_without_local_taxonomy():
    """No local_only/deny constraint → a cloud classifier is fine (the guarantee only bites when the
    org's data policy actually requires the perimeter)."""
    gw = _gw()
    ctx, client, H, C, _ = await _org_client(gw)
    try:
        tax = {"labels": {"public": {"constraint": "allow"}}}
        r = await client.put("/v1/admin/org/routing-policy",
                             json={"classifier_model": "or-haiku-4.5", "taxonomy": tax},
                             headers=H, cookies=C)
        assert r.status_code == 200, r.text
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_team_put_rejects_classifier_model():
    """The classifier is an ORG-level setting; a team PUT that names it is rejected (org-only)."""
    gw = _gw()
    settings = default_settings(auth_token="", driver=False, fake_exec=False)
    async with in_process_app(gateway=gw, settings=settings) as (client, app):
        auth = app.state.auth
        uid = await auth.create_user("owner@acme.com", None, email_verified=True)
        org = (await auth.resolve_membership(uid))["org_id"]
        team_id = await auth.create_team(org, "eng")
        sess = await auth.create_session(uid, 3600)
        H, C = {"authorization": ""}, {"toto_session": sess}
        r = await client.put(f"/v1/admin/teams/{team_id}/routing-policy",
                             json={"classifier_model": "or-haiku-4.5"}, headers=H, cookies=C)
        assert r.status_code == 422 and r.json()["error"]["code"] == "classifier_model_org_only"
