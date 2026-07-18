"""Cyclical availability probe: pure reconcile_availability() over vanished/undeclared/key-absent/
empty cases, the GET/POST endpoints (stubbed probe result), and fetch_provider_models via a
MockTransport."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from toto_gateway.catalog import CatalogEntry
from toto_gateway.catalog_sync import fetch_provider_models, reconcile_availability

BASE = "https://api.directlabs.example/v1"


def _entry(**kw) -> CatalogEntry:
    base = {"lane": "economy", "endpoint": "openai", "residency_class": "cloud",
            "base_url": BASE, "api_key_env": "DIRECTLABS_API_KEY"}
    return CatalogEntry(**{**base, **kw})


# --- reconcile_availability (pure) ---


def test_vanished_and_undeclared_both_detected():
    entries = [_entry(id="dl-chat", upstream_model="deep-chat-v3"),
               _entry(id="dl-gone", upstream_model="deep-chat-v2")]  # v2 retired upstream
    live = {BASE: ["deep-chat-v3", "deep-chat-v4"]}  # v4 is new, undeclared
    out = reconcile_availability(entries, live)
    assert out[BASE]["vanished"] == ["deep-chat-v2"]
    assert out[BASE]["undeclared"] == ["deep-chat-v4"]


def test_all_declared_ids_live_no_drift():
    entries = [_entry(id="dl-chat", upstream_model="deep-chat-v3")]
    out = reconcile_availability(entries, {BASE: ["deep-chat-v3"]})
    assert out[BASE] == {"vanished": [], "undeclared": []}


def test_key_absent_provider_skipped():
    """A base_url absent from the live map (its key wasn't set → not probed) yields nothing."""
    entries = [_entry(id="dl-chat", upstream_model="deep-chat-v3")]
    out = reconcile_availability(entries, {})  # nothing was probed
    assert out == {}


def test_non_openai_and_null_base_url_not_declared():
    entries = [_entry(id="claude", endpoint="anthropic", base_url=None,
                      api_key_env="ANTHROPIC_API_KEY", upstream_model="claude-sonnet-4-6"),
               _entry(id="dl-chat", upstream_model="deep-chat-v3")]
    # anthropic entry never counts as declared → deep-chat-v3 is the only declared id
    out = reconcile_availability(entries, {BASE: ["deep-chat-v3", "other"]})
    assert out[BASE]["vanished"] == []
    assert out[BASE]["undeclared"] == ["other"]


def test_empty_catalog():
    assert reconcile_availability([], {}) == {}
    # live ids with nothing declared → all undeclared
    out = reconcile_availability([], {BASE: ["a", "b"]})
    assert out[BASE]["undeclared"] == ["a", "b"] and out[BASE]["vanished"] == []


def test_effective_upstream_falls_back_to_id():
    entries = [_entry(id="deep-chat-v3", upstream_model=None)]  # id IS the upstream model
    out = reconcile_availability(entries, {BASE: ["deep-chat-v3"]})
    assert out[BASE] == {"vanished": [], "undeclared": []}


# --- fetch_provider_models (MockTransport) ---


async def _fetch(handler):
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        return await fetch_provider_models(client, BASE, "sk-key")


def test_fetch_openai_list_shape():
    import asyncio

    def handler(request):
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer sk-key"
        return httpx.Response(200, json={"data": [{"id": "deep-chat-v3"}, {"id": "deep-chat-v4"},
                                                  {"no_id": True}, "junk"]})

    ids = asyncio.run(_fetch(handler))
    assert ids == ["deep-chat-v3", "deep-chat-v4"]  # non-dict / id-less rows tolerated


def test_fetch_raises_on_http_error():
    import asyncio

    def handler(request):
        return httpx.Response(500, json={})

    try:
        asyncio.run(_fetch(handler))
        assert False, "expected httpx error"
    except httpx.HTTPStatusError:
        pass


# --- endpoints ---


def _app(tmp_path, monkeypatch):
    from toto_gateway.app import create_app
    from toto_gateway.config import Settings

    settings = Settings(catalog="catalog.yaml", trace_jsonl="", trace_db="", trace_stdout=False,
                        auth_token="test-operator-token", db=f"{tmp_path}/gw.db", fake_exec=True)
    return create_app(settings=settings)


def test_get_availability_empty_before_first_probe(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        body = client.get("/v1/admin/catalog/availability").json()
    assert body == {"checked_at": None, "providers": {}}


def test_get_availability_returns_stored_result(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        app.state.catalog_availability = {"checked_at": 123.0, "providers": {
            BASE: {"checked_at": 123.0, "vanished": ["deep-chat-v2"], "undeclared": [], "error": None}}}
        body = client.get("/v1/admin/catalog/availability").json()
    assert body["providers"][BASE]["vanished"] == ["deep-chat-v2"]


def test_post_availability_triggers_probe(tmp_path, monkeypatch):
    import toto_gateway.routes.admin_catalog_sync as route_mod

    async def fake_probe(entries):
        return {"checked_at": 1.0, "providers": {
            BASE: {"checked_at": 1.0, "vanished": [], "undeclared": ["new-model"], "error": None}}}

    monkeypatch.setattr(route_mod, "probe_availability", fake_probe)
    app = _app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        body = client.post("/v1/admin/catalog/availability").json()
    assert body["providers"][BASE]["undeclared"] == ["new-model"]
    assert app.state.catalog_availability == body  # stored for the subsequent GET


def test_availability_requires_auth(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as client:
        r = client.get("/v1/admin/catalog/availability",
                       headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
