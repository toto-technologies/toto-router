"""BYOK: at-rest encryption, STRICT per-user key storage, the runner seam, and the route.

Mirrors the style of test_runs.py (RunStore fixture) and test_auth.py (register→login on a
TestClient). The seam tests isolate the byok_keys contextvar with copy_context so no set leaks
across tests. See toto_gateway/credentials.py + docs: fireworks-byok.
"""

from __future__ import annotations

import contextvars
import os
import sqlite3
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from toto_gateway.app import create_app
from toto_gateway.auth import AuthStore
from toto_gateway.catalog import CatalogEntry
from toto_gateway.config import Settings
from toto_gateway.credentials import (
    byok_keys,
    credentials_secret,
    credentials_secret_old,
    decrypt,
    encrypt,
    last4,
)
from toto_gateway.runners.openai import OpenAIRunner


# --- (a) encrypt / decrypt roundtrip + last4 --------------------------------

def test_encrypt_decrypt_roundtrip():
    secret = "a-server-secret"
    ct = encrypt(secret, "sk-or-v1-supersecret")
    assert ct != "sk-or-v1-supersecret"                 # not plaintext
    assert decrypt(secret, ct) == "sk-or-v1-supersecret"


def test_decrypt_with_wrong_secret_fails():
    ct = encrypt("secret-one", "sk-abc")
    with pytest.raises(Exception):
        decrypt("secret-two", ct)                        # a rotated secret can't decrypt old keys


def test_dual_key_rotation_zero_downtime():
    """PT-E: promote a new primary + keep the old as decrypt-fallback (MultiFernet). Old ciphertext
    still decrypts; a new write uses the new primary (the old key alone can no longer read it)."""
    from toto_gateway.credentials import demo

    demo()  # runs the encrypt/decrypt/last4/rotation self-checks (asserts inside) in CI

    a, b = "old-secret-A", "new-secret-B"
    old_ct = encrypt(a, "sk-user")
    assert decrypt(b, old_ct, old=a) == "sk-user"        # rotation window: old ciphertext survives
    new_ct = encrypt(b, "sk-user")                        # new write uses the new primary B
    with pytest.raises(Exception):
        decrypt(a, new_ct)                                # A alone can't read a B-encrypted token
    assert decrypt(b, new_ct, old=a) == "sk-user"


def test_last4():
    assert last4("sk-or-v1-abcd") == "abcd"
    assert last4("xy") == ""                              # <8 chars → no hint (never leak a short key)
    assert last4("short12") == ""                         # 7 chars → still suppressed
    assert last4("exactly8") == "tly8"                    # >=8 → last 4 is safe to show


# --- (b) store: set / list / get_map / delete, STRICT per-user scoping -------

_PG_URL = os.environ.get("TOTO_GW_TEST_DATABASE_URL")


@pytest_asyncio.fixture(params=[
    pytest.param("sqlite", id="sqlite"),
    pytest.param("postgres", id="postgres", marks=[
        pytest.mark.pg,
        pytest.mark.skipif(not _PG_URL, reason="set TOTO_GW_TEST_DATABASE_URL for the PG lane"),
    ]),
])
async def store(request):
    if request.param == "sqlite":
        yield AuthStore(":memory:")
        return
    auth = AuthStore(database_url=_PG_URL)
    auth._db.execute("DELETE FROM provider_keys")
    try:
        yield auth
    finally:
        await auth._exec("DELETE FROM provider_keys")
        await auth.close_pool()


async def test_set_list_get_delete(store):
    await store.set_provider_key("userA", "openrouter", "ENC-A-OR", "1111")
    got = await store.get_provider_key_map("userA")
    assert got == {"openrouter": "ENC-A-OR"}
    listing = await store.list_provider_keys("userA")
    assert listing == [{"provider": "openrouter", "last4": "1111"}]
    # list NEVER leaks key material
    assert all("encrypted_key" not in row and "key" not in row for row in listing)

    # re-PUT replaces (upsert on the (user, provider) PK)
    await store.set_provider_key("userA", "openrouter", "ENC-A-OR-2", "2222")
    assert (await store.get_provider_key_map("userA")) == {"openrouter": "ENC-A-OR-2"}

    await store.delete_provider_key("userA", "openrouter")
    assert (await store.get_provider_key_map("userA")) == {}
    assert (await store.list_provider_keys("userA")) == []


