"""Auth-plane DDL: the base schema plus the guarded-ALTER migrations.

Dual-dialect (SQLite + Postgres): TEXT/REAL/INTEGER only, no NULL-in-PK, JSON stored as TEXT and
parsed app-side, UPSERT via ON CONFLICT. Everything is additive and forward-only — fresh DBs get
columns from _SCHEMA, existing DBs gain them via guarded ALTERs in apply_migrations.
"""

from __future__ import annotations

import sqlite3

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
-- Per-user BYOK provider keys. One row per (user, provider). STRICTLY user-scoped -- no NULL
-- grandfathering, two users share nothing (anonymous has no keys). encrypted_key is Fernet
-- ciphertext (see credentials.py). last4 is a plaintext UI hint only.
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
-- Append-only audit trail (SOC2). Metadata only, NEVER content. INSERT-only posture: the
-- prod app DB role gets INSERT but not UPDATE/DELETE (GRANT hardening).
CREATE TABLE IF NOT EXISTS audit_events (
  id         TEXT PRIMARY KEY,       -- random hex (avoids the AUTOINCREMENT vs SERIAL dialect split)
  ts         REAL NOT NULL,
  action     TEXT NOT NULL,          -- register | login | login_failed | logout | verify | revoke | admin:*
  user_id    TEXT,
  ip         TEXT,                   -- first X-Forwarded-For hop
  request_id TEXT
);
-- Control-plane tenancy. org then team then member, the membership row carrying the role. Roles
-- are owner, admin, member, auditor. A pre-tenancy user is lazily provisioned a personal org
-- (owner) on first resolve (see resolve_membership) -- that IS the backfill, no boot scan.
CREATE TABLE IF NOT EXISTS organizations (
  org_id         TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  created_at     REAL NOT NULL,
  status         TEXT NOT NULL DEFAULT 'active',  -- active | suspended
  -- Zero-retention mode: 1 => this org's requests leave ZERO payload bytes in any durable
  -- TELEMETRY store (request_content, response cache, experience corpus, driver spans, LangSmith
  -- mirror), overriding TOTO_GW_LOG_CONTENT and every cache/eval flag DOWNWARD (opt-out always wins).
  -- Traces still record full metadata. Absence -> 0 -> env flags apply.
  zero_retention INTEGER NOT NULL DEFAULT 0,
  -- Content-plane retention policy: per-sink retention DAYS as a JSON object, e.g.
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
-- PK is (org_id, user_id): one org-level membership per user (team_id is a nullable column, NOT
-- part of the PK — Postgres forbids NULL in a PK column, so team-scoped rows would come later as
-- their own additive shape rather than a NULL-bearing composite key).
CREATE TABLE IF NOT EXISTS memberships (
  org_id     TEXT NOT NULL,
  user_id    TEXT NOT NULL,
  team_id    TEXT,                            -- NULL = org-level membership
  role       TEXT NOT NULL,                   -- owner | admin | member | auditor
  created_at REAL NOT NULL,
  PRIMARY KEY (org_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships (user_id);
-- Catalog-scoped RBAC: per-team allow/deny overlay over catalog ids + residency classes. Keyed
-- by team_id, org_id carried so the admin API enforces org isolation (no cross-org read/write).
-- A per-TEAM row: mode='allow' => default-deny (only listed models pass) — 'deny' =>
-- default-allow (listed models blocked). The ORG-DEFAULT row (team_id == org_id sentinel, same
-- pattern as routing_policies) carries the org GOVERNANCE mode instead: 'allow_all' (permissive)
-- or 'allowlist' (deny-by-default — only the org's approved set = its models list + org catalog
-- adoptions resolves). Absence of a row = permissive (unchanged routing). Comments here stay
-- free of semicolons on purpose, since executescript splits the DDL on that character.
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
-- Invitations. A pending invite is a row with accepted_ts NULL plus a random token, and accepting
-- it stamps accepted_ts and adds the membership. No unique on email — re-inviting issues a fresh
-- token, and the newest one wins because accept adds an idempotent membership (no supersede
-- needed). Comments here stay free of semicolons on purpose, since executescript splits the DDL
-- on that character.
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
-- Per-team routing overlay. The GLOBAL tag->model map lives in routing/labels.yaml -- this row
-- overlays it PER TEAM: bindings is a JSON object carrying ONLY the labels the team changed
-- (absence of a key = the global YAML default stands), optimize is the team preset applied on
-- the fallback path. custom_labels is a JSON list of the team's INVENTED task types
-- [{name, desc, model}] -- labels NOT in labels.yaml that the classifier may emit for this team
-- and that route to their bound model. Keyed by team_id, org_id carried so the admin API enforces
-- org isolation. Absence of a row = pure global behavior (effective_policy carries no overlay ->
-- byte-identical routing). Comments here stay free of semicolons on purpose, since executescript
-- splits the DDL on that character.
CREATE TABLE IF NOT EXISTS routing_policies (
  team_id       TEXT PRIMARY KEY,
  org_id        TEXT NOT NULL,
  bindings      TEXT NOT NULL DEFAULT '{}',   -- JSON object label->catalog id (overridden labels only)
  optimize      TEXT,                          -- quality | balanced | cost, NULL = global preset
  custom_labels TEXT NOT NULL DEFAULT '[]',   -- JSON list of {name, desc, model} team-invented task types
  prewarm       INTEGER NOT NULL DEFAULT 0,   -- per-org cache pre-warm toggle (0=off default; latency tool)
  stick_ttls    TEXT NOT NULL DEFAULT '{}',   -- JSON object label->seconds: per-task-type memo hold
  cache         TEXT NOT NULL DEFAULT '{}',   -- JSON: per-org cache-behavior overrides {preset, auto_inject, auto_inject_min_messages, warmth_routing}; absent key = inherit global env
  fail_policy   TEXT NOT NULL DEFAULT 'open', -- open (degrade to floor, default) | closed (503 on smart-routing degradation) | a JSON per-reason matrix
  taxonomy      TEXT NOT NULL DEFAULT '{}',   -- JSON: data-classification labels bound to residency constraints {labels:{<l>:{constraint,desc}}, default}
  classifier_model TEXT,                        -- org's chosen in-perimeter classifier id (NULL = gateway default)
  optimizer_steers_tools INTEGER NOT NULL DEFAULT 0, -- 0=bindings govern tool traffic (advisor); 1=pre-precedence benchmark override
  version       INTEGER NOT NULL DEFAULT 1,
  updated_by    TEXT,
  updated_at    REAL NOT NULL
);
-- Server-side catalog adoptions: one-click add of a provider-library model into a caller's
-- EFFECTIVE catalog. Keyed by scope_key = team_id or org_id (mirrors the routing-policy fallback
-- so a personal-org owner's OWN adoptions apply to their traffic). entry_json is the full
-- materialized CatalogEntry -- its facts (price, context, capabilities, upstream pin) are derived
-- SERVER-SIDE from the provider discovery snapshot, NEVER client-sent -- parsed back at request
-- time into the effective catalog (base wins on id collision). upstream_model/provider are
-- denormalized for cheap listing + validation. Comments here stay free of semicolons on purpose,
-- since executescript splits the DDL on that character.
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
-- Manual price overrides, for entries whose provider publishes no machine-readable pricing or
-- whose YAML price rotted. Prices are stored PER 1K tokens (converted from the console's per-Mtok
-- input at the API boundary, exact divide by 1000). scope_key is team_id or org_id or the literal
-- platform sentinel -- precedence at resolution is team over org over platform.
-- cache_read_multiplier NULL means keep the entry's own multiplier. Applied at the
-- effective_catalog seam so compute_cost_usd and routing inherit the override with zero changes.
-- Comments stay free of semicolons (executescript splits on them).
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
-- Monthly USD budgets. Keyed by team_id, org_id carried for the admin API's org-isolation check
-- -- SAME shape as routing_policies. The ORG-DEFAULT row (team_id == org_id sentinel) is the
-- fallback a teamless caller resolves. action is what happens at 100 percent: observe (serve+stamp)
-- | downgrade (cheapest eligible model) | reject (402). thresholds is a JSON list of alert fractions
-- (default 0.5/0.8/1.0). Absence of a row = no budget. Comments free of semicolons (executescript
-- splits on it).
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
-- Threshold-alert dedupe: one row the first time a scope crosses a threshold in a month, so the
-- budget:threshold audit fires ONCE per (scope, month, threshold). scope_key = team_id or org_id
-- (mirrors the budget fallback). Append-only like audit_events. PK gives the dedupe for free.
CREATE TABLE IF NOT EXISTS budget_alerts (
  scope_key TEXT NOT NULL,
  period    TEXT NOT NULL,
  threshold REAL NOT NULL,
  fired_at  REAL NOT NULL,
  PRIMARY KEY (scope_key, period, threshold)
);
-- OIDC SSO relying-party config. One row per org: the IdP issuer, client id, the Fernet-encrypted
-- client secret (same at-rest posture as provider_keys -- NEVER plaintext, NEVER echoed), and the
-- sso_required switch. Keyed by org_id. Comments stay free of semicolons on purpose, since
-- executescript splits DDL on that char.
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
-- SCIM 2.0 provisioning config. One row per org: the sha256 digest of the per-org SCIM bearer
-- (same at-rest posture as auth_tokens -- only the hash, never the secret), the IdP group->role
-- map as a JSON blob, and the enabled switch. Sibling to org_sso_configs rather than extra
-- columns on it, so an org can hold a SCIM token without a full OIDC relying-party config
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

-- Audit-export config. One row per org: the destination + cadence + retention for the scheduled
-- hash-chained JSONL export, and (when a customer S3 bucket is the sink) that bucket's
-- Fernet-encrypted secret key -- same at-rest posture as org_sso_configs (NEVER plaintext, NEVER
-- echoed; GET reports has_s3_secret only). last_run/last_error surface the scheduler's health in
-- the config GET. No semicolons in comments.
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
-- Audit-export batch ledger. One row per (org, stream, batch): the object key of the
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


def apply_migrations(db, pg: bool) -> None:
    """Guarded ALTERs over a DB that predates a column (fresh DBs already have every column via
    _SCHEMA, except the ALTER-only user columns noted below). SQLite raises OperationalError on a
    duplicate column (caught and ignored); Postgres uses IF NOT EXISTS. Caller commits."""
    ine = "IF NOT EXISTS " if pg else ""
    # ALTER-only user column (never in _SCHEMA): the user's eternal companion conversation —
    # its turn-1 run_id.
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {ine}companion_conv_id TEXT")
    except sqlite3.OperationalError:
        pass  # sqlite: column already exists (PG uses IF NOT EXISTS → no error)
    # Per-user chat-model lever: which model answers this user's companion chat — a catalog id
    # or the 'smart' sentinel. NULL = the configured default.
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {ine}companion_model TEXT")
    except sqlite3.OperationalError:
        pass
    # Per-user API tokens (purpose 'api') ride the auth_tokens table: label + last_used are
    # display metadata for GET /v1/tokens. rotated_at stamps the NEW credential produced by a
    # rotation, surfaced in the org compliance list.
    for col, typ in (("label", "TEXT"), ("last_used", "REAL"), ("rotated_at", "REAL")):
        try:
            db.execute(f"ALTER TABLE auth_tokens ADD COLUMN {ine}{col} {typ}")
        except sqlite3.OperationalError:
            pass
    # Per-credential org binding (multi-org): which org THIS token/session resolves the caller
    # into. A session (purpose 'session') carries the active org the user switched to; an API
    # token (purpose 'api') carries the org it was minted against. NULL -> unbound -> identity
    # falls back to the oldest membership. The binding is read at resolution time
    # (deps._resolve_identity -> resolve_membership(preferred_org_id=...)).
    try:
        db.execute(f"ALTER TABLE auth_tokens ADD COLUMN {ine}org_id TEXT")
    except sqlite3.OperationalError:
        pass
    # Widen the audit floor with org + target + metadata so admin/policy events are
    # org-scoped-readable at GET /v1/admin/audit. INSERT-only, no backfill (old rows keep NULL
    # org_id → operator-only, correct).
    for col in ("org_id", "target_type", "target_id", "metadata"):
        try:
            db.execute(f"ALTER TABLE audit_events ADD COLUMN {ine}{col} TEXT")
        except sqlite3.OperationalError:
            pass
    # OIDC SSO: generalize the google_sub seam to any IdP. google_sub stays (Google login still
    # writes it); oidc_sub + oidc_issuer are the general pair, matched as a composite so two IdPs
    # can't collide on a shared sub string. ALTER-only additive, like companion_conv_id.
    for col in ("oidc_sub", "oidc_issuer"):
        try:
            db.execute(f"ALTER TABLE users ADD COLUMN {ine}{col} TEXT")
        except sqlite3.OperationalError:
            pass
    # (oidc_issuer, oidc_sub) is the SSO identity lookup + a uniqueness guard against double
    # provisioning. NULLs are distinct in a UNIQUE index on both SQLite and Postgres, so the many
    # password-only users (both columns NULL) don't collide.
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oidc "
               "ON users (oidc_issuer, oidc_sub)")
    # Custom task types: additive JSON column on the routing overlay. Absence of the column value
    # defaults to '[]' -> no custom labels -> byte-identical routing.
    try:
        db.execute(
            f"ALTER TABLE routing_policies ADD COLUMN {ine}custom_labels TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError:
        pass
    # Pre-warm toggle: additive per-org boolean on the routing overlay. Absence -> 0 -> OFF.
    try:
        db.execute(
            f"ALTER TABLE routing_policies ADD COLUMN {ine}prewarm INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Per-task-type stickiness holds: additive JSON column. Absence -> '{}' -> flat holds.
    try:
        db.execute(
            f"ALTER TABLE routing_policies ADD COLUMN {ine}stick_ttls TEXT NOT NULL DEFAULT '{{}}'")
    except sqlite3.OperationalError:
        pass
    # Per-org cache-behavior overrides: additive JSON column. Absence -> '{}' -> inherit every
    # cache knob from the global env defaults.
    try:
        db.execute(
            f"ALTER TABLE routing_policies ADD COLUMN {ine}cache TEXT NOT NULL DEFAULT '{{}}'")
    except sqlite3.OperationalError:
        pass
    # Fail policy: additive per-org switch. Absence -> 'open' -> degrade-to-floor fall-through.
    try:
        db.execute(
            f"ALTER TABLE routing_policies ADD COLUMN {ine}fail_policy TEXT NOT NULL DEFAULT 'open'")
    except sqlite3.OperationalError:
        pass
    # Data-classification taxonomy: additive JSON column. Absence -> '{}' -> no taxonomy ->
    # no data-policy constraint (byte-identical routing).
    try:
        db.execute(
            f"ALTER TABLE routing_policies ADD COLUMN {ine}taxonomy TEXT NOT NULL DEFAULT '{{}}'")
    except sqlite3.OperationalError:
        pass
    # Pluggable in-perimeter classifier: additive per-org classifier id. Absence -> NULL -> the
    # gateway default classifier (byte-identical routing).
    try:
        db.execute(
            f"ALTER TABLE routing_policies ADD COLUMN {ine}classifier_model TEXT")
    except sqlite3.OperationalError:
        pass
    # Binding-precedence escape hatch: additive per-org boolean. Absence -> 0 -> bindings govern
    # tool traffic (the optimizer is an advisor); 1 restores the pre-precedence benchmark override.
    try:
        db.execute(
            f"ALTER TABLE routing_policies ADD COLUMN {ine}optimizer_steers_tools INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Zero-retention mode: additive per-org privacy switch. Absence -> 0 -> env flags apply
    # (TOTO_GW_LOG_CONTENT / cache).
    try:
        db.execute(
            f"ALTER TABLE organizations ADD COLUMN {ine}zero_retention INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # Token-hygiene org policy: max_token_lifetime_days is the MINT ceiling (0/absent = no cap; a
    # mint clamps its requested lifetime DOWN to it, existing tokens are NOT retro-expired).
    # token_rotation_grace_minutes is how long a rotated-out secret keeps working (default 60;
    # 0 = immediate). Both are org account settings (sibling to zero_retention); absence -> the
    # documented defaults. Fresh DBs default from here too.
    for col, ddl in (("max_token_lifetime_days", "INTEGER NOT NULL DEFAULT 0"),
                     ("token_rotation_grace_minutes", "INTEGER NOT NULL DEFAULT 60")):
        try:
            db.execute(
                f"ALTER TABLE organizations ADD COLUMN {ine}{col} {ddl}")
        except sqlite3.OperationalError:
            pass
    # Content-plane retention policy: additive per-org JSON column, sibling to zero_retention.
    # Empty -> keep-forever.
    try:
        db.execute(
            f"ALTER TABLE organizations ADD COLUMN {ine}retention_policy TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # The org-scoped read is the hot query; one composite index covers filter+order.
    db.execute("CREATE INDEX IF NOT EXISTS idx_audit_org_ts "
               "ON audit_events (org_id, ts)")
