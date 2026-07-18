"""Tests for FastAPI routes: /healthz, /v1/models, /v1/chat/completions.

All tests use TestClient (ASGI test transport) — no live server needed.
Auth tests toggle TOTO_GW_AUTH_TOKEN via Settings injection.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from toto_gateway.app import create_app
from toto_gateway.catalog import Catalog
from toto_gateway.config import Settings
from toto_gateway.gateway import Gateway
from toto_gateway.runners.fake import FakeRunner
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.trace import MemoryTraceWriter


def _app_client(auth_token: str = "") -> tuple[TestClient, MemoryTraceWriter]:
    catalog = Catalog.load("catalog.yaml")
    settings = Settings(
        catalog="catalog.yaml",
        trace_jsonl="",
        trace_db="",
        trace_stdout=False,
        auth_token=auth_token,
        db=":memory:",
    )
    writer = MemoryTraceWriter()
    registry = RunnerRegistry(factory=lambda entry: FakeRunner(entry))
    gw = Gateway(catalog=catalog, registry=registry, writer=writer)
    app = create_app(settings=settings, gateway=gw)
    return TestClient(app, raise_server_exceptions=True), writer


# --- /healthz ---


def test_healthz_returns_ok(test_client: TestClient):
    resp = test_client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"


def test_healthz_includes_version(test_client: TestClient):
    resp = test_client.get("/healthz")
    assert "version" in resp.json()


def test_healthz_does_not_leak_catalog(test_client: TestClient):
    """Liveness is status+version only now — the catalog listing lives at /v1/models, not on an
    unauthenticated liveness probe (plan D6). Readiness is /readyz."""
    body = test_client.get("/healthz").json()
    assert set(body) == {"status", "version"}


def test_readyz_ready(test_client: TestClient):
    resp = test_client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


# --- /v1/models ---


def test_list_models_status_200(test_client: TestClient):
    resp = test_client.get("/v1/models")
    assert resp.status_code == 200


def test_list_models_openai_shape(test_client: TestClient):
    """Response matches OpenAI /v1/models shape: {object: 'list', data: [...]}."""
    resp = test_client.get("/v1/models")
    body = resp.json()
    assert body["object"] == "list"
    assert "data" in body
    assert isinstance(body["data"], list)
    assert len(body["data"]) > 0


def test_list_models_each_has_id(test_client: TestClient):
    resp = test_client.get("/v1/models")
    for model in resp.json()["data"]:
        assert "id" in model


def test_list_models_toto_extensions(test_client: TestClient):
    """lane and residency_class extension fields are present on each model."""
    resp = test_client.get("/v1/models")
    for model in resp.json()["data"]:
        if model["id"] == "smart":
            continue  # virtual routing sentinel (SR1): no fixed upstream/lane — not a catalog row
        assert "lane" in model, f"model {model['id']} missing lane"
        assert "residency_class" in model, f"model {model['id']} missing residency_class"
        # upstream_model lets catalog-join UIs (settings page) map an entry to its real model.
        assert model.get("upstream_model"), f"model {model['id']} missing upstream_model"


def test_list_models_real_identity_fields(test_client: TestClient):
    """Each row carries the real-identity fields the console needs (chunk N1): a clean provider
    label, price_in/out, context_window, lane, residency — populated from the catalog."""
    valid_providers = {"anthropic", "openai", "openrouter", "fireworks", "local", "fake"}
    for model in test_client.get("/v1/models").json()["data"]:
        mid = model["id"]
        if mid == "smart":
            continue  # virtual routing sentinel (SR1): no provider/lane/price — not a catalog row
        assert model.get("provider") in valid_providers, f"{mid} bad provider {model.get('provider')}"
        assert model.get("lane"), f"{mid} missing lane"
        assert model.get("residency"), f"{mid} missing residency"
        assert model.get("price_in") is not None, f"{mid} missing price_in"
        assert model.get("price_out") is not None, f"{mid} missing price_out"
        assert model.get("context_window"), f"{mid} missing context_window"


def test_list_models_or_alias_reads_as_openrouter(test_client: TestClient):
    """An or-* entry (endpoint=openai + OPENROUTER_API_KEY) must read as provider 'openrouter',
    not 'openai' — the whole point of N1 is demoting the routing alias to the real provider."""
    rows = {m["id"]: m for m in test_client.get("/v1/models").json()["data"]}
    for mid, m in rows.items():
        if mid.startswith("or-"):
            assert m["provider"] == "openrouter", f"{mid} should be openrouter, got {m['provider']}"


def test_list_models_includes_echo_local(test_client: TestClient):
    resp = test_client.get("/v1/models")
    ids = [m["id"] for m in resp.json()["data"]]
    assert "echo-local" in ids
    assert "echo-cloud" in ids


def test_list_models_residency_values(test_client: TestClient):
    """residency_class values must be in_perimeter or cloud."""
    resp = test_client.get("/v1/models")
    valid = {"in_perimeter", "cloud"}
    for model in resp.json()["data"]:
        if model["id"] == "smart":
            continue  # virtual routing sentinel (SR1): no residency — not a catalog row
        assert model["residency_class"] in valid


# --- /v1/chat/completions (non-streaming) ---


def test_chat_non_stream_200(test_client: TestClient):
    resp = test_client.post(
        "/v1/chat/completions",
        json={"model": "echo-local", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200


def test_chat_non_stream_openai_shape(test_client: TestClient):
    """Response matches OpenAI ChatCompletion shape."""
    resp = test_client.post(
        "/v1/chat/completions",
        json={"model": "echo-local", "messages": [{"role": "user", "content": "test"}]},
    )
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert "choices" in body
    assert len(body["choices"]) >= 1
    assert "message" in body["choices"][0]
    assert "usage" in body


def test_chat_non_stream_has_content(test_client: TestClient):
    """Response content is a non-empty string."""
    resp = test_client.post(
        "/v1/chat/completions",
        json={"model": "echo-cloud", "messages": [{"role": "user", "content": "say hello"}]},
    )
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    assert content and len(content) > 0


def test_chat_unknown_model_404(test_client: TestClient):
    """Unknown model returns 404 with OpenAI error shape."""
    resp = test_client.post(
        "/v1/chat/completions",
        json={"model": "nonexistent-model-abc", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "model_not_found"


# --- /v1/chat/completions (streaming) ---


def test_chat_stream_returns_sse(test_client: TestClient):
    """Streaming response uses text/event-stream content type."""
    with test_client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "echo-local",
            "messages": [{"role": "user", "content": "stream test"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]


def test_chat_stream_yields_data_lines(test_client: TestClient):
    """SSE lines are 'data: ...' prefixed."""
    with test_client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "echo-local",
            "messages": [{"role": "user", "content": "sse data lines"}],
            "stream": True,
        },
    ) as resp:
        lines = []
        for line in resp.iter_lines():
            if line:
                lines.append(line)
        data_lines = [ln for ln in lines if ln.startswith("data: ")]
        assert len(data_lines) >= 2  # at least one chunk + [DONE]


def test_chat_stream_ends_with_done(test_client: TestClient):
    """The last SSE line is 'data: [DONE]'."""
    with test_client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "echo-local",
            "messages": [{"role": "user", "content": "ends with done"}],
            "stream": True,
        },
    ) as resp:
        lines = [ln for ln in resp.iter_lines() if ln]
        assert lines[-1] == "data: [DONE]"


def test_chat_stream_data_lines_are_valid_json(test_client: TestClient):
    """Each data line (except [DONE]) is valid JSON matching chunk shape."""
    with test_client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "echo-local",
            "messages": [{"role": "user", "content": "json validity"}],
            "stream": True,
        },
    ) as resp:
        for line in resp.iter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = line[len("data: "):]
            if payload == "[DONE]":
                continue
            obj = json.loads(payload)
            assert "id" in obj
            assert "object" in obj
            assert obj["object"] == "chat.completion.chunk"


def test_chat_stream_unknown_model_404(test_client: TestClient):
    """Streaming unknown model also returns 404 (resolved before opening stream)."""
    resp = test_client.post(
        "/v1/chat/completions",
        json={
            "model": "ghost-model-xyz",
            "messages": [{"role": "user", "content": "unknown"}],
            "stream": True,
        },
    )
    assert resp.status_code == 404


# --- Auth ---


def test_public_routes_reachable_without_auth():
    """Lockout guard: health probes stay open to an unauthenticated caller even with the operator
    credential configured, while user-data routes (chat) AND /v1/models require auth. /v1/models
    became authed with catalog-adoption: it lists the CALLER's effective catalog, so it must know
    who's asking."""
    client, _ = _app_client(auth_token="op-secret")
    client.headers.pop("authorization", None)  # unauthenticated
    for path in ("/healthz", "/readyz"):
        assert client.get(path).status_code == 200, path
    assert client.get("/v1/models").status_code == 401  # effective catalog → auth required
    # a user-data route is NOT public
    assert client.post(
        "/v1/chat/completions",
        json={"model": "echo-local", "messages": [{"role": "user", "content": "hi"}]},
    ).status_code == 401


