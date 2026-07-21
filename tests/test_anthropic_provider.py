"""Anthropic as a direct provider: the catalog.anthropic.yaml fragment shape, registry routing
(every endpoint=anthropic entry → the native FrontierRunner, whatever its lane), stored-key
resolution at the frontier client seam (stored beats env, fail-closed on unreadable), the native
wire contract (x-api-key + anthropic-version on POST /v1/messages), and the operator key-store
row."""

from __future__ import annotations

import contextvars
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from toto_gateway.app import create_app
from toto_gateway.catalog import Catalog
from toto_gateway.config import Settings
from toto_gateway.credentials import ProviderCredentialUnavailable, byok_keys, byok_unavailable_envs
from toto_gateway.runners.frontier import FrontierRunner
from toto_gateway.runners.registry import default_factory

FRAGMENT = "catalog.anthropic.yaml"


# --- fragment shape -----------------------------------------------------------


def test_fragment_loads_standalone_and_pins_shape():
    cat = Catalog.load(FRAGMENT)
    assert {m.id for m in cat.models} == {"claude-sonnet-5", "claude-opus-4.8", "claude-haiku-4.5"}
    for m in cat.models:
        assert m.endpoint == "anthropic"            # native Messages API, not the compat wire
        assert m.api_key_env == "ANTHROPIC_API_KEY"
        assert m.residency_class == "cloud"
        assert m.price_usd_per_1k.cache_write_multiplier == 1.25  # Anthropic 5-min write premium
        assert m.price_usd_per_1k.cache_read_multiplier == 0.1
    # published $/1M ÷ 1000 (see fragment header for sources)
    assert cat.get("claude-opus-4.8").price_usd_per_1k.prompt == 0.005
    assert cat.get("claude-opus-4.8").price_usd_per_1k.completion == 0.025
    assert cat.get("claude-haiku-4.5").context_window == 200000
    assert cat.get("claude-sonnet-5").context_window == 1000000


def test_registry_routes_every_anthropic_entry_to_frontier_runner():
    """endpoint names the wire protocol: the economy-lane haiku entry must dispatch natively,
    never fall through to the bare-URL MLX runner (the pre-fragment registry did exactly that)."""
    for entry in Catalog.load(FRAGMENT).models:
        assert isinstance(default_factory(entry), FrontierRunner), entry.id


# --- credential seam ----------------------------------------------------------


def _entry():
    return Catalog.load(FRAGMENT).require("claude-haiku-4.5")


def test_stored_key_builds_ephemeral_client_and_beats_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-stale")
    runner = FrontierRunner(_entry())

    ctx = contextvars.copy_context()
    ctx.run(byok_keys.set, {"ANTHROPIC_API_KEY": "sk-ant-stored-key"})
    client = ctx.run(runner._get_client)
    assert client.api_key == "sk-ant-stored-key"    # stored wins
    assert runner._client is None                    # ephemeral — never cached

    # env fallback outside the overlay: cached client resolves the env key (SDK default)
    c1 = runner._get_client()
    assert c1.api_key == "sk-ant-env-stale"
    assert runner._get_client() is c1                # cached


def test_unreadable_stored_key_fails_closed():
    runner = FrontierRunner(_entry())
    ctx = contextvars.copy_context()
    ctx.run(byok_unavailable_envs.set, frozenset({"ANTHROPIC_API_KEY"}))
    with pytest.raises(ProviderCredentialUnavailable) as err:
        ctx.run(runner._get_client)
    assert err.value.providers == ("anthropic",)


# --- wire contract: native Messages API auth + endpoint ------------------------


async def test_wire_sends_x_api_key_and_anthropic_version():
    from anthropic import AsyncAnthropic

    from toto_gateway.schemas import ChatCompletionRequest

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["x-api-key"] = request.headers.get("x-api-key")
        captured["anthropic-version"] = request.headers.get("anthropic-version")
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "msg_01", "type": "message", "role": "assistant",
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 2},
        })

    client = AsyncAnthropic(
        api_key="sk-ant-wire-test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    entry = _entry()
    runner = FrontierRunner(entry, client=client)
    resp = await runner.chat(
        ChatCompletionRequest(model=entry.id,
                              messages=[{"role": "user", "content": "hello"}]),
        entry,
    )

    assert captured["path"] == "/v1/messages"
    # NATIVE API auth: the key rides x-api-key (+ anthropic-version), never Authorization: Bearer.
    assert captured["x-api-key"] == "sk-ant-wire-test"
    assert captured["anthropic-version"]
    assert captured["authorization"] is None
    assert captured["body"]["model"] == "claude-haiku-4-5"  # upstream id, not the catalog id
    assert resp.choices[0].message.content == "hi"
    assert resp.model == entry.id


# --- operator key store row ---------------------------------------------------


def test_admin_key_store_round_trip():
    settings = Settings(
        catalog="catalog.yaml", trace_jsonl="", trace_db="", trace_stdout=False,
        driver=True, fake_exec=True, db=":memory:", toto_token="",
        driver_model="echo-cloud", triage_model="echo-local",
        cookie_secure=False, credentials_secret="unit-test-secret",
    )
    with TestClient(create_app(settings=settings)) as client:
        r = client.put("/v1/admin/provider-keys/anthropic", json={"key": "sk-ant-api03-TEST42"})
        assert r.status_code == 200, r.text
        assert r.json() == {"provider": "anthropic", "configured": True, "masked": "ST42",
                            "source": "stored"}
        rows = {p["provider"]: p for p in client.get("/v1/admin/provider-keys").json()["providers"]}
        assert rows["anthropic"]["source"] == "stored"
        assert rows["anthropic"]["label"] == "Anthropic"
        assert client.delete("/v1/admin/provider-keys/anthropic").json()["configured"] is False
