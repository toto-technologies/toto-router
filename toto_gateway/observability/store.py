"""Org observability persistence: encrypted provider admin keys + a provider-poll cache.

Two tables on the SAME operational DB as the auth/run store, reached through the house
`AsyncStoreMixin` over its own connection (exactly like `AuthStore` — WAL handles the extra
SQLite connection, and the async pool is per-store on Postgres):

  org_admin_keys         — one Fernet-encrypted provider admin key per (org, provider). Key material
                           is ciphertext only (see credentials.py); last4 + org_name are plaintext
                           UI hints. The route encrypts before calling here and never echoes the key.
  org_provider_snapshots — a poll cache keyed (org, provider, kind, params_hash). Providers ask for
                           <=1 req/min sustained, so a dashboard reload must not re-hit them: a
                           snapshot younger than TTL (900s) is served instead of a live fetch.

ponytail: own connection to the same DB (the AuthStore pattern), not a shared handle threaded
through app.py — keeps app.py wiring to the 2-line plane mount the contract asks for.
"""

from __future__ import annotations

import json
import threading
import time

from .. import db as _db_mod

SNAPSHOT_TTL = 900.0  # seconds — a reload inside this window is served from cache, not the provider

_SCHEMA = """
CREATE TABLE IF NOT EXISTS org_admin_keys (
  org_id         TEXT NOT NULL,
  provider       TEXT NOT NULL,
  key_ciphertext TEXT NOT NULL,
  last4          TEXT NOT NULL DEFAULT '',
  org_name       TEXT,
  created_at     REAL NOT NULL,
  PRIMARY KEY (org_id, provider)
);
CREATE TABLE IF NOT EXISTS org_provider_snapshots (
  org_id      TEXT NOT NULL,
  provider    TEXT NOT NULL,
  kind        TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  fetched_at  REAL NOT NULL,
  payload     TEXT NOT NULL,
  PRIMARY KEY (org_id, provider, kind, params_hash)
);
"""


class ObservabilityStore(_db_mod.AsyncStoreMixin):
    def __init__(self, path: str = ":memory:", database_url: str = "",
                 pool: dict | None = None) -> None:
        from .. import db as _db

        self._db, self._pg = _db.connect(database_url, path)          # sync conn: init DDL
        self._pool = _db.make_async_pool(database_url, **(pool or {}))  # async pool: runtime queries
        self._lock = threading.Lock()  # guards the shared sqlite conn (SQLite mode)
        self._db.executescript(_SCHEMA)

    # --- admin keys (Fernet ciphertext in, never out) --------------------------

    async def set_key(self, org_id: str, provider: str, key_ciphertext: str,
                      last4: str, org_name: str | None) -> None:
        """Upsert an org's encrypted admin key for a provider (re-PUT replaces it)."""
        await self._exec(
            "INSERT INTO org_admin_keys (org_id, provider, key_ciphertext, last4, org_name, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(org_id, provider) DO UPDATE SET "
            "key_ciphertext = excluded.key_ciphertext, last4 = excluded.last4, "
            "org_name = excluded.org_name, created_at = excluded.created_at",
            (org_id, provider, key_ciphertext, last4, org_name, time.time()),
        )

    async def get_key(self, org_id: str, provider: str) -> dict | None:
        """The stored row INCLUDING ciphertext — for the fetch path, which decrypts to call the
        provider. Never serialized to an HTTP response."""
        row = await self._one(
            "SELECT provider, key_ciphertext, last4, org_name FROM org_admin_keys "
            "WHERE org_id = ? AND provider = ?",
            (org_id, provider),
        )
        return dict(row) if row else None

    async def list_keys(self, org_id: str) -> list[dict]:
        """Configured providers for an org: provider + last4 + org_name, NO key material."""
        rows = await self._all(
            "SELECT provider, last4, org_name FROM org_admin_keys WHERE org_id = ? "
            "ORDER BY provider",
            (org_id,),
        )
        return [dict(r) for r in rows]

    async def delete_key(self, org_id: str, provider: str) -> None:
        await self._exec(
            "DELETE FROM org_admin_keys WHERE org_id = ? AND provider = ?",
            (org_id, provider),
        )

    # --- provider-poll cache ---------------------------------------------------

    async def get_snapshot(self, org_id: str, provider: str, kind: str, params_hash: str,
                           ttl: float = SNAPSHOT_TTL) -> dict | None:
        """A cached payload younger than `ttl`, as {"payload": ..., "age_seconds": float}, else None
        (missing or stale). Stale rows are left for the next put to overwrite (idempotent PK)."""
        row = await self._one(
            "SELECT fetched_at, payload FROM org_provider_snapshots "
            "WHERE org_id = ? AND provider = ? AND kind = ? AND params_hash = ?",
            (org_id, provider, kind, params_hash),
        )
        if not row:
            return None
        age = time.time() - row["fetched_at"]
        if age > ttl:
            return None
        return {"payload": json.loads(row["payload"]), "age_seconds": age}

    async def put_snapshot(self, org_id: str, provider: str, kind: str, params_hash: str,
                           payload: dict) -> None:
        await self._exec(
            "INSERT INTO org_provider_snapshots (org_id, provider, kind, params_hash, fetched_at, "
            "payload) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(org_id, provider, kind, params_hash) DO UPDATE SET "
            "fetched_at = excluded.fetched_at, payload = excluded.payload",
            (org_id, provider, kind, params_hash, time.time(), json.dumps(payload, default=str)),
        )
