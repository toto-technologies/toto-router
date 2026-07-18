"""Fireworks catalog sync: pure reconcile() over every status/drift kind + the route (no-key
and happy paths via a mocked transport)."""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.catalog_sync import reconcile

ACCT = "toto-tech"


def _entry(**kw) -> CatalogEntry:
    base = {"lane": "economy", "endpoint": "openai", "residency_class": "cloud",
            "base_url": "https://api.fireworks.ai/inference/v1", "api_key_env": "FIREWORKS_API_KEY"}
    return CatalogEntry(**{**base, **kw})


def _model(name: str, base_model: str = "accounts/fireworks/models/qwen3-4b") -> dict:
    return {"name": name, "display_name": "", "state": "READY", "base_model": base_model,
            "create_time": ""}


def _dep(name: str, base_model: str, state: str = "READY") -> dict:
    return {"name": name, "base_model": base_model, "state": state, "create_time": ""}


# --- reconcile: the four per-entry statuses ---


def test_status_ok_suffix_matches():
    model = f"accounts/{ACCT}/models/docx-v1"
    dep = f"accounts/{ACCT}/deployments/abc"
    entries = [_entry(id="fw-docx", upstream_model=f"{model}#{dep}")]
    out = reconcile(entries, ACCT, [_model(model)], [_dep(dep, model)])
    assert out["catalog_entries"][0]["status"] == "ok"
    assert out["ok"] == [{"catalog_id": "fw-docx", "deployment": dep, "deployment_state": "READY"}]
    assert out["drift"] == []


def test_status_ok_no_suffix_but_ready_deploy():
    model = f"accounts/{ACCT}/models/docx-v1"
    dep = f"accounts/{ACCT}/deployments/abc"
    entries = [_entry(id="fw-docx", upstream_model=model)]  # no #suffix
    out = reconcile(entries, ACCT, [_model(model)], [_dep(dep, model)])
    assert out["catalog_entries"][0]["status"] == "ok"
    assert out["ok"][0]["deployment"] == dep


def test_status_serverless_never_drift():
    entries = [_entry(id="fw-glm", upstream_model="accounts/fireworks/models/glm-5p2")]
    out = reconcile(entries, ACCT, [], [])
    assert out["catalog_entries"][0]["status"] == "serverless"
    assert out["drift"] == [] and out["ok"] == []


def test_status_cataloged_not_deployed():
    model = f"accounts/{ACCT}/models/docx-v1"
    dep = f"accounts/{ACCT}/deployments/abc"
    entries = [_entry(id="fw-docx", upstream_model=f"{model}#{dep}")]
    # deployment exists but is not READY → counts as no live deployment
    out = reconcile(entries, ACCT, [_model(model)], [_dep(dep, model, state="CREATING")])
    assert out["catalog_entries"][0]["status"] == "cataloged_not_deployed"
    d = out["drift"][0]
    assert d["kind"] == "cataloged_not_deployed" and d["severity"] == "info"
    assert d["catalog_id"] == "fw-docx"


# --- reconcile: the three drift kinds ---


def test_drift_stale_suffix():
    model = f"accounts/{ACCT}/models/docx-v1"
    old = f"accounts/{ACCT}/deployments/dead"
    new = f"accounts/{ACCT}/deployments/live"
    entries = [_entry(id="fw-docx", upstream_model=f"{model}#{old}")]
    out = reconcile(entries, ACCT, [_model(model)], [_dep(new, model)])  # only `new` is READY
    assert out["catalog_entries"][0]["status"] == "stale_suffix"
    d = next(x for x in out["drift"] if x["kind"] == "stale_suffix")
    assert d["cataloged_deployment"] == old
    assert d["live_deployment"] == new
    assert d["suggested_upstream"] == f"{model}#{new}"


def test_drift_not_cataloged_with_and_without_deployment():
    m_dep = f"accounts/{ACCT}/models/orphan-deployed"
    m_bare = f"accounts/{ACCT}/models/orphan-bare"
    dep = f"accounts/{ACCT}/deployments/xyz"
    out = reconcile([], ACCT, [_model(m_dep), _model(m_bare)], [_dep(dep, m_dep)])
    by_model = {d["model"]: d for d in out["drift"] if d["kind"] == "not_cataloged"}
    assert by_model[m_dep]["deployment"] == dep
    assert by_model[m_bare]["deployment"] is None
    assert all(d["severity"] == "warn" for d in by_model.values())