def test_auth_missing_bearer_returns_401():
    """When auth_token is set, chat requests without Authorization header get 401.

    Note: /healthz does NOT require auth; /v1/models and /v1/chat/completions do (catalog-adoption
    made /v1/models scope-aware, so it lists the caller's effective catalog behind auth).
    """
    client, _ = _app_client(auth_token="super-secret-token")
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "echo-local", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


def test_auth_wrong_token_returns_401():
    """Wrong bearer token on chat endpoint returns 401."""
    client, _ = _app_client(auth_token="correct-token")
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "echo-local", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_auth_correct_token_returns_200():
    """Correct bearer token on chat endpoint returns 200."""
    token = "my-gateway-token"
    client, _ = _app_client(auth_token=token)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "echo-local", "messages": [{"role": "user", "content": "authed"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_healthz_open_when_auth_configured():
    """Healthz is NOT behind auth — even with a token configured it's open."""
    client, _ = _app_client(auth_token="some-secret-token")
    resp = client.get("/healthz")
    assert resp.status_code == 200  # no auth required on health endpoint


def test_auth_bearer_prefix_required():
    """Token without 'Bearer ' prefix is rejected (must match 'Bearer <token>' exactly)."""
    client, _ = _app_client(auth_token="my-token")
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "echo-local", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "my-token"},  # missing "Bearer " prefix
    )
    assert resp.status_code == 401


