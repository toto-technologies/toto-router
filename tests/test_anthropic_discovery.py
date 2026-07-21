"""Anthropic model discovery + adoption — offline. Covers the /v1/models mapping (id/display_name
only — no context/price/capability claims), catalog reconcile, the discovery route (no-key graceful
+ happy path over a mocked transport asserting the native x-api-key + anthropic-version headers),
adoption wiring (endpoint=anthropic, no base_url, an- prefix), and the stored-beats-env key seam.
No live Anthropic call — live behavior is honestly unverified without a real key.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from harness.appharness import in_process_app
from toto_gateway.catalog import Catalog

# One raw Anthropic /v1/models entry (the endpoint returns id/display_name/created_at only).
AN_RAW = {"type": "model", "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5",
          "created_at": "2026-05-01T00:00:00Z"}


# --- mapping ---


def test_map_parses_id_and_display_name():
    from toto_gateway.catalog_sync import map_anthropic_model

    row = map_anthropic_model(AN_RAW)
    assert row["slug"] == "claude-sonnet-5"
    assert row["name"] == "Claude Sonnet 5"
    # the models API exposes no facts for these — unknowns stay 0/False, never invented
    assert row["context_window"] == 0
    assert row["price_in"] == 0.0 and row["price_out"] == 0.0
    assert row["tools"] is False and row["vision"] is False


def test_map_skips_nameless_entry():
    from toto_gateway.catalog_sync import map_anthropic_model

    assert map_anthropic_model({"display_name": "no id"}) is None


def test_reconcile_flags_cataloged_against_real_fragment():
    from toto_gateway.catalog_sync import map_anthropic_model, reconcile_anthropic_library

    cat = Catalog.load("catalog.anthropic.yaml")
    rows = [map_anthropic_model(AN_RAW),
            map_anthropic_model({"id": "claude-nonexistent", "display_name": "Not In Catalog"})]
    out = reconcile_anthropic_library(cat.models, rows)
    by = {m["slug"]: m for m in out}
    assert by["claude-sonnet-5"]["cataloged"] is True
    assert by["claude-sonnet-5"]["catalog_id"] == "claude-sonnet-5"
    assert by["claude-nonexistent"]["cataloged"] is False


# --- discovery route ---


def _an_app(tmp_path, monkeypatch, *, key: str | None):
    from toto_gateway.app import create_app
    from toto_gateway.config import Settings

    if key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    settings = Settings(catalog="catalog.anthropic.yaml", trace_jsonl="", trace_db="",
                        trace_stdout=False, auth_token="test-operator-token",
                        db=f"{tmp_path}/gw.db", fake_exec=True)
    return create_app(settings=settings)


_HDR = {"Authorization": "Bearer test-operator-token"}


def test_route_no_key_degrades_gracefully(tmp_path, monkeypatch):
    with TestClient(_an_app(tmp_path, monkeypatch, key=None)) as client:
        body = client.get("/v1/admin/catalog/discovery/anthropic", headers=_HDR).json()
    assert body["provider"] == "anthropic"
    assert body["key_present"] is False and body["total"] == 0 and body["models"] == []
    assert "ANTHROPIC_API_KEY" in body["error"]  # honest tab-chip message, not a wall


def _an_transport():
    def handler(request):
        assert request.url.host == "api.anthropic.com"
        assert request.url.path == "/v1/models"
        # the NATIVE auth scheme — x-api-key + anthropic-version, never Authorization: Bearer
        assert request.headers.get("x-api-key") == "an_key"
        assert request.headers.get("anthropic-version")
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [AN_RAW], "has_more": False,
                                         "first_id": AN_RAW["id"], "last_id": AN_RAW["id"]})

    return httpx.MockTransport(handler)


def test_route_happy_path_flags_cataloged(tmp_path, monkeypatch):
    import toto_gateway.catalog_sync as cs

    orig = httpx.AsyncClient

    def patched(*a, **k):
        k["transport"] = _an_transport()
        return orig(*a, **k)

    monkeypatch.setattr(cs.httpx, "AsyncClient", patched)
    with TestClient(_an_app(tmp_path, monkeypatch, key="an_key")) as client:
        body = client.get("/v1/admin/catalog/discovery/anthropic", headers=_HDR).json()
    assert body["key_present"] is True and body["error"] is None and body["total"] == 1
    m = body["models"][0]
    assert m["slug"] == "claude-sonnet-5" and m["cataloged"] is True
    assert m["catalog_id"] == "claude-sonnet-5"


def test_route_requires_auth(tmp_path, monkeypatch):
    with TestClient(_an_app(tmp_path, monkeypatch, key=None)) as client:
        r = client.get("/v1/admin/catalog/discovery/anthropic",
                       headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


# --- stored-beats-env key seam ---


def test_stored_or_env_overlay_wins(monkeypatch):
    from toto_gateway.credentials import byok_keys, stored_or_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "env_key")
    assert stored_or_env("ANTHROPIC_API_KEY") == "env_key"
    tok = byok_keys.set({"ANTHROPIC_API_KEY": "stored_key"})
    try:
        assert stored_or_env("ANTHROPIC_API_KEY") == "stored_key"
    finally:
        byok_keys.reset(tok)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert stored_or_env("ANTHROPIC_API_KEY") is None


# --- adoption wiring (native endpoint, no base_url) ---


async def test_adopt_anthropic_materializes_native_endpoint(monkeypatch):
    import toto_gateway.routes.admin_catalog_adoptions as adopt

    async def fake_fetch(key):
        return {"models": [{"slug": "claude-haiku-4-5", "name": "Claude Haiku 4.5",
                            "context_window": 0, "price_in": 0.0, "price_out": 0.0,
                            "tools": False, "vision": False}], "filtered_out": 0, "error": None}

    monkeypatch.setattr(adopt, "fetch_anthropic_library", fake_fetch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "an_key")

    async with in_process_app() as (client, app):  # OSS operator → `local` adoption scope
        r = await client.post("/v1/admin/catalog/adoptions",
                              json={"source": "anthropic", "slug": "claude-haiku-4-5"})
        assert r.status_code in (200, 201), r.text
        entry = r.json()["entry"]
        assert entry["id"] == "an-claude-haiku-4-5"  # an- prefix, no banned tier word
        assert entry["provider"] == "anthropic"
        from toto_gateway.catalog import CatalogEntry
        rows = await app.state.auth.list_adoptions("local")
        stored = CatalogEntry.model_validate(
            next(r["entry"] for r in rows if r["id"] == "an-claude-haiku-4-5"))
        # the native Messages adapter, same as a shipped catalog.anthropic.yaml row
        assert stored.endpoint == "anthropic" and stored.base_url is None
        assert stored.api_key_env == "ANTHROPIC_API_KEY"
        assert stored.upstream_model == "claude-haiku-4-5"


async def test_adopt_anthropic_without_key_503s(monkeypatch):
    import toto_gateway.routes.admin_catalog_adoptions as adopt

    async def fake_fetch(key):  # should never be reached without the key
        raise AssertionError("fetch attempted without ANTHROPIC_API_KEY")

    monkeypatch.setattr(adopt, "fetch_anthropic_library", fake_fetch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async with in_process_app() as (client, app):
        r = await client.post("/v1/admin/catalog/adoptions",
                              json={"source": "anthropic", "slug": "claude-haiku-4-5"})
        assert r.status_code == 503
        assert "ANTHROPIC_API_KEY" in r.json()["error"]["message"]


# --- catalog row vision fact (picker glyph source) ---


def test_model_row_vision_known_vs_unknown():
    from toto_gateway.catalog import CatalogEntry, Price
    from toto_gateway.routes.admin_catalog import _model_row

    base = dict(lane="economy", endpoint="openai", base_url="https://x/v1", api_key_env="K",
                residency_class="cloud", price_usd_per_1k=Price(prompt=0, completion=0),
                context_window=1, upstream_model="u")
    seeing = CatalogEntry(id="a", modalities=("text", "image"), **base)
    text_only = CatalogEntry(id="b", modalities=("text",), **base)
    unknown = CatalogEntry(id="c", **base)  # no modalities declared → no claim either way
    assert _model_row(seeing)["vision"] is True
    assert _model_row(text_only)["vision"] is False
    assert _model_row(unknown)["vision"] is None
