"""Operator provider-key management (OSS single-tenant Settings): the /v1/admin/provider-keys
route, resolution precedence at dispatch (stored beats env, env fallback), cloudflare's two-part
key+account resolution, the availability probe with a stored key, the stored-key default-catalog
pick at boot, and the zero-config at-rest secret."""

from __future__ import annotations

import contextvars
import os
import stat

from fastapi.testclient import TestClient

from toto_gateway.app import create_app
from toto_gateway.auth import AuthStore
from toto_gateway.catalog import CatalogEntry
from toto_gateway.config import Settings
from toto_gateway.credentials import byok_keys
from toto_gateway.runners.fake import FakeRunner


def _settings(**over) -> Settings:
    base = dict(
        catalog="catalog.yaml", trace_jsonl="", trace_db="", trace_stdout=False,
        driver=True, fake_exec=True, db=":memory:", toto_token="",
        driver_model="echo-cloud", triage_model="echo-local",
        cookie_secure=False, credentials_secret="unit-test-secret",
    )
    return Settings(**{**base, **over})


# --- the route: masking, source reporting, validation ------------------------


def test_route_roundtrip_masking_and_source(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("FIREWORKS_API_KEY", "sk-fw-from-environment")
    with TestClient(create_app(settings=_settings())) as client:
        rows = {p["provider"]: p for p in client.get("/v1/admin/provider-keys").json()["providers"]}
        assert set(rows) == {"openrouter", "fireworks", "cloudflare", "openai", "gemini",
                             "anthropic"}
        assert rows["openrouter"] == {
            "provider": "openrouter", "label": "OpenRouter", "powers": rows["openrouter"]["powers"],
            "configured": False, "masked": None, "source": None,
            "env_var": "OPENROUTER_API_KEY", "account_env": None}
        # env-provided key: informational row, no key material at all
        assert rows["fireworks"]["source"] == "environment"
        assert rows["fireworks"]["configured"] is True
        assert rows["fireworks"]["masked"] is None
        assert rows["cloudflare"]["account_env"] == "CLOUDFLARE_ACCOUNT_ID"

        # PUT → stored + masked, raw key never echoed
        r = client.put("/v1/admin/provider-keys/openrouter", json={"key": "sk-or-v1-SECRET42"})  # gitleaks:allow — dummy fixture
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider"] == "openrouter" and body["configured"] is True
        assert body["masked"] == "ET42" and body["source"] == "stored"
        assert isinstance(body["models_added"], list)  # live-recompose outcome (may be empty)
        assert "sk-or-v1-SECRET42" not in r.text

        # a stored key over an env key: stored wins (source flips to stored)
        r = client.put("/v1/admin/provider-keys/fireworks", json={"key": "sk-fw-stored-KEY99"})
        assert r.status_code == 200
        rows = {p["provider"]: p for p in client.get("/v1/admin/provider-keys").json()["providers"]}
        assert rows["openrouter"]["source"] == "stored" and rows["openrouter"]["masked"] == "ET42"
        assert rows["fireworks"]["source"] == "stored" and rows["fireworks"]["masked"] == "EY99"

        # DELETE reverts: openrouter → unconfigured, fireworks → its env row
        assert client.delete("/v1/admin/provider-keys/openrouter").json()["source"] is None
        assert client.delete("/v1/admin/provider-keys/fireworks").json()["source"] == "environment"


def test_route_validation():
    with TestClient(create_app(settings=_settings())) as client:
        assert client.put("/v1/admin/provider-keys/nope", json={"key": "x"}).status_code == 400
        assert client.put("/v1/admin/provider-keys/openai", json={"key": "  "}).status_code == 400
        # cloudflare is a two-field row: token without account_id is rejected
        r = client.put("/v1/admin/provider-keys/cloudflare", json={"key": "cf-token-value-1"})
        assert r.status_code == 400
        assert "account_id" in r.json()["error"]["message"]


def test_cloudflare_account_id_must_be_32_hex():
    """The mistake that burns real tokens: an email or truncated id stores fine, then every
    request 404s against a nonexistent account. Hard-reject with a pointer at the dashboard URL."""
    with TestClient(create_app(settings=_settings())) as client:
        def put(account_id):
            return client.put("/v1/admin/provider-keys/cloudflare",
                              json={"key": "cf-token-value-1", "account_id": account_id})

        for bad in ("Alex@toto.tech",                       # the literal mistake
                    "c8c30db3dddc4ad31065d336368c790",      # 31 chars
                    "c8c30db3dddc4ad31065d336368c7905a",    # 33 chars
                    "g8c30db3dddc4ad31065d336368c7905"):    # non-hex char
            r = put(bad)
            assert r.status_code == 400, bad
            assert "dash.cloudflare.com" in r.json()["error"]["message"]

        assert put("c8c30db3dddc4ad31065d336368c7905").status_code == 200  # lowercase hex
        assert put("C8C30DB3DDDC4AD31065D336368C7905").status_code == 200  # uppercase hex


def test_route_no_secret_fails_closed():
    with TestClient(create_app(settings=_settings(credentials_secret=""))) as client:
        r = client.put("/v1/admin/provider-keys/openrouter", json={"key": "sk-or-abcdef"})
        assert r.status_code == 503


def test_non_operator_user_gets_403():
    with TestClient(create_app(settings=_settings())) as client:
        client.post("/v1/auth/register", json={"email": "u@example.com", "password": "password123"})
        client.post("/v1/auth/login", json={"email": "u@example.com", "password": "password123"})
        client.headers.pop("authorization", None)  # session cookie identity, not the operator
        assert client.get("/v1/admin/provider-keys").status_code == 403


# --- dispatch precedence: stored beats env, env is the fallback ---------------


class _SpyRunner(FakeRunner):
    seen: dict = {}

    async def chat(self, req, entry):
        _SpyRunner.seen["byok"] = dict(byok_keys.get())
        return await super().chat(req, entry)


def _spy_chat(client) -> dict:
    _SpyRunner.seen = {}
    gw = client.app.state.gateway
    gw.registry._factory = lambda e: _SpyRunner(e)
    gw.registry.clear()
    r = client.post("/v1/chat/completions",
                    json={"model": "echo-local", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    return _SpyRunner.seen["byok"]


def test_stored_key_reaches_dispatch_and_beats_env(monkeypatch):
    """Operator saves a key in Settings → the very next request resolves it (no restart), even
    when a stale env var exists: the runner override reads byok_keys before os.environ."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-stale")
    with TestClient(create_app(settings=_settings())) as client:
        r = client.put("/v1/admin/provider-keys/openrouter", json={"key": "sk-or-v1-LIVEKEY"})
        assert r.status_code == 200, r.text
        assert _spy_chat(client) == {"OPENROUTER_API_KEY": "sk-or-v1-LIVEKEY"}


def test_env_is_the_fallback_when_nothing_stored(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-only")
    with TestClient(create_app(settings=_settings())) as client:
        # empty overlay → the runner's cached client keeps using os.environ (the platform path)
        assert _spy_chat(client) == {}


def test_delete_reverts_to_env_fallback(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-restored")
    with TestClient(create_app(settings=_settings())) as client:
        client.put("/v1/admin/provider-keys/openrouter", json={"key": "sk-or-v1-TEMPKEY"})
        assert client.delete("/v1/admin/provider-keys/openrouter").status_code == 200
        assert _spy_chat(client) == {}


# --- cloudflare: two-part resolution (token + account id in base_url) ---------


def test_cloudflare_token_and_account_resolve_together(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "env-account-stale")
    with TestClient(create_app(settings=_settings())) as client:
        r = client.put("/v1/admin/provider-keys/cloudflare",
                       json={"key": "cf-token-SECRET77", "account_id": "c8c30db3dddc4ad31065d336368c7905"})
        assert r.status_code == 200, r.text
        assert r.json()["masked"] == "ET77"
        assert "cf-token-SECRET77" not in r.text  # token never echoed

        byok = _spy_chat(client)
        assert byok["CLOUDFLARE_API_TOKEN"] == "cf-token-SECRET77"
        assert byok["CLOUDFLARE_ACCOUNT_ID"] == "c8c30db3dddc4ad31065d336368c7905"


def test_cloudflare_base_url_interpolates_stored_account_over_env(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "env-account-stale")
    entry = CatalogEntry(
        id="cf-test", lane="economy", endpoint="openai", residency_class="cloud",
        base_url="https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1",
        api_key_env="CLOUDFLARE_API_TOKEN")

    ctx = contextvars.copy_context()
    ctx.run(byok_keys.set, {"CLOUDFLARE_API_TOKEN": "cf-tok", "CLOUDFLARE_ACCOUNT_ID": "acct-99"})
    assert ctx.run(lambda: entry.resolved_base_url) \
        == "https://api.cloudflare.com/client/v4/accounts/acct-99/ai/v1"
    # outside the overlay, the env fallback still applies (pre-existing behavior)
    assert entry.resolved_base_url \
        == "https://api.cloudflare.com/client/v4/accounts/env-account-stale/ai/v1"


# --- availability probe counts a stored key as configured ---------------------


async def test_probe_uses_stored_key(monkeypatch):
    import toto_gateway.catalog_sync as sync_mod

    calls: list[tuple[str, str]] = []

    async def fake_fetch(client, base_url, key):
        calls.append((base_url, key))
        return []

    monkeypatch.setattr(sync_mod, "fetch_provider_models", fake_fetch)
    monkeypatch.delenv("DIRECTLABS_API_KEY", raising=False)
    entry = CatalogEntry(id="dl", lane="economy", endpoint="openai", residency_class="cloud",
                         base_url="https://api.directlabs.example/v1",
                         api_key_env="DIRECTLABS_API_KEY", upstream_model="deep-chat-v3")

    token = byok_keys.set({"DIRECTLABS_API_KEY": "sk-stored-key"})
    try:
        out = await sync_mod.probe_availability([entry])
    finally:
        byok_keys.reset(token)
    assert calls == [("https://api.directlabs.example/v1", "sk-stored-key")]
    assert "https://api.directlabs.example/v1" in out["providers"]


# --- boot seams: default catalog + zero-config secret -------------------------


async def _store_operator_key(db_path: str, provider: str = "openrouter") -> None:
    store = AuthStore(db_path)
    await store.set_org_provider_key("local", provider, "ENC-any", "1234")


async def test_stored_openrouter_key_defaults_catalog_on_boot(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    db = f"{tmp_path}/gw.db"
    await _store_operator_key(db)

    settings = _settings(catalog="", db=db)  # unset → defaulted pick
    assert settings.catalog == "catalog.yaml"  # no env key at validation time
    create_app(settings=settings)
    assert settings.catalog == "catalog.openrouter.yaml"  # stored key upgraded the default

    explicit = _settings(catalog="catalog.yaml", db=db)  # an explicit choice is never touched
    create_app(settings=explicit)
    assert explicit.catalog == "catalog.yaml"


async def test_zero_config_secret_bootstraps_and_persists(tmp_path, monkeypatch):
    monkeypatch.delenv("TOTO_GW_CREDENTIALS_SECRET", raising=False)
    db = f"{tmp_path}/gw.db"

    settings = _settings(db=db, credentials_secret="")
    with TestClient(create_app(settings=settings)) as client:
        # no env secret, yet storage works: the secret was generated beside the DB
        r = client.put("/v1/admin/provider-keys/openrouter", json={"key": "sk-or-v1-BOOT42"})
        assert r.status_code == 200, r.text
    secret_file = tmp_path / "credentials.secret"
    assert secret_file.is_file()
    assert stat.S_IMODE(os.stat(secret_file).st_mode) == 0o600
    assert settings.credentials_secret == secret_file.read_text().strip() != ""

    # a second boot reuses the SAME secret, so the stored ciphertext still decrypts
    settings2 = _settings(db=db, credentials_secret="")
    with TestClient(create_app(settings=settings2)) as client:
        rows = {p["provider"]: p for p in client.get("/v1/admin/provider-keys").json()["providers"]}
        assert rows["openrouter"]["source"] == "stored"
        assert _spy_chat(client) == {"OPENROUTER_API_KEY": "sk-or-v1-BOOT42"}
    assert settings2.credentials_secret == settings.credentials_secret


def test_memory_db_keeps_fail_closed_secret_requirement():
    """No silent ephemeral secrets: :memory:/PG deploys still require an explicit secret."""
    settings = _settings(credentials_secret="")
    create_app(settings=settings)
    assert settings.credentials_secret == ""
