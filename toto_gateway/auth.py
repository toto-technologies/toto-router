"""AuthStore — users + opaque server-side sessions, same SQLite file as RunStore.

Separate from RunStore because auth must gate the app regardless of the driver flag (RunStore
is only built when the driver is on). Same idioms: stdlib sqlite3, WAL, single connection
guarded by a lock, CREATE TABLE IF NOT EXISTS + guarded ALTERs. No auth/crypto library:
`hashlib.scrypt` for passwords, `secrets` for tokens, `hmac.compare_digest` for every compare.
See docs/plans/2026-07-02-user-accounts-auth.md (Decisions 2 & 4).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import db as _db_mod


def _parse_retention(raw) -> dict:
    """A stored retention_policy value → {sink: days} with only positive-int day values kept.
    Tolerant: bad JSON or non-positive/non-int entries are dropped (keep-forever). Key validation
    lives at the write boundary (the admin route); this just reads defensively."""
    if not raw:
        return {}
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return {k: int(v) for k, v in obj.items()
            if isinstance(v, int) and not isinstance(v, bool) and v > 0}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id        TEXT PRIMARY KEY,
  email          TEXT NOT NULL UNIQUE,
  password_hash  TEXT,
  email_verified INTEGER NOT NULL DEFAULT 0,
  google_sub     TEXT UNIQUE,
  created_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL,
  purpose    TEXT NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
-- BYOK provider keys (docs: fireworks-byok). One row per (user, provider). STRICTLY user-scoped
-- like user_memory -- no NULL grandfathering, two users share nothing (anonymous has no keys).
-- encrypted_key is Fernet ciphertext (see credentials.py). last4 is a plaintext UI hint only.
CREATE TABLE IF NOT EXISTS provider_keys (
  user_id       TEXT NOT NULL,
  provider      TEXT NOT NULL,
  encrypted_key TEXT NOT NULL,
  last4         TEXT NOT NULL,
  created_at    REAL NOT NULL,
  PRIMARY KEY (user_id, provider)
);
-- Org-wide BYOK provider keys, set by an org owner in the console (routes/org_credentials.py).
-- STRICTLY org-scoped like provider_keys; resolve-time precedence is user > org > platform env.
CREATE TABLE IF NOT EXISTS org_provider_keys (
  org_id        TEXT NOT NULL,
  provider      TEXT NOT NULL,
  encrypted_key TEXT NOT NULL,
  last4         TEXT NOT NULL,
  created_at    REAL NOT NULL,
  PRIMARY KEY (org_id, provider)
);
CREATE TABLE IF NOT EXISTS rate_limits (
  scope        TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  count        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (scope, window_start)
);
-- Append-only audit trail (SOC2 CC7). Metadata only, NEVER content. INSERT-only posture: the
-- prod app DB role gets INSERT but not UPDATE/DELETE (GRANT hardening — see migration runbook).
CREATE TABLE IF NOT EXISTS audit_events (
  id         TEXT PRIMARY KEY,       -- random hex (avoids the AUTOINCREMENT vs SERIAL dialect split)
  ts         REAL NOT NULL,
  action     TEXT NOT NULL,          -- register | login | login_failed | logout | verify | revoke | admin:*
  user_id    TEXT,
  ip         TEXT,                   -- first X-Forwarded-For hop
  request_id TEXT
);
-- Control-plane tenancy (C1). org then team then member, the membership row carrying the role.
-- Additive and forward-only, dual-dialect (no NULL-in-PK, no dialect-specific types). Roles are
-- owner, admin, member, auditor. A pre-tenancy user is lazily provisioned a personal org (owner) on first
-- resolve (see resolve_membership) -- that IS the backfill, no boot scan. SCHEMA_VERSION bumped to 2.
CREATE TABLE IF NOT EXISTS organizations (
  org_id         TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  created_at     REAL NOT NULL,
  status         TEXT NOT NULL DEFAULT 'active',  -- active | suspended
  -- Zero-retention mode (W1-C4): 1 => this org's requests leave ZERO payload bytes in any durable
  -- TELEMETRY store (request_content, response cache, experience corpus, driver spans, LangSmith
  -- mirror), overriding TOTO_GW_LOG_CONTENT and every cache/eval flag DOWNWARD (opt-out always wins).
  -- Traces still record full metadata. Absence -> 0 -> env flags apply, exactly as before.
  zero_retention INTEGER NOT NULL DEFAULT 0,
  -- Content-plane retention policy (W3-C6): per-sink retention DAYS as a JSON object, e.g.
  -- '{"documents": 90, "memory": 365}'. This governs USER-INVOKED PRODUCT storage (the content
  -- plane's documents + doc_embeddings and the explicit user_memory facts) — the sinks zero_retention
  -- deliberately EXCLUDES. Disjoint from the global content_retention_days (that ages out the
  -- observability request_content capture, a different table). 0/absent for a sink = keep forever.
  retention_policy TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS teams (
  team_id    TEXT PRIMARY KEY,
  org_id     TEXT NOT NULL,
  name       TEXT NOT NULL,
  created_at REAL NOT NULL,
  status     TEXT NOT NULL DEFAULT 'active'
);
-- PK is (org_id, user_id): thin slice = one org-level membership per user (team_id is a nullable
-- column, NOT part of the PK — Postgres forbids NULL in a PK column, so team-scoped rows come
-- later as their own additive shape rather than a NULL-bearing composite key).
CREATE TABLE IF NOT EXISTS memberships (
  org_id     TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  team_id    TEXT,                            -- NULL = org-level membership (thin slice)
  role       TEXT NOT NULL,                   -- owner | admin | member | auditor
  created_at REAL NOT NULL,
  PRIMARY KEY (org_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships (user_id);
-- Catalog-scoped RBAC (C2/C3). Per-team allow/deny overlay over catalog ids + residency classes.
-- Keyed by team_id, org_id carried so the admin API enforces org isolation (no cross-org read/
-- write). A per-TEAM row: mode='allow' => default-deny (only listed models pass) — 'deny' =>
-- default-allow (listed models blocked). The ORG-DEFAULT row (team_id == org_id sentinel, same
-- pattern as routing_policies) carries the C3 org GOVERNANCE mode instead: 'allow_all' (permissive,
-- today's behavior) or 'allowlist' (deny-by-default — only the org's approved set = its models list
-- + org catalog adoptions resolves). Absence of a row = permissive (unchanged routing). Additive,
-- dual-dialect: no dialect-specific types, JSON lists stored as TEXT (parsed app-side), UPSERT via
-- ON CONFLICT.
CREATE TABLE IF NOT EXISTS catalog_policies (
  team_id       TEXT PRIMARY KEY,
  org_id        TEXT NOT NULL,
  mode          TEXT NOT NULL DEFAULT 'allow',   -- allow (default-deny) | deny (default-allow)
  models        TEXT NOT NULL DEFAULT '[]',       -- JSON array of catalog ids
  residency     TEXT,                             -- JSON array of allowed residency classes, NULL=all
  default_model TEXT,                             -- substituted when the caller omits model
  version       INTEGER NOT NULL DEFAULT 1,
  updated_by    TEXT,
  updated_at    REAL NOT NULL
);
-- Invitations (C5). Additive, dual-dialect (TEXT/DOUBLE PRECISION only). A pending invite is a row
-- with accepted_ts NULL plus a random token, and accepting it stamps accepted_ts and adds the
-- membership. No unique on email — re-inviting issues a fresh token, and the newest one wins because
-- accept adds an idempotent membership (thin slice, no supersede needed). Comments here stay free of
-- semicolons on purpose, since executescript splits the DDL on that character.
CREATE TABLE IF NOT EXISTS invitations (
  id          TEXT PRIMARY KEY,
  org_id      TEXT NOT NULL,
  email       TEXT NOT NULL,
  role        TEXT NOT NULL,                   -- owner | admin | member | auditor
  token       TEXT NOT NULL UNIQUE,
  created_ts  REAL NOT NULL,
  accepted_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_invitations_org ON invitations (org_id);
-- Per-team routing overlay (control-plane C6 + CT). The GLOBAL tag->model map lives in
-- routing/labels.yaml (NVIDIA's 11 task types + redact) -- this row overlays it PER TEAM: bindings
-- is a JSON object carrying ONLY the labels the team changed (absence of a key = the global YAML
-- default stands), optimize is the team preset applied on the fallback path. custom_labels (CT) is
-- a JSON list of the team's INVENTED task types [{name, desc, model}] -- labels NOT in labels.yaml
-- that the classifier may emit for this team and that route to their bound model. Keyed by team_id,
-- org_id carried so the admin API enforces org isolation. Absence of a row = pure global behavior
-- (effective_policy carries no overlay -> byte-identical routing). Additive, dual-dialect: TEXT/
-- REAL only, JSON stored as TEXT (parsed app-side), UPSERT via ON CONFLICT. Comments here stay
-- free of semicolons on purpose, since executescript splits the DDL on that character.
CREATE TABLE IF NOT EXISTS routing_policies (
  team_id       TEXT PRIMARY KEY,
  org_id        TEXT NOT NULL,
  bindings      TEXT NOT NULL DEFAULT '{}',   -- JSON object label->catalog id (overridden labels only)
  optimize      TEXT,                          -- quality | balanced | cost, NULL = global preset
  custom_labels TEXT NOT NULL DEFAULT '[]',   -- JSON list of {name, desc, model} team-invented task types (CT)
  prewarm       INTEGER NOT NULL DEFAULT 0,   -- per-org cache pre-warm toggle (0=off default; latency tool)
  stick_ttls    TEXT NOT NULL DEFAULT '{}',   -- JSON object label->seconds: per-task-type memo hold (S2)
  cache         TEXT NOT NULL DEFAULT '{}',   -- JSON: per-org cache-behavior overrides (A8) {preset, auto_inject, auto_inject_min_messages, warmth_routing}; absent key = inherit global env
  fail_policy   TEXT NOT NULL DEFAULT 'open', -- W1-C1: open (degrade to floor, default) | closed (503 on smart-routing degradation); W2-C7 also stores a JSON per-reason matrix
  taxonomy      TEXT NOT NULL DEFAULT '{}',   -- W2-C7 JSON: data-classification labels bound to residency constraints {labels:{<l>:{constraint,desc}}, default}
  classifier_model TEXT,                        -- W3-C1: org's chosen in-perimeter classifier id (NULL = gateway default)
  version       INTEGER NOT NULL DEFAULT 1,
  updated_by    TEXT,
  updated_at    REAL NOT NULL
);
-- Server-side catalog adoptions (catalog-adoption, Alex 2026-07-11): one-click add of a provider-
-- library model into a caller's EFFECTIVE catalog, reversing the paste-YAML ruling. Keyed by
-- scope_key = team_id or org_id (mirrors the routing-policy fallback so a personal-org owner's OWN
-- adoptions apply to their traffic). entry_json is the full materialized CatalogEntry -- its facts
-- (price, context, capabilities, upstream pin) are derived SERVER-SIDE from the provider discovery
-- snapshot, NEVER client-sent -- parsed back at request time into the effective catalog (base wins on
-- id collision). upstream_model/provider are denormalized for cheap listing + validation. Additive,
-- dual-dialect (TEXT/REAL only), UPSERT via ON CONFLICT. Comments here stay free of semicolons on
-- purpose, since executescript splits the DDL on that character.
CREATE TABLE IF NOT EXISTS catalog_adoptions (
  scope_key      TEXT NOT NULL,
  id             TEXT NOT NULL,
  entry_json     TEXT NOT NULL,
  upstream_model TEXT NOT NULL,
  provider       TEXT NOT NULL,
  created_by     TEXT,
  updated_at     REAL NOT NULL,
  PRIMARY KEY (scope_key, id)
);
-- Manual price overrides (catalog-pricing-plane, Alex 2026-07-14). For entries whose provider
-- publishes no machine-readable pricing (Groq, direct labs) or whose YAML price rotted. Prices
-- are stored PER 1K tokens (converted from the console's per-Mtok input at the API boundary,
-- exact divide by 1000). scope_key is team_id or org_id or the literal platform sentinel --
-- precedence at resolution is team over org over platform. cache_read_multiplier NULL means
-- keep the entry's own multiplier. Applied at the effective_catalog seam so compute_cost_usd
-- and routing inherit the override with zero changes. Additive, dual-dialect (TEXT/REAL only),
-- UPSERT via ON CONFLICT. Comments stay free of semicolons (executescript splits on them).
CREATE TABLE IF NOT EXISTS price_overrides (
  scope_key             TEXT NOT NULL,
  model_id              TEXT NOT NULL,
  prompt_usd_per_1k     REAL NOT NULL,
  completion_usd_per_1k REAL NOT NULL,
  cache_read_multiplier REAL,
  updated_by            TEXT,
  updated_at            REAL NOT NULL,
  PRIMARY KEY (scope_key, model_id)
);
-- Monthly USD budgets (W2-C5). Keyed by team_id, org_id carried for the admin API's org-isolation
-- check -- SAME shape as routing_policies. The ORG-DEFAULT row (team_id == org_id sentinel) is the
-- fallback a teamless caller resolves. action is what happens at 100 percent: observe (serve+stamp)
-- | downgrade (cheapest eligible model) | reject (402). thresholds is a JSON list of alert fractions
-- (default 0.5/0.8/1.0). Absence of a row = no budget = today's behavior. Additive, dual-dialect
-- (TEXT/REAL only), UPSERT via ON CONFLICT. Comments free of semicolons (executescript splits on it).
CREATE TABLE IF NOT EXISTS budgets (
  team_id     TEXT PRIMARY KEY,
  org_id      TEXT NOT NULL,
  monthly_usd REAL NOT NULL DEFAULT 0,
  action      TEXT NOT NULL DEFAULT 'observe',
  thresholds  TEXT NOT NULL DEFAULT '[0.5, 0.8, 1.0]',
  version     INTEGER NOT NULL DEFAULT 1,
  updated_by  TEXT,
  updated_at  REAL NOT NULL
);
-- Threshold-alert dedupe (W2-C5). One row the first time a scope crosses a threshold in a month, so
-- the budget:threshold audit fires ONCE per (scope, month, threshold). scope_key = team_id or org_id
-- (mirrors the budget fallback). Append-only like audit_events. PK gives the dedupe for free.
CREATE TABLE IF NOT EXISTS budget_alerts (
  scope_key TEXT NOT NULL,
  period    TEXT NOT NULL,
  threshold REAL NOT NULL,
  fired_at  REAL NOT NULL,
  PRIMARY KEY (scope_key, period, threshold)
);
-- OIDC SSO relying-party config (W1-C6). One row per org: the IdP issuer, client id, the
-- Fernet-encrypted client secret (same at-rest posture as provider_keys -- NEVER plaintext, NEVER
-- echoed), and the sso_required switch. Keyed by org_id. Additive, dual-dialect (TEXT/REAL/INTEGER
-- only). Comments stay free of semicolons on purpose, since executescript splits DDL on that char.
CREATE TABLE IF NOT EXISTS org_sso_configs (
  org_id            TEXT PRIMARY KEY,
  issuer            TEXT NOT NULL,
  client_id         TEXT NOT NULL,
  client_secret_enc TEXT NOT NULL,
  sso_required      INTEGER NOT NULL DEFAULT 0,
  created_at        REAL NOT NULL,
  updated_at        REAL NOT NULL
);
-- Email-domain -> org map for SSO resolution. domain is the PK, so a domain belongs to AT MOST one
-- org (set_sso_config rejects a domain already claimed by another org). This is the indexed lookup
-- both the SSO start endpoint and the password-login sso_required check hit -- the domain's SSO
-- posture is public knowledge (the start endpoint reveals it), so keying login on it leaks nothing.
CREATE TABLE IF NOT EXISTS sso_domains (
  domain  TEXT PRIMARY KEY,
  org_id  TEXT NOT NULL
);
-- Server-side OIDC login state (single-use, TTL). One row per in-flight authorize redirect: the
-- state token is the PK, carrying the org, the nonce, the PKCE code_verifier, and the same-origin
-- return path. consume_login_state deletes on read (single-use) and the row's expires_at bounds the
-- window. Never holds user data -- it exists only between the authorize redirect and the callback.
CREATE TABLE IF NOT EXISTS sso_login_states (
  state         TEXT PRIMARY KEY,
  org_id        TEXT NOT NULL,
  nonce         TEXT NOT NULL,
  code_verifier TEXT NOT NULL,
  redirect_to   TEXT NOT NULL,
  expires_at    REAL NOT NULL,
  created_at    REAL NOT NULL
);
-- SCIM 2.0 provisioning config (W2-C2). One row per org: the sha256 digest of the per-org SCIM
-- bearer (same at-rest posture as auth_tokens -- only the hash, never the secret), the IdP
-- group->role map as a JSON blob, and the enabled switch. Sibling to org_sso_configs rather than
-- extra columns on it, so an org can hold a SCIM token without a full OIDC relying-party config
-- (Okta's SCIM app is provisioned separately from its SSO app). Keyed by org_id. Comments stay
-- free of semicolons on purpose, since executescript splits DDL on that character.
CREATE TABLE IF NOT EXISTS org_scim_configs (
  org_id         TEXT PRIMARY KEY,
  token_hash     TEXT,
  group_role_map TEXT NOT NULL DEFAULT '{}',
  enabled        INTEGER NOT NULL DEFAULT 0,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL
);

-- Audit-export config (W2-C4). One row per org: the destination + cadence + retention for the
-- scheduled hash-chained JSONL export, and (when a customer S3 bucket is the sink) that bucket's
-- Fernet-encrypted secret key -- same at-rest posture as org_sso_configs (NEVER plaintext, NEVER
-- echoed; GET reports has_s3_secret only). last_run/last_error surface the scheduler's health in
-- the config GET. Additive, dual-dialect (TEXT/REAL/INTEGER only, no semicolons in comments).
CREATE TABLE IF NOT EXISTS audit_export_configs (
  org_id         TEXT PRIMARY KEY,
  enabled        INTEGER NOT NULL DEFAULT 0,
  cadence_hours  REAL NOT NULL DEFAULT 24,
  retention_days INTEGER NOT NULL DEFAULT 30,
  destination    TEXT NOT NULL DEFAULT 'gateway',
  s3_endpoint    TEXT NOT NULL DEFAULT '',
  s3_bucket      TEXT NOT NULL DEFAULT '',
  s3_region      TEXT NOT NULL DEFAULT 'us-east-1',
  s3_access_key  TEXT NOT NULL DEFAULT '',
  s3_secret_enc  TEXT NOT NULL DEFAULT '',
  s3_prefix      TEXT NOT NULL DEFAULT '',
  last_run       REAL,
  last_error     TEXT,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL
);
-- Audit-export batch ledger (W2-C4). One row per (org, stream, batch): the object key of the
-- gateway-stored JSONL file, its sha256, the prev_sha256 it chains to, the row count, and the
-- export cursor (last exported row key -- gateway_events.id or audit_events.ts) captured in that
-- batch. This table IS the hash chain AND the listing: the chain tip is the max-batch row, and an
-- auditor verifies no batch was altered/removed by checking recorded sha == recompute and every
-- prev_sha256 == the prior batch's sha. Retention prunes a contiguous head prefix (legitimate),
-- so a valid chain starts at any batch but never has an internal gap.
CREATE TABLE IF NOT EXISTS audit_export_batches (
  org_id      TEXT NOT NULL,
  stream      TEXT NOT NULL,
  batch       INTEGER NOT NULL,
  object_key  TEXT NOT NULL,
  sha256      TEXT NOT NULL,
  prev_sha256 TEXT NOT NULL,
  rows        INTEGER NOT NULL,
  cursor      TEXT NOT NULL,
  created_at  REAL NOT NULL,
  PRIMARY KEY (org_id, stream, batch)
);
-- Org storage connector (BYOS): each org may point object storage (documents, artifacts) at its
-- OWN private S3-compatible bucket. Enabled row overrides the platform default (TOTO_GW_S3_* env,
-- else filesystem) at resolve time (storage.resolve_object_store). Secret posture identical to
-- audit_export_configs/org_sso_configs: Fernet ciphertext at rest, write-only over the API, GET
-- reports has_s3_secret only. last_test/last_error record the most recent connection test.
CREATE TABLE IF NOT EXISTS org_storage_configs (
  org_id              TEXT PRIMARY KEY,
  enabled             INTEGER NOT NULL DEFAULT 0,
  s3_endpoint         TEXT NOT NULL DEFAULT '',
  s3_bucket           TEXT NOT NULL DEFAULT '',
  s3_region           TEXT NOT NULL DEFAULT 'us-east-1',
  s3_access_key       TEXT NOT NULL DEFAULT '',
  s3_secret_enc       TEXT NOT NULL DEFAULT '',
  s3_force_path_style INTEGER NOT NULL DEFAULT 1,
  last_test           REAL,
  last_error          TEXT,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL
);
"""

