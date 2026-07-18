"""GET /v1/admin/catalog/models + the catalog schema additions (aliases, source)."""

from __future__ import annotations

import textwrap

from fastapi.testclient import TestClient

from toto_gateway.catalog import Catalog
from toto_gateway.routes.admin_catalog import is_fine_tuned


# --- schema: aliases + source survive YAML load ---


def test_aliases_survive_load(tmp_path):
    frag = tmp_path / "frag.yaml"
    frag.write_text(textwrap.dedent("""
        models:
          - {id: m1, lane: fake, endpoint: fake, residency_class: cloud, aliases: [m1-alias]}
    """))
    cat = Catalog.load(str(frag))
    assert cat.get("m1").aliases == ["m1-alias"]
    assert Catalog.load("catalog.fireworks.yaml").get("fw-glm-5.2").aliases == []  # default


def test_source_records_fragment_basename():
    cat = Catalog.load("catalog.fireworks.yaml")
    assert cat.get("fw-glm-5.2").source == "catalog.fireworks.yaml"


def test_source_override_later_fragment_wins(tmp_path):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text(textwrap.dedent("""
        models:
          - {id: m1, lane: fake, endpoint: fake, residency_class: in_perimeter}
    """))
    b.write_text(textwrap.dedent("""
        models:
          - {id: m1, lane: fake, endpoint: fake, residency_class: cloud}
    """))
    cat = Catalog.load(f"{a},{b}")
    assert cat.get("m1").source == "b.yaml"        # later fragment owns the entry now
    assert cat.get("m1").residency_class == "cloud"


def test_is_fine_tuned_heuristic():
    assert is_fine_tuned("accounts/toto-tech/models/docx-formatting-editor-v1#accounts/toto-tech/deployments/x")
    assert not is_fine_tuned("accounts/fireworks/models/glm-5p2")  # serverless platform model
    assert not is_fine_tuned("claude-sonnet-4-6")


# --- route ---


def test_route_returns_all_catalog_models(test_client: TestClient):
    r = test_client.get("/v1/admin/catalog/models")
    assert r.status_code == 200
    models = r.json()["models"]
    ids = {m["id"] for m in models}
    assert "smart" not in ids                       # virtual model excluded
    assert ids == {e.id for e in test_client.app.state.gateway.catalog.models}
    row = next(m for m in models if m["id"] == "echo-cloud")
    assert row["provider"] == "fake"
    assert row["source"] == "catalog.yaml"
    assert row["fine_tuned"] is False


def test_route_reports_fine_tune_and_provider(tmp_path):
    from toto_gateway.app import create_app
    from toto_gateway.config import Settings

    frag = tmp_path / "frag.yaml"
    frag.write_text(textwrap.dedent("""
        models:
          - id: fw-test-ft
            aliases: [test-ft]
            lane: economy
            endpoint: openai
            base_url: https://api.fireworks.ai/inference/v1
            api_key_env: FIREWORKS_API_KEY
            residency_class: cloud
            price_usd_per_1k: { prompt: 0.0, completion: 0.0 }
            context_window: 262144
            upstream_model: accounts/acme/models/ft-v1#accounts/acme/deployments/d1
    """))
    settings = Settings(catalog=f"catalog.yaml,catalog.fireworks.yaml,{frag}", trace_jsonl="",
                        trace_db="", trace_stdout=False, auth_token="test-operator-token",
                        db=f"{tmp_path}/gw.db", fake_exec=True)
    with TestClient(create_app(settings=settings)) as client:
        models = {m["id"]: m for m in client.get("/v1/admin/catalog/models").json()["models"]}
    ft = models["fw-test-ft"]
    assert ft["provider"] == "fireworks"
    assert ft["fine_tuned"] is True
    assert ft["aliases"] == ["test-ft"]
    assert ft["source"] == "frag.yaml"
    assert ft["context_window"] == 262144
    assert models["fw-glm-5.2"]["fine_tuned"] is False   # serverless platform model


def test_route_requires_auth(test_client: TestClient):
    r = test_client.get("/v1/admin/catalog/models", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