def test_suggested_yaml_parses_to_valid_entry():
    model = f"accounts/{ACCT}/models/orphan-deployed"
    dep = f"accounts/{ACCT}/deployments/xyz"
    out = reconcile([], ACCT, [_model(model)], [_dep(dep, model)])
    snippet = out["drift"][0]["suggested_yaml"]
    parsed = yaml.safe_load(snippet)          # a one-item list
    assert isinstance(parsed, list) and len(parsed) == 1
    entry = CatalogEntry.model_validate(parsed[0])
    assert entry.id == "fw-orphan-deployed"
    assert entry.effective_upstream_model == f"{model}#{dep}"
    assert entry.api_key_env == "FIREWORKS_API_KEY"


def test_non_fireworks_entries_ignored():
    entries = [_entry(id="claude", endpoint="anthropic", api_key_env="ANTHROPIC_API_KEY",
                      base_url=None, upstream_model="claude-sonnet-4-6")]
    out = reconcile(entries, ACCT, [], [])
    assert out["catalog_entries"] == []       # anthropic entry doesn't participate


# --- route ---


def _app(tmp_path, monkeypatch, key: str | None):
    from toto_gateway.app import create_app
    from toto_gateway.config import Settings

    if key is None:
        monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FIREWORKS_API_KEY", key)
    settings = Settings(catalog="catalog.yaml,catalog.fireworks.yaml", trace_jsonl="",
                        trace_db="", trace_stdout=False, auth_token="test-operator-token",
                        db=f"{tmp_path}/gw.db", fake_exec=True)
    return create_app(settings=settings)


def test_route_no_key(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, None)) as client:
        body = client.get("/v1/admin/catalog/sync/fireworks").json()
    assert body["key_present"] is False
    assert body["error"] == "FIREWORKS_API_KEY not configured"
    assert body["account_models"] == [] and body["drift"] == []
    assert body["provider"] == "fireworks"


def test_route_happy_path(tmp_path, monkeypatch):
    import toto_gateway.routes.admin_catalog_sync as route_mod

    async def fake_fetch(api_key):
        model = "accounts/toto-tech/models/docx-formatting-editor-v1"
        dep = "accounts/toto-tech/deployments/b6omdtjm"
        return {"account": "toto-tech", "error": None,
                "account_models": [_model(model)], "deployments": [_dep(dep, model)]}

    monkeypatch.setattr(route_mod, "fetch_fireworks", fake_fetch)
    with TestClient(_app(tmp_path, monkeypatch, "fake-key")) as client:
        body = client.get("/v1/admin/catalog/sync/fireworks").json()
    assert body["key_present"] is True
    assert body["account"] == "toto-tech"
    # Account fine-tunes are no longer in the shared catalog (scoped-discovery paradigm), so the
    # live deployment reads as not_cataloged drift — the panel's honest "exists upstream" signal.
    assert any(d["kind"] == "not_cataloged" for d in body["drift"])
    assert body["ok"] == []
    assert body["error"] is None


