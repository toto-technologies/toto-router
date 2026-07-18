"""Tests for the raw passthrough's routing floor — GuardRouter + Policy.

Content-based lane *selection* now lives in the driver's metadata classifier
(tests/test_driver_classify.py). Here we test only the deterministic safety floor: the
fail-closed guard can force local, hard policy constraints can force a lane, and otherwise the
requested model passes through unchanged. All offline (FakeRunner echoes deterministically).
"""

from __future__ import annotations

import asyncio

import pytest

from toto_gateway.catalog import Catalog
from toto_gateway.gateway import Gateway
from toto_gateway.pipeline import ALLOW, DOWNGRADE_LOCAL, GuardVerdict, Signal
from toto_gateway.routing.decision import GuardRouter
from toto_gateway.routing.policy import Policy
from toto_gateway.runners.fake import FakeRunner
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.schemas import ChatCompletionRequest, Message
from toto_gateway.trace import MemoryTraceWriter

CATALOG_PATH = "catalog.yaml"


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return Catalog.load(CATALOG_PATH)


@pytest.fixture()
def writer() -> MemoryTraceWriter:
    return MemoryTraceWriter()


@pytest.fixture()
def registry(catalog: Catalog) -> RunnerRegistry:
    return RunnerRegistry(factory=lambda entry: FakeRunner(entry))


def _make_req(text: str, model: str = "echo-cloud") -> ChatCompletionRequest:
    return ChatCompletionRequest(model=model, messages=[Message(role="user", content=text)])


# --- Unit: GuardRouter decisions --------------------------------------------


def test_passthrough_honors_requested_model(catalog: Catalog) -> None:
    d = GuardRouter().decide(_make_req("anything", model="echo-cloud"),
                             Signal(), GuardVerdict(action=ALLOW), catalog)
    assert d.model_id == "echo-cloud"
    assert d.reason == "passthrough"


def test_guard_downgrade_local_forces_in_perimeter(catalog: Catalog) -> None:
    """DOWNGRADE_LOCAL must land on an in_perimeter model — the guard keeps real teeth."""
    d = GuardRouter().decide(_make_req("decompose this investment thesis into key risks"),
                             Signal(), GuardVerdict(action=DOWNGRADE_LOCAL), catalog)
    assert catalog.require(d.model_id).residency_class == "in_perimeter"
    assert d.reason == "guard:downgrade_local"


def test_policy_redact_intent_forces_local(catalog: Catalog) -> None:
    """signal.intent=='redact' is forced local by policy, even with a frontier model requested."""
    d = GuardRouter().decide(_make_req("summarize these 10-K filings with citations"),
                             Signal(intent="redact"), GuardVerdict(action=ALLOW), catalog)
    assert catalog.require(d.model_id).residency_class == "in_perimeter"
    assert "policy" in d.reason


# --- Unit: Policy.conflicts() -----------------------------------------------


def test_policy_conflicts_detected() -> None:
    policy = Policy(
        _raw={
            "rules": [
                {"intent": "summarize", "lane": "economy"},
                {"intent": "summarize", "lane": "frontier"},  # contradiction
            ],
            "max_local_context": 32768,
        }
    )
    conflicts = policy.conflicts()
    assert len(conflicts) >= 1
    assert any("summarize" in c for c in conflicts)


def test_policy_no_false_conflicts() -> None:
    policy = Policy(
        _raw={
            "rules": [
                {"intent": "redact", "lane": "economy"},
                {"when": "mnpi", "lane": "economy"},
            ],
            "max_local_context": 32768,
        }
    )
    assert policy.conflicts() == []


# --- Gateway integration ----------------------------------------------------


def _guard_gateway(catalog: Catalog, registry: RunnerRegistry, writer: MemoryTraceWriter) -> Gateway:
    """The raw passthrough wired with its safety floor (HeuristicExtractor + RuleGuard +
    GuardRouter), FakeRunner on every lane."""
    from toto_gateway.signals.extractor import HeuristicExtractor
    from toto_gateway.signals.guards import RuleGuard

    return Gateway(
        catalog=catalog, registry=registry, writer=writer,
        extractor=HeuristicExtractor(), guard=RuleGuard(), router=GuardRouter(),
    )


def test_gateway_passthrough_honors_model(catalog, registry, writer) -> None:
    gw = _guard_gateway(catalog, registry, writer)
    asyncio.run(gw.complete(_make_req("hello there, how are you", model="echo-cloud")))
    trace = writer.records[-1]
    assert trace.status == "ok", f"dispatch failed: {trace.error}"
    assert trace.model == "echo-cloud"
    assert trace.route_reason == "passthrough"


def test_gateway_redact_downgrades_local(catalog, registry, writer) -> None:
    """'redact all MNPI…' → guard downgrade_local → an in_perimeter model, end-to-end."""
    gw = _guard_gateway(catalog, registry, writer)
    asyncio.run(gw.complete(_make_req("redact all MNPI from this draft deal memo", model="echo-cloud")))
    trace = writer.records[-1]
    assert trace.status == "ok", f"dispatch failed: {trace.error}"
    assert trace.residency_class == "in_perimeter"
    assert trace.route_reason in ("guard:downgrade_local", "policy:local")
