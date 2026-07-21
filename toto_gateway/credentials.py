"""BYOK (bring-your-own-key) storage — per-user provider API keys, encrypted at rest.

A logged-in user stores their own OpenRouter / Fireworks key; at run time the OpenAI runner
uses THEIR key instead of the platform env key (fallback: the platform key). Key material is
Fernet-encrypted at rest (never plaintext, never rolled crypto). The Fernet key is derived from
TOTO_GW_CREDENTIALS_SECRET — this codebase has no session-signing/app secret to reuse (sessions
are random opaque tokens, passwords are per-row scrypt), so a dedicated at-rest secret is the
honest choice. No secret configured → the write path fails closed (503), never stores plaintext.

The seam is a contextvar: require_auth decrypts a user's keys into `byok_keys` for the request,
the runner reads it. A .set() inside a request task is isolated to that request; asyncio.create_task
copies the context at creation, so it propagates into the run task without threading a signature.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import os
import secrets as _secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .benchmarking.domain import CredentialScopeRef


@dataclass(frozen=True)
class Provider:
    label: str
    api_key_env: str  # the env var the platform key lives in (also the runner's lookup key)
    powers: str       # one-line description for the Settings UI
    # Second, non-secret per-provider field interpolated into base_url (Cloudflare's account id in
    # .../accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1). Set → the stored row is a JSON blob carrying
    # both fields (pack_provider_key), and load_byok maps the account id under this env name so
    # expand_env_refs resolves it from the same overlay the key rides in.
    account_env: str | None = None


# The known BYOK providers — single source of truth (the route and the seam both read this).
PROVIDERS: dict[str, Provider] = {
    "openrouter": Provider("OpenRouter", "OPENROUTER_API_KEY", "Economy-tier routing via OpenRouter"),
    "fireworks": Provider("Fireworks", "FIREWORKS_API_KEY", "Fireworks serverless inference + fine-tunes"),
    "cloudflare": Provider("Cloudflare", "CLOUDFLARE_API_TOKEN", "Workers AI models at the edge",
                           account_env="CLOUDFLARE_ACCOUNT_ID"),
    "openai": Provider("OpenAI", "OPENAI_API_KEY", "GPT models via the direct OpenAI API"),
    "gemini": Provider("Gemini", "GEMINI_API_KEY", "Google Gemini via the direct API"),
    "anthropic": Provider("Anthropic", "ANTHROPIC_API_KEY", "Claude models via the direct API"),
}

# Per-request BYOK override: {api_key_env: decrypted_key}. Default empty → the platform-key path
# is byte-for-byte unchanged. Set in require_auth for a logged-in user; read in OpenAIRunner.
byok_keys: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("byok_keys", default={})
byok_unavailable_envs: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "byok_unavailable_envs", default=frozenset())


class ProviderCredentialUnavailable(Exception):
    """Configured BYOK state exists but cannot be resolved safely for these providers."""

    def __init__(self, providers: tuple[str, ...], reason: str, *,
                 selected: dict[str, tuple[str, CredentialScopeRef]] | None = None) -> None:
        self.providers = providers
        self.reason = reason
        self.selected = dict(selected or {})
        super().__init__(f"provider credential unavailable for {', '.join(providers)}: {reason}")


def pack_provider_key(provider: str, key: str, account_id: str = "") -> str:
    """The plaintext blob a provider row encrypts: the bare key, or JSON when the provider carries
    a second non-secret field (account_env) — both fields live and die as one row."""
    definition = PROVIDERS[provider]
    if definition.account_env:
        return json.dumps({"api_key": key, "account_id": account_id})
    return key


def provider_env_map(provider: str, plaintext: str) -> dict[str, str]:
    """A decrypted provider row → {env_var: value} for the byok_keys overlay. A two-field provider
    (cloudflare) stores JSON: the token rides under api_key_env and the account id under
    account_env, so the base_url ${...} interpolation resolves from the same overlay the key does.
    A bare-string row (a key stored before the second field existed) is still just the token."""
    definition = PROVIDERS.get(provider)
    if definition is None:
        return {}
    if definition.account_env and plaintext.startswith("{"):
        try:
            data = json.loads(plaintext)
        except ValueError:
            return {definition.api_key_env: plaintext}
        out = {definition.api_key_env: str(data.get("api_key") or "")}
        account = str(data.get("account_id") or "")
        if account:
            out[definition.account_env] = account
        return {env: value for env, value in out.items() if value}
    return {definition.api_key_env: plaintext}


def stored_or_env(env: str) -> str | None:
    """The effective secret for `env`: the request-scoped stored-key overlay first (require_auth →
    load_byok → byok_keys), then the process env — the same stored-beats-env order dispatch uses,
    so a key pasted in Settings works on the very next request without a restart."""
    return byok_keys.get().get(env) or os.environ.get(env) or None


def expand_env_refs(text: str) -> str:
    """${ENV} interpolation with stored credentials first: the request-scoped byok_keys overlay
    (e.g. a stored Cloudflare account id) wins over os.environ; anything left falls through to
    os.path.expandvars. No $ in the text → returned unchanged."""
    if "$" not in text:
        return text
    for env, value in byok_keys.get().items():
        text = text.replace("${" + env + "}", value)
    return os.path.expandvars(text)


def last4(key: str) -> str:
    """Last 4 chars for the UI hint — but only when the key is long enough that 4 chars aren't
    the whole secret. A real provider key is 40+ chars; a <8-char value gets no hint rather than
    leaking itself into the (plaintext) last4 column + GET /v1/credentials."""
    return key[-4:] if len(key) >= 8 else ""


# --- Key source (decision #10): env (default) or Vault, in front of the MultiFernet -----------
# The at-rest key material ([primary, old]) is read through ONE seam, _kms_keys(). Everything
# downstream (the MultiFernet, dual-key rotation, load_byok) is unchanged — it just asks this
# seam for the secret strings instead of reading Settings directly. Cached per provider-config so
# the auth hot path (load_byok, every authenticated request) doesn't re-hit Vault each time.
_KMS_CACHE: dict[tuple, tuple[str, str]] = {}


def reset_kms_cache() -> None:
    """Test/rotation helper: drop the cached key material so the next read re-resolves."""
    _KMS_CACHE.clear()


def _vault_keys(settings) -> tuple[str, str]:
    """Read [primary, old] key material from Vault KV v2 via hvac. FAIL-CLOSED: an unreachable
    Vault, a bad token, a missing path, or an empty primary key all RAISE — the caller never gets
    a weak/empty key to fall back to. (hvac's own read raises on connection/auth/path errors; we
    only add the empty-primary guard.)"""
    import hvac  # local import: only paid for when provider=vault

    client = hvac.Client(url=settings.vault_addr, token=settings.vault_token)
    # ponytail: KV v2 (Vault's default engine). A v1 mount would need read_secret() instead — add
    # a version knob only if a deploy actually runs a v1 mount.
    resp = client.secrets.kv.v2.read_secret_version(path=settings.vault_kv_path)
    data = resp["data"]["data"]
    primary = data.get("credentials_secret", "")
    if not primary:
        raise RuntimeError(
            f"TOTO_GW_KMS_PROVIDER=vault but no non-empty 'credentials_secret' at KV path "
            f"{settings.vault_kv_path!r} — refusing to run with a weak/empty at-rest key")
    return primary, data.get("credentials_secret_old", "") or ""


def _kms_keys(settings) -> tuple[str, str]:
    """Resolve (primary, old) at-rest key material from the configured provider. env → the Settings
    fields (unchanged); vault → Vault KV (fail-closed). Result is cached per provider-config."""
    provider = getattr(settings, "kms_provider", "env")
    if provider == "env":
        return settings.credentials_secret, getattr(settings, "credentials_secret_old", "")
    if provider != "vault":
        raise RuntimeError(f"unknown TOTO_GW_KMS_PROVIDER={provider!r} (expected env|vault)")
    cache_key = (settings.vault_addr, settings.vault_token, settings.vault_kv_path)
    cached = _KMS_CACHE.get(cache_key)
    if cached is None:
        cached = _KMS_CACHE[cache_key] = _vault_keys(settings)
    return cached


def credentials_secret(settings) -> str:
    """The primary secret backing at-rest encryption; empty → storage not configured (fail closed).
    Sourced via the configured KMS provider (env default, or Vault — decision #10)."""
    return _kms_keys(settings)[0]


def credentials_secret_old(settings) -> str:
    """Optional PREVIOUS secret, a decrypt-only fallback during a rotation window. Empty (the
    default) → single-key, exactly as before. Sourced via the same KMS provider as the primary."""
    return _kms_keys(settings)[1]


def _fernet_key(secret: str) -> bytes:
    # Any server secret → a stable urlsafe-base64 32-byte Fernet key. sha256 gives exactly 32 bytes.
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def _fernet(secret: str, old: str = ""):
    """A MultiFernet keyed [primary, old?]. It ENCRYPTS under `secret` (the first key) and DECRYPTS
    by trying primary then `old` — the zero-downtime rotation seam (docs/ops/secrets.md). Set the
    new secret primary + the previous one as _OLD and ciphertext written under either still decrypts;
    new writes always use the primary, so a lazy re-encrypt on next read drains rows off the old key.
    Single key (old="") behaves byte-identically to the pre-rotation Fernet."""
    from cryptography.fernet import Fernet, MultiFernet

    keys = [Fernet(_fernet_key(secret))]
    if old:
        keys.append(Fernet(_fernet_key(old)))
    return MultiFernet(keys)


def encrypt(secret: str, plaintext: str) -> str:
    return _fernet(secret).encrypt(plaintext.encode()).decode()


def decrypt(secret: str, token: str, old: str = "") -> str:
    return _fernet(secret, old).decrypt(token.encode()).decode()


async def resolve_provider_credentials(
    settings,
    store,
    user_id: str | None,
    providers: tuple[str, ...] | None = None,
    org_id: str | None = None,
) -> dict[str, tuple[str, CredentialScopeRef]]:
    """Select exactly one usable credential per provider: user BYOK, then the org-wide key
    (set by an org owner in the console), then platform env."""
    requested = providers or tuple(PROVIDERS)
    # {provider: (ciphertext, scope)} — org rows first, user rows overwrite (precedence).
    encrypted: dict[str, tuple[str, CredentialScopeRef]] = {}
    try:
        if org_id is not None:
            encrypted.update({
                provider: (ct, CredentialScopeRef(kind="organization", scope_id=org_id))
                for provider, ct in (await store.get_org_provider_key_map(org_id)).items()})
        if user_id is not None:
            encrypted.update({
                provider: (ct, CredentialScopeRef(kind="user", scope_id=user_id))
                for provider, ct in (await store.get_provider_key_map(user_id)).items()})
    except Exception as error:  # noqa: BLE001 - indeterminate BYOK must fail closed
        raise ProviderCredentialUnavailable(requested, "store_unavailable") from error

    configured = tuple(provider for provider in requested if provider in encrypted)
    unavailable: dict[str, str] = {}
    primary = old = ""
    if configured:
        try:
            primary = credentials_secret(settings)
            old = credentials_secret_old(settings)
        except Exception as error:  # noqa: BLE001 - KMS state is part of credential validity
            unavailable.update(dict.fromkeys(configured, "kms_unavailable"))
            kms_error = error
        else:
            kms_error = None
        if not primary:
            unavailable.update(dict.fromkeys(configured, "kms_unavailable"))
    else:
        kms_error = None

    selected: dict[str, tuple[str, CredentialScopeRef]] = {}
    for provider in requested:
        definition = PROVIDERS[provider]
        if provider in encrypted:
            if provider in unavailable:
                continue
            ciphertext, scope = encrypted[provider]
            if not isinstance(ciphertext, str) or not ciphertext:
                unavailable[provider] = "malformed_ciphertext"
                continue
            try:
                plaintext = decrypt(primary, ciphertext, old)
            except Exception:  # noqa: BLE001 - configured but unreadable must fail closed
                unavailable[provider] = "decrypt_failed"
                continue
            if not plaintext:
                unavailable[provider] = "decrypt_failed"
                continue
            selected[provider] = (plaintext, scope)
            continue
        platform = os.environ.get(definition.api_key_env, "").strip()
        if platform:
            selected[provider] = (
                platform,
                CredentialScopeRef(kind="platform", scope_id="platform"),
            )
    if unavailable:
        reasons = set(unavailable.values())
        reason = reasons.pop() if len(reasons) == 1 else "credential_unavailable"
        error = ProviderCredentialUnavailable(
            tuple(provider for provider in requested if provider in unavailable),
            reason,
            selected=selected,
        )
        if kms_error is not None:
            raise error from kms_error
        raise error
    return selected


async def load_byok(settings, store, user_id: str | None,
                    org_id: str | None = None) -> dict[str, CredentialScopeRef]:
    """Bind user/org overrides and return the authoritative effective scope for every usable
    provider.

    Absent BYOK may use the platform environment key. Indeterminate configured BYOK state blocks
    only the affected providers while retaining independently resolved providers and local lanes.
    """
    byok_keys.set({})
    byok_unavailable_envs.set(frozenset())
    try:
        selected = await resolve_provider_credentials(settings, store, user_id, org_id=org_id)
    except ProviderCredentialUnavailable as error:
        selected = error.selected
        byok_unavailable_envs.set(frozenset(
            PROVIDERS[provider].api_key_env for provider in error.providers))
    byok_keys.set({
        env: value
        for provider, (credential, scope) in selected.items()
        if scope.kind != "platform"
        for env, value in provider_env_map(provider, credential).items()
    })
    return {provider: scope for provider, (_, scope) in selected.items()}


# --- OSS zero-config boot helpers (single-tenant, SQLite-file deploys) ---------------------------

def bootstrap_local_secret(settings) -> str:
    """Zero-config at-rest secret for the open edition: generate once, persist next to the SQLite
    DB (mode 0600), reuse forever — so pasting a key in Settings works without any env var. Only
    when no TOTO_GW_CREDENTIALS_SECRET is set and the DB is a real file; a :memory:/Postgres
    deploy keeps the explicit-secret requirement (fail closed). TRADEOFF, stated plainly: the
    secret sits on the same disk as the DB, so it defends a leaked DB file or backup — not a fully
    compromised host. Set TOTO_GW_CREDENTIALS_SECRET (or the Vault KMS provider) to separate them."""
    if not settings.db or settings.db == ":memory:" or settings.database_url:
        return ""
    path = Path(settings.db).resolve().parent / "credentials.secret"
    try:
        if path.is_file():
            existing = path.read_text().strip()
            if existing:
                return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = _secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secret)
        return secret
    except OSError:
        return ""  # unreadable/unwritable → storage stays unconfigured; writes 503 loudly