def test_route_requires_auth(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, "fake-key")) as client:
        r = client.get("/v1/admin/catalog/sync/fireworks",
                       headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def test_route_transport_error_returns_200(tmp_path, monkeypatch):
    """An upstream HTTP failure surfaces as `error`, not a 500 — via a MockTransport that raises."""
    import httpx

    import toto_gateway.catalog_sync as cs

    def handler(request):
        raise httpx.ConnectError("boom")

    orig = httpx.AsyncClient

    def patched(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return orig(*a, **k)

    monkeypatch.setattr(cs.httpx, "AsyncClient", patched)
    with TestClient(_app(tmp_path, monkeypatch, "fake-key")) as client:
        r = client.get("/v1/admin/catalog/sync/fireworks")
    assert r.status_code == 200
    assert r.json()["error"] is not None


def test_real_catalog_fireworks_entries_reconcile():
    """The shipped catalog.fireworks.yaml entries all reconcile without error (smoke over shapes)."""
    cat = Catalog.load("catalog.yaml,catalog.fireworks.yaml")
    out = reconcile(cat.models, ACCT, [], [])
    statuses = {c["status"] for c in out["catalog_entries"]}
    assert "serverless" in statuses  # fw-glm-5.2 / fw-deepseek-v4-pro


# --- OpenRouter discovery: mapping ---


def _or_entry(m: dict) -> dict:
    """A raw OpenRouter /models entry with sensible defaults, overridable per test."""
    return {"id": "vendor/model", "name": "Model", "context_length": 8192,
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "supported_parameters": ["tools", "temperature"],
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}, **m}


def test_map_per_token_to_per_1k_and_string_prices():
    from toto_gateway.catalog_sync import map_openrouter_model

    row = map_openrouter_model(_or_entry({"pricing": {"prompt": "0.00055", "completion": "0.0022"}}))
    assert row["price_in"] == 0.00055 * 1000
    assert row["price_out"] == 0.0022 * 1000


def test_map_negative_and_missing_price_guarded():
    from toto_gateway.catalog_sync import map_openrouter_model

    row = map_openrouter_model(_or_entry({"pricing": {"prompt": "-1", "completion": None}}))
    assert row["price_in"] == 0.0 and row["price_out"] == 0.0
    no_pricing = map_openrouter_model(_or_entry({"pricing": None}))
    assert no_pricing["price_in"] == 0.0


def test_map_capability_derivation():
    from toto_gateway.catalog_sync import map_openrouter_model

    vision = map_openrouter_model(_or_entry({
        "supported_parameters": [], "architecture": {"input_modalities": ["text", "image"],
                                                     "output_modalities": ["text"]}}))
    assert vision["tools"] is False and vision["vision"] is True


def test_map_missing_fields_permissive():
    from toto_gateway.catalog_sync import map_openrouter_model

    row = map_openrouter_model({"id": "x/y"})  # nothing but an id
    assert row is not None
    assert row["slug"] == "x/y" and row["context_window"] == 0
    assert row["tools"] is False and row["vision"] is False


def test_map_skips_non_text_output():
    from toto_gateway.catalog_sync import map_openrouter_model

    assert map_openrouter_model(_or_entry({
        "architecture": {"output_modalities": ["image"]}})) is None


# --- OpenRouter discovery: reconcile ---


def test_reconcile_openrouter_flags_cataloged():
    from toto_gateway.catalog_sync import map_openrouter_model, reconcile_openrouter

    cat = Catalog.load("catalog.openrouter.yaml")
    models = [map_openrouter_model(_or_entry({"id": "anthropic/claude-sonnet-4.6"})),
              map_openrouter_model(_or_entry({"id": "some/unknown-model"}))]
    out = reconcile_openrouter(cat.models, models)
    assert [m["slug"] for m in out] == ["anthropic/claude-sonnet-4.6", "some/unknown-model"]  # sorted
    by = {m["slug"]: m for m in out}
    assert by["anthropic/claude-sonnet-4.6"]["cataloged"] is True
    assert by["anthropic/claude-sonnet-4.6"]["catalog_id"] == "or-sonnet-4.6"
    assert by["some/unknown-model"]["cataloged"] is False
    assert by["some/unknown-model"]["catalog_id"] is None


# --- OpenRouter discovery: route ---


def _or_app(tmp_path, monkeypatch, key: str | None):
    from toto_gateway.app import create_app
    from toto_gateway.config import Settings

    if key is None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", key)
    settings = Settings(catalog="catalog.openrouter.yaml", trace_jsonl="", trace_db="",
                        trace_stdout=False, auth_token="test-operator-token",
                        db=f"{tmp_path}/gw.db", fake_exec=True)
    return create_app(settings=settings)


def _or_transport(models_json: list[dict]):
    import httpx

    def handler(request):
        assert request.url.host == "openrouter.ai"
        return httpx.Response(200, json={"data": models_json})

    return httpx.MockTransport(handler)


def test_or_route_happy_path_no_key(tmp_path, monkeypatch):
    import httpx

    import toto_gateway.catalog_sync as cs

    transport = _or_transport([_or_entry({"id": "anthropic/claude-sonnet-4.6"}),
                               _or_entry({"id": "aaa/first"})])
    orig = httpx.AsyncClient

    def patched(*a, **k):
        k["transport"] = transport
        return orig(*a, **k)

    monkeypatch.setattr(cs.httpx, "AsyncClient", patched)
    with TestClient(_or_app(tmp_path, monkeypatch, None)) as client:
        body = client.get("/v1/admin/catalog/discovery/openrouter").json()
    assert body["provider"] == "openrouter"
    assert body["key_present"] is False and body["error"] is None  # missing key is NOT an error
    assert body["total"] == 2
    assert [m["slug"] for m in body["models"]] == ["aaa/first", "anthropic/claude-sonnet-4.6"]
    assert next(m for m in body["models"] if m["slug"] == "anthropic/claude-sonnet-4.6")["cataloged"]


def test_or_route_upstream_failure_returns_200(tmp_path, monkeypatch):
    import httpx

    import toto_gateway.catalog_sync as cs

    def handler(request):
        raise httpx.ConnectError("boom")

    orig = httpx.AsyncClient

    def patched(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return orig(*a, **k)

    monkeypatch.setattr(cs.httpx, "AsyncClient", patched)
    with TestClient(_or_app(tmp_path, monkeypatch, "sk-key")) as client:
        r = client.get("/v1/admin/catalog/discovery/openrouter")
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is not None and body["models"] == [] and body["total"] == 0
    assert body["key_present"] is True


def test_or_route_requires_auth(tmp_path, monkeypatch):
    with TestClient(_or_app(tmp_path, monkeypatch, None)) as client:
        r = client.get("/v1/admin/catalog/discovery/openrouter",
                       headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


# --- Fireworks library discovery: mapping ---


def _fw_lib_entry(m: dict) -> dict:
    """A raw Fireworks platform-library entry with sensible defaults, overridable per test."""
    return {"name": "accounts/fireworks/models/glm-5p2", "displayName": "GLM 5.2",
            "contextLength": 202752, "tunable": True, "supportsTools": True,
            "supportsImageInput": False, "state": "READY", "kind": "HF_BASE_MODEL", **m}


def test_fw_lib_map_display_name_fallback():
    from toto_gateway.catalog_sync import map_fireworks_library_model

    row = map_fireworks_library_model(_fw_lib_entry({"displayName": ""}))
    assert row["name"] == "glm-5p2"  # derived from last name segment
    assert row["slug"] == "accounts/fireworks/models/glm-5p2"


def test_fw_lib_map_capability_booleans():
    from toto_gateway.catalog_sync import map_fireworks_library_model

    row = map_fireworks_library_model(_fw_lib_entry({
        "tunable": False, "supportsTools": False, "supportsImageInput": True}))
    assert row["tunable"] is False and row["tools"] is False and row["vision"] is True


def test_fw_lib_map_skips_deprecated_embedding_nonready():
    from toto_gateway.catalog_sync import map_fireworks_library_model

    assert map_fireworks_library_model(_fw_lib_entry({"state": "DELETING"})) is None
    assert map_fireworks_library_model(_fw_lib_entry({"deprecationDate": "2026-01-01"})) is None
    assert map_fireworks_library_model(_fw_lib_entry({"kind": "EMBEDDING_MODEL"})) is None


def test_fw_lib_map_missing_fields_permissive():
    from toto_gateway.catalog_sync import map_fireworks_library_model

    row = map_fireworks_library_model({"name": "accounts/fireworks/models/x"})
    assert row is not None  # no state → treated as READY
    assert row["context_window"] == 0 and row["tunable"] is False
    assert row["name"] == "x"


# --- Fireworks library discovery: reconcile ---


def test_reconcile_fw_lib_flags_cataloged_and_excludes_account_finetune():
    from toto_gateway.catalog_sync import map_fireworks_library_model, reconcile_fireworks_library

    cat = Catalog.load("catalog.yaml,catalog.fireworks.yaml")
    models = [map_fireworks_library_model(_fw_lib_entry({"name": "accounts/fireworks/models/glm-5p2"})),
              map_fireworks_library_model(_fw_lib_entry({"name": "accounts/fireworks/models/qwen3-4b"}))]
    out = reconcile_fireworks_library(cat.models, models)
    by = {m["slug"]: m for m in out}
    # fw-glm-5.2's upstream IS a platform slug → cataloged.
    assert by["accounts/fireworks/models/glm-5p2"]["catalog_id"] == "fw-glm-5.2"
    # fw-docx-editor's account model (accounts/toto-tech/...) is NOT a platform slug → no library
    # entry matches it; the qwen base it fine-tuned from stays uncataloged.
    assert by["accounts/fireworks/models/qwen3-4b"]["cataloged"] is False
    assert [m["slug"] for m in out] == sorted(m["slug"] for m in out)  # sorted


# --- Fireworks library discovery: route ---


def _fw_lib_transport(models_json: list[dict], next_token: str | None = None):
    import httpx

    def handler(request):
        assert "accounts/fireworks/models" in request.url.path
        assert request.headers.get("authorization", "").startswith("Bearer ")
        return httpx.Response(200, json={"models": models_json, "nextPageToken": next_token})

    return httpx.MockTransport(handler)


def test_fw_lib_route_happy_path(tmp_path, monkeypatch):
    import httpx

    import toto_gateway.catalog_sync as cs

    transport = _fw_lib_transport([
        _fw_lib_entry({"name": "accounts/fireworks/models/glm-5p2"}),
        _fw_lib_entry({"name": "accounts/fireworks/models/aaa-first"}),
        _fw_lib_entry({"name": "accounts/fireworks/models/bge-m3", "kind": "EMBEDDING_MODEL"}),
    ])
    orig = httpx.AsyncClient

    def patched(*a, **k):
        k["transport"] = transport
        return orig(*a, **k)

    monkeypatch.setattr(cs.httpx, "AsyncClient", patched)
    with TestClient(_or_app_fw(tmp_path, monkeypatch, "fw-key")) as client:
        body = client.get("/v1/admin/catalog/discovery/fireworks").json()
    assert body["provider"] == "fireworks" and body["key_present"] is True and body["error"] is None
    assert body["total"] == 2 and body["filtered_out"] == 1  # embedding skipped
    assert [m["slug"] for m in body["models"]] == [
        "accounts/fireworks/models/aaa-first", "accounts/fireworks/models/glm-5p2"]
    assert next(m for m in body["models"]
                if m["slug"] == "accounts/fireworks/models/glm-5p2")["catalog_id"] == "fw-glm-5.2"


def test_fw_lib_route_no_key(tmp_path, monkeypatch):
    with TestClient(_or_app_fw(tmp_path, monkeypatch, None)) as client:
        body = client.get("/v1/admin/catalog/discovery/fireworks").json()
    assert body["key_present"] is False
    assert body["error"] == "FIREWORKS_API_KEY not configured"
    assert body["total"] == 0 and body["filtered_out"] == 0 and body["models"] == []


def test_fw_lib_route_upstream_failure_returns_200(tmp_path, monkeypatch):
    import httpx

    import toto_gateway.catalog_sync as cs

    def handler(request):
        raise httpx.ConnectError("boom")

    orig = httpx.AsyncClient

    def patched(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return orig(*a, **k)

    monkeypatch.setattr(cs.httpx, "AsyncClient", patched)
    with TestClient(_or_app_fw(tmp_path, monkeypatch, "fw-key")) as client:
        r = client.get("/v1/admin/catalog/discovery/fireworks")
    assert r.status_code == 200
    body = r.json()
    assert body["error"] is not None and body["models"] == [] and body["total"] == 0


def test_fw_lib_route_requires_auth(tmp_path, monkeypatch):
    with TestClient(_or_app_fw(tmp_path, monkeypatch, None)) as client:
        r = client.get("/v1/admin/catalog/discovery/fireworks",
                       headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401


def _or_app_fw(tmp_path, monkeypatch, key: str | None):
    """App whose catalog includes the fireworks fragment (so glm-5p2 is cataloged)."""
    from toto_gateway.app import create_app
    from toto_gateway.config import Settings

    if key is None:
        monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FIREWORKS_API_KEY", key)
    settings = Settings(catalog="catalog.yaml,catalog.fireworks.yaml", trace_jsonl="",
                        trace_db="", trace_stdout=False, auth_token="test-operator-token",
                        db=f"{tmp_path}/gw.db", fake_exec=True)
    return create_app(settings=settings)
