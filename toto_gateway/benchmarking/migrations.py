"""Forward-only benchmark-platform schema migrations.

The legacy ``benchmark_scores`` and ``model_aliases`` tables are intentionally absent here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS benchmark_platform_migrations (
  version    INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at REAL NOT NULL
);
"""


V1_SQL = """
CREATE TABLE IF NOT EXISTS model_identities (
  identity_id       TEXT PRIMARY KEY,
  vendor            TEXT NOT NULL,
  family            TEXT NOT NULL,
  release           TEXT,
  reasoning_variant TEXT,
  quantization      TEXT,
  fine_tune         TEXT,
  context_variant   TEXT,
  display_name      TEXT NOT NULL,
  provisional       INTEGER NOT NULL DEFAULT 0 CHECK (provisional IN (0, 1)),
  created_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS model_identity_aliases (
  alias_id        TEXT PRIMARY KEY,
  provider        TEXT NOT NULL,
  source_id       TEXT NOT NULL,
  identity_id     TEXT NOT NULL,
  method          TEXT NOT NULL,
  confidence      REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  evidence_json   TEXT NOT NULL DEFAULT '{}',
  reviewer        TEXT,
  reviewer_org_id TEXT,
  decided_at      REAL NOT NULL,
  superseded_at   REAL,
  CHECK (superseded_at IS NULL OR superseded_at >= decided_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_identity_aliases_active
  ON model_identity_aliases (provider, source_id) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_model_identity_aliases_identity
  ON model_identity_aliases (identity_id);

CREATE TABLE IF NOT EXISTS inventory_snapshots (
  snapshot_id          TEXT PRIMARY KEY,
  provider             TEXT NOT NULL CHECK (provider IN ('openrouter', 'fireworks')),
  scope_kind           TEXT NOT NULL CHECK (scope_kind IN ('platform', 'organization', 'user')),
  scope_id             TEXT NOT NULL,
  status               TEXT NOT NULL CHECK (status IN ('success', 'partial', 'failed')),
  started_at           REAL NOT NULL,
  completed_at         REAL NOT NULL,
  expires_at           REAL NOT NULL,
  pagination_complete  INTEGER NOT NULL CHECK (pagination_complete IN (0, 1)),
  adapter_revision     TEXT NOT NULL,
  source_metadata_json TEXT NOT NULL DEFAULT '{}',
  error_summary        TEXT,
  CHECK (completed_at >= started_at),
  CHECK (expires_at >= completed_at),
  CHECK (status <> 'success' OR pagination_complete = 1)
);
CREATE INDEX IF NOT EXISTS idx_inventory_snapshots_scope_latest
  ON inventory_snapshots (scope_kind, scope_id, provider, completed_at);

CREATE TABLE IF NOT EXISTS provider_offers (
  snapshot_offer_id TEXT PRIMARY KEY,
  snapshot_id       TEXT NOT NULL,
  offer_id          TEXT NOT NULL,
  identity_id       TEXT NOT NULL,
  route_id          TEXT NOT NULL,
  provider          TEXT NOT NULL CHECK (provider IN ('openrouter', 'fireworks')),
  upstream_model_id TEXT NOT NULL,
  base_url          TEXT NOT NULL,
  scope_kind        TEXT NOT NULL CHECK (scope_kind IN ('platform', 'organization', 'user')),
  scope_id          TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  pricing_json      TEXT NOT NULL,
  adapter_revision  TEXT NOT NULL,
  raw_metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE (snapshot_id, offer_id),
  UNIQUE (snapshot_id, route_id)
);
CREATE INDEX IF NOT EXISTS idx_provider_offers_snapshot
  ON provider_offers (snapshot_id, route_id);
CREATE INDEX IF NOT EXISTS idx_provider_offers_identity
  ON provider_offers (identity_id);
"""


V2_SQL = """
CREATE TABLE IF NOT EXISTS inventory_refresh_operations (
  operation_id    TEXT PRIMARY KEY,
  actor_id        TEXT NOT NULL,
  scope           TEXT NOT NULL CHECK (scope IN ('effective', 'platform', 'user')),
  target_user_id  TEXT NOT NULL DEFAULT '',
  idempotency_key TEXT NOT NULL,
  fingerprint     TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (
    status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')
  ),
  result_json     TEXT NOT NULL DEFAULT '{}',
  error           TEXT,
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  expires_at      REAL NOT NULL,
  UNIQUE (actor_id, scope, target_user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_inventory_refresh_operations_expiry
  ON inventory_refresh_operations (status, expires_at);
"""

V3_SQL = """
ALTER TABLE inventory_refresh_operations
  ADD COLUMN plan_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE inventory_refresh_operations
  ADD COLUMN lease_owner TEXT;
ALTER TABLE inventory_refresh_operations
  ADD COLUMN lease_expires_at REAL;
CREATE INDEX IF NOT EXISTS idx_inventory_refresh_operations_recovery
  ON inventory_refresh_operations (status, lease_expires_at, created_at);
"""


# Widen the scope CHECK to admit 'organization' (org-wide key saves warm the org partition). SQLite
# can't ALTER a CHECK, so rebuild the table — the statements are dialect-neutral (REAL→DOUBLE
# PRECISION is applied at load for Postgres) and carry every V2+V3 column across.
V4_SQL = """
CREATE TABLE inventory_refresh_operations_v4 (
  operation_id     TEXT PRIMARY KEY,
  actor_id         TEXT NOT NULL,
  scope            TEXT NOT NULL CHECK (scope IN ('effective', 'platform', 'user', 'organization')),
  target_user_id   TEXT NOT NULL DEFAULT '',
  idempotency_key  TEXT NOT NULL,
  fingerprint      TEXT NOT NULL,
  status           TEXT NOT NULL CHECK (
    status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')
  ),
  result_json      TEXT NOT NULL DEFAULT '{}',
  error            TEXT,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL,
  expires_at       REAL NOT NULL,
  plan_json        TEXT NOT NULL DEFAULT '{}',
  lease_owner      TEXT,
  lease_expires_at REAL,
  UNIQUE (actor_id, scope, target_user_id, idempotency_key)
);
INSERT INTO inventory_refresh_operations_v4
  SELECT operation_id, actor_id, scope, target_user_id, idempotency_key, fingerprint, status,
         result_json, error, created_at, updated_at, expires_at, plan_json, lease_owner,
         lease_expires_at
  FROM inventory_refresh_operations;
DROP TABLE inventory_refresh_operations;
ALTER TABLE inventory_refresh_operations_v4 RENAME TO inventory_refresh_operations;
CREATE INDEX IF NOT EXISTS idx_inventory_refresh_operations_expiry
  ON inventory_refresh_operations (status, expires_at);
CREATE INDEX IF NOT EXISTS idx_inventory_refresh_operations_recovery
  ON inventory_refresh_operations (status, lease_expires_at, created_at);
"""


MIGRATIONS = (
    Migration(1, "provider_inventory", V1_SQL),
    Migration(2, "inventory_refresh_operations", V2_SQL),
    Migration(3, "inventory_refresh_operation_leases", V3_SQL),
    Migration(4, "inventory_refresh_scope_organization", V4_SQL),
)