def stored_org_key_providers(settings, org_id: str) -> set[str]:
    """Boot-time synchronous peek at org_provider_keys — which providers have a stored key BEFORE
    the async stores exist. Drives the default-catalog pick, so an OpenRouter key pasted in
    Settings lights up catalog.openrouter.yaml on the next boot. SQLite-file deploys only (a
    Postgres deploy configures env vars); first boot / any error → empty set (default stands)."""
    if not settings.db or settings.db == ":memory:" or settings.database_url:
        return set()
    try:
        with sqlite3.connect(settings.db) as db:
            rows = db.execute(
                "SELECT provider FROM org_provider_keys WHERE org_id = ?", (org_id,)).fetchall()
        return {row[0] for row in rows}
    except sqlite3.Error:
        return set()


# Providers whose shipped catalog fragment composes into the DEFAULTED catalog when their key is
# configured. openrouter's fragment is SELF-CONTAINED (it carries the echo/test fake lanes the
# offline demo + driver fallback need); fireworks/cloudflare are overlays that compose on top. The
# other PROVIDERS (openai/gemini) are BYOK-only — no shipped fragment — so they never change the
# composed set. An explicit TOTO_GW_CATALOG is the operator's override and is never composed.
_PROVIDER_FRAGMENTS = {
    "openrouter": "catalog.openrouter.yaml",
    "fireworks": "catalog.fireworks.yaml",
    "cloudflare": "catalog.cloudflare.yaml",
}