async def test_strict_scoping_two_users_share_nothing(store):
    await store.set_provider_key("userA", "openrouter", "ENC-A", "aaaa")
    await store.set_provider_key("userB", "openrouter", "ENC-B", "bbbb")
    # A sees only A's key, B only B's — no NULL grandfathering, no cross-user bleed
    assert (await store.get_provider_key_map("userA")) == {"openrouter": "ENC-A"}
    assert (await store.get_provider_key_map("userB")) == {"openrouter": "ENC-B"}
    assert (await store.list_provider_keys("userA")) == [{"provider": "openrouter", "last4": "aaaa"}]
    # deleting A's key leaves B's intact
    await store.delete_provider_key("userA", "openrouter")
    assert (await store.get_provider_key_map("userB")) == {"openrouter": "ENC-B"}


async def test_auth_store_reads_legacy_provider_key_row(tmp_path):
    db_path = tmp_path / "legacy-provider-keys.db"
    encrypted = encrypt("a-server-secret", "sk-or-legacy-key")
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE provider_keys ("
            "user_id TEXT NOT NULL, provider TEXT NOT NULL, encrypted_key TEXT NOT NULL, "
            "last4 TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (user_id, provider))"
        )
        db.execute(
            "INSERT INTO provider_keys VALUES (?, ?, ?, ?, ?)",
            ("legacy-user", "openrouter", encrypted, "-key", 1.0),
        )

    auth = AuthStore(str(db_path))
    assert await auth.get_provider_key_map("legacy-user") == {"openrouter": encrypted}
    assert decrypt("a-server-secret", encrypted) == "sk-or-legacy-key"


# --- (c) the runner seam ----------------------------------------------------

def _entry() -> CatalogEntry:
    return CatalogEntry(
        id="fw-glm-5.2", lane="economy", endpoint="openai",
        base_url="https://api.fireworks.ai/inference/v1",
        api_key_env="FIREWORKS_API_KEY", residency_class="cloud",
        upstream_model="accounts/fireworks/models/qwen3-coder-30b-a3b-instruct",
    )


def test_seam_byok_key_builds_ephemeral_client_not_the_cached_one():
    entry = _entry()
    sentinel = object()
    runner = OpenAIRunner(entry, client=sentinel)  # injected "platform" client (the cache)

    ctx = contextvars.copy_context()  # isolate the .set() so it can't leak to other tests
    ctx.run(byok_keys.set, {"FIREWORKS_API_KEY": "sk-user-byok"})
    client = ctx.run(runner._get_client)

    assert client is not sentinel                    # did NOT return the cached platform client
    assert client.api_key == "sk-user-byok"          # used the user's own key
    assert runner._client is sentinel                # cache untouched (ephemeral, not stored)


