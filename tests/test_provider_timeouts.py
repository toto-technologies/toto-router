"""P1 — tunable, sane per-provider timeouts (Wave 1 provider-I/O hardening).

The SDK default read timeout is 600s: one slow provider then pins an event-loop task + a
concurrency slot for 10 minutes. These assert the runners build their clients with our explicit,
env-tunable httpx.Timeout instead — and that the default caps the 600s bleed at 60s.

Note on the integration altitude: the faults harness fakes providers via httpx.MockTransport,
which does NOT enforce httpx timeouts (it just awaits the handler). So a "hung upstream aborts in
<=read_timeout" assertion needs a real socket, which the offline harness deliberately avoids. The
observable stall behavior the harness CAN drive is covered by the stream-stall deadline (P5, our
own asyncio.wait_for) and the breaker (P4). Here we prove the wiring: the client carries the
bounded timeout, not the SDK default.
"""

from __future__ import annotations

from toto_gateway.catalog import CatalogEntry
from toto_gateway.config import Settings


def _entry(**kw) -> CatalogEntry:
    base = dict(id="x", lane="frontier", endpoint="openai", residency_class="cloud",
                upstream_model="m")
    base.update(kw)
    return CatalogEntry(**base)


# --- unit: Settings.provider_timeout ------------------------------------------------------

def test_provider_timeout_caps_read_and_tracks_write_pool():
    t = Settings(provider_connect_timeout=3, provider_read_timeout=42).provider_timeout()
    assert t.connect == 3 and t.read == 42 and t.write == 42 and t.pool == 42


def test_default_read_timeout_caps_the_600s_sdk_default():
    # The falsifiable done-criterion: the shipped default is 60s, not the SDK's 600s.
    assert Settings().provider_timeout().read == 60.0


def test_local_timeout_is_longer_and_independently_tunable():
    s = Settings(provider_read_timeout=60, provider_read_timeout_local=300)
    assert s.provider_timeout(local=True).read == 300
    assert s.provider_timeout(local=False).read == 60


def test_read_timeout_is_env_tunable(monkeypatch):
    monkeypatch.setenv("TOTO_GW_PROVIDER_READ_TIMEOUT", "17")
    assert Settings().provider_timeout().read == 17


# --- unit: each runner applies the configured timeout to its real client ------------------

def test_openai_runner_applies_configured_timeout(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import toto_gateway.runners.openai as oa

    monkeypatch.setattr(
        oa, "get_settings",
        lambda: Settings(provider_read_timeout=12, provider_connect_timeout=3))
    client = oa.OpenAIRunner(_entry())._get_client()
    assert client.timeout.read == 12 and client.timeout.connect == 3
    assert client.max_retries == 0  # our layer is the single retry authority (P2)


def test_frontier_runner_applies_configured_timeout(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    import toto_gateway.runners.frontier as fr

    monkeypatch.setattr(fr, "get_settings", lambda: Settings(provider_read_timeout=15))
    client = fr.FrontierRunner(_entry(endpoint="anthropic"))._get_client()
    assert client.timeout.read == 15
    assert client.max_retries == 0


def test_mlx_runner_applies_local_timeout(monkeypatch):
    import toto_gateway.runners.mlx as mx

    monkeypatch.setattr(mx, "get_settings", lambda: Settings(provider_read_timeout_local=123))
    runner = mx.MLXRunner(_entry(lane="economy", endpoint="http://localhost:8000/v1"))
    assert runner._client.timeout.read == 123