# Catalog-policy modes (fail-closed: anything else is rejected at write time). Two disjoint
# vocabularies share the `mode` column by row scope: a per-TEAM row carries an allow/deny model
# LIST (C2); the ORG-DEFAULT row (team_id == org_id sentinel) carries the org GOVERNANCE mode
# (C3) — allow_all (today's behavior, permissive) or allowlist (deny-by-default: only the org's
# approved set resolves). The admin endpoints validate each path against its own subset; the store
# guard below is the union so an unknown string is still rejected at write time.
CATALOG_TEAM_MODES = ("allow", "deny")          # C2 per-team list mode
CATALOG_ORG_MODES = ("allow_all", "allowlist")  # C3 org governance mode
CATALOG_MODES = CATALOG_TEAM_MODES + CATALOG_ORG_MODES

# Routing-overlay optimize presets (C6) -- mirrors benchmarks.OPTIMIZE, fail-closed at write time.
ROUTING_OPTIMIZE = ("quality", "balanced", "cost")

# The thin-slice ladder (owner > admin > member) plus the lateral read-only `auditor` (W1-C5).
# `auditor` is a VALID assignable role but is NOT in the rank ladder (deps._ROLE_RANK) — it grants
# read-only access to org surfaces and refuses every mutation. Membership/invite validation accepts
# it here; the read-vs-write gating lives in require_role / require_read_role.
ROLES = ("owner", "admin", "member", "auditor")

# SCIM group->role resolution (W2-C2): a user in several mapped IdP groups gets the HIGHEST role.
# `owner` is DELIBERATELY absent -- SCIM can never grant ownership (a hard rule: ownership is the
# billing/deletion authority, never IdP-assignable). `auditor` (read-only, lateral) sits below
# `member` here so an admin+auditor user resolves to admin (most capable wins). Unmapped groups
# contribute nothing; no mapped group at all -> the default `member`.
_SCIM_ROLE_RANK = {"auditor": 1, "member": 2, "admin": 3}


def resolve_scim_role(groups: list[str], group_role_map: dict) -> str:
    """Highest role the user's IdP groups map to, else 'member'. Owner is never grantable (filtered
    even if the map names it). `groups` are IdP group display names; `group_role_map` is
    {group_name: role}. ponytail: linear scan over a user's groups -- a handful per user, not a hot
    path (runs on create/PATCH only)."""
    best, best_rank = "member", _SCIM_ROLE_RANK["member"]
    for g in groups:
        role = group_role_map.get(g)
        if role == "owner" or role not in _SCIM_ROLE_RANK:
            continue  # owner never grantable; unmapped/unknown ignored
        if _SCIM_ROLE_RANK[role] > best_rank:
            best, best_rank = role, _SCIM_ROLE_RANK[role]
    return best

# scrypt params (OWASP-acceptable, memory-hard); stored self-describing so they can be raised
# later and stale hashes re-hashed on login. n must be a power of two.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2**14, 8, 1, 32
VERIFY_TTL = 24 * 3600  # verification tokens: 24h, single-use
# ponytail: no expiry v1 — revocation (DELETE /v1/tokens/{id}) is the lever; a real TTL knob
# slots into mint_api_token if a customer asks. auth_tokens.expires_at is NOT NULL, so "no
# expiry" is a far-future stamp.
API_TOKEN_TTL = 100 * 365 * 86400
# last_used write-throttle (W2-C3): the auth hot path stamps last_used at most once per this window
# per token (compared against the stored value), so a busy CI token adds no per-request write.
_LAST_USED_THROTTLE = 15 * 60


def hash_password(password: str) -> str:
    """`scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>` — self-describing so params live with the hash."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        dklen=_SCRYPT_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a self-describing scrypt string. False on any malformation."""
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# A well-formed hash to burn on unknown-email login so the response time doesn't leak account
# existence. Computed once at import (same params as a real verify).
_DUMMY_HASH = hash_password("toto-dummy-password-for-timing")