def test_auth_models_requires_auth_when_configured():
    """/v1/models requires auth when a token is configured (catalog-adoption: the list is the
    caller's effective catalog). No/wrong credential → 401; the matching operator bearer → 200."""
    token = "some-secret-token"
    client, _ = _app_client(auth_token=token)
    client.headers.pop("authorization", None)
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# --- Task-id header propagation ---


def test_task_id_header_propagated_to_trace():
    """x-toto-task-id header appears in the trace record."""
    client, writer = _app_client()
    client.post(
        "/v1/chat/completions",
        json={"model": "echo-local", "messages": [{"role": "user", "content": "task"}]},
        headers={"x-toto-task-id": "task-routing-001"},
    )
    assert writer.records[-1].task_id == "task-routing-001"


# --- Harness header detection ---


def test_harness_header_propagated_to_trace():
    """x-toto-harness header appears in the trace record."""
    client, writer = _app_client()
    client.post(
        "/v1/chat/completions",
        json={"model": "echo-local", "messages": [{"role": "user", "content": "pi call"}]},
        headers={"x-toto-harness": "pi"},
    )
    assert writer.records[-1].harness == "pi"


def test_spa_deep_link_falls_back_to_html(tmp_path, monkeypatch):
    """adapter-static prerenders routes as <route>.html; /svelte/<route> must serve them
    (Starlette raises HTTPException(404) for missing files — the fallback catches it)."""
    # The /svelte SPA is the app plane's front door — app-plane modules are absent in the OSS
    # export tree (where the plane never mounts), so this test skips there.
    import pytest
    pytest.importorskip("toto_gateway.routes.canvas")
    build = tmp_path / "frontend" / "build"
    build.mkdir(parents=True)
    (build / "index.html").write_text("<html>index</html>")
    (build / "canvas.html").write_text("<html>canvas</html>")
    monkeypatch.chdir(tmp_path)

    from pathlib import Path as _P

    from fastapi.testclient import TestClient

    from toto_gateway.app import create_app
    from toto_gateway.config import Settings

    repo = _P(__file__).resolve().parent.parent
    app = create_app(Settings(db=":memory:", catalog=str(repo / "catalog.yaml"),
                              benchmarks=str(repo / "benchmarks.yaml")))
    with TestClient(app) as c:
        assert c.get("/svelte/").status_code == 200
        r = c.get("/svelte/canvas")
        assert r.status_code == 200 and "canvas" in r.text
        assert c.get("/svelte/nope").status_code == 404