def compose_default_catalog(providers) -> str:
    """The composed default-catalog path for the set of providers with a configured key. openrouter's
    fragment is the self-contained base (echo/test lanes included); every other keyed provider
    overlays on it, or on catalog.yaml when openrouter isn't keyed. Deterministic order (the
    _PROVIDER_FRAGMENTS insertion order) so the composed string is stable across calls — recompose
    compares it to decide whether anything changed."""
    base = "catalog.openrouter.yaml" if "openrouter" in providers else "catalog.yaml"
    overlays = [f for p, f in _PROVIDER_FRAGMENTS.items()
                if p != "openrouter" and p in providers]
    return ",".join([base, *overlays]) if overlays else base


def configured_key_providers(settings, org_id: str) -> set[str]:
    """Providers with a usable key right now — an env var set OR a stored row — the union the
    default catalog composes from. Reuses stored_org_key_providers (the SQLite peek) plus an env
    scan over PROVIDERS."""
    env = {p for p, d in PROVIDERS.items() if os.environ.get(d.api_key_env, "").strip()}
    return env | stored_org_key_providers(settings, org_id)


# --- Provision-on-signup: the per-user Toto app key lives in the SAME encrypted vault ------------
# It's stored in the provider_keys table under a "toto" slot, but deliberately NOT registered in
# PROVIDERS: it's an app key, not an LLM-runner key, so it must stay out of the BYOK Settings UI
# (list_credentials iterates PROVIDERS) and out of the byok_keys contextvar (load_byok skips
# providers absent from PROVIDERS). Same Fernet-at-rest + dual-key rotation as every other credential.
TOTO_PROVIDER = "toto"


