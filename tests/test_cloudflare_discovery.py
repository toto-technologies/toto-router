"""Cloudflare Workers AI discovery + adoption — offline. Covers the model-catalog mapping
(@cf slugs → lean rows), catalog reconcile, the discovery route (no-creds graceful + happy path
over a mocked transport), and adoption wiring (materialize to the templated base_url + the
two-part-credential guards). No live Cloudflare call."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from harness.appharness import in_process_app
from toto_gateway.catalog import Catalog

# One raw Cloudflare models/search entry (properties = list of {property_id, value}, per CF docs).
CF_RAW = {
    "name": "@cf/openai/gpt-oss-120b",
    "task": {"name": "Text Generation"},
    "description": "OpenAI gpt-oss 120B open model.",
    "properties": [
        {"property_id": "context_window", "value": "128000"},
        {"property_id": "function_calling", "value": "true"},
    ],
}


# --- mapping ---


def test_map_parses_slug_ctx_and_tools():
    from toto_gateway.catalog_sync import map_cloudflare_model

    row = map_cloudflare_model(CF_RAW)
    assert row["slug"] == "@cf/openai/gpt-oss-120b"
    assert row["name"] == "gpt-oss-120b"  # last path segment
    assert row["context_window"] == 128000
    assert row["tools"] is True and row["vision"] is False
    assert row["price_in"] == 0.0 and row["price_out"] == 0.0  # CF models API has no price


def test_map_skips_non_text_generation():
    from toto_gateway.catalog_sync import map_cloudflare_model

    assert map_cloudflare_model({"name": "@cf/openai/whisper", "task": {"name": "Automatic Speech Recognition"}}) is None
    assert map_cloudflare_model({"task": {"name": "Text Generation"}}) is None  # no name → skip


def test_map_missing_properties_permissive():
    from toto_gateway.catalog_sync import map_cloudflare_model

    row = map_cloudflare_model({"name": "@cf/x/y"})  # no task, no properties
    assert row is not None
    assert row["context_window"] == 0 and row["tools"] is False


def test_reconcile_flags_cataloged_against_real_fragment():
    from toto_gateway.catalog_sync import map_cloudflare_model, reconcile_cloudflare_library

    cat = Catalog.load("catalog.cloudflare.yaml")
    rows = [map_cloudflare_model(CF_RAW),
            map_cloudflare_model({"name": "@cf/nobody/not-in-catalog", "task": {"name": "Text Generation"}})]
    out = reconcile_cloudflare_library(cat.models, rows)
    by = {m["slug"]: m for m in out}
    assert by["@cf/openai/gpt-oss-120b"]["cataloged"] is True
    assert by["@cf/openai/gpt-oss-120b"]["catalog_id"] == "cf-gpt-oss-120b"
    assert by["@cf/nobody/not-in-catalog"]["cataloged"] is False


# --- discovery route ---


def _cf_app(tmp_path, monkeypatch, *, token: str | None, account: str | None):
    from toto_gateway.app import create_app
    from toto_gateway.config import Settings

    for name, val in (("CLOUDFLARE_API_TOKEN", token), ("CLOUDFLARE_ACCOUNT_ID", account)):
        monkeypatch.delenv(name, raising=False) if val is None else monkeypatch.setenv(name, val)
    settings = Settings(catalog="catalog.cloudflare.yaml", trace_jsonl="", trace_db="",
                        trace_stdout=False, auth_token="test-operator-token",
                        db=f"{tmp_path}/gw.db", fake_exec=True)
    return create_app(settings=settings)


_HDR = {"Authorization": "Bearer test-operator-token"}


def test_route_no_creds_degrades_gracefully(tmp_path, monkeypatch):
    with TestClient(_cf_app(tmp_path, monkeypatch, token=None, account=None)) as client:
        body = client.get("/v1/admin/catalog/discovery/cloudflare", headers=_HDR).json()
    assert body["provider"] == "cloudflare"
    assert body["key_present"] is False and body["total"] == 0 and body["models"] == []
    assert "CLOUDFLARE_API_TOKEN" in body["error"]  # honest tab-chip message, not a wall


def _cf_transport():
    def handler(request):
        assert request.url.host == "api.cloudflare.com"
        assert "/ai/models/search" in request.url.path
        return httpx.Response(200, json={"result": [CF_RAW], "result_info": {"total_pages": 1}})

    return httpx.MockTransport(handler)


def test_route_happy_path_flags_cataloged(tmp_path, monkeypatch):
    import toto_gateway.catalog_sync as cs

    orig = httpx.AsyncClient

    def patched(*a, **k):
        k["transport"] = _cf_transport()
        return orig(*a, **k)

    monkeypatch.setattr(cs.httpx, "AsyncClient", patched)
    with TestClient(_cf_app(tmp_path, monkeypatch, token="cf_tok", account="acc123")) as client:
        body = client.get("/v1/admin/catalog/discovery/cloudflare", headers=_HDR).json()
    assert body["key_present"] is True and body["error"] is None and body["total"] == 1
    m = body["models"][0]
    assert m["slug"] == "@cf/openai/gpt-oss-120b" and m["cataloged"] is True
    assert m["catalog_id"] == "cf-gpt-oss-120b"


def test_route_requires_auth(tmp_path, monkeypatch):
    with TestClient(_cf_app(tmp_path, monkeypatch, token=None, account=None)) as client:
        r = client.get("/v1/admin/catalog/discovery/cloudflare",
                       headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


# --- adoption wiring (materialize + two-part-credential guards) ---


async def test_adopt_cloudflare_materializes_templated_base_url(monkeypatch):
    import toto_gateway.routes.admin_catalog_adoptions as adopt

    async def fake_fetch(token, account):
        return {"models": [{"slug": "@cf/openai/gpt-oss-20b", "name": "gpt-oss-20b",
                            "context_window": 128000, "price_in": 0.0, "price_out": 0.0,
                            "tools": True, "vision": False}], "filtered_out": 0, "error": None}

    monkeypatch.setattr(adopt, "fetch_cloudflare_library", fake_fetch)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf_tok")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")

    async with in_process_app() as (client, app):  # OSS operator → `local` adoption scope
        r = await client.post("/v1/admin/catalog/adoptions",
                              json={"source": "cloudflare", "slug": "@cf/openai/gpt-oss-20b"})
        assert r.status_code in (200, 201), r.text
        entry = r.json()["entry"]
        assert entry["id"] == "cf-gpt-oss-20b"  # cf- prefix, no banned tier word
        assert entry["provider"] == "cloudflare"
        # base_url keeps the template — the runner expands it, same as a shipped CF row
        from toto_gateway.catalog import CatalogEntry
        rows = await app.state.auth.list_adoptions("local")
        stored = CatalogEntry.model_validate(next(r["entry"] for r in rows if r["id"] == "cf-gpt-oss-20b"))
        assert stored.base_url == "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1"
        assert stored.upstream_model == "@cf/openai/gpt-oss-20b"


async def test_adopt_cloudflare_without_account_id_503s(monkeypatch):
    import toto_gateway.routes.admin_catalog_adoptions as adopt

    async def fake_fetch(token, account):  # should never be reached without the account id
        raise AssertionError("fetch attempted without CLOUDFLARE_ACCOUNT_ID")

    monkeypatch.setattr(adopt, "fetch_cloudflare_library", fake_fetch)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf_tok")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)

    async with in_process_app() as (client, app):
        r = await client.post("/v1/admin/catalog/adoptions",
                              json={"source": "cloudflare", "slug": "@cf/openai/gpt-oss-20b"})
        assert r.status_code == 503
        assert "CLOUDFLARE_ACCOUNT_ID" in r.json()["error"]["message"]