def test_seam_no_byok_uses_platform_env_key_and_caches(monkeypatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", "sk-platform")
    runner = OpenAIRunner(_entry())  # no injected client → lazy platform client
    # default context: byok_keys is empty
    c1 = runner._get_client()
    c2 = runner._get_client()
    assert c1 is c2                                   # cached (same object)
    assert c1.api_key == "sk-platform"               # platform env key


# --- (d) the route ----------------------------------------------------------

def _settings(**over) -> Settings:
    base = dict(
        catalog="catalog.yaml", trace_jsonl="", trace_db="", trace_stdout=False,
        driver=True, fake_exec=True, db=":memory:", toto_token="",
        driver_model="echo-cloud", triage_model="echo-local",
        cookie_secure=False, credentials_secret="unit-test-secret",
    )
    return Settings(**{**base, **over})


def _login(client: TestClient, email: str = "cred@example.com") -> None:
    r = client.post("/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    r = client.post("/v1/auth/login", json={"email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    # BYOK is PER-USER: drop conftest's default operator Bearer so the session cookie is the
    # identity (operator resolves to user_id=None, which the route rejects with 401).
    client.headers.pop("authorization", None)


def test_route_put_get_delete_never_leaks_raw_key():
    with TestClient(create_app(settings=_settings())) as client:
        _login(client)

        # initial GET: known providers, none configured
        r = client.get("/v1/credentials")
        assert r.status_code == 200, r.text
        creds = {c["provider"]: c for c in r.json()["credentials"]}
        assert set(creds) == {"openrouter", "fireworks"}
        assert creds["openrouter"]["configured"] is False
        assert creds["openrouter"]["last4"] is None
        assert creds["fireworks"]["powers"]  # one-line description present

        # PUT a key → configured + last4, raw key never echoed
        r = client.put("/v1/credentials/openrouter", json={"key": "sk-or-v1-SECRET42"})  # gitleaks:allow — dummy fixture, never a real key
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"provider": "openrouter", "configured": True, "last4": "ET42"}
        assert "sk-or-v1-SECRET42" not in r.text

        # GET reflects it, still no raw key
        r = client.get("/v1/credentials")
        creds = {c["provider"]: c for c in r.json()["credentials"]}
        assert creds["openrouter"]["configured"] is True
        assert creds["openrouter"]["last4"] == "ET42"
        assert "sk-or-v1-SECRET42" not in r.text

        # DELETE clears it
        r = client.delete("/v1/credentials/openrouter")
        assert r.status_code == 200
        assert r.json() == {"provider": "openrouter", "configured": False, "last4": None}
        creds = {c["provider"]: c for c in client.get("/v1/credentials").json()["credentials"]}
        assert creds["openrouter"]["configured"] is False


def test_credentials_work_when_driver_is_disabled():
    with TestClient(create_app(settings=_settings(driver=False))) as client:
        _login(client)
        response = client.put(
            "/v1/credentials/openrouter", json={"key": "sk-or-test-key-1234"}
        )
        assert response.status_code == 200
        assert response.json()["configured"] is True


def test_route_validation():
    with TestClient(create_app(settings=_settings())) as client:
        _login(client)
        assert client.put("/v1/credentials/nope", json={"key": "x"}).status_code == 400  # bad provider
        assert client.put("/v1/credentials/openrouter", json={"key": "   "}).status_code == 400  # blank


def test_route_anonymous_gets_401():
    with TestClient(create_app(settings=_settings())) as client:
        # open mode (no auth_token, require_login off) — anonymous reaches the route, which
        # itself demands a per-user identity.
        r = client.get("/v1/credentials")
        assert r.status_code == 401
        assert r.json()["error"]["message"] == "login required"


def test_route_no_secret_fails_closed_on_put():
    with TestClient(create_app(settings=_settings(credentials_secret=""))) as client:
        _login(client)
        r = client.put("/v1/credentials/openrouter", json={"key": "sk-abc"})
        assert r.status_code == 503
        assert r.json()["error"]["message"] == "credential storage not configured"


# --- (e) key save triggers a scoped inventory refresh ------------------------

class _SpyPlatform:
    """Duck-typed BenchmarkPlatform: records compile/submit, satisfies lifespan start/close."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.compiled: list = []
        self.submitted: list = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def compile(self, intent, actor):
        if self.fail:
            raise RuntimeError("inventory plane down")
        self.compiled.append((intent, actor))
        return SimpleNamespace(plan_id="plan-spy", intent=intent, actor_id=actor.actor_id)

    async def submit(self, plan, actor, *, idempotency_key):
        self.submitted.append(idempotency_key)
        return SimpleNamespace(operation_id="op-spy", status="pending")


def _spy_app(spy: _SpyPlatform, **settings_over):
    app = create_app(settings=_settings(**settings_over))
    app.state.benchmark_platform = spy
    return app


def test_put_key_triggers_scoped_inventory_refresh():
    spy = _SpyPlatform()
    with TestClient(_spy_app(spy, fake_exec=False)) as client:
        _login(client)
        r = client.put("/v1/credentials/fireworks", json={"key": "fw-byok-key-1234"})
        assert r.status_code == 200, r.text
    assert len(spy.compiled) == 1
    intent, actor = spy.compiled[0]
    assert intent.providers == ("fireworks",)
    assert intent.scope == "effective"  # the saved key changes what effective resolves to
    assert intent.user_id == actor.user_id is not None
    assert len(spy.submitted) == 1


def test_put_key_survives_refresh_failure():
    """The refresh is best-effort — an inventory-plane outage must never lose the key save."""
    spy = _SpyPlatform(fail=True)
    with TestClient(_spy_app(spy, fake_exec=False)) as client:
        _login(client)
        r = client.put("/v1/credentials/fireworks", json={"key": "fw-byok-key-1234"})
        assert r.status_code == 200, r.text
        assert r.json()["configured"] is True
    assert spy.submitted == []


def test_put_key_skips_refresh_in_fake_exec_mode():
    """fake_exec means no real provider HTTP anywhere — inventory discovery included."""
    spy = _SpyPlatform()
    with TestClient(_spy_app(spy)) as client:  # _settings defaults fake_exec=True
        _login(client)
        assert client.put(
            "/v1/credentials/fireworks", json={"key": "fw-byok-key-1234"}
        ).status_code == 200
    assert spy.compiled == []


# --- config helper ----------------------------------------------------------

def test_credentials_secret_reads_setting():
    assert credentials_secret(_settings(credentials_secret="xyz")) == "xyz"
    assert credentials_secret(_settings(credentials_secret="")) == ""


# --- (f) KMS provider: env (unchanged) vs Vault (decision #10) ---------------
# The env path above is already covered by every other test in this file (they all run
# kms_provider="env" by default). These prove the Vault key source: keys load from a mocked
# Vault, a BYOK secret encrypts under env keys and DECRYPTS identically when the same keys are
# served via Vault, rotation works through Vault, and a missing-Vault/missing-key config RAISES.

class _FakeVaultClient:
    """Stand-in for hvac.Client. `store` is the KV secret's data dict; None → raise on read
    (unreachable Vault / bad path / bad token — hvac itself raises, we just simulate that)."""

    def __init__(self, store, *, url=None, token=None):
        self._store = store

        class _KV2:
            def read_secret_version(inner, path):  # noqa: N805
                if store is None:
                    raise RuntimeError("vault unreachable (simulated)")
                return {"data": {"data": store}}

        class _KV:
            v2 = _KV2()

        class _Secrets:
            kv = _KV()

        self.secrets = _Secrets()


def _patch_vault(monkeypatch, store):
    """Monkeypatch the hvac module credentials.py imports lazily, and clear the KMS cache so each
    test re-resolves against its own fake store."""
    import sys
    import types

    from toto_gateway.credentials import reset_kms_cache

    reset_kms_cache()
    fake_hvac = types.ModuleType("hvac")
    fake_hvac.Client = lambda url=None, token=None: _FakeVaultClient(store, url=url, token=token)
    monkeypatch.setitem(sys.modules, "hvac", fake_hvac)


def _vault_settings(**over):
    return _settings(
        kms_provider="vault", vault_addr="https://vault.test:8200",
        vault_token="s.faketoken", vault_kv_path="toto/credentials",
        credentials_secret="", credentials_secret_old="", **over)


def test_vault_keys_load(monkeypatch):
    _patch_vault(monkeypatch, {"credentials_secret": "vault-primary-secret"})
    s = _vault_settings()
    assert credentials_secret(s) == "vault-primary-secret"
    assert credentials_secret_old(s) == ""


def test_vault_encrypt_env_decrypts_vault_identical(monkeypatch):
    """Falsifiable done: identical encrypt/decrypt whether keys come from env or a mocked Vault.
    A BYOK secret encrypted under the env-served key decrypts byte-identically when the SAME key
    is served via Vault."""
    secret = "shared-at-rest-secret"
    ct = encrypt(secret, "sk-or-v1-byok-value")            # env path (explicit secret)

    _patch_vault(monkeypatch, {"credentials_secret": secret})
    s = _vault_settings()
    # decrypt using the key VAULT serves — no plaintext secret passed, it comes from the provider
    assert decrypt(credentials_secret(s), ct, credentials_secret_old(s)) == "sk-or-v1-byok-value"


def test_vault_dual_key_rotation(monkeypatch):
    """Rotation works identically through Vault: primary B + old A. Ciphertext written under A
    still decrypts; a new write uses B, and A alone can no longer read it."""
    a, b = "vault-old-A", "vault-new-B"
    old_ct = encrypt(a, "sk-user")                          # written when A was primary

    _patch_vault(monkeypatch, {"credentials_secret": b, "credentials_secret_old": a})
    s = _vault_settings()
    prim, old = credentials_secret(s), credentials_secret_old(s)
    assert (prim, old) == (b, a)
    assert decrypt(prim, old_ct, old) == "sk-user"          # old ciphertext survives
    new_ct = encrypt(prim, "sk-user")                       # new write under the new primary B
    with pytest.raises(Exception):
        decrypt(a, new_ct)                                  # A alone can't read a B-encrypted token
    assert decrypt(prim, new_ct, old) == "sk-user"


def test_vault_unreachable_fails_closed(monkeypatch):
    """FAIL-CLOSED: provider=vault but Vault unreachable → RAISE, never an empty/weak key."""
    _patch_vault(monkeypatch, None)                         # store=None → read raises
    with pytest.raises(Exception):
        credentials_secret(_vault_settings())


def test_vault_missing_key_fails_closed(monkeypatch):
    """FAIL-CLOSED: Vault reachable but the primary key is absent → RAISE."""
    _patch_vault(monkeypatch, {"unrelated": "x"})           # no 'credentials_secret' field
    with pytest.raises(Exception):
        credentials_secret(_vault_settings())


def test_vault_missing_config_fails_closed_at_startup(monkeypatch):
    """create_app resolves the key material at boot, so a broken Vault config crashes on startup
    (fail-closed at startup), not later on a write path."""
    _patch_vault(monkeypatch, None)                         # Vault unreachable at boot
    with pytest.raises(Exception):
        create_app(settings=_vault_settings())


def test_unknown_kms_provider_raises(monkeypatch):
    from toto_gateway.credentials import reset_kms_cache
    reset_kms_cache()
    with pytest.raises(Exception):
        credentials_secret(_settings(kms_provider="aws-kms"))


# --- (e) end-to-end seam: require_auth -> load_byok -> runner (the load-bearing hop) ----------

def test_byok_reaches_runner_through_real_request(monkeypatch):
    """The feature's core property: a stored key set by require_auth must reach the runner in the
    SAME request. Drives the real ASGI stack (login -> PUT key -> POST /v1/chat/completions) with a
    spy runner that records the request-scoped byok_keys — not a hand-set contextvar. Guards the
    dep->endpoint hop the quality review flagged as untested (finding #5)."""
    from toto_gateway.credentials import byok_keys as _byok
    from toto_gateway.runners.fake import FakeRunner

    seen: dict[str, dict] = {}

    class SpyRunner(FakeRunner):
        async def chat(self, req, entry):
            seen["byok"] = dict(_byok.get())  # what the runner sees, in-request
            return await super().chat(req, entry)

    with TestClient(create_app(settings=_settings())) as client:
        gw = client.app.state.gateway
        gw.registry._factory = lambda e: SpyRunner(e)  # every lane -> spy
        gw.registry.clear()

        _login(client)
        r = client.put("/v1/credentials/openrouter", json={"key": "sk-or-v1-REACHESRUNNER"})
        assert r.status_code == 200, r.text

        r = client.post("/v1/chat/completions",
                        json={"model": "echo-local", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text

    # the runner saw the user's key under the platform env-var name — the whole chain worked
    assert seen["byok"] == {"OPENROUTER_API_KEY": "sk-or-v1-REACHESRUNNER"}


def test_byok_absent_for_anonymous_request(monkeypatch):
    """Mirror: with no key stored (and logged in), the runner sees an empty byok map — the
    platform-key path. Proves load_byok doesn't spuriously populate."""
    from toto_gateway.credentials import byok_keys as _byok
    from toto_gateway.runners.fake import FakeRunner

    seen: dict[str, dict] = {}

    class SpyRunner(FakeRunner):
        async def chat(self, req, entry):
            seen["byok"] = dict(_byok.get())
            return await super().chat(req, entry)

    with TestClient(create_app(settings=_settings())) as client:
        gw = client.app.state.gateway
        gw.registry._factory = lambda e: SpyRunner(e)
        gw.registry.clear()
        _login(client)  # logged in, but no key PUT
        r = client.post("/v1/chat/completions",
                        json={"model": "echo-local", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text

    assert seen["byok"] == {}


def test_unreadable_byok_blocks_only_its_static_provider_before_wire(monkeypatch):
    """Broken configured BYOK is not absence, but it must not deny unrelated local work."""
    import openai

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-platform-must-not-reach-wire")
    wire_calls = []

    def wire_spy(*args, **kwargs):
        wire_calls.append((args, kwargs))
        raise AssertionError("blocked BYOK provider reached the OpenAI wire boundary")

    monkeypatch.setattr(openai, "AsyncOpenAI", wire_spy)
    with TestClient(create_app(settings=_settings(driver=False, fake_exec=False))) as client:
        _login(client, "broken-provider@example.com")
        assert client.put(
            "/v1/credentials/openrouter", json={"key": "sk-or-v1-original-user-key"}
        ).status_code == 200
        client.app.state.auth._db.execute(
            "UPDATE provider_keys SET encrypted_key = ? WHERE provider = ?",
            ("configured-but-unreadable", "openrouter"),
        )
        client.app.state.auth._db.commit()

        local = client.post(
            "/v1/chat/completions",
            json={"model": "echo-local", "messages": [{"role": "user", "content": "hi"}]},
        )
        provider = client.post(
            "/v1/chat/completions",
            json={"model": "or-sonnet-4.6", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert local.status_code == 200, local.text
    assert provider.status_code == 502, provider.text
    assert wire_calls == []