async def store_toto_key(settings, store, user_id: str, key: str) -> bool:
    """Encrypt+store the user's Toto app API key in the credential vault. Returns False (no-op) when
    the at-rest secret is unset — fail closed, never store plaintext."""
    secret = credentials_secret(settings)
    if not secret:
        return False
    await store.set_provider_key(user_id, TOTO_PROVIDER, encrypt(secret, key), last4(key))
    return True


async def get_toto_key(settings, store, user_id: str) -> str | None:
    """The user's vaulted Toto app key, decrypted — or None (secret unset, no key, DB hiccup, or a
    rotated-secret ciphertext that won't decrypt). Every miss degrades to the shared token upstream."""
    secret = credentials_secret(settings)
    if not secret:
        return None
    old = credentials_secret_old(settings)
    try:
        key_map = await store.get_provider_key_map(user_id)
    except Exception:  # noqa: BLE001 — a DB hiccup must never break resolution; fall back to shared
        return None
    enc = key_map.get(TOTO_PROVIDER)
    if not enc:
        return None
    try:
        return decrypt(secret, enc, old)
    except Exception:  # noqa: BLE001 — stale/rotated ciphertext → fall back to the shared token
        return None


async def provision_and_store(settings, store, user_id: str, email: str, name: str) -> str | None:
    """Idempotently ensure this user has a vaulted Toto key: return the existing one, else provision
    a fresh identity+key, vault it, and return it. None when provisioning is off/unavailable or the
    at-rest secret is unset (the caller degrades to the shared token). Fail-open throughout."""
    existing = await get_toto_key(settings, store, user_id)
    if existing:
        return existing
    from .driver.toto_client import provision_toto_user  # local import: keeps the auth hot path light

    data = await provision_toto_user(settings, email, name)
    if not data:
        return None
    key = data["api_key"]
    await store_toto_key(settings, store, user_id, key)
    return key