def burn_dummy_hash() -> None:
    """Spend a scrypt on a dummy hash — call on unknown-email login to equalize timing."""
    verify_password("wrong", _DUMMY_HASH)


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class AuthStore(_db_mod.AsyncStoreMixin):
    def __init__(self, path: str = ":memory:", database_url: str = "",
                 pool: dict | None = None) -> None:
        from . import db as _db

        self._db, self._pg = _db.connect(database_url, path)  # sync conn: init DDL
        self._pool = _db.make_async_pool(database_url, **(pool or {}))  # async pool: runtime queries
        self._db.executescript(_SCHEMA)
        # Guarded ALTER (same convention as RunStore): the user's eternal companion
        # conversation — its turn-1 run_id (companion plan Decisions 4 & 6).
        ine = "IF NOT EXISTS " if self._pg else ""
        try:
            self._db.execute(f"ALTER TABLE users ADD COLUMN {ine}companion_conv_id TEXT")
        except sqlite3.OperationalError:
            pass  # sqlite: column already exists (PG uses IF NOT EXISTS → no error)
        # Per-user chat-model lever (AX tunability): which model answers this user's companion
        # chat — a catalog id or the 'smart' sentinel. NULL = the configured default.
        try:
            self._db.execute(f"ALTER TABLE users ADD COLUMN {ine}companion_model TEXT")
        except sqlite3.OperationalError:
            pass
        # Per-user API tokens (purpose 'api') ride the same table: label + last_used are
        # display metadata for GET /v1/tokens (user-api plan). rotated_at (W2-C3) stamps the NEW
        # credential produced by a rotation, surfaced in the org compliance list.
        for col, typ in (("label", "TEXT"), ("last_used", "REAL"), ("rotated_at", "REAL")):
            try:
                self._db.execute(f"ALTER TABLE auth_tokens ADD COLUMN {ine}{col} {typ}")
            except sqlite3.OperationalError:
                pass
        # Per-credential org binding (W2-C1 multi-org): which org THIS token/session resolves the
        # caller into. A session (purpose 'session') carries the active org the user switched to; an
        # API token (purpose 'api') carries the org it was minted against. NULL -> unbound -> identity
        # falls back to the oldest membership (backward-compatible). Guarded ALTER; the binding is
        # read at resolution time (deps._resolve_identity -> resolve_membership(preferred_org_id=...)).
        try:
            self._db.execute(f"ALTER TABLE auth_tokens ADD COLUMN {ine}org_id TEXT")
        except sqlite3.OperationalError:
            pass
        # Control-plane audit (C3): widen the SOC2 floor with org + target + metadata so admin/
        # policy events are org-scoped-readable at GET /v1/admin/audit. Additive, INSERT-only, no
        # backfill (old rows keep NULL org_id → operator-only, correct). Same guarded-ALTER idiom.
        for col in ("org_id", "target_type", "target_id", "metadata"):
            try:
                self._db.execute(f"ALTER TABLE audit_events ADD COLUMN {ine}{col} TEXT")
            except sqlite3.OperationalError:
                pass
        # OIDC SSO (W1-C6): generalize the google_sub seam to any IdP. google_sub stays (Google
        # login still writes it); oidc_sub + oidc_issuer are the general pair, matched as a composite
        # so two IdPs can't collide on a shared sub string. Guarded ALTER; fresh DBs get neither here
        # nor in _SCHEMA (these columns are ALTER-only additive, like companion_conv_id).
        for col in ("oidc_sub", "oidc_issuer"):
            try:
                self._db.execute(f"ALTER TABLE users ADD COLUMN {ine}{col} TEXT")
            except sqlite3.OperationalError:
                pass
        # (oidc_issuer, oidc_sub) is the SSO identity lookup + a uniqueness guard against double
        # provisioning. NULLs are distinct in a UNIQUE index on both SQLite and Postgres, so the many
        # password-only users (both columns NULL) don't collide.
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oidc "
                         "ON users (oidc_issuer, oidc_sub)")
        # Custom task types (CT): additive JSON column on the C6 routing overlay. Guarded ALTER so
        # a DB created before CT gains the column (fresh DBs get it from _SCHEMA above). Absence of
        # the column value defaults to '[]' -> no custom labels -> byte-identical routing.
        try:
            self._db.execute(
                f"ALTER TABLE routing_policies ADD COLUMN {ine}custom_labels TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        # Pre-warm toggle: additive per-org boolean on the C6 routing overlay. Guarded ALTER so a
        # DB created before it gains the column (fresh DBs get it from _SCHEMA). Absence -> 0 -> OFF.
        try:
            self._db.execute(
                f"ALTER TABLE routing_policies ADD COLUMN {ine}prewarm INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Per-task-type stickiness holds (S2): additive JSON column on the C6 routing overlay.
        # Guarded ALTER so a DB created before it gains the column; absence -> '{}' -> flat holds.
        try:
            self._db.execute(
                f"ALTER TABLE routing_policies ADD COLUMN {ine}stick_ttls TEXT NOT NULL DEFAULT '{{}}'")
        except sqlite3.OperationalError:
            pass
        # Per-org cache-behavior overrides (A8): additive JSON column on the C6 routing overlay.
        # Guarded ALTER so a DB created before it gains the column; absence -> '{}' -> inherit every
        # cache knob from the global env defaults (the pre-A8 behavior, byte-identical).
        try:
            self._db.execute(
                f"ALTER TABLE routing_policies ADD COLUMN {ine}cache TEXT NOT NULL DEFAULT '{{}}'")
        except sqlite3.OperationalError:
            pass
        # Fail policy (W1-C1): additive per-org switch on the C6 routing overlay. Guarded ALTER so a
        # DB created before it gains the column; absence -> 'open' -> today's fall-through behavior.
        try:
            self._db.execute(
                f"ALTER TABLE routing_policies ADD COLUMN {ine}fail_policy TEXT NOT NULL DEFAULT 'open'")
        except sqlite3.OperationalError:
            pass
        # Data-classification taxonomy (W2-C7): additive JSON column on the C6 routing overlay.
        # Guarded ALTER so a DB created before it gains the column; absence -> '{}' -> no taxonomy ->
        # no data-policy constraint (byte-identical routing).
        try:
            self._db.execute(
                f"ALTER TABLE routing_policies ADD COLUMN {ine}taxonomy TEXT NOT NULL DEFAULT '{{}}'")
        except sqlite3.OperationalError:
            pass
        # Pluggable in-perimeter classifier (W3-C1): additive per-org classifier id on the C6 overlay.
        # Guarded ALTER so a DB created before it gains the column; absence -> NULL -> the gateway
        # default classifier (byte-identical routing).
        try:
            self._db.execute(
                f"ALTER TABLE routing_policies ADD COLUMN {ine}classifier_model TEXT")
        except sqlite3.OperationalError:
            pass
        # Zero-retention mode (W1-C4): additive per-org privacy switch. Guarded ALTER so a DB
        # created before it gains the column (fresh DBs get it from _SCHEMA above). Absence -> 0 ->
        # env flags apply (TOTO_GW_LOG_CONTENT / cache), byte-identical to pre-C4 behavior.
        try:
            self._db.execute(
                f"ALTER TABLE organizations ADD COLUMN {ine}zero_retention INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Token-hygiene org policy (W2-C3): max_token_lifetime_days is the MINT ceiling (0/absent =
        # no cap; a mint clamps its requested lifetime DOWN to it, existing tokens are NOT retro-
        # expired). token_rotation_grace_minutes is how long a rotated-out secret keeps working
        # (default 60; 0 = immediate). Both are org account settings (sibling to zero_retention),
        # guarded ALTER, absence -> the documented defaults. Fresh DBs default from here too.
        for col, ddl in (("max_token_lifetime_days", "INTEGER NOT NULL DEFAULT 0"),
                         ("token_rotation_grace_minutes", "INTEGER NOT NULL DEFAULT 60")):
            try:
                self._db.execute(
                    f"ALTER TABLE organizations ADD COLUMN {ine}{col} {ddl}")
            except sqlite3.OperationalError:
                pass
        # Content-plane retention policy (W3-C6): additive per-org JSON column, sibling to
        # zero_retention. Guarded ALTER so a pre-C6 DB gains it (empty -> keep-forever, unchanged).
        try:
            self._db.execute(
                f"ALTER TABLE organizations ADD COLUMN {ine}retention_policy TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # The org-scoped read is the hot query; one composite index covers filter+order.
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_audit_org_ts "
                         "ON audit_events (org_id, ts)")
        self._db.commit()
        self._lock = threading.Lock()

    # --- users ----------------------------------------------------------------

    async def get_user_by_email(self, email: str) -> dict | None:
        row = await self._one("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
        return dict(row) if row else None

    async def get_user(self, user_id: str) -> dict | None:
        row = await self._one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(row) if row else None

    async def create_user(self, email: str, password_hash: str | None, *,
                    email_verified: bool = False, google_sub: str | None = None) -> str:
        """Insert a user, return its user_id. Raises sqlite3.IntegrityError on duplicate email."""
        user_id = secrets.token_hex(8)
        try:
            await self._exec(
                "INSERT INTO users (user_id, email, password_hash, email_verified, "
                "google_sub, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, email.strip().lower(), password_hash, int(email_verified),
                 google_sub, time.time()),
            )
        except Exception as exc:  # normalize PG's UniqueViolation to the sqlite contract
            if self._pg and type(exc).__name__ == "UniqueViolation":
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise
        # Provision the personal org (owner) at creation so every new user is tenanted from
        # request #1; existing users get theirs lazily via resolve_membership.
        await self.resolve_membership(user_id)
        return user_id

    async def mark_verified(self, user_id: str) -> None:
        await self._exec("UPDATE users SET email_verified = 1 WHERE user_id = ?", (user_id,))

    async def has_users(self) -> bool:
        return await self._one("SELECT 1 FROM users LIMIT 1") is not None

    # --- BYOK provider keys (docs: fireworks-byok) -----------------------------
    # STRICT per-user scoping: every method keys on user_id, no NULL grandfathering. Anonymous
    # (user_id None) has no keys — `WHERE user_id = ?` with None matches nothing (fail closed).

    async def set_provider_key(self, user_id: str, provider: str, encrypted: str,
                               last4: str) -> None:
        """Upsert this user's encrypted key for a provider (re-PUT replaces it)."""
        await self._exec(
            "INSERT INTO provider_keys (user_id, provider, encrypted_key, last4, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, provider) DO UPDATE SET "
            "encrypted_key = excluded.encrypted_key, last4 = excluded.last4, "
            "created_at = excluded.created_at",
            (user_id, provider, encrypted, last4, time.time()),
        )

    async def list_provider_keys(self, user_id: str) -> list[dict]:
        """This user's providers + last4 — NO key material. For the Settings status list."""
        rows = await self._all(
            "SELECT provider, last4 FROM provider_keys WHERE user_id = ? ORDER BY provider",
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def get_provider_key_map(self, user_id: str) -> dict[str, str]:
        """{provider: encrypted_key} for the auth-time BYOK load. Ciphertext only (decrypt in
        credentials.py)."""
        rows = await self._all(
            "SELECT provider, encrypted_key FROM provider_keys WHERE user_id = ?",
            (user_id,),
        )
        return {r["provider"]: r["encrypted_key"] for r in rows}

    async def delete_provider_key(self, user_id: str, provider: str) -> None:
        await self._exec(
            "DELETE FROM provider_keys WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )

    # --- org-wide BYOK provider keys (routes/org_credentials.py) ---------------
    # Same posture as the per-user methods, keyed on org_id. `WHERE org_id = ?` with None
    # matches nothing (fail closed for the org-less caller).

    async def set_org_provider_key(self, org_id: str, provider: str, encrypted: str,
                                   last4: str) -> None:
        await self._exec(
            "INSERT INTO org_provider_keys (org_id, provider, encrypted_key, last4, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(org_id, provider) DO UPDATE SET "
            "encrypted_key = excluded.encrypted_key, last4 = excluded.last4, "
            "created_at = excluded.created_at",
            (org_id, provider, encrypted, last4, time.time()),
        )

    async def list_org_provider_keys(self, org_id: str) -> list[dict]:
        rows = await self._all(
            "SELECT provider, last4, created_at FROM org_provider_keys WHERE org_id = ? "
            "ORDER BY provider",
            (org_id,),
        )
        return [dict(r) for r in rows]

    async def get_org_provider_key_map(self, org_id: str) -> dict[str, str]:
        rows = await self._all(
            "SELECT provider, encrypted_key FROM org_provider_keys WHERE org_id = ?",
            (org_id,),
        )
        return {r["provider"]: r["encrypted_key"] for r in rows}

    async def delete_org_provider_key(self, org_id: str, provider: str) -> None:
        await self._exec(
            "DELETE FROM org_provider_keys WHERE org_id = ? AND provider = ?",
            (org_id, provider),
        )

    # --- tenancy (control-plane C1) -------------------------------------------
    # org -> team -> member. Roles owner/admin/member. The personal-org id is DERIVED from the
    # user_id ("o_" + user_id) so provisioning is idempotent and race-safe with zero locking: two
    # concurrent first-requests for the same user target the same rows and ON CONFLICT DO NOTHING.

    def _ignore(self, cols: str) -> str:
        """`ON CONFLICT (...) DO NOTHING` — valid on both SQLite (3.24+) and Postgres."""
        return f"ON CONFLICT ({cols}) DO NOTHING"

    async def create_org(self, name: str, *, org_id: str | None = None) -> str:
        """Insert an org (random id unless one is supplied), return its org_id. Idempotent on id."""
        org_id = org_id or ("o_" + secrets.token_hex(8))
        await self._exec(
            f"INSERT INTO organizations (org_id, name, created_at, status) VALUES (?, ?, ?, 'active') "
            f"{self._ignore('org_id')}",
            (org_id, name, time.time()),
        )
        return org_id

    async def get_org(self, org_id: str) -> dict | None:
        row = await self._one("SELECT * FROM organizations WHERE org_id = ?", (org_id,))
        return dict(row) if row else None

    async def get_team(self, team_id: str) -> dict | None:
        row = await self._one("SELECT * FROM teams WHERE team_id = ?", (team_id,))
        return dict(row) if row else None

    async def create_team(self, org_id: str, name: str, *, team_id: str | None = None) -> str:
        team_id = team_id or ("t_" + secrets.token_hex(8))
        await self._exec(
            f"INSERT INTO teams (team_id, org_id, name, created_at, status) "
            f"VALUES (?, ?, ?, ?, 'active') {self._ignore('team_id')}",
            (team_id, org_id, name, time.time()),
        )
        return team_id

    async def add_membership(self, org_id: str, user_id: str, role: str,
                             *, team_id: str | None = None) -> None:
        """Attach a user to an org with a role. Idempotent on (org_id, user_id) — a re-add is a
        no-op (use set_role to change a role). Role is validated fail-closed."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        await self._exec(
            f"INSERT INTO memberships (org_id, user_id, team_id, role, created_at) "
            f"VALUES (?, ?, ?, ?, ?) {self._ignore('org_id, user_id')}",
            (org_id, user_id, team_id, role, time.time()),
        )

    async def get_membership(self, user_id: str) -> dict | None:
        """Read the user's first membership without provisioning one."""
        row = await self._one(
            "SELECT org_id, team_id, role FROM memberships WHERE user_id = ? "
            "ORDER BY created_at LIMIT 1",
            (user_id,),
        )
        return dict(row) if row is not None else None

    async def get_membership_in(self, user_id: str, org_id: str) -> dict | None:
        """The user's membership in ONE specific org, else None. The multi-org selector: used to
        honor a credential's org binding at resolve time and to validate a switch/mint request (a
        foreign org_id returns None -> 403 or safe fallback, never a cross-org leak)."""
        row = await self._one(
            "SELECT org_id, team_id, role FROM memberships WHERE user_id = ? AND org_id = ?",
            (user_id, org_id),
        )
        return dict(row) if row is not None else None

    async def list_user_memberships(self, user_id: str) -> list[dict]:
        """Every org this user belongs to — {org_id, org_name, role} — for the switch UI and
        GET /v1/auth/memberships. Ordered oldest-first (matches the default-resolution order)."""
        rows = await self._all(
            "SELECT m.org_id, o.name AS org_name, m.role FROM memberships m "
            "JOIN organizations o ON o.org_id = m.org_id WHERE m.user_id = ? ORDER BY m.created_at",
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def resolve_membership(self, user_id: str, *, preferred_org_id: str | None = None) -> dict:
        """The user's {org_id, team_id, role}. Lazily provisions a personal org (owner) the first
        time a pre-tenancy user is seen — this IS the backfill for existing users (no boot scan).

        `preferred_org_id` (W2-C1 multi-org): when the caller's credential is bound to an org (a
        switched session / an org-scoped API token) and the user STILL holds a membership there,
        resolve THAT org instead of the oldest row — the fix for oldest-row-wins. A stale/foreign
        binding (membership since removed, or never held) falls through to the deterministic default
        (oldest), never 500s and never leaks another org (fail-safe).
        ponytail: one SELECT on the hot auth path per authed request, one INSERT once-ever per
        user; add a request-scoped cache only if it shows in p95."""
        if preferred_org_id:
            bound = await self.get_membership_in(user_id, preferred_org_id)
            if bound is not None:
                return bound
        row = await self.get_membership(user_id)
        if row is not None:
            return row
        # No membership yet: provision the personal org (owner). Derived org_id → idempotent.
        user = await self.get_user(user_id)
        name = user["email"].split("@")[0] if user and user.get("email") else "Personal"
        org_id = await self.create_org(f"{name}'s org", org_id="o_" + user_id)
        await self.add_membership(org_id, user_id, "owner")
        return {"org_id": org_id, "team_id": None, "role": "owner"}

    async def backfill_personal_orgs(self) -> int:
        """Provision a personal org for every user lacking one. Optional batch companion to the
        lazy resolve path (call at boot to warm it); returns the count provisioned. Idempotent."""
        rows = await self._all(
            "SELECT user_id FROM users WHERE user_id NOT IN (SELECT user_id FROM memberships)")
        for r in rows:
            await self.resolve_membership(r["user_id"])
        return len(rows)

    # --- catalog-scoped RBAC (control-plane C2) -------------------------------
    # Per-team allow/deny overlay over catalog ids. Keyed by team_id; org_id carried for the
    # admin API's org-isolation check. Absence of a row = permissive (effective_policy returns
    # None → the router keeps its global policy, ZERO behavior change).

    async def get_catalog_policy(self, team_id: str) -> dict | None:
        """The team's catalog policy as a dict (models/residency parsed from JSON), or None when
        the team has no policy. None = permissive."""
        if not team_id:
            return None
        row = await self._one("SELECT * FROM catalog_policies WHERE team_id = ?", (team_id,))
        if row is None:
            return None
        d = dict(row)
        d["models"] = json.loads(d.get("models") or "[]")
        d["residency"] = json.loads(d["residency"]) if d.get("residency") else None
        return d

    async def set_catalog_policy(self, team_id: str, org_id: str, *, mode: str = "allow",
                                 models: list[str] | None = None,
                                 residency: list[str] | None = None,
                                 default_model: str | None = None,
                                 updated_by: str | None = None) -> dict:
        """Upsert the team's catalog policy, bumping `version` on every write. Fail-closed on an
        unknown mode. Returns the stored policy. One dual-dialect UPSERT: `excluded.`/self-reference
        work identically on SQLite (3.24+) and Postgres."""
        if mode not in CATALOG_MODES:
            raise ValueError(f"unknown catalog-policy mode {mode!r}")
        models_json = json.dumps(list(models or []))
        residency_json = json.dumps(list(residency)) if residency is not None else None
        await self._exec(
            "INSERT INTO catalog_policies (team_id, org_id, mode, models, residency, "
            "default_model, version, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (team_id) DO UPDATE SET org_id=excluded.org_id, mode=excluded.mode, "
            "models=excluded.models, residency=excluded.residency, "
            "default_model=excluded.default_model, version=catalog_policies.version+1, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (team_id, org_id, mode, models_json, residency_json, default_model, updated_by,
             time.time()),
        )
        return await self.get_catalog_policy(team_id)

    # --- routing overlay (control-plane C6) -----------------------------------
    # Per-team tag->model overlay on top of the global routing/labels.yaml. Keyed by team_id;
    # org_id carried for the admin API's org-isolation check. Absence of a row = pure global
    # behavior (effective_policy carries no overlay -> unchanged routing, ZERO change).

    async def get_routing_policy(self, team_id: str) -> dict | None:
        """The team's routing overlay as a dict (bindings parsed from JSON), or None when the team
        has no policy. None = pure global behavior."""
        if not team_id:
            return None
        row = await self._one("SELECT * FROM routing_policies WHERE team_id = ?", (team_id,))
        if row is None:
            return None
        d = dict(row)
        d["bindings"] = json.loads(d.get("bindings") or "{}")
        d["custom_labels"] = json.loads(d.get("custom_labels") or "[]")  # CT team-invented task types
        d["prewarm"] = bool(d.get("prewarm"))  # 0/1 column -> bool for the API view + prewarm route read
        d["stick_ttls"] = json.loads(d.get("stick_ttls") or "{}")  # per-task-type memo holds (S2)
        d["cache"] = json.loads(d.get("cache") or "{}")  # per-org cache-behavior overrides (A8)
        # W1-C1: open (default) | closed; W2-C7: a JSON object is a per-reason matrix (parse it back).
        fp = d.get("fail_policy") or "open"
        if isinstance(fp, str) and fp.startswith("{"):
            try:
                fp = json.loads(fp)
            except (json.JSONDecodeError, ValueError):
                fp = "open"
        d["fail_policy"] = fp
        d["taxonomy"] = json.loads(d.get("taxonomy") or "{}")  # W2-C7 data-classification taxonomy
        d["classifier_model"] = d.get("classifier_model") or None  # W3-C1 org classifier (NULL = default)
        return d

    async def set_routing_policy(self, team_id: str, org_id: str, *,
                                 bindings: dict[str, str] | None = None,
                                 optimize: str | None = None,
                                 custom_labels: list[dict] | None = None,
                                 prewarm: bool | None = None,
                                 stick_ttls: dict | None = None,
                                 cache: dict | None = None,
                                 fail_policy: str | dict | None = None,
                                 taxonomy: dict | None = None,
                                 classifier_model: str | None = None,
                                 updated_by: str | None = None) -> dict:
        """Upsert the team's routing overlay, bumping `version` on every write. Fail-closed on an
        unknown optimize preset (catalog-existence of a bound model + custom-label slug/collision are
        validated at the API layer, which has the catalog + global vocab handles). custom_labels (CT)
        is the team's invented task types [{name, desc, model}]. Returns the stored policy. One
        dual-dialect UPSERT."""
        if optimize is not None and optimize not in ROUTING_OPTIMIZE:
            raise ValueError(f"unknown optimize preset {optimize!r}")
        bindings_json = json.dumps(dict(bindings or {}))
        custom_json = json.dumps(list(custom_labels or []))
        prewarm_int = int(bool(prewarm))  # full-replace semantics: omitted -> OFF, like bindings
        stick_json = json.dumps(dict(stick_ttls or {}))  # full-replace: omitted -> {} -> flat holds
        cache_json = json.dumps(dict(cache or {}))  # full-replace: omitted -> {} -> inherit global env
        # W1-C1 scalar 'open'/'closed'; W2-C7 a per-reason matrix dict stored as JSON. Full-replace:
        # omitted -> 'open'.
        if isinstance(fail_policy, dict):
            fp = json.dumps(fail_policy)
        else:
            fp = "closed" if fail_policy == "closed" else "open"
        taxonomy_json = json.dumps(dict(taxonomy or {}))  # full-replace: omitted -> {} -> no taxonomy
        cm = classifier_model or None  # W3-C1 full-replace: omitted -> NULL -> gateway default classifier
        await self._exec(
            "INSERT INTO routing_policies (team_id, org_id, bindings, optimize, custom_labels, prewarm, "
            "stick_ttls, cache, fail_policy, taxonomy, classifier_model, version, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (team_id) DO UPDATE SET org_id=excluded.org_id, bindings=excluded.bindings, "
            "optimize=excluded.optimize, custom_labels=excluded.custom_labels, "
            "prewarm=excluded.prewarm, stick_ttls=excluded.stick_ttls, cache=excluded.cache, "
            "fail_policy=excluded.fail_policy, taxonomy=excluded.taxonomy, "
            "classifier_model=excluded.classifier_model, "
            "version=routing_policies.version+1, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (team_id, org_id, bindings_json, optimize, custom_json, prewarm_int, stick_json,
             cache_json, fp, taxonomy_json, cm, updated_by, time.time()),
        )
        return await self.get_routing_policy(team_id)

    # --- monthly budgets (W2-C5) ----------------------------------------------
    # Per-team monthly USD budget; org-default = the sentinel row (team_id == org_id), SAME shape as
    # routing_policies. Absence of a row = no budget = unchanged behavior.

    async def get_budget(self, team_id: str) -> dict | None:
        """The scope's budget row (thresholds parsed from JSON), or None. None = no budget."""
        if not team_id:
            return None
        row = await self._one("SELECT * FROM budgets WHERE team_id = ?", (team_id,))
        if row is None:
            return None
        d = dict(row)
        d["thresholds"] = json.loads(d.get("thresholds") or "[]")
        return d

    async def list_budgets(self, org_id: str) -> list[dict]:
        """Every budget row in an org (org-default sentinel included), by team_id. [] = none."""
        rows = await self._all("SELECT * FROM budgets WHERE org_id = ? ORDER BY team_id", (org_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["thresholds"] = json.loads(d.get("thresholds") or "[]")
            out.append(d)
        return out

    async def set_budget(self, team_id: str, org_id: str, *, monthly_usd: float,
                         action: str = "observe", thresholds: list | None = None,
                         updated_by: str | None = None) -> dict:
        """Upsert the scope's budget, bumping `version`. Fail-closed on an unknown action. Returns
        the stored budget. One dual-dialect UPSERT (same idiom as set_routing_policy)."""
        from .budgets import BUDGET_ACTIONS

        if action not in BUDGET_ACTIONS:
            raise ValueError(f"unknown budget action {action!r}")
        thr_json = json.dumps(list(thresholds) if thresholds is not None else [0.5, 0.8, 1.0])
        await self._exec(
            "INSERT INTO budgets (team_id, org_id, monthly_usd, action, thresholds, version, "
            "updated_by, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (team_id) DO UPDATE SET org_id=excluded.org_id, "
            "monthly_usd=excluded.monthly_usd, action=excluded.action, "
            "thresholds=excluded.thresholds, version=budgets.version+1, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (team_id, org_id, float(monthly_usd), action, thr_json, updated_by, time.time()),
        )
        return await self.get_budget(team_id)

    async def delete_budget(self, key: str) -> bool:
        """Remove a budget row by its scope key (team_id / org-default sentinel / member key). True if
        a row was there, False if not — the route maps False to 404. Used to clear a per-member cap so
        the member falls back to the team/org-default again."""
        if await self.get_budget(key) is None:
            return False
        await self._exec("DELETE FROM budgets WHERE team_id = ?", (key,))
        return True

    async def budget_alert_fire_once(self, scope_key: str, period: str, threshold: float) -> bool:
        """True only the FIRST time (scope, month, threshold) is recorded — the dedupe behind the
        once-per-threshold budget alert. A PK conflict (already fired) OR any error returns False:
        fail-safe, never double-alert, never crash the budget check."""
        try:
            await self._exec(
                "INSERT INTO budget_alerts (scope_key, period, threshold, fired_at) "
                "VALUES (?, ?, ?, ?)", (scope_key, period, float(threshold), time.time()))
            return True
        except Exception:
            return False

    # --- catalog adoptions (catalog-adoption) ---------------------------------
    # Server-side "add this provider-library model to my catalog." Scoped by scope_key = team_id or
    # org_id (resolved in deps._resolve_adoptions, the SAME fallback as _resolve_routing_policy).
    # entry_json is the materialized CatalogEntry the API derived from the provider discovery
    # snapshot — the store just persists it; all fact-derivation + naming validation happens in the
    # admin route (which holds the base catalog + discovery handles). Absence of rows = base catalog
    # only (ZERO behavior change).

    @staticmethod
    def _adoption_row(row) -> dict:
        d = dict(row)
        d["entry"] = json.loads(d["entry_json"])  # the materialized CatalogEntry dict
        return d

    async def list_adoptions(self, scope_key: str) -> list[dict]:
        """The scope's adoptions (each with `entry` = the parsed CatalogEntry dict), by id. [] = none."""
        if not scope_key:
            return []
        rows = await self._all(
            "SELECT scope_key, id, entry_json, upstream_model, provider, created_by, updated_at "
            "FROM catalog_adoptions WHERE scope_key = ? ORDER BY id", (scope_key,))
        return [self._adoption_row(r) for r in rows]

    async def get_adoption(self, scope_key: str, id: str) -> dict | None:
        if not scope_key:
            return None
        row = await self._one(
            "SELECT scope_key, id, entry_json, upstream_model, provider, created_by, updated_at "
            "FROM catalog_adoptions WHERE scope_key = ? AND id = ?", (scope_key, id))
        return self._adoption_row(row) if row is not None else None

    async def add_adoption(self, scope_key: str, id: str, *, entry_json: str,
                           upstream_model: str, provider: str,
                           created_by: str | None = None) -> dict:
        """Upsert one adoption. One dual-dialect UPSERT (re-adopting the same id replaces the entry)."""
        await self._exec(
            "INSERT INTO catalog_adoptions (scope_key, id, entry_json, upstream_model, provider, "
            "created_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (scope_key, id) DO UPDATE SET entry_json=excluded.entry_json, "
            "upstream_model=excluded.upstream_model, provider=excluded.provider, "
            "created_by=excluded.created_by, updated_at=excluded.updated_at",
            (scope_key, id, entry_json, upstream_model, provider, created_by, time.time()))
        return await self.get_adoption(scope_key, id)

    async def remove_adoption(self, scope_key: str, id: str) -> bool:
        """Delete one adoption, scope-pinned. False when the row isn't in this scope (→ 404, so a
        caller can't probe another scope's ids)."""
        if not scope_key:
            return False
        row = await self._one(
            "SELECT id FROM catalog_adoptions WHERE scope_key = ? AND id = ?", (scope_key, id))
        if row is None:
            return False
        await self._exec("DELETE FROM catalog_adoptions WHERE scope_key = ? AND id = ?",
                         (scope_key, id))
        return True

    # --- price overrides (catalog-pricing-plane) — same shape/discipline as adoptions ---------

    async def list_price_overrides(self, *scope_keys: str) -> list[dict]:
        """Override rows for the given scopes (deduped, falsy keys dropped), ordered by model_id.
        Callers merge precedence themselves — this is a plain read."""
        keys = tuple(dict.fromkeys(k for k in scope_keys if k))
        if not keys:
            return []
        marks = ",".join("?" for _ in keys)
        rows = await self._all(
            "SELECT scope_key, model_id, prompt_usd_per_1k, completion_usd_per_1k, "
            "cache_read_multiplier, updated_by, updated_at "
            f"FROM price_overrides WHERE scope_key IN ({marks}) ORDER BY model_id", keys)
        return [dict(r) for r in rows]

    async def set_price_override(self, scope_key: str, model_id: str, *,
                                 prompt_usd_per_1k: float, completion_usd_per_1k: float,
                                 cache_read_multiplier: float | None = None,
                                 updated_by: str | None = None) -> dict:
        """Upsert one override (per-1k figures — the API boundary owns the per-Mtok conversion)."""
        await self._exec(
            "INSERT INTO price_overrides (scope_key, model_id, prompt_usd_per_1k, "
            "completion_usd_per_1k, cache_read_multiplier, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (scope_key, model_id) DO UPDATE SET "
            "prompt_usd_per_1k=excluded.prompt_usd_per_1k, "
            "completion_usd_per_1k=excluded.completion_usd_per_1k, "
            "cache_read_multiplier=excluded.cache_read_multiplier, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (scope_key, model_id, prompt_usd_per_1k, completion_usd_per_1k,
             cache_read_multiplier, updated_by, time.time()))
        rows = await self.list_price_overrides(scope_key)
        return next(r for r in rows if r["model_id"] == model_id)

    async def remove_price_override(self, scope_key: str, model_id: str) -> bool:
        """Delete one override, scope-pinned (mirrors remove_adoption's 404 discipline)."""
        if not scope_key:
            return False
        row = await self._one(
            "SELECT model_id FROM price_overrides WHERE scope_key = ? AND model_id = ?",
            (scope_key, model_id))
        if row is None:
            return False
        await self._exec("DELETE FROM price_overrides WHERE scope_key = ? AND model_id = ?",
                         (scope_key, model_id))
        return True

    # --- tenancy admin (control-plane C5) -------------------------------------
    # Every method is ORG-SCOPED: the org_id is passed in (resolved server-side from the caller's
    # Identity, never a param) and every WHERE pins it, so a caller can only ever touch their own
    # org's rows (IDOR discipline). Mutations that target a specific row SELECT-to-verify-ownership
    # then act (same idiom as revoke_api_token) and return False when the row isn't in this org.

    async def list_teams(self, org_id: str) -> list[dict]:
        rows = await self._all(
            "SELECT team_id, org_id, name, created_at, status FROM teams "
            "WHERE org_id = ? AND status = 'active' ORDER BY created_at", (org_id,))
        return [dict(r) for r in rows]

    async def rename_team(self, org_id: str, team_id: str, name: str) -> bool:
        """Rename a team, org-scoped. False if the team isn't in this org (→ 404, no cross-org edit)."""
        row = await self._one(
            "SELECT team_id FROM teams WHERE team_id = ? AND org_id = ? AND status = 'active'",
            (team_id, org_id))
        if row is None:
            return False
        await self._exec("UPDATE teams SET name = ? WHERE team_id = ?", (name, team_id))
        return True

    async def delete_team(self, org_id: str, team_id: str) -> bool:
        """Soft-delete a team (status='deleted'), org-scoped. False if not in this org. Soft so a
        stray membership.team_id doesn't dangle to a hard-gone row (thin slice has none, but cheap)."""
        row = await self._one(
            "SELECT team_id FROM teams WHERE team_id = ? AND org_id = ? AND status = 'active'",
            (team_id, org_id))
        if row is None:
            return False
        await self._exec("UPDATE teams SET status = 'deleted' WHERE team_id = ?", (team_id,))
        return True

    async def list_members(self, org_id: str) -> list[dict]:
        """Org members with their role + email (joined from users). Ordered by join time."""
        rows = await self._all(
            "SELECT m.user_id, m.role, m.team_id, m.created_at, u.email "
            "FROM memberships m JOIN users u ON u.user_id = m.user_id "
            "WHERE m.org_id = ? ORDER BY m.created_at", (org_id,))
        return [dict(r) for r in rows]

    async def _owner_count(self, org_id: str) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS n FROM memberships WHERE org_id = ? AND role = 'owner'", (org_id,))
        return row["n"] if row else 0

    async def set_role(self, org_id: str, user_id: str, role: str) -> bool:
        """Change a member's role, org-scoped. Returns False if the user isn't a member of this org.
        Raises ValueError on an unknown role or on demoting the org's LAST owner (that would orphan
        the org — nobody could ever administer it again). The route maps False→404, ValueError→409."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        row = await self._one(
            "SELECT role FROM memberships WHERE org_id = ? AND user_id = ?", (org_id, user_id))
        if row is None:
            return False
        if row["role"] == "owner" and role != "owner" and await self._owner_count(org_id) <= 1:
            raise ValueError("cannot demote the last owner")
        await self._exec(
            "UPDATE memberships SET role = ? WHERE org_id = ? AND user_id = ?",
            (role, org_id, user_id))
        return True

    async def remove_membership(self, org_id: str, user_id: str) -> bool:
        """Remove a user from an org, org-scoped. False if they aren't a member. Raises ValueError
        on removing the LAST owner (would orphan the org). Route maps False→404, ValueError→409."""
        row = await self._one(
            "SELECT role FROM memberships WHERE org_id = ? AND user_id = ?", (org_id, user_id))
        if row is None:
            return False
        if row["role"] == "owner" and await self._owner_count(org_id) <= 1:
            raise ValueError("cannot remove the last owner")
        await self._exec(
            "DELETE FROM memberships WHERE org_id = ? AND user_id = ?", (org_id, user_id))
        return True

    async def rename_org(self, org_id: str, name: str) -> None:
        await self._exec("UPDATE organizations SET name = ? WHERE org_id = ?", (name, org_id))

    async def set_zero_retention(self, org_id: str, on: bool) -> None:
        """Flip the org's zero-retention switch (W1-C4). Stored as 0/1; the identity resolver reads
        it at auth time so every downstream telemetry sink can gate on it."""
        await self._exec("UPDATE organizations SET zero_retention = ? WHERE org_id = ?",
                         (1 if on else 0, org_id))

    # --- content-plane retention policy (W3-C6) -------------------------------
    # Per-org retention DAYS per product-storage sink, stored as JSON on the org row (sibling to
    # zero_retention). The prune sweep (retention.py) reads it; absent/0 for a sink = keep forever.

    async def set_retention_policy(self, org_id: str, policy: dict) -> None:
        """Persist the org's per-sink retention policy (validated by the route). Empty dict clears
        it (back to keep-forever). Stored as a JSON string in organizations.retention_policy."""
        await self._exec("UPDATE organizations SET retention_policy = ? WHERE org_id = ?",
                         (json.dumps(policy) if policy else '', org_id))

    async def get_retention_policy(self, org_id: str) -> dict:
        """The org's per-sink retention policy as a dict ({} = nothing set = keep everything)."""
        row = await self._one(
            "SELECT retention_policy FROM organizations WHERE org_id = ?", (org_id,))
        return _parse_retention(dict(row).get("retention_policy") if row else None)

    async def list_retention_orgs(self) -> list[dict]:
        """Every org with a non-empty retention policy — the sweep's work list. [{org_id, policy}]."""
        rows = await self._all(
            "SELECT org_id, retention_policy FROM organizations "
            "WHERE retention_policy <> '' AND retention_policy IS NOT NULL")
        out = []
        for r in rows:
            d = dict(r)
            policy = _parse_retention(d.get("retention_policy"))
            if policy:
                out.append({"org_id": d["org_id"], "policy": policy})
        return out

    async def list_org_user_ids(self, org_id: str) -> list[str]:
        """Every member user_id of an org (the per-user scope the content plane prunes by)."""
        rows = await self._all(
            "SELECT user_id FROM memberships WHERE org_id = ?", (org_id,))
        return [r["user_id"] for r in rows]

    # --- audit export (W2-C4) -------------------------------------------------
    # Per-org config (secret Fernet-encrypted upstream, like org_sso_configs) + the batch ledger
    # that doubles as the hash chain and the listing. See audit_export.py for the export engine.

    async def set_audit_export_config(self, org_id: str, *, enabled: bool, cadence_hours: float,
                                      retention_days: int, destination: str, s3_endpoint: str,
                                      s3_bucket: str, s3_region: str, s3_access_key: str,
                                      s3_secret_enc: str, s3_prefix: str) -> None:
        """Upsert an org's audit-export config. s3_secret_enc is ciphertext (the route encrypts and
        keeps the stored value on a metadata-only edit); last_run/last_error are owned by the
        scheduler (set_audit_export_run) and untouched here."""
        now = time.time()
        await self._exec(
            "INSERT INTO audit_export_configs (org_id, enabled, cadence_hours, retention_days, "
            "destination, s3_endpoint, s3_bucket, s3_region, s3_access_key, s3_secret_enc, "
            "s3_prefix, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (org_id) DO UPDATE SET enabled=excluded.enabled, "
            "cadence_hours=excluded.cadence_hours, retention_days=excluded.retention_days, "
            "destination=excluded.destination, s3_endpoint=excluded.s3_endpoint, "
            "s3_bucket=excluded.s3_bucket, s3_region=excluded.s3_region, "
            "s3_access_key=excluded.s3_access_key, s3_secret_enc=excluded.s3_secret_enc, "
            "s3_prefix=excluded.s3_prefix, updated_at=excluded.updated_at",
            (org_id, 1 if enabled else 0, cadence_hours, retention_days, destination, s3_endpoint,
             s3_bucket, s3_region, s3_access_key, s3_secret_enc, s3_prefix, now, now))

    async def get_audit_export_config(self, org_id: str) -> dict | None:
        """The org's audit-export config, or None. Carries s3_secret_enc (ciphertext) — the route
        decrypts it to build the customer-S3 client and NEVER echoes it to the admin API."""
        row = await self._one("SELECT * FROM audit_export_configs WHERE org_id = ?", (org_id,))
        if row is None:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        return d

    async def list_audit_export_orgs(self) -> list[dict]:
        """Every org with export enabled (the scheduler's work list). Carries ciphertext; the
        scheduler decrypts per org to reach the customer bucket."""
        rows = await self._all(
            "SELECT * FROM audit_export_configs WHERE enabled = 1 ORDER BY org_id", ())
        out = []
        for r in rows:
            d = dict(r)
            d["enabled"] = True
            out.append(d)
        return out

    async def set_audit_export_run(self, org_id: str, *, last_run: float,
                                   last_error: str | None) -> None:
        """Stamp the scheduler's last-run outcome (surfaced in the config GET). Best-effort caller."""
        await self._exec(
            "UPDATE audit_export_configs SET last_run = ?, last_error = ? WHERE org_id = ?",
            (last_run, last_error, org_id))

    async def set_org_storage_config(self, org_id: str, *, enabled: bool, s3_endpoint: str,
                                     s3_bucket: str, s3_region: str, s3_access_key: str,
                                     s3_secret_enc: str, s3_force_path_style: bool) -> None:
        """Upsert an org's BYOS storage connector. s3_secret_enc is ciphertext (the route encrypts
        and keeps the stored value on a metadata-only edit); last_test/last_error are owned by the
        test endpoint (set_org_storage_test) and untouched here."""
        now = time.time()
        await self._exec(
            "INSERT INTO org_storage_configs (org_id, enabled, s3_endpoint, s3_bucket, s3_region, "
            "s3_access_key, s3_secret_enc, s3_force_path_style, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (org_id) DO UPDATE SET enabled=excluded.enabled, "
            "s3_endpoint=excluded.s3_endpoint, s3_bucket=excluded.s3_bucket, "
            "s3_region=excluded.s3_region, s3_access_key=excluded.s3_access_key, "
            "s3_secret_enc=excluded.s3_secret_enc, "
            "s3_force_path_style=excluded.s3_force_path_style, updated_at=excluded.updated_at",
            (org_id, 1 if enabled else 0, s3_endpoint, s3_bucket, s3_region, s3_access_key,
             s3_secret_enc, 1 if s3_force_path_style else 0, now, now))

    async def get_org_storage_config(self, org_id: str) -> dict | None:
        """The org's storage connector config, or None. Carries s3_secret_enc (ciphertext) — the
        resolver decrypts it to build the org's S3 client and it is NEVER echoed over the API."""
        row = await self._one("SELECT * FROM org_storage_configs WHERE org_id = ?", (org_id,))
        if row is None:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["s3_force_path_style"] = bool(d["s3_force_path_style"])
        return d

    async def set_org_storage_test(self, org_id: str, *, last_test: float,
                                   last_error: str | None) -> None:
        """Stamp the most recent connection-test outcome (surfaced in the config GET)."""
        await self._exec(
            "UPDATE org_storage_configs SET last_test = ?, last_error = ? WHERE org_id = ?",
            (last_test, last_error, org_id))

    async def audit_export_tip(self, org_id: str, stream: str) -> dict | None:
        """The chain tip for (org, stream): the highest-batch ledger row, or None if never exported.
        The next batch is tip.batch + 1 and its prev_sha256 is tip.sha256."""
        row = await self._one(
            "SELECT * FROM audit_export_batches WHERE org_id = ? AND stream = ? "
            "ORDER BY batch DESC LIMIT 1", (org_id, stream))
        return dict(row) if row else None

    async def record_audit_export_batch(self, org_id: str, stream: str, *, batch: int,
                                        object_key: str, sha256: str, prev_sha256: str,
                                        rows: int, cursor: str, created_at: float) -> None:
        """Append one batch to the ledger (the hash-chain link + the listing entry)."""
        await self._exec(
            "INSERT INTO audit_export_batches (org_id, stream, batch, object_key, sha256, "
            "prev_sha256, rows, cursor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (org_id, stream, batch, object_key, sha256, prev_sha256, rows, cursor, created_at))

    async def list_audit_export_batches(self, org_id: str, stream: str | None = None) -> list[dict]:
        """The org's export batches (the auditor-readable listing), newest first. Filter by stream."""
        where = ["org_id = ?"]
        params: list = [org_id]
        if stream is not None:
            where.append("stream = ?")
            params.append(stream)
        rows = await self._all(
            f"SELECT org_id, stream, batch, object_key, sha256, prev_sha256, rows, created_at "
            f"FROM audit_export_batches WHERE {' AND '.join(where)} "
            f"ORDER BY stream, batch DESC", tuple(params))
        return [dict(r) for r in rows]

    async def get_audit_export_batch(self, org_id: str, stream: str, batch: int) -> dict | None:
        """One batch ledger row (the download route resolves object_key through it, org-scoped)."""
        row = await self._one(
            "SELECT * FROM audit_export_batches WHERE org_id = ? AND stream = ? AND batch = ?",
            (org_id, stream, batch))
        return dict(row) if row else None

    async def prune_audit_export_batches(self, org_id: str, before_ts: float) -> list[dict]:
        """Delete gateway-stored batch rows older than before_ts and return them (so the caller can
        delete the objects). Retention trims the OLDEST batches — a contiguous head prefix — so the
        surviving chain stays internally gapless and verifiable from its new floor."""
        rows = await self._all(
            "SELECT org_id, stream, batch, object_key FROM audit_export_batches "
            "WHERE org_id = ? AND created_at < ?", (org_id, before_ts))
        pruned = [dict(r) for r in rows]
        if pruned:
            await self._exec(
                "DELETE FROM audit_export_batches WHERE org_id = ? AND created_at < ?",
                (org_id, before_ts))
        return pruned

    async def list_audit_since(self, org_id: str, after_ts: float, limit: int) -> list[dict]:
        """audit_events rows for one org with ts > after_ts, oldest first (the export cursor walks
        forward). Metadata only, by construction of the table. org-scoped — never another org's rows."""
        rows = await self._all(
            "SELECT id, ts, action, user_id, ip, request_id, org_id, target_type, target_id, "
            "metadata FROM audit_events WHERE org_id = ? AND ts > ? ORDER BY ts ASC LIMIT ?",
            (org_id, after_ts, max(1, int(limit))))
        return [dict(r) for r in rows]

    # --- invitations (C5) -----------------------------------------------------

    async def create_invitation(self, org_id: str, email: str, role: str) -> dict:
        """Create a pending invitation, return its row (token included — shown to the inviter so
        they can hand it to the invitee). Role validated fail-closed."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        inv_id = "inv_" + secrets.token_hex(8)
        token = secrets.token_urlsafe(24)
        now = time.time()
        await self._exec(
            "INSERT INTO invitations (id, org_id, email, role, token, created_ts, accepted_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (inv_id, org_id, email.strip().lower(), role, token, now))
        return {"id": inv_id, "org_id": org_id, "email": email.strip().lower(), "role": role,
                "token": token, "created_ts": now, "accepted_ts": None}

    async def list_invitations(self, org_id: str, *, pending_only: bool = True) -> list[dict]:
        """This org's invitations. pending_only → only those not yet accepted."""
        clause = " AND accepted_ts IS NULL" if pending_only else ""
        rows = await self._all(
            "SELECT id, org_id, email, role, token, created_ts, accepted_ts FROM invitations "
            f"WHERE org_id = ?{clause} ORDER BY created_ts DESC", (org_id,))
        return [dict(r) for r in rows]

    async def accept_invitation(self, token: str, user_id: str) -> dict | None:
        """Bind the accepting user to the invited org with the invited role, then stamp the invite
        accepted. Returns the invitation row on success, None if the token is unknown or already
        accepted. Idempotent membership add (a user already in the org keeps their existing role —
        add_membership is ON CONFLICT DO NOTHING; use set_role to change it)."""
        row = await self._one(
            "SELECT id, org_id, email, role FROM invitations "
            "WHERE token = ? AND accepted_ts IS NULL", (token,))
        if row is None:
            return None
        await self.add_membership(row["org_id"], user_id, row["role"])
        await self._exec(
            "UPDATE invitations SET accepted_ts = ? WHERE id = ?", (time.time(), row["id"]))
        return {"id": row["id"], "org_id": row["org_id"], "email": row["email"],
                "role": row["role"]}

    # --- OIDC SSO (W1-C6) ------------------------------------------------------
    # Org SSO config (secret Fernet-encrypted upstream — the store persists ciphertext, never
    # plaintext), the domain->org map (uniqueness enforced by the sso_domains PK), server-side
    # single-use login state, and JIT provisioning matched on (issuer, sub) then verified email.

    async def set_sso_config(self, org_id: str, *, issuer: str, client_id: str,
                             client_secret_enc: str, domains: list[str],
                             sso_required: bool) -> None:
        """Upsert an org's SSO config and REPLACE its domain set. Raises ValueError('domain_taken')
        if any domain is already claimed by a different org (the sso_domains PK is global). Domains
        are lowercased/stripped; empties dropped."""
        clean = sorted({d.strip().lower() for d in domains if d.strip()})
        for d in clean:  # a domain owned by another org must not be silently re-pointed
            row = await self._one("SELECT org_id FROM sso_domains WHERE domain = ?", (d,))
            if row is not None and row["org_id"] != org_id:
                raise ValueError("domain_taken")
        now = time.time()
        await self._exec(
            "INSERT INTO org_sso_configs (org_id, issuer, client_id, client_secret_enc, "
            "sso_required, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (org_id) DO UPDATE SET issuer=excluded.issuer, client_id=excluded.client_id, "
            "client_secret_enc=excluded.client_secret_enc, sso_required=excluded.sso_required, "
            "updated_at=excluded.updated_at",
            (org_id, issuer, client_id, client_secret_enc, int(sso_required), now, now))
        # Replace this org's domain rows (full-replace semantics, like the routing overlay).
        await self._exec("DELETE FROM sso_domains WHERE org_id = ?", (org_id,))
        for d in clean:
            await self._exec("INSERT INTO sso_domains (domain, org_id) VALUES (?, ?)", (d, org_id))

    async def _sso_domains(self, org_id: str) -> list[str]:
        rows = await self._all(
            "SELECT domain FROM sso_domains WHERE org_id = ? ORDER BY domain", (org_id,))
        return [r["domain"] for r in rows]

    async def get_sso_config(self, org_id: str) -> dict | None:
        """The org's SSO config with its domains, or None. Carries client_secret_enc (ciphertext) —
        the route decrypts it for the token exchange and NEVER echoes it to the admin API."""
        row = await self._one("SELECT * FROM org_sso_configs WHERE org_id = ?", (org_id,))
        if row is None:
            return None
        d = dict(row)
        d["sso_required"] = bool(d["sso_required"])
        d["domains"] = await self._sso_domains(org_id)
        return d

    async def get_sso_config_by_domain(self, domain: str) -> dict | None:
        """Resolve an email domain to its org's SSO config (the start endpoint + the login
        sso_required check). One indexed join. None when the domain has no SSO org."""
        if not domain:
            return None
        row = await self._one(
            "SELECT c.* FROM org_sso_configs c JOIN sso_domains d ON d.org_id = c.org_id "
            "WHERE d.domain = ?", (domain.strip().lower(),))
        if row is None:
            return None
        d = dict(row)
        d["sso_required"] = bool(d["sso_required"])
        return d

    async def create_login_state(self, *, org_id: str, nonce: str, code_verifier: str,
                                 redirect_to: str, ttl_seconds: float) -> str:
        """Store a single-use OIDC login state, return the state token (rides the authorize redirect)."""
        state = secrets.token_urlsafe(24)
        now = time.time()
        await self._exec(
            "INSERT INTO sso_login_states (state, org_id, nonce, code_verifier, redirect_to, "
            "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (state, org_id, nonce, code_verifier, redirect_to, now + ttl_seconds, now))
        return state

    async def consume_login_state(self, state: str) -> dict | None:
        """Single-use: return the state row and delete it atomically. None if unknown/expired. Same
        DELETE ... RETURNING (PG) / non-yielding SELECT+DELETE (SQLite) idiom as consume_token."""
        if self._pg:
            row = await self._one(
                "DELETE FROM sso_login_states WHERE state = ? "
                "RETURNING org_id, nonce, code_verifier, redirect_to, expires_at", (state,))
        else:
            row = await self._one(
                "SELECT org_id, nonce, code_verifier, redirect_to, expires_at "
                "FROM sso_login_states WHERE state = ?", (state,))
            if row is not None:
                await self._exec("DELETE FROM sso_login_states WHERE state = ?", (state,))
        if row is None or row["expires_at"] < time.time():
            return None
        return dict(row)

    async def get_user_by_oidc(self, issuer: str, sub: str) -> dict | None:
        row = await self._one(
            "SELECT * FROM users WHERE oidc_issuer = ? AND oidc_sub = ?", (issuer, sub))
        return dict(row) if row else None

    async def _set_user_oidc(self, user_id: str, issuer: str, sub: str) -> None:
        await self._exec(
            "UPDATE users SET oidc_issuer = ?, oidc_sub = ? WHERE user_id = ?",
            (issuer, sub, user_id))

    async def _create_sso_user(self, email: str, issuer: str, sub: str, org_id: str,
                               role: str = "member") -> str:
        """Insert a JIT-provisioned SSO user (email_verified, oidc identity) and attach the CONFIGURED
        org membership only — no personal org — so resolve_membership returns the enterprise org as
        their home (that's what 'lands in the right org' means). Raises sqlite3.IntegrityError on a
        racing duplicate."""
        user_id = secrets.token_hex(8)
        try:
            await self._exec(
                "INSERT INTO users (user_id, email, password_hash, email_verified, "
                "oidc_issuer, oidc_sub, created_at) VALUES (?, ?, NULL, 1, ?, ?, ?)",
                (user_id, email.strip().lower(), issuer, sub, time.time()))
        except Exception as exc:  # normalize PG's UniqueViolation to the sqlite contract
            if self._pg and type(exc).__name__ == "UniqueViolation":
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise
        await self.add_membership(org_id, user_id, role)
        return user_id

    async def provision_sso_login(self, *, issuer: str, sub: str, email: str,
                                  org_id: str) -> tuple[str, bool]:
        """Resolve an SSO login to a user_id, JIT-provisioning if new. Returns (user_id, provisioned).
        Match order: (issuer, sub) first, then verified email (link the oidc identity + attach the org
        membership), else create the user in the org as `member`. Never widens an existing user's
        role — an email match keeps whatever role they already hold (add_membership is idempotent)."""
        found = await self.get_user_by_oidc(issuer, sub)
        if found is not None:
            return found["user_id"], False
        existing = await self.get_user_by_email(email)
        if existing is not None:
            await self._set_user_oidc(existing["user_id"], issuer, sub)
            await self.add_membership(org_id, existing["user_id"], "member")
            return existing["user_id"], False
        return await self._create_sso_user(email, issuer, sub, org_id), True

    # --- SCIM 2.0 provisioning (W2-C2) -----------------------------------------
    # Per-org config in org_scim_configs: the SCIM bearer's sha256 digest (only the hash, like
    # auth_tokens), the group->role map (JSON), and the enabled flag. The SCIM endpoints authenticate
    # ONLY via org_by_scim_token -- no session/user credential is ever accepted there.

    async def get_scim_config(self, org_id: str) -> dict:
        """The org's SCIM config: {enabled, group_role_map (dict), has_token}. Always returns a dict
        (defaults when no row yet) so the admin surface can render before first save."""
        row = await self._one("SELECT * FROM org_scim_configs WHERE org_id = ?", (org_id,))
        if row is None:
            return {"enabled": False, "group_role_map": {}, "has_token": False}
        try:
            gm = json.loads(row["group_role_map"] or "{}")
        except (ValueError, TypeError):
            gm = {}
        return {"enabled": bool(row["enabled"]), "group_role_map": gm,
                "has_token": bool(row["token_hash"])}

    async def set_scim_config(self, org_id: str, *, group_role_map: dict, enabled: bool) -> None:
        """Upsert the group->role map + enabled flag WITHOUT touching the token (generate/revoke own
        that column). Owner is stripped from the map at rest -- SCIM can never grant ownership."""
        clean = {str(g): r for g, r in group_role_map.items() if r in ROLES and r != "owner"}
        now = time.time()
        await self._exec(
            "INSERT INTO org_scim_configs (org_id, group_role_map, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (org_id) DO UPDATE SET "
            "group_role_map=excluded.group_role_map, enabled=excluded.enabled, "
            "updated_at=excluded.updated_at",
            (org_id, json.dumps(clean), int(enabled), now, now))

    async def generate_scim_token(self, org_id: str) -> str:
        """Mint (or rotate) the org's SCIM bearer -- returns the raw token ONCE, stores only its
        sha256 digest. Rotation is implicit: writing a new hash invalidates the prior token. Enables
        SCIM as a side effect (a token with the switch off would be dead on arrival)."""
        raw = "scim_" + secrets.token_urlsafe(32)
        now = time.time()
        await self._exec(
            "INSERT INTO org_scim_configs (org_id, token_hash, enabled, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?) ON CONFLICT (org_id) DO UPDATE SET "
            "token_hash=excluded.token_hash, enabled=1, updated_at=excluded.updated_at",
            (org_id, _token_hash(raw), now, now))
        return raw

    async def revoke_scim_token(self, org_id: str) -> None:
        """Clear the org's SCIM bearer (IdP can no longer provision). Leaves the group map intact."""
        await self._exec(
            "UPDATE org_scim_configs SET token_hash = NULL, updated_at = ? WHERE org_id = ?",
            (time.time(), org_id))

    async def org_by_scim_token(self, raw: str) -> str | None:
        """The org a live, ENABLED SCIM bearer authenticates -- else None. The ONLY auth path for the
        SCIM endpoints; hard-scopes every SCIM request to exactly one org (org A's token can never
        touch org B). No expiry (revocation is the lever, same as API tokens)."""
        if not raw:
            return None
        row = await self._one(
            "SELECT org_id FROM org_scim_configs WHERE token_hash = ? AND enabled = 1",
            (_token_hash(raw),))
        return row["org_id"] if row else None

    async def scim_provision(self, *, org_id: str, email: str, role: str,
                             external_id: str | None, issuer: str | None) -> tuple[str, bool]:
        """SCIM create: resolve the email to a user (creating one, email_verified, if new), attach a
        membership in THIS org at `role`, and set the oidc identity when the payload carried it.
        Returns (user_id, created_now). Idempotent: a repeat with the same email returns the existing
        user with created_now=False (the route turns that into the RFC 409 uniqueness response).
        Never grants owner (the route resolves role via resolve_scim_role, which excludes owner)."""
        existing = await self.get_user_by_email(email)
        if existing is not None:
            uid = existing["user_id"]
            if external_id and issuer and not existing.get("oidc_sub"):
                await self._set_user_oidc(uid, issuer, external_id)
            await self.add_membership(org_id, uid, role if role != "owner" else "member")
            return uid, False
        uid = secrets.token_hex(8)
        await self._exec(
            "INSERT INTO users (user_id, email, password_hash, email_verified, "
            "oidc_issuer, oidc_sub, created_at) VALUES (?, ?, NULL, 1, ?, ?, ?)",
            (uid, email.strip().lower(), issuer, external_id, time.time()))
        await self.add_membership(org_id, uid, role if role != "owner" else "member")
        return uid, True

    async def scim_deactivate(self, org_id: str, user_id: str) -> dict:
        """The deprovision money path (W2-C2). Kill this user's access to THIS org within the request:
        drop their org membership and revoke every credential BOUND to this org (sessions AND api
        tokens where auth_tokens.org_id == this org). Multi-org safe: their org-B-bound and their
        still-resolving unbound credentials survive UNLESS this was their last org -- when no
        membership remains anywhere, we also revoke their unbound credentials (a personal/unbound
        session would otherwise keep resolving), i.e. a global credential kill. Returns counts for
        the audit row. ponytail: two scoped DELETEs + a conditional third; no per-token loop."""
        removed = await self.remove_membership(org_id, user_id)
        killed = await self._exec_count(
            "DELETE FROM auth_tokens WHERE user_id = ? AND org_id = ?", (user_id, org_id))
        remaining = await self.list_user_memberships(user_id)
        if not remaining:  # last org -> also kill unbound (personal) credentials: total lockout
            killed += await self._exec_count(
                "DELETE FROM auth_tokens WHERE user_id = ? AND org_id IS NULL", (user_id,))
        return {"membership_removed": removed, "credentials_revoked": killed,
                "last_org": not remaining}

    async def set_companion_conv(self, user_id: str, conv_id: str) -> None:
        """Pin the user's eternal companion conversation (set once, on the first message)."""
        await self._exec(
            "UPDATE users SET companion_conv_id = ? WHERE user_id = ?", (conv_id, user_id),
        )

    async def companion_conv(self, user_id: str) -> str | None:
        row = await self._one(
            "SELECT companion_conv_id FROM users WHERE user_id = ?", (user_id,),
        )
        return row["companion_conv_id"] if row else None

    async def set_companion_model(self, user_id: str, model: str | None) -> None:
        """The user's chat-model lever; None clears back to the configured default."""
        await self._exec(
            "UPDATE users SET companion_model = ? WHERE user_id = ?", (model, user_id),
        )

    async def companion_model(self, user_id: str) -> str | None:
        row = await self._one(
            "SELECT companion_model FROM users WHERE user_id = ?", (user_id,),
        )
        return row["companion_model"] if row else None

    async def write_audit(self, action: str, *, user_id: str | None = None, ip: str | None = None,
                    request_id: str | None = None, org_id: str | None = None,
                    target_type: str | None = None, target_id: str | None = None,
                    metadata: str | None = None) -> None:
        """Append one audit event. Metadata only — never content. INSERT-only: there is no
        UPDATE/DELETE path for audit_events anywhere in the codebase — immutability is structural
        (prod also GRANTs the app role INSERT-but-not-UPDATE/DELETE). Best-effort at call sites
        (wrap in audit.record for the fire-and-forget-safe emit). `org_id` is what makes a row
        org-scoped-readable at GET /v1/admin/audit; `metadata` is a pre-serialized JSON string."""
        await self._exec(
            "INSERT INTO audit_events (id, ts, action, user_id, ip, request_id, "
            "org_id, target_type, target_id, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (secrets.token_hex(12), time.time(), action, user_id, ip, request_id,
             org_id, target_type, target_id, metadata),
        )

    async def list_audit_events(self, org_id: str | None, *, is_operator: bool = False,
                          action: str | None = None, actor: str | None = None,
                          since: float | None = None, until: float | None = None,
                          limit: int = 50, offset: int = 0) -> list[dict]:
        """Org-scoped audit read (IDOR-critical): a normal admin sees ONLY their own org's rows.
        The operator (platform super-credential, org_id None) sees all — matching Identity's
        unscoped-operator semantics. Filterable by action / actor / time window, paginated. Pure
        SELECT: no mutation counterpart exists (append-only)."""
        where: list[str] = []
        params: list = []
        if not is_operator:  # normal admin: hard org filter — never another org's rows
            where.append("org_id = ?")
            params.append(org_id)
        if action:
            where.append("action = ?")
            params.append(action)
        if actor:
            where.append("user_id = ?")
            params.append(actor)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if until is not None:
            where.append("ts <= ?")
            params.append(until)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        rows = await self._all(
            "SELECT id, ts, action, user_id, org_id, target_type, target_id, metadata, "
            f"ip, request_id FROM audit_events{clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [dict(r) for r in rows]

    async def count_audit_events(self, org_id: str, *, action: str,
                                 since: float | None = None) -> int:
        """Cheap COUNT(*) over one org's rows for a single action (C3 denied-attempts counter — the
        console Governance panel reads it without paging the feed). Always org-scoped (the caller is
        a scoped admin, resolved via _scope_org). ponytail: idx_audit_org_ts covers the org+ts scan."""
        where = ["org_id = ?", "action = ?"]
        params: list = [org_id, action]
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        row = await self._one(
            f"SELECT COUNT(*) AS n FROM audit_events WHERE {' AND '.join(where)}", tuple(params))
        return int((dict(row) if row else {}).get("n", 0))

    async def count_audit_events_global(self, action: str, *, since: float | None = None) -> int:
        """Platform-wide COUNT(*) for one action (NOT org-scoped) — the egress printable page's
        7-day observed/blocked counters. Egress rows are deploy-level (org_id NULL), so this
        deliberately counts across all rows; the metadata is host/subsystem only, never tenant data."""
        where = ["action = ?"]
        params: list = [action]
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        row = await self._one(
            f"SELECT COUNT(*) AS n FROM audit_events WHERE {' AND '.join(where)}", tuple(params))
        return int((dict(row) if row else {}).get("n", 0))

    async def all_sso_issuers(self) -> list[str]:
        """Every configured org SSO issuer (for the egress allowlist's dynamic issuer admission)."""
        rows = await self._all("SELECT DISTINCT issuer FROM org_sso_configs")
        return [r["issuer"] for r in (dict(x) for x in rows) if r.get("issuer")]

    async def ping(self) -> None:
        """Readiness probe — raises if the DB connection is unusable. /readyz goes through this."""
        await self._one("SELECT 1")

    # --- tokens (verify / session, one table) ---------------------------------

    async def mint_token(self, user_id: str, purpose: str, ttl_seconds: float,
                   *, supersede: bool = False, label: str | None = None,
                   org_id: str | None = None) -> str:
        """Create an opaque token; store only its sha256 digest. Returns the raw token (shown
        once). `supersede` deletes the user's prior tokens of this purpose first (re-issue).
        `org_id` binds the credential to one org (W2-C1) — the active org for a session, the
        minted-against org for an API token; NULL leaves resolution on the oldest-membership default."""
        raw = secrets.token_urlsafe(32)
        now = time.time()
        if supersede:
            await self._exec(
                "DELETE FROM auth_tokens WHERE user_id = ? AND purpose = ?", (user_id, purpose),
            )
        await self._exec(
            "INSERT INTO auth_tokens (token_hash, user_id, purpose, expires_at, created_at, label, "
            "org_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_token_hash(raw), user_id, purpose, now + ttl_seconds, now, label, org_id),
        )
        return raw

    async def token_org(self, raw: str, purpose: str) -> str | None:
        """The org this live token/session is bound to, else None (W2-C1). One PK-indexed read on
        the auth hot path — the caller already resolved the user via lookup_token; this reads the
        sibling binding column so identity resolution can prefer it.
        ponytail: a separate read rather than folding into lookup_token keeps that method's
        single-column contract; collapse them only if this shows in p95."""
        row = await self._one(
            "SELECT org_id FROM auth_tokens WHERE token_hash = ? AND purpose = ?",
            (_token_hash(raw), purpose),
        )
        return row["org_id"] if row and row["org_id"] else None

    async def set_session_org(self, raw: str, org_id: str | None) -> None:
        """Bind (or clear) a live session's active org (W2-C1 switch endpoint + SSO login). The
        route validates membership before calling; this just writes the binding on the session row."""
        await self._exec(
            "UPDATE auth_tokens SET org_id = ? WHERE token_hash = ? AND purpose = 'session'",
            (org_id, _token_hash(raw)),
        )

    async def lookup_token(self, raw: str, purpose: str) -> str | None:
        """user_id for a live token of this purpose, else None. Read-only (no consume)."""
        row = await self._one(
            "SELECT user_id, expires_at FROM auth_tokens WHERE token_hash = ? AND purpose = ?",
            (_token_hash(raw), purpose),
        )
        if row is None or row["expires_at"] < time.time():
            return None
        return row["user_id"]

    async def consume_token(self, raw: str, purpose: str) -> str | None:
        """Single-use: return user_id and delete the row, atomically. None if absent/expired."""
        if self._pg:
            # DELETE ... RETURNING is a single atomic statement — two replicas racing the
            # same one-time token can't both win (only one DELETE affects the row).
            row = await self._one(
                "DELETE FROM auth_tokens WHERE token_hash = ? AND purpose = ? "
                "RETURNING user_id, expires_at", (_token_hash(raw), purpose),
            )
        else:
            # SQLite: the SELECT and DELETE run without an await-suspension between them (the
            # inline path never yields), so no other coroutine can slip in — still single-use.
            row = await self._one(
                "SELECT user_id, expires_at FROM auth_tokens WHERE token_hash = ? AND purpose = ?",
                (_token_hash(raw), purpose),
            )
            if row is not None:
                await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(raw),))
        if row is None:
            return None
        return row["user_id"] if row["expires_at"] >= time.time() else None

    async def revoke_token(self, raw: str) -> None:
        await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(raw),))

    # --- per-user API tokens (purpose 'api') -----------------------------------
    # token_id is the sha256 digest's first 12 hex chars — derivable, collision-safe at this
    # scale, and never invertible to the secret. No extra id column needed.

    async def _org_token_policy(self, org_id: str | None) -> tuple[int, int]:
        """(max_token_lifetime_days, token_rotation_grace_minutes) for an org, defaults on miss."""
        if not org_id:
            return 0, 60
        org = await self.get_org(org_id)
        if org is None:
            return 0, 60
        return int(org.get("max_token_lifetime_days") or 0), int(
            org.get("token_rotation_grace_minutes") if org.get("token_rotation_grace_minutes")
            is not None else 60)

    async def mint_api_token(self, user_id: str, label: str, *, org_id: str | None = None,
                             expires_in_days: float | None = None) -> tuple[str, str]:
        """(raw_token, token_id). The raw token is returned exactly once — only its sha256 digest is
        stored, same at-rest posture as sessions/verify tokens. `org_id` binds the token to one of
        the caller's orgs (W2-C1); the route validates membership before minting. `expires_in_days`
        is the requested lifetime — CLAMPED DOWN to the org's max_token_lifetime_days cap (W2-C3);
        None + no cap = no expiry (far-future stamp), None + a cap = the cap (the ceiling wins)."""
        eff_org = org_id or (await self.get_membership(user_id) or {}).get("org_id")
        cap, _grace = await self._org_token_policy(eff_org)
        days = expires_in_days
        if cap > 0:
            days = cap if days is None else min(days, cap)
        ttl = days * 86400 if days else API_TOKEN_TTL
        raw = await self.mint_token(user_id, "api", ttl, label=label, org_id=org_id)
        return raw, _token_hash(raw)[:12]

    async def list_api_tokens(self, user_id: str) -> list[dict]:
        """This user's API tokens — metadata only, never a hash or secret. `org_id` is the token's
        org binding (W2-C1), NULL when unbound; `org_name` is joined for display. expires_at is the
        hygiene ceiling (a far-future stamp reads as no-expiry to the UI)."""
        rows = await self._all(
            "SELECT substr(t.token_hash, 1, 12) AS token_id, t.label, t.created_at, t.last_used, "
            "t.expires_at, t.rotated_at, t.org_id, o.name AS org_name FROM auth_tokens t "
            "LEFT JOIN organizations o ON o.org_id = t.org_id "
            "WHERE t.user_id = ? AND t.purpose = 'api' ORDER BY t.created_at",
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def revoke_api_token(self, user_id: str, token_id: str) -> bool:
        """Delete one of THIS user's API tokens by id. False if it isn't theirs / doesn't
        exist — the route turns that into 404 (fail-closed, no cross-user revocation)."""
        row = await self._one(
            "SELECT token_hash FROM auth_tokens "
            "WHERE user_id = ? AND purpose = 'api' AND substr(token_hash, 1, 12) = ?",
            (user_id, token_id),
        )
        if row is None:
            return False
        await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (row["token_hash"],))
        return True

    async def rotate_api_token(self, user_id: str, token_id: str,
                               *, grace_minutes: int | None = None) -> tuple[str, str, float | None] | None:
        """Rotate one of THIS user's API tokens: mint a NEW secret (returned once) carrying the same
        label + org binding, and let the OLD secret keep working for a grace window then die. Returns
        (new_raw, new_token_id, old_expires_at) or None when the id isn't theirs (route → 404).

        ponytail: the grace is expressed as the OLD row's expires_at (now + grace) — the existing
        auth-path expiry check (lookup/resolve_bearer) then kills it for free, so the hot path stays
        a single PK-indexed read (no OR-lookup, no stored prev-hash). grace<=0 deletes the old row
        now. Two rows exist only during the grace window; the new token_id differs (new secret)."""
        row = await self._one(
            "SELECT token_hash, label, org_id FROM auth_tokens "
            "WHERE user_id = ? AND purpose = 'api' AND substr(token_hash, 1, 12) = ?",
            (user_id, token_id),
        )
        if row is None:
            return None
        if grace_minutes is None:
            _cap, grace_minutes = await self._org_token_policy(
                row["org_id"] or (await self.get_membership(user_id) or {}).get("org_id"))
        new_raw, new_id = await self.mint_api_token(user_id, row["label"], org_id=row["org_id"])
        now = time.time()
        await self._exec("UPDATE auth_tokens SET rotated_at = ? WHERE token_hash = ?",
                         (now, _token_hash(new_raw)))
        if grace_minutes <= 0:
            await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (row["token_hash"],))
            return new_raw, new_id, None
        old_expires = now + grace_minutes * 60
        await self._exec("UPDATE auth_tokens SET expires_at = ? WHERE token_hash = ?",
                         (old_expires, row["token_hash"]))
        return new_raw, new_id, old_expires

    async def resolve_bearer(self, raw: str) -> dict | None:
        """The auth HOT PATH for API + service bearers: ONE PK-indexed read. Returns
        {user_id, purpose, org_id, expired} for a live api/service token, {..., expired: True} for an
        expired one (so the route can 401 `token_expired` distinctly), or None when the bearer is
        neither (→ fall through to the session cookie / anon). Touches last_used AT MOST once per
        _LAST_USED_THROTTLE window (compared against the value just read) — no write per request."""
        h = _token_hash(raw)
        row = await self._one(
            "SELECT user_id, purpose, org_id, expires_at, last_used FROM auth_tokens "
            "WHERE token_hash = ? AND purpose IN ('api', 'service')",
            (h,),
        )
        if row is None:
            return None
        now = time.time()
        base = {"user_id": row["user_id"], "purpose": row["purpose"], "org_id": row["org_id"]}
        if row["expires_at"] < now:
            return {**base, "expired": True}
        if row["last_used"] is None or now - row["last_used"] >= _LAST_USED_THROTTLE:
            await self._exec("UPDATE auth_tokens SET last_used = ? WHERE token_hash = ?", (now, h))
        return {**base, "expired": False}

    # --- service-account tokens (purpose 'service', ORG-owned) -----------------
    # Not tied to a person (W2-C3): user_id carries the OWNING ORG id (never a real user_id, which is
    # a 16-hex token — no collision), so a SCIM deprovision of any USER (DELETE ... WHERE user_id=<uid>)
    # can never touch them. Minted/listed/revoked at the org level; identity resolves org-scoped with
    # role 'member' + actor 'service'. REQUIRED to be org-bound (org_id == the owning org).

    async def mint_service_token(self, org_id: str, label: str,
                                 *, expires_in_days: float | None = None) -> tuple[str, str]:
        """(raw, token_id). Org-owned CI credential. Lifetime clamped to the org cap like api tokens."""
        cap, _grace = await self._org_token_policy(org_id)
        days = expires_in_days
        if cap > 0:
            days = cap if days is None else min(days, cap)
        ttl = days * 86400 if days else API_TOKEN_TTL
        raw = await self.mint_token(org_id, "service", ttl, label=label, org_id=org_id)
        return raw, _token_hash(raw)[:12]

    async def list_service_tokens(self, org_id: str) -> list[dict]:
        """The org's service tokens — metadata only. Keyed on the owning org (user_id == org_id)."""
        rows = await self._all(
            "SELECT substr(token_hash, 1, 12) AS token_id, label, created_at, last_used, "
            "expires_at, rotated_at FROM auth_tokens "
            "WHERE org_id = ? AND purpose = 'service' ORDER BY created_at",
            (org_id,),
        )
        return [dict(r) for r in rows]

    async def revoke_service_token(self, org_id: str, token_id: str) -> bool:
        """Delete one of the org's service tokens by id. False if not this org's (route → 404)."""
        row = await self._one(
            "SELECT token_hash FROM auth_tokens "
            "WHERE org_id = ? AND purpose = 'service' AND substr(token_hash, 1, 12) = ?",
            (org_id, token_id),
        )
        if row is None:
            return False
        await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (row["token_hash"],))
        return True

    # --- org-level bulk revoke + compliance list (W2-C3 admin) -----------------

    async def revoke_org_credentials(self, org_id: str, *, user_id: str | None = None,
                                     include_sessions: bool = False,
                                     include_service: bool = False) -> dict:
        """Bulk-revoke, org-scoped. `user_id` set → just that member's org-bound credentials (admin
        act); else org-wide (owner act). purposes = api always, + session when include_sessions, +
        service when include_service (org-wide only; a per-user revoke never owns service tokens).
        Returns counts. ponytail: one DELETE per purpose class, no per-token loop; org-BOUND only
        (org_id column == this org) — an unbound personal token that merely resolves here is untouched."""
        purposes = ["api"]
        if include_sessions:
            purposes.append("session")
        if include_service and user_id is None:
            purposes.append("service")
        placeholders = ", ".join("?" for _ in purposes)
        counts: dict[str, int] = {}
        for p in purposes:
            if user_id is not None:
                n = await self._exec_count(
                    "DELETE FROM auth_tokens WHERE org_id = ? AND user_id = ? AND purpose = ?",
                    (org_id, user_id, p))
            else:
                n = await self._exec_count(
                    "DELETE FROM auth_tokens WHERE org_id = ? AND purpose = ?", (org_id, p))
            counts[p] = n
        counts["total"] = sum(counts.values())
        return counts

    async def list_org_credentials(self, org_id: str) -> list[dict]:
        """Every live api + service credential attributable to this org (the compliance screen): the
        org's service tokens (org_id == org) AND every member's api tokens (org-bound OR personal).
        Owner email joined for api tokens; service tokens carry a label, no owner. Sessions excluded
        (ephemeral cookie creds). Admin/auditor-readable at GET /v1/admin/tokens."""
        rows = await self._all(
            "SELECT substr(t.token_hash, 1, 12) AS token_id, t.purpose, t.user_id, t.org_id, "
            "t.label, t.created_at, t.expires_at, t.last_used, t.rotated_at, u.email AS owner_email "
            "FROM auth_tokens t LEFT JOIN users u ON u.user_id = t.user_id "
            "WHERE t.purpose IN ('api', 'service') AND ("
            "  t.org_id = ? OR t.user_id IN (SELECT user_id FROM memberships WHERE org_id = ?)) "
            "ORDER BY t.created_at DESC",
            (org_id, org_id),
        )
        return [dict(r) for r in rows]

    async def set_token_policy(self, org_id: str, *, max_token_lifetime_days: int,
                               token_rotation_grace_minutes: int) -> None:
        """Write the org's token-hygiene policy (W2-C3). Clamps to sane bounds; the mint/rotate paths
        read it via _org_token_policy. Owner/admin-gated at the route."""
        cap = max(0, int(max_token_lifetime_days))
        grace = max(0, int(token_rotation_grace_minutes))
        await self._exec(
            "UPDATE organizations SET max_token_lifetime_days = ?, "
            "token_rotation_grace_minutes = ? WHERE org_id = ?", (cap, grace, org_id))

    # --- sessions (a session is a token with purpose 'session') ---------------

    async def create_session(self, user_id: str, ttl_seconds: float,
                             *, org_id: str | None = None) -> str:
        """A session is a token with purpose 'session'. `org_id` seeds the active-org binding —
        SSO login passes the provisioned org so the user lands there (W2-C1 belt-and-braces)."""
        return await self.mint_token(user_id, "session", ttl_seconds, org_id=org_id)

    async def session_user(self, raw: str, *, require_verified: bool = True) -> dict | None:
        """The verified user behind a live session cookie, else None. `require_verified`
        must match the same `settings.require_email_verify` flag `login()` already gates new
        sessions on — callers own threading it through, this method has no settings access.
        Defaults True (the historical, production behavior) so any caller that doesn't pass
        it explicitly keeps the old, safe assumption. 2026-07-04 bug: this was unconditional,
        so on an environment with require_email_verify off (staging/local — open registration,
        no verification step) login() would issue a real session for an unverified user, and
        every subsequent request to session_user() then rejected that exact session as invalid
        — a dead-end "missing or invalid credentials" for a user who had just signed in
        successfully seconds earlier."""
        user_id = await self.lookup_token(raw, "session")
        if user_id is None:
            return None
        user = await self.get_user(user_id)
        if user is None or (require_verified and not user["email_verified"]):
            return None
        return user

    async def revoke_session(self, raw: str) -> None:
        await self.revoke_token(raw)
