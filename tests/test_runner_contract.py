"""Tests for the Runner contract — FakeRunner satisfies the Protocol.

Verifies:
- FakeRunner passes isinstance(runner, Runner) (runtime_checkable Protocol)
- cartridge_manifest() shape
- load/unload raise NotImplementedInPhase0
- health() returns Telemetry
"""

from __future__ import annotations

import pytest

from toto_gateway.catalog import CatalogEntry, Price
from toto_gateway.runners.base import (
    CartridgeManifest,
    NotImplementedInPhase0,
    Runner,
    Telemetry,
)
from toto_gateway.runners.fake import FakeRunner
from toto_gateway.schemas import Message


def _fake_entry(model_id: str = "echo-local") -> CatalogEntry:
    """Minimal fake CatalogEntry for testing the runner contract."""
    return CatalogEntry(
        id=model_id,
        lane="fake",
        endpoint="fake",
        residency_class="in_perimeter",
        price_usd_per_1k=Price(prompt=0.0, completion=0.0),
    )


@pytest.fixture()
def fake_runner() -> FakeRunner:
    return FakeRunner(_fake_entry())


# --- Protocol conformance ---


def test_fake_runner_is_runner_instance(fake_runner: FakeRunner):
    """FakeRunner satisfies the @runtime_checkable Runner Protocol."""
    assert isinstance(fake_runner, Runner)


def test_fake_runner_has_runner_id(fake_runner: FakeRunner):
    """runner_id attribute is present and a non-empty string."""
    assert hasattr(fake_runner, "runner_id")
    assert isinstance(fake_runner.runner_id, str)
    assert fake_runner.runner_id


def test_runner_id_includes_model_id():
    """runner_id encodes the model id for traceability."""
    entry = _fake_entry("echo-cloud")
    runner = FakeRunner(entry)
    assert "echo-cloud" in runner.runner_id


# --- cartridge_manifest ---


def test_cartridge_manifest_returns_manifest(fake_runner: FakeRunner):
    manifest = fake_runner.cartridge_manifest()
    assert isinstance(manifest, CartridgeManifest)


def test_cartridge_manifest_base_model(fake_runner: FakeRunner):
    """base_model in manifest matches the entry id."""
    manifest = fake_runner.cartridge_manifest()
    assert manifest.base_model == "echo-local"


def test_cartridge_manifest_empty_adapters(fake_runner: FakeRunner):
    """adapters list is empty in Phase 0."""
    manifest = fake_runner.cartridge_manifest()
    assert manifest.adapters == []


def test_cartridge_manifest_empty_contexts(fake_runner: FakeRunner):
    """contexts list is empty in Phase 0."""
    manifest = fake_runner.cartridge_manifest()
    assert manifest.contexts == []


# --- load / unload (Phase 0 stubs) ---


@pytest.mark.asyncio
async def test_load_raises_not_implemented_phase0(fake_runner: FakeRunner):
    """load() raises NotImplementedInPhase0."""
    with pytest.raises(NotImplementedInPhase0):
        await fake_runner.load("some-cartridge")


@pytest.mark.asyncio
async def test_unload_raises_not_implemented_phase0(fake_runner: FakeRunner):
    """unload() raises NotImplementedInPhase0."""
    with pytest.raises(NotImplementedInPhase0):
        await fake_runner.unload("some-cartridge")


def test_not_implemented_phase0_is_not_implemented_error():
    """NotImplementedInPhase0 is a subclass of NotImplementedError."""
    assert issubclass(NotImplementedInPhase0, NotImplementedError)


# --- health ---


def test_health_returns_telemetry(fake_runner: FakeRunner):
    """health() returns a Telemetry instance."""
    result = fake_runner.health()
    assert isinstance(result, Telemetry)


def test_health_is_healthy(fake_runner: FakeRunner):
    """Fake runner reports healthy=True."""
    result = fake_runner.health()
    assert result.healthy is True


def test_telemetry_has_required_fields(fake_runner: FakeRunner):
    """Telemetry has utilization, queue_depth, hot_cartridges fields."""
    t = fake_runner.health()
    assert hasattr(t, "utilization")
    assert hasattr(t, "queue_depth")
    assert hasattr(t, "hot_cartridges")
    assert isinstance(t.hot_cartridges, list)


# --- models() ---


def test_models_returns_list(fake_runner: FakeRunner):
    """models() returns a list of Model objects."""
    from toto_gateway.schemas import Model
    result = fake_runner.models()
    assert isinstance(result, list)
    assert all(isinstance(m, Model) for m in result)


def test_models_includes_entry_id(fake_runner: FakeRunner):
    """The runner's own model id is included in the models list."""
    model_ids = [m.id for m in fake_runner.models()]
    assert "echo-local" in model_ids


def test_models_has_lane_and_residency(fake_runner: FakeRunner):
    """Model cards include the Toto extension fields lane + residency_class."""
    for model in fake_runner.models():
        assert model.lane is not None
        assert model.residency_class is not None


# --- chat() and stream() smoke (integration at runner level) ---


@pytest.mark.asyncio
async def test_chat_returns_response(fake_runner: FakeRunner):
    """chat() returns a non-None ChatCompletionResponse with non-empty content."""
    from toto_gateway.schemas import ChatCompletionRequest

    req = ChatCompletionRequest(
        model="echo-local",
        messages=[Message(role="user", content="runner contract chat test")],
    )
    entry = _fake_entry()
    resp = await fake_runner.chat(req, entry)
    assert resp is not None
    assert resp.choices[0].message.content is not None


@pytest.mark.asyncio
async def test_stream_yields_chunks(fake_runner: FakeRunner):
    """stream() yields at least a role chunk, content chunk, and stop chunk."""
    from toto_gateway.schemas import ChatCompletionRequest

    req = ChatCompletionRequest(
        model="echo-local",
        messages=[Message(role="user", content="runner stream test")],
        stream=True,
    )
    entry = _fake_entry()
    chunks = []
    async for chunk in fake_runner.stream(req, entry):
        chunks.append(chunk)

    assert len(chunks) >= 3  # role + content(s) + stop + usage


@pytest.mark.asyncio
async def test_stream_emits_trailing_usage_chunk(fake_runner: FakeRunner):
    """FakeRunner always emits a trailing usage chunk (contract for exact accounting)."""
    from toto_gateway.schemas import ChatCompletionRequest

    req = ChatCompletionRequest(
        model="echo-local",
        messages=[Message(role="user", content="trailing usage chunk")],
        stream=True,
    )
    entry = _fake_entry()
    usage_chunks = []
    async for chunk in fake_runner.stream(req, entry):
        if chunk.usage is not None:
            usage_chunks.append(chunk)

    assert len(usage_chunks) >= 1, "FakeRunner must emit at least one trailing usage chunk"
    assert usage_chunks[-1].usage.total_tokens > 0