def demo() -> None:
    """Self-check: encrypt→decrypt roundtrip + last4 + zero-downtime dual-key rotation."""
    from cryptography.fernet import InvalidToken

    s = "test-secret-value"
    ct = encrypt(s, "sk-or-supersecret")
    assert ct != "sk-or-supersecret" and decrypt(s, ct) == "sk-or-supersecret"
    assert last4("sk-or-supersecret") == "cret" and last4("ab") == ""  # <8 chars → no hint

    # Rotation (PT-E): a key was encrypted under secret A; we promote B as primary and keep A as the
    # decrypt-fallback. Old ciphertext must still decrypt, and a NEW write must use B (so A-only can
    # no longer read it — proof the primary actually changed, not just added).
    a, b = "old-secret-A", "new-secret-B"
    old_ct = encrypt(a, "sk-user-key")
    assert decrypt(b, old_ct, old=a) == "sk-user-key", "old ciphertext must survive rotation"
    new_ct = encrypt(b, "sk-user-key")
    try:
        decrypt(a, new_ct)  # A alone must NOT read a B-encrypted token
        raise AssertionError("new write must be encrypted under the new primary B, not A")
    except InvalidToken:
        pass
    assert decrypt(b, new_ct, old=a) == "sk-user-key"
    print("credentials demo ok")


if __name__ == "__main__":
    demo()
