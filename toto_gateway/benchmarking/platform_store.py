"""Focused dual-dialect store for immutable benchmark-platform inventory records."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .. import db as _db_mod
from .domain import (
    Actor,
    CredentialScopeRef,
    IdentityAliasDecision,
    InventorySnapshot,
    ModelIdentity,
    OfferCapabilities,
    OfferPricing,
    ProviderName,
    ProviderOffer,
    RoutingCandidate,
    stable_snapshot_offer_id,
)
from .migrations import BOOTSTRAP_SQL, MIGRATIONS

_MIGRATION_ADVISORY_LOCK = 0x544F544F424D0001
_IDENTITY_INSERT_SQL = (
    "INSERT INTO model_identities "
    "(identity_id, vendor, family, release, reasoning_variant, quantization, fine_tune, "
    "context_variant, display_name, provisional, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (identity_id) DO NOTHING RETURNING identity_id"
)
_IDENTITY_SELECT_SQL = "SELECT * FROM model_identities WHERE identity_id = ?"
_SNAPSHOT_INSERT_SQL = (
    "INSERT INTO inventory_snapshots "
    "(snapshot_id, provider, scope_kind, scope_id, status, started_at, completed_at, "
    "expires_at, pagination_complete, adapter_revision, source_metadata_json, error_summary) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_OFFER_INSERT_SQL = (
    "INSERT INTO provider_offers "
    "(snapshot_offer_id, snapshot_id, offer_id, identity_id, route_id, provider, "
    "upstream_model_id, base_url, scope_kind, scope_id, capabilities_json, pricing_json, "
    "adapter_revision, raw_metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_ALIAS_IDENTITY_EXISTS_SQL = "SELECT 1 FROM model_identities WHERE identity_id = ?"
_ALIAS_CURRENT_SQL = (
    "SELECT decided_at FROM model_identity_aliases WHERE provider = ? "
    "AND source_id = ? AND superseded_at IS NULL"
)
_ALIAS_SUPERSEDE_SQL = (
    "UPDATE model_identity_aliases SET superseded_at = ? "
    "WHERE provider = ? AND source_id = ? AND superseded_at IS NULL"
)
_ALIAS_INSERT_SQL = (
    "INSERT INTO model_identity_aliases "
    "(alias_id, provider, source_id, identity_id, method, confidence, evidence_json, "
    "reviewer, reviewer_org_id, decided_at, superseded_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)"
)
_OPERATION_INSERT_SQL = (
    "INSERT INTO inventory_refresh_operations "
    "(operation_id, actor_id, scope, target_user_id, idempotency_key, fingerprint, status, "
    "plan_json, result_json, error, created_at, updated_at, expires_at, lease_owner, "
    "lease_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (actor_id, scope, target_user_id, idempotency_key) DO NOTHING"
)
_OPERATION_BY_KEY_SQL = (
    "SELECT * FROM inventory_refresh_operations WHERE actor_id = ? AND scope = ? "
    "AND target_user_id = ? AND idempotency_key = ?"
)
_OPERATION_TERMINAL = ("succeeded", "partial", "failed", "cancelled")
_OPERATION_STATUSES = ("queued", "running", *_OPERATION_TERMINAL)
_RECONCILIATION_CONTEXT_LIMIT = 100_000
_ALIAS_HISTORY_KEY_LIMIT = 100_000
_OPERATION_EXECUTION_CLAIM_SQL = (
    "UPDATE inventory_refresh_operations SET status = 'running', lease_owner = ?, "
    "lease_expires_at = ?, updated_at = ? WHERE operation_id = ? AND "
    "(status = 'queued' OR (status = 'running' AND "
    "(lease_expires_at IS NULL OR lease_expires_at <= ?))) RETURNING *"
)


@dataclass(frozen=True)
class InventoryIdentityPage:
    identities: tuple[ModelIdentity, ...]
    offers: tuple[ProviderOffer, ...]
    has_more: bool


@dataclass(frozen=True)
class InventoryInspection:
    snapshots: tuple[InventorySnapshot, ...]
    attempts: dict[ProviderName, dict]
    page: InventoryIdentityPage


@dataclass(frozen=True)
class InventoryExecutionFence:
    operation_id: str
    owner_id: str
    now: float


class InventoryExecutionFenceError(RuntimeError):
    """Raised when an inventory worker no longer owns a live execution lease."""


class BenchmarkPlatformStore(_db_mod.AsyncStoreMixin):
    def __init__(
        self,
        path: str = ":memory:",
        database_url: str = "",
        pool: dict | None = None,
    ) -> None:
        self._db, self._pg = _db_mod.connect(database_url, path)
        self._pool = _db_mod.make_async_pool(database_url, **(pool or {}))
        self._pool_opened = False
        self._lock = threading.Lock()
        self._owns_connection = True

    @classmethod
    def from_connection(cls, connection: sqlite3.Connection) -> "BenchmarkPlatformStore":
        store = cls.__new__(cls)
        connection.row_factory = sqlite3.Row
        store._db = connection
        store._pg = False
        store._pool = None
        store._pool_opened = False
        store._lock = threading.Lock()
        store._owns_connection = False
        return store

    async def migrate(self) -> None:
        """Apply each forward migration once under a database-wide transaction lock."""
        with self._lock:
            if self._pg:
                connection = self._db._c
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_ADVISORY_LOCK,)
                    )
                    self._apply_migrations(connection.execute, postgres=True)
                return
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._apply_migrations(self._db.execute, postgres=False)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    @classmethod
    def _apply_migrations(cls, execute, *, postgres: bool) -> None:
        for statement in cls._migration_statements(BOOTSTRAP_SQL, postgres=postgres):
            execute(statement)
        applied = {
            row["version"]
            for row in execute("SELECT version FROM benchmark_platform_migrations").fetchall()
        }
        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            for statement in cls._migration_statements(migration.sql, postgres=postgres):
                execute(statement)
            insert_sql = (
                "INSERT INTO benchmark_platform_migrations (version, name, applied_at) "
                "VALUES (?, ?, ?) ON CONFLICT (version) DO NOTHING"
            )
            if postgres:
                insert_sql = _db_mod._PgConn._t(insert_sql)
            execute(insert_sql, (migration.version, migration.name, time.time()))

    @staticmethod
    def _migration_statements(script: str, *, postgres: bool) -> tuple[str, ...]:
        statements = tuple(
            statement.strip() for statement in script.split(";") if statement.strip()
        )
        if not postgres:
            return statements
        return tuple(
            _db_mod._PgConn._t(statement.replace(" REAL", " DOUBLE PRECISION"))
            for statement in statements
        )

    async def close(self) -> None:
        await self.close_pool()
        if self._owns_connection:
            self._db.close()

    def _sqlite_transaction(self, operation, *args):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                result = operation(*args)
                self._db.commit()
                return result
            except Exception:
                self._db.rollback()
                raise

    async def _postgres_transaction(self, operation, *args):
        await self._open_pool()
        async with self._pool.connection() as connection:
            async with connection.transaction():
                return await operation(connection, *args)

    async def table_names(self) -> list[str]:
        if self._pg:
            rows = await self._all(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() ORDER BY table_name"
            )
            return [row["table_name"] for row in rows]
        rows = await self._all("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        return [row["name"] for row in rows]

    async def column_names(self, table: str) -> list[str]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
            raise ValueError("unsafe table name")
        with self._lock:
            cursor = self._db.execute(f'SELECT * FROM "{table}" WHERE 1 = 0')
            return [description[0] for description in cursor.description]

    # --- focused inventory-refresh operation ledger -------------------------

    async def claim_inventory_refresh_operation(
        self,
        *,
        operation_id: str,
        actor_id: str,
        scope: str,
        target_user_id: str | None,
        idempotency_key: str,
        fingerprint: str,
        now: float,
        expires_at: float,
        plan: Mapping | None = None,
    ) -> tuple[bool, dict]:
        target = target_user_id or ""
        params = (
            operation_id,
            actor_id,
            scope,
            target,
            idempotency_key,
            fingerprint,
            "queued",
            self._json(dict(plan or {})),
            "{}",
            None,
            now,
            now,
            expires_at,
            None,
            None,
        )
        key = (actor_id, scope, target, idempotency_key)
        if self._pool is not None:
            return await self._postgres_transaction(
                self._claim_inventory_refresh_operation_postgres, params, key, fingerprint
            )
        return self._sqlite_transaction(
            self._claim_inventory_refresh_operation_sqlite, params, key, fingerprint
        )

    def _claim_inventory_refresh_operation_sqlite(
        self, params: tuple, key: tuple, fingerprint: str
    ) -> tuple[bool, dict]:
        cursor = self._db.execute(_OPERATION_INSERT_SQL, params)
        row = self._db.execute(_OPERATION_BY_KEY_SQL, key).fetchone()
        return self._validate_operation_claim(cursor.rowcount > 0, row, fingerprint)

    async def _claim_inventory_refresh_operation_postgres(
        self, connection, params: tuple, key: tuple, fingerprint: str
    ) -> tuple[bool, dict]:
        cursor = await connection.execute(_db_mod._PgConn._t(_OPERATION_INSERT_SQL), params)
        row = await (
            await connection.execute(_db_mod._PgConn._t(_OPERATION_BY_KEY_SQL), key)
        ).fetchone()
        return self._validate_operation_claim(cursor.rowcount > 0, row, fingerprint)

    @classmethod
    def _validate_operation_claim(cls, created: bool, row, fingerprint: str) -> tuple[bool, dict]:
        if row is None:
            raise RuntimeError("inventory refresh operation claim was not retained")
        if row["fingerprint"] != fingerprint:
            raise ValueError("Idempotency-Key reused with different intent")
        return created, cls._operation_from_row(row)

    async def claim_inventory_refresh_execution(
        self,
        operation_id: str,
        *,
        owner_id: str,
        now: float,
        lease_expires_at: float,
    ) -> dict | None:
        params = (owner_id, lease_expires_at, now, operation_id, now)
        if self._pool is not None:
            return await self._postgres_transaction(
                self._claim_inventory_refresh_execution_postgres, params
            )
        return self._sqlite_transaction(self._claim_inventory_refresh_execution_sqlite, params)

    def _claim_inventory_refresh_execution_sqlite(self, params: tuple) -> dict | None:
        row = self._db.execute(_OPERATION_EXECUTION_CLAIM_SQL, params).fetchone()
        return self._operation_from_row(row) if row is not None else None

    async def _claim_inventory_refresh_execution_postgres(
        self, connection, params: tuple
    ) -> dict | None:
        row = await (
            await connection.execute(_db_mod._PgConn._t(_OPERATION_EXECUTION_CLAIM_SQL), params)
        ).fetchone()
        return self._operation_from_row(row) if row is not None else None

    async def renew_inventory_refresh_execution(
        self,
        operation_id: str,
        *,
        owner_id: str,
        now: float,
        lease_expires_at: float,
    ) -> bool:
        params = (lease_expires_at, now, operation_id, owner_id)
        if self._pool is not None:
            return await self._postgres_transaction(
                self._renew_inventory_refresh_execution_postgres, params
            )
        return self._sqlite_transaction(self._renew_inventory_refresh_execution_sqlite, params)

    def _renew_inventory_refresh_execution_sqlite(self, params: tuple) -> bool:
        row = self._db.execute(
            "UPDATE inventory_refresh_operations SET lease_expires_at = ?, updated_at = ? "
            "WHERE operation_id = ? AND status = 'running' AND lease_owner = ? RETURNING "
            "operation_id",
            params,
        ).fetchone()
        return row is not None

    async def _renew_inventory_refresh_execution_postgres(self, connection, params: tuple) -> bool:
        row = await (
            await connection.execute(
                _db_mod._PgConn._t(
                    "UPDATE inventory_refresh_operations SET lease_expires_at = ?, "
                    "updated_at = ? WHERE operation_id = ? AND status = 'running' "
                    "AND lease_owner = ? RETURNING operation_id"
                ),
                params,
            )
        ).fetchone()
        return row is not None

    async def finish_inventory_refresh_execution(
        self,
        operation_id: str,
        *,
        owner_id: str,
        status: str,
        result: Mapping,
        error: str | None,
        now: float,
    ) -> dict | None:
        if status not in _OPERATION_TERMINAL:
            raise ValueError(f"invalid terminal inventory refresh status {status!r}")
        params = (status, self._json(dict(result)), error, now, operation_id, owner_id)
        if self._pool is not None:
            return await self._postgres_transaction(
                self._finish_inventory_refresh_execution_postgres, params
            )
        return self._sqlite_transaction(self._finish_inventory_refresh_execution_sqlite, params)

    def _finish_inventory_refresh_execution_sqlite(self, params: tuple) -> dict | None:
        row = self._db.execute(
            "UPDATE inventory_refresh_operations SET status = ?, result_json = ?, error = ?, "
            "updated_at = ?, lease_owner = NULL, lease_expires_at = NULL "
            "WHERE operation_id = ? AND status = 'running' AND lease_owner = ? RETURNING *",
            params,
        ).fetchone()
        return self._operation_from_row(row) if row is not None else None

    async def _finish_inventory_refresh_execution_postgres(
        self, connection, params: tuple
    ) -> dict | None:
        row = await (
            await connection.execute(
                _db_mod._PgConn._t(
                    "UPDATE inventory_refresh_operations SET status = ?, result_json = ?, "
                    "error = ?, updated_at = ?, lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE operation_id = ? AND status = 'running' AND lease_owner = ? "
                    "RETURNING *"
                ),
                params,
            )
        ).fetchone()
        return self._operation_from_row(row) if row is not None else None

    async def recoverable_inventory_refresh_operations(
        self, *, now: float, limit: int = 100
    ) -> tuple[dict, ...]:
        limit = max(1, min(int(limit), 500))
        rows = await self._all(
            "SELECT * FROM inventory_refresh_operations WHERE status = 'queued' OR "
            "(status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)) "
            "ORDER BY created_at, operation_id LIMIT ?",
            (now, limit),
        )
        return tuple(self._operation_from_row(row) for row in rows)

    async def get_inventory_refresh_operation(self, operation_id: str) -> dict | None:
        row = await self._one(
            "SELECT * FROM inventory_refresh_operations WHERE operation_id = ?", (operation_id,)
        )
        return self._operation_from_row(row) if row is not None else None

    async def update_inventory_refresh_operation(
        self,
        operation_id: str,
        *,
        status: str,
        result: Mapping,
        error: str | None,
        now: float,
    ) -> dict:
        if status not in _OPERATION_STATUSES:
            raise ValueError(f"invalid inventory refresh operation status {status!r}")
        await self._exec(
            "UPDATE inventory_refresh_operations SET status = ?, result_json = ?, error = ?, "
            "updated_at = ? WHERE operation_id = ?",
            (status, self._json(dict(result)), error, now, operation_id),
        )
        row = await self.get_inventory_refresh_operation(operation_id)
        if row is None:
            raise ValueError(f"unknown inventory refresh operation {operation_id!r}")
        return row

    async def prune_inventory_refresh_operations(self, *, now: float, limit: int = 500) -> int:
        limit = max(1, min(int(limit), 1000))
        if self._pool is not None:
            return await self._postgres_transaction(
                self._prune_inventory_refresh_operations_postgres, now, limit
            )
        return self._sqlite_transaction(self._prune_inventory_refresh_operations_sqlite, now, limit)

    def _prune_inventory_refresh_operations_sqlite(self, now: float, limit: int) -> int:
        rows = self._db.execute(
            "SELECT operation_id FROM inventory_refresh_operations "
            "WHERE status IN ('succeeded', 'partial', 'failed', 'cancelled') "
            "AND expires_at <= ? ORDER BY expires_at, operation_id LIMIT ?",
            (now, limit),
        ).fetchall()
        self._db.executemany(
            "DELETE FROM inventory_refresh_operations WHERE operation_id = ?",
            [(row["operation_id"],) for row in rows],
        )
        return len(rows)

    async def _prune_inventory_refresh_operations_postgres(
        self, connection, now: float, limit: int
    ) -> int:
        rows = await (
            await connection.execute(
                _db_mod._PgConn._t(
                    "SELECT operation_id FROM inventory_refresh_operations "
                    "WHERE status IN ('succeeded', 'partial', 'failed', 'cancelled') "
                    "AND expires_at <= ? ORDER BY expires_at, operation_id LIMIT ? "
                    "FOR UPDATE SKIP LOCKED"
                ),
                (now, limit),
            )
        ).fetchall()
        for row in rows:
            await connection.execute(
                _db_mod._PgConn._t(
                    "DELETE FROM inventory_refresh_operations WHERE operation_id = ?"
                ),
                (row["operation_id"],),
            )
        return len(rows)

    @staticmethod
    def _operation_from_row(row) -> dict:
        result = dict(row)
        result["target_user_id"] = result["target_user_id"] or None
        result["plan"] = json.loads(result.pop("plan_json"))
        result["result"] = json.loads(result.pop("result_json"))
        return result

    async def ensure_identities(self, identities: Sequence[ModelIdentity]) -> int:
        """Atomically insert identities; equal concurrent writes converge, conflicts roll back."""
        validated = tuple(
            ModelIdentity.model_validate(identity.model_dump()) for identity in identities
        )
        if not validated:
            return 0
        if self._pool is not None:
            return await self._postgres_transaction(self._ensure_identities_postgres, validated)
        return self._sqlite_transaction(self._ensure_identities_sqlite, validated)

    async def reconciliation_context(
        self,
        providers: Sequence[ProviderName],
        *,
        limit: int = _RECONCILIATION_CONTEXT_LIMIT,
    ) -> tuple[tuple[ModelIdentity, ...], tuple[IdentityAliasDecision, ...]]:
        """Load bounded persisted identity truth for an inventory reconciliation pass."""
        selected = tuple(dict.fromkeys(providers))
        if not selected:
            raise ValueError("at least one reconciliation provider is required")
        if not 1 <= limit <= _RECONCILIATION_CONTEXT_LIMIT:
            raise ValueError(
                f"reconciliation context limit must be between 1 and "
                f"{_RECONCILIATION_CONTEXT_LIMIT}"
            )
        placeholders = ",".join("?" for _ in selected)
        identity_rows = await self._all(
            "SELECT * FROM model_identities ORDER BY identity_id LIMIT ?", (limit + 1,)
        )
        alias_rows = await self._all(
            "SELECT * FROM model_identity_aliases WHERE superseded_at IS NULL "
            f"AND provider IN ({placeholders}) ORDER BY provider, source_id, decided_at, alias_id "
            "LIMIT ?",
            (*selected, limit + 1),
        )
        if len(identity_rows) > limit or len(alias_rows) > limit:
            raise ValueError("persisted reconciliation context exceeds the configured bound")
        return (
            tuple(self._identity_from_row(row) for row in identity_rows),
            tuple(self._alias_from_row(row) for row in alias_rows),
        )

    async def latest_alias_decisions(
        self, keys: Sequence[tuple[ProviderName, str]]
    ) -> dict[tuple[ProviderName, str], IdentityAliasDecision]:
        """Load latest decisions for a bounded provider/source set in one database read."""
        requested = tuple(dict.fromkeys(keys))
        if not requested:
            return {}
        if len(requested) > _ALIAS_HISTORY_KEY_LIMIT:
            raise ValueError(
                f"alias history lookup exceeds {_ALIAS_HISTORY_KEY_LIMIT} provider/source keys"
            )
        payload = self._json([
            {"provider": provider, "source_id": source_id}
            for provider, source_id in requested
        ])
        requested_sql = (
            "SELECT provider, source_id FROM jsonb_to_recordset(?::jsonb) "
            "AS requested(provider text, source_id text)"
            if self._pool is not None
            else "SELECT json_extract(value, '$.provider') AS provider, "
                 "json_extract(value, '$.source_id') AS source_id FROM json_each(?)"
        )
        rows = await self._all(
            "WITH requested AS (" + requested_sql + "), ranked_aliases AS ("
            "SELECT alias.*, ROW_NUMBER() OVER ("
            "PARTITION BY alias.provider, alias.source_id "
            "ORDER BY alias.decided_at DESC, alias.alias_id DESC) AS alias_rank "
            "FROM model_identity_aliases alias JOIN requested "
            "ON requested.provider = alias.provider "
            "AND requested.source_id = alias.source_id) "
            "SELECT * FROM ranked_aliases WHERE alias_rank = 1 "
            "ORDER BY provider, source_id",
            (payload,),
        )
        return {
            (row["provider"], row["source_id"]): self._alias_from_row(row)
            for row in rows
        }

    def _ensure_identities_sqlite(self, identities: tuple[ModelIdentity, ...]) -> int:
        inserted = 0
        for identity in identities:
            expected = self._identity_params(identity)
            created = self._db.execute(_IDENTITY_INSERT_SQL, expected).fetchone()
            inserted += int(created is not None)
            row = self._db.execute(_IDENTITY_SELECT_SQL, (identity.identity_id,)).fetchone()
            self._check_identity_content(row, identity, expected)
        return inserted

    async def _ensure_identities_postgres(
        self, connection, identities: tuple[ModelIdentity, ...]
    ) -> int:
        insert_sql = _db_mod._PgConn._t(_IDENTITY_INSERT_SQL)
        select_sql = _db_mod._PgConn._t(_IDENTITY_SELECT_SQL)
        inserted = 0
        for identity in identities:
            expected = self._identity_params(identity)
            created = await (await connection.execute(insert_sql, expected)).fetchone()
            inserted += int(created is not None)
            row = await (await connection.execute(select_sql, (identity.identity_id,))).fetchone()
            self._check_identity_content(row, identity, expected)
        return inserted

    @classmethod
    def _check_identity_content(cls, row, identity: ModelIdentity, expected: tuple) -> None:
        actual = tuple(row[column] for column in cls._identity_columns())
        if actual != expected:
            raise ValueError(
                f"identity {identity.identity_id!r} already exists with different immutable content"
            )

    @staticmethod
    def _identity_columns() -> tuple[str, ...]:
        return (
            "identity_id",
            "vendor",
            "family",
            "release",
            "reasoning_variant",
            "quantization",
            "fine_tune",
            "context_variant",
            "display_name",
            "provisional",
            "created_at",
        )

    @classmethod
    def _identity_params(cls, identity: ModelIdentity) -> tuple:
        values = identity.model_dump()
        values["provisional"] = int(identity.provisional)
        return tuple(values[column] for column in cls._identity_columns())

    async def commit_inventory(
        self, snapshot: InventorySnapshot, offers: Sequence[ProviderOffer]
    ) -> None:
        await self._commit_inventory_transaction(snapshot, tuple(offers))

    async def _commit_inventory_transaction(
        self, snapshot: InventorySnapshot, offers: tuple[ProviderOffer, ...]
    ) -> None:
        snapshot, offers = self._validate_inventory(snapshot, offers)
        if self._pool is not None:
            await self._postgres_transaction(self._insert_inventory_postgres, snapshot, offers)
            return
        self._sqlite_transaction(self._insert_inventory_sqlite, snapshot, offers)

    @classmethod
    def _validate_inventory(
        cls, snapshot: InventorySnapshot, offers: tuple[ProviderOffer, ...]
    ) -> tuple[InventorySnapshot, tuple[ProviderOffer, ...]]:
        snapshot = InventorySnapshot.model_validate(snapshot.model_dump())
        offers = tuple(ProviderOffer.model_validate(offer.model_dump()) for offer in offers)
        if snapshot.status == "stale":
            raise ValueError("stale is derived at read time and cannot be persisted")
        if snapshot.status == "failed" and offers:
            raise ValueError("a failed inventory snapshot cannot contain offers")
        if snapshot.offers:
            raise ValueError("pass offers separately to commit_inventory")
        for offer in offers:
            if offer.provider != snapshot.provider:
                raise ValueError("offer provider does not match its snapshot")
            if offer.credential_scope != snapshot.credential_scope:
                raise ValueError("offer credential scope does not match its snapshot")
            if offer.adapter_revision != snapshot.adapter_revision:
                raise ValueError("offer adapter revision does not match its snapshot")
            expected = stable_snapshot_offer_id(snapshot.snapshot_id, offer.offer_id)
            if offer.snapshot_offer_id != expected:
                raise ValueError(f"snapshot_offer_id must equal {expected}")
        return snapshot, offers

    @classmethod
    def _snapshot_params(cls, snapshot: InventorySnapshot) -> tuple:
        return (
            snapshot.snapshot_id,
            snapshot.provider,
            snapshot.credential_scope.kind,
            snapshot.credential_scope.scope_id,
            snapshot.status,
            snapshot.started_at,
            snapshot.completed_at,
            snapshot.expires_at,
            int(snapshot.pagination_complete),
            snapshot.adapter_revision,
            cls._json(snapshot.source_metadata),
            snapshot.error_summary,
        )

    def _insert_inventory_sqlite(
        self, snapshot: InventorySnapshot, offers: tuple[ProviderOffer, ...]
    ) -> None:
        self._db.execute(_SNAPSHOT_INSERT_SQL, self._snapshot_params(snapshot))
        if offers:
            self._db.executemany(
                _OFFER_INSERT_SQL,
                [self._offer_params(snapshot.snapshot_id, offer) for offer in offers],
            )

    async def _insert_inventory_postgres(
        self, connection, snapshot: InventorySnapshot, offers: tuple[ProviderOffer, ...]
    ) -> None:
        await connection.execute(
            _db_mod._PgConn._t(_SNAPSHOT_INSERT_SQL), self._snapshot_params(snapshot)
        )
        if offers:
            await connection.cursor().executemany(
                _db_mod._PgConn._t(_OFFER_INSERT_SQL),
                [self._offer_params(snapshot.snapshot_id, offer) for offer in offers],
            )

    @classmethod
    def _offer_params(cls, snapshot_id: str, offer: ProviderOffer) -> tuple:
        return (
            offer.snapshot_offer_id,
            snapshot_id,
            offer.offer_id,
            offer.identity_id,
            offer.route_id,
            offer.provider,
            offer.upstream_model_id,
            offer.base_url,
            offer.credential_scope.kind,
            offer.credential_scope.scope_id,
            cls._json(offer.capabilities.model_dump(mode="json")),
            cls._json(offer.pricing.model_dump(mode="json")),
            offer.adapter_revision,
            cls._json(offer.raw_metadata),
        )

    async def commit_reconciled_inventory(
        self,
        snapshot: InventorySnapshot,
        offers: Sequence[ProviderOffer],
        decisions: Sequence[IdentityAliasDecision],
        actor: Actor,
        *,
        execution_fence: InventoryExecutionFence | None = None,
    ) -> tuple[IdentityAliasDecision, ...]:
        """Atomically activate reconciliation decisions and their immutable inventory evidence."""
        snapshot, validated_offers = self._validate_inventory(snapshot, tuple(offers))
        validated_decisions, actor = self._validate_alias_decisions(tuple(decisions), actor)
        offer_keys = {
            (offer.provider, offer.upstream_model_id, offer.identity_id)
            for offer in validated_offers
        }
        for decision in validated_decisions:
            if decision.evidence.get("snapshot_id") != snapshot.snapshot_id:
                raise ValueError("reconciliation decision must reference its inventory snapshot")
            key = (decision.provider, decision.source_id, decision.identity_id)
            if key not in offer_keys:
                raise ValueError("reconciliation decision does not match a snapshot offer")
        if self._pool is not None:
            return await self._postgres_transaction(
                self._commit_reconciled_postgres,
                snapshot,
                validated_offers,
                validated_decisions,
                actor,
                execution_fence,
            )
        return self._sqlite_transaction(
            self._commit_reconciled_sqlite,
            snapshot,
            validated_offers,
            validated_decisions,
            actor,
            execution_fence,
        )

    def _commit_reconciled_sqlite(
        self,
        snapshot: InventorySnapshot,
        offers: tuple[ProviderOffer, ...],
        decisions: tuple[IdentityAliasDecision, ...],
        actor: Actor,
        execution_fence: InventoryExecutionFence | None,
    ) -> tuple[IdentityAliasDecision, ...]:
        if execution_fence is not None:
            self._assert_inventory_execution_fence_sqlite(execution_fence)
        stored = self._write_alias_decisions_sqlite(decisions, actor)
        self._insert_inventory_sqlite(snapshot, offers)
        return stored

    async def _commit_reconciled_postgres(
        self,
        connection,
        snapshot: InventorySnapshot,
        offers: tuple[ProviderOffer, ...],
        decisions: tuple[IdentityAliasDecision, ...],
        actor: Actor,
        execution_fence: InventoryExecutionFence | None,
    ) -> tuple[IdentityAliasDecision, ...]:
        if execution_fence is not None:
            await self._assert_inventory_execution_fence_postgres(connection, execution_fence)
        stored = await self._write_alias_decisions_postgres(connection, decisions, actor)
        await self._insert_inventory_postgres(connection, snapshot, offers)
        return stored

    def _assert_inventory_execution_fence_sqlite(
        self, fence: InventoryExecutionFence
    ) -> None:
        row = self._db.execute(
            "SELECT 1 FROM inventory_refresh_operations WHERE operation_id = ? "
            "AND status = 'running' AND lease_owner = ? AND lease_expires_at > ?",
            (fence.operation_id, fence.owner_id, fence.now),
        ).fetchone()
        if row is None:
            raise InventoryExecutionFenceError("inventory execution lease is no longer valid")

    async def _assert_inventory_execution_fence_postgres(
        self, connection, fence: InventoryExecutionFence
    ) -> None:
        row = await (
            await connection.execute(
                _db_mod._PgConn._t(
                    "SELECT 1 FROM inventory_refresh_operations WHERE operation_id = ? "
                    "AND status = 'running' AND lease_owner = ? AND lease_expires_at > ? "
                    "FOR UPDATE"
                ),
                (fence.operation_id, fence.owner_id, fence.now),
            )
        ).fetchone()
        if row is None:
            raise InventoryExecutionFenceError("inventory execution lease is no longer valid")

    async def latest_inventory(
        self,
        selection: Mapping[ProviderName, CredentialScopeRef],
        *,
        max_staleness_s: float,
        now: float | None = None,
    ) -> Sequence[InventorySnapshot]:
        if max_staleness_s < 0:
            raise ValueError("max_staleness_s must be non-negative")
        provider_scopes = tuple(selection.items())
        if not provider_scopes:
            return ()
        scope_sql, params = self._selection_clause(provider_scopes)
        rows = await self._all(
            "SELECT * FROM inventory_snapshots WHERE status = 'success' "
            f"AND pagination_complete = 1 AND ({scope_sql}) "
            "ORDER BY provider, scope_kind, scope_id, completed_at DESC, snapshot_id DESC",
            params,
        )
        selected = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            key = (row["provider"], row["scope_kind"], row["scope_id"])
            if key not in seen:
                selected.append(row)
                seen.add(key)

        inspected_at = time.time() if now is None else now
        snapshots = []
        for row in selected:
            offers = await self._offers_for_snapshot(row["snapshot_id"])
            stale = inspected_at >= row["expires_at"] or (
                inspected_at - row["completed_at"] > max_staleness_s
            )
            snapshots.append(self._snapshot_from_row(row, offers, stale=stale))
        return tuple(snapshots)

    async def latest_inventory_metadata(
        self,
        selection: Mapping[ProviderName, CredentialScopeRef],
        *,
        max_staleness_s: float,
        now: float | None = None,
    ) -> Sequence[InventorySnapshot]:
        """Return one latest complete success per exact scope without loading offers."""
        if max_staleness_s < 0:
            raise ValueError("max_staleness_s must be non-negative")
        provider_scopes = tuple(selection.items())
        if not provider_scopes:
            return ()
        scope_sql, params = self._selection_clause(provider_scopes)
        rows = await self._all(
            "WITH ranked_snapshots AS ("
            "SELECT inventory_snapshots.*, ROW_NUMBER() OVER ("
            "PARTITION BY provider, scope_kind, scope_id "
            "ORDER BY completed_at DESC, snapshot_id DESC) AS snapshot_rank "
            "FROM inventory_snapshots WHERE status = 'success' "
            f"AND pagination_complete = 1 AND ({scope_sql})) "
            "SELECT * FROM ranked_snapshots WHERE snapshot_rank = 1 "
            "ORDER BY provider, scope_kind, scope_id",
            params,
        )
        inspected_at = time.time() if now is None else now
        return tuple(
            self._snapshot_from_row(
                row,
                (),
                stale=(
                    inspected_at >= row["expires_at"]
                    or inspected_at - row["completed_at"] > max_staleness_s
                ),
            )
            for row in rows
        )

    async def latest_inventory_attempts(
        self, selection: Mapping[ProviderName, CredentialScopeRef]
    ) -> dict[ProviderName, dict]:
        provider_scopes = tuple(selection.items())
        if not provider_scopes:
            return {}
        scope_sql, params = self._selection_clause(provider_scopes)
        rows = await self._all(
            "WITH ranked_snapshots AS ("
            "SELECT provider, scope_kind, scope_id, status, completed_at, error_summary, "
            "ROW_NUMBER() OVER (PARTITION BY provider, scope_kind, scope_id "
            "ORDER BY completed_at DESC, snapshot_id DESC) AS snapshot_rank "
            f"FROM inventory_snapshots WHERE ({scope_sql})) "
            "SELECT provider, status, completed_at, error_summary FROM ranked_snapshots "
            "WHERE snapshot_rank = 1 ORDER BY provider",
            params,
        )
        return {row["provider"]: dict(row) for row in rows}

    async def inventory_inspection(
        self,
        selection: Mapping[ProviderName, CredentialScopeRef],
        *,
        max_staleness_s: float,
        now: float,
        availability: str,
        after_identity_id: str | None,
        limit: int,
        identity_ref: str | None = None,
    ) -> InventoryInspection:
        """Pin provider state and paged offers to one chosen snapshot view."""
        if max_staleness_s < 0:
            raise ValueError("max_staleness_s must be non-negative")
        provider_scopes = tuple(selection.items())
        if not provider_scopes:
            return InventoryInspection((), {}, InventoryIdentityPage((), (), False))
        scope_sql, params = self._selection_clause(provider_scopes)
        rows = await self._all(
            "WITH ranked_successes AS ("
            "SELECT inventory_snapshots.*, ROW_NUMBER() OVER ("
            "PARTITION BY provider, scope_kind, scope_id "
            "ORDER BY completed_at DESC, snapshot_id DESC) AS snapshot_rank "
            "FROM inventory_snapshots WHERE status = 'success' "
            f"AND pagination_complete = 1 AND ({scope_sql})), "
            "ranked_attempts AS ("
            "SELECT inventory_snapshots.*, ROW_NUMBER() OVER ("
            "PARTITION BY provider, scope_kind, scope_id "
            "ORDER BY completed_at DESC, snapshot_id DESC) AS snapshot_rank "
            f"FROM inventory_snapshots WHERE ({scope_sql})) "
            "SELECT 'success' AS view_kind, * FROM ranked_successes WHERE snapshot_rank = 1 "
            "UNION ALL SELECT 'attempt' AS view_kind, * FROM ranked_attempts "
            "WHERE snapshot_rank = 1 ORDER BY provider, view_kind",
            params + params,
        )
        snapshots = tuple(
            self._snapshot_from_row(
                row,
                (),
                stale=(
                    now >= row["expires_at"]
                    or now - row["completed_at"] > max_staleness_s
                ),
            )
            for row in rows
            if row["view_kind"] == "success"
        )
        attempts = {
            row["provider"]: {
                "snapshot_id": row["snapshot_id"],
                "provider": row["provider"],
                "status": row["status"],
                "completed_at": row["completed_at"],
                "error_summary": row["error_summary"],
            }
            for row in rows
            if row["view_kind"] == "attempt"
        }
        page = await self.inventory_identity_page(
            selection,
            availability=availability,
            after_identity_id=after_identity_id,
            limit=limit,
            identity_ref=identity_ref,
            snapshot_ids=tuple(snapshot.snapshot_id for snapshot in snapshots),
        )
        return InventoryInspection(snapshots, attempts, page)

    async def inventory_identity_page(
        self,
        selection: Mapping[ProviderName, CredentialScopeRef],
        *,
        availability: str,
        after_identity_id: str | None,
        limit: int,
        identity_ref: str | None = None,
        snapshot_ids: Sequence[str] | None = None,
    ) -> InventoryIdentityPage:
        """Keyset-page identities and offers from only the selected latest snapshots."""
        if availability not in {"all", "available"}:
            raise ValueError("availability must be 'all' or 'available'")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        provider_scopes = tuple(selection.items())
        if not provider_scopes:
            return InventoryIdentityPage((), (), False)
        scope_sql, scope_params = self._selection_clause(provider_scopes)
        if snapshot_ids is None:
            latest_cte = self._latest_selected_offers_cte(scope_sql)
            selected_offer_params: tuple = scope_params
        elif snapshot_ids:
            placeholders = ",".join("?" for _ in snapshot_ids)
            latest_cte = (
                "WITH selected_offers AS (SELECT offers.* FROM provider_offers offers "
                f"WHERE offers.snapshot_id IN ({placeholders}))"
            )
            selected_offer_params = tuple(snapshot_ids)
        else:
            latest_cte = (
                "WITH selected_offers AS (SELECT offers.* FROM provider_offers offers "
                "WHERE 0 = 1)"
            )
            selected_offer_params = ()
        identity_params: tuple = selected_offer_params
        if availability == "available":
            where = (
                "WHERE EXISTS (SELECT 1 FROM selected_offers selected "
                "WHERE selected.identity_id = identity.identity_id)"
            )
        else:
            historical_scope_sql, historical_scope_params = self._selection_clause(
                provider_scopes, table_alias="scoped"
            )
            where = (
                "WHERE EXISTS (SELECT 1 FROM provider_offers scoped "
                "WHERE scoped.identity_id = identity.identity_id "
                f"AND ({historical_scope_sql}))"
            )
            identity_params += historical_scope_params
        if identity_ref is not None:
            provider_placeholders = ",".join("?" for _ in provider_scopes)
            where += (
                " AND EXISTS (SELECT 1 FROM selected_offers exact_offer "
                "WHERE exact_offer.identity_id = identity.identity_id) "
                "AND (identity.identity_id = ? OR EXISTS (SELECT 1 "
                "FROM model_identity_aliases alias "
                "WHERE alias.identity_id = identity.identity_id "
                "AND alias.source_id = ? AND alias.superseded_at IS NULL "
                f"AND alias.provider IN ({provider_placeholders})))"
            )
            identity_params += (
                identity_ref,
                identity_ref,
                *(provider for provider, _scope in provider_scopes),
            )
        if after_identity_id is not None:
            where += " AND identity.identity_id > ?"
            identity_params += (after_identity_id,)
        identity_columns = self._identity_columns()
        offer_columns = (
            "snapshot_offer_id",
            "offer_id",
            "identity_id",
            "route_id",
            "provider",
            "upstream_model_id",
            "base_url",
            "scope_kind",
            "scope_id",
            "capabilities_json",
            "pricing_json",
            "adapter_revision",
            "raw_metadata_json",
        )
        rows = await self._all(
            latest_cte
            + ", paged_identities AS (SELECT identity.* FROM model_identities identity "
            + where
            + " ORDER BY identity.identity_id LIMIT ?) SELECT "
            + ", ".join(f"identity.{column} AS identity__{column}" for column in identity_columns)
            + ", "
            + ", ".join(f"offer.{column} AS offer__{column}" for column in offer_columns)
            + " FROM paged_identities identity LEFT JOIN selected_offers offer "
            "ON offer.identity_id = identity.identity_id "
            "ORDER BY identity.identity_id, offer.route_id, offer.snapshot_offer_id",
            identity_params + (limit + 1,),
        )
        identities_by_id: dict[str, ModelIdentity] = {}
        offer_rows = []
        for row in rows:
            identity_data = {column: row[f"identity__{column}"] for column in identity_columns}
            identity = ModelIdentity.model_validate(identity_data)
            identities_by_id.setdefault(identity.identity_id, identity)
            if row["offer__snapshot_offer_id"] is not None:
                offer_rows.append({column: row[f"offer__{column}"] for column in offer_columns})
        paged_identities = tuple(identities_by_id.values())
        has_more = len(paged_identities) > limit
        identities = paged_identities[:limit]
        visible_identity_ids = {identity.identity_id for identity in identities}
        return InventoryIdentityPage(
            identities,
            tuple(
                self._offer_from_row(row)
                for row in offer_rows
                if row["identity_id"] in visible_identity_ids
            ),
            has_more,
        )

    async def all_routing_candidates(
        self, *, now: float | None = None
    ) -> Sequence[RoutingCandidate]:
        """Load one committed latest snapshot per provider/scope in one query."""
        rows = await self._all(
            "WITH ranked_snapshots AS ("
            "SELECT snapshot_id, completed_at, expires_at, ROW_NUMBER() OVER ("
            "PARTITION BY provider, scope_kind, scope_id "
            "ORDER BY completed_at DESC, snapshot_id DESC) AS snapshot_rank "
            "FROM inventory_snapshots WHERE status = 'success' AND pagination_complete = 1) "
            "SELECT offers.*, snapshots.snapshot_id AS selected_snapshot_id, "
            "snapshots.completed_at AS selected_completed_at, "
            "snapshots.expires_at AS selected_expires_at "
            "FROM ranked_snapshots snapshots JOIN provider_offers offers "
            "ON offers.snapshot_id = snapshots.snapshot_id "
            "WHERE snapshots.snapshot_rank = 1 AND snapshots.expires_at > ? "
            "ORDER BY offers.route_id, offers.snapshot_offer_id",
            (time.time() if now is None else now,),
        )
        return self._routing_candidates_from_rows(rows)

    async def latest_routing_candidate_revisions(
        self, *, now: float | None = None
    ) -> dict[tuple[str, str, str], str]:
        """Return the cheap latest fresh snapshot revision for every provider/scope partition."""
        rows = await self._all(
            "WITH ranked_snapshots AS ("
            "SELECT snapshot_id, provider, scope_kind, scope_id, expires_at, ROW_NUMBER() OVER ("
            "PARTITION BY provider, scope_kind, scope_id "
            "ORDER BY completed_at DESC, snapshot_id DESC) AS snapshot_rank "
            "FROM inventory_snapshots WHERE status = 'success' AND pagination_complete = 1) "
            "SELECT snapshot_id, provider, scope_kind, scope_id FROM ranked_snapshots "
            "WHERE snapshot_rank = 1 AND expires_at > ? "
            "ORDER BY provider, scope_kind, scope_id",
            (time.time() if now is None else now,),
        )
        return {
            (row["provider"], row["scope_kind"], row["scope_id"]): row["snapshot_id"]
            for row in rows
        }

    async def routing_candidates_for_scopes(
        self,
        scopes: Sequence[tuple[ProviderName, CredentialScopeRef]],
        *,
        now: float | None = None,
    ) -> Sequence[RoutingCandidate]:
        """Load offers only for the named provider/scope latest-snapshot partitions."""
        selected = tuple(dict.fromkeys(scopes))
        if not selected:
            return ()
        scope_sql, scope_params = self._selection_clause(selected)
        rows = await self._all(
            "WITH ranked_snapshots AS ("
            "SELECT snapshot_id, completed_at, expires_at, ROW_NUMBER() OVER ("
            "PARTITION BY provider, scope_kind, scope_id "
            "ORDER BY completed_at DESC, snapshot_id DESC) AS snapshot_rank "
            "FROM inventory_snapshots WHERE status = 'success' AND pagination_complete = 1 "
            f"AND ({scope_sql})) "
            "SELECT offers.*, snapshots.snapshot_id AS selected_snapshot_id, "
            "snapshots.completed_at AS selected_completed_at, "
            "snapshots.expires_at AS selected_expires_at "
            "FROM ranked_snapshots snapshots JOIN provider_offers offers "
            "ON offers.snapshot_id = snapshots.snapshot_id "
            "WHERE snapshots.snapshot_rank = 1 AND snapshots.expires_at > ? "
            "ORDER BY offers.route_id, offers.snapshot_offer_id",
            (*scope_params, time.time() if now is None else now),
        )
        return self._routing_candidates_from_rows(rows)

    @classmethod
    def _routing_candidates_from_rows(cls, rows) -> tuple[RoutingCandidate, ...]:
        candidates = []
        for row in rows:
            offer = cls._offer_from_row(row)
            candidates.append(
                RoutingCandidate(
                    snapshot_id=row["selected_snapshot_id"],
                    snapshot_completed_at=row["selected_completed_at"],
                    snapshot_expires_at=row["selected_expires_at"],
                    **offer.model_dump(),
                )
            )
        return tuple(candidates)

    async def routing_candidates(
        self,
        selection: Mapping[ProviderName, CredentialScopeRef],
        *,
        now: float | None = None,
    ) -> Sequence[RoutingCandidate]:
        snapshots = await self.latest_inventory(selection, max_staleness_s=float("inf"), now=now)
        candidates = []
        for snapshot in snapshots:
            if snapshot.status != "success":
                continue
            for offer in snapshot.offers:
                candidates.append(
                    RoutingCandidate(
                        snapshot_id=snapshot.snapshot_id,
                        snapshot_completed_at=snapshot.completed_at,
                        snapshot_expires_at=snapshot.expires_at,
                        **offer.model_dump(),
                    )
                )
        return tuple(sorted(candidates, key=lambda candidate: candidate.route_id))

    async def replace_alias_decision(
        self, decision: IdentityAliasDecision, actor: Actor
    ) -> IdentityAliasDecision:
        decisions, actor = self._validate_alias_decisions((decision,), actor)
        if self._pool is not None:
            stored = await self._postgres_transaction(
                self._write_alias_decisions_postgres, decisions, actor
            )
            return stored[0]
        return self._sqlite_transaction(self._write_alias_decisions_sqlite, decisions, actor)[0]

    @staticmethod
    def _validate_alias_decisions(
        decisions: tuple[IdentityAliasDecision, ...], actor: Actor
    ) -> tuple[tuple[IdentityAliasDecision, ...], Actor]:
        actor = Actor.model_validate(actor.model_dump())
        validated = tuple(
            IdentityAliasDecision.model_validate(decision.model_dump()) for decision in decisions
        )
        if any(decision.superseded_at is not None for decision in validated):
            raise ValueError("a replacement decision must be active")
        return validated, actor

    def _write_alias_decisions_sqlite(
        self, decisions: tuple[IdentityAliasDecision, ...], actor: Actor
    ) -> tuple[IdentityAliasDecision, ...]:
        stored = []
        for decision in decisions:
            if (
                self._db.execute(_ALIAS_IDENTITY_EXISTS_SQL, (decision.identity_id,)).fetchone()
                is None
            ):
                raise ValueError(f"unknown identity {decision.identity_id!r}")
            current = self._db.execute(
                _ALIAS_CURRENT_SQL, (decision.provider, decision.source_id)
            ).fetchone()
            self._check_alias_order(current, decision)
            self._db.execute(
                _ALIAS_SUPERSEDE_SQL,
                (decision.decided_at, decision.provider, decision.source_id),
            )
            stored_decision, params = self._alias_insert_params(decision, actor)
            self._db.execute(_ALIAS_INSERT_SQL, params)
            stored.append(stored_decision)
        return tuple(stored)

    async def _write_alias_decisions_postgres(
        self, connection, decisions: tuple[IdentityAliasDecision, ...], actor: Actor
    ) -> tuple[IdentityAliasDecision, ...]:
        stored = []
        for decision in decisions:
            identity = await (
                await connection.execute(
                    _db_mod._PgConn._t(_ALIAS_IDENTITY_EXISTS_SQL),
                    (decision.identity_id,),
                )
            ).fetchone()
            if identity is None:
                raise ValueError(f"unknown identity {decision.identity_id!r}")
            current = await (
                await connection.execute(
                    _db_mod._PgConn._t(_ALIAS_CURRENT_SQL),
                    (decision.provider, decision.source_id),
                )
            ).fetchone()
            self._check_alias_order(current, decision)
            await connection.execute(
                _db_mod._PgConn._t(_ALIAS_SUPERSEDE_SQL),
                (decision.decided_at, decision.provider, decision.source_id),
            )
            stored_decision, params = self._alias_insert_params(decision, actor)
            await connection.execute(_db_mod._PgConn._t(_ALIAS_INSERT_SQL), params)
            stored.append(stored_decision)
        return tuple(stored)

    @classmethod
    def _alias_insert_params(
        cls, decision: IdentityAliasDecision, actor: Actor
    ) -> tuple[IdentityAliasDecision, tuple]:
        reviewer = decision.reviewer or actor.actor_id
        stored = decision.model_copy(update={"reviewer": reviewer})
        return stored, (
            decision.alias_id,
            decision.provider,
            decision.source_id,
            decision.identity_id,
            decision.method,
            decision.confidence,
            cls._json(decision.evidence),
            reviewer,
            actor.org_id,
            decision.decided_at,
        )

    @staticmethod
    def _check_alias_order(current, decision: IdentityAliasDecision) -> None:
        if current is not None and current["decided_at"] >= decision.decided_at:
            raise ValueError("replacement decision must be newer than the active decision")

    async def alias_decisions(
        self, provider: str, source_id: str
    ) -> Sequence[IdentityAliasDecision]:
        rows = await self._all(
            "SELECT * FROM model_identity_aliases WHERE provider = ? AND source_id = ? "
            "ORDER BY decided_at, alias_id",
            (provider, source_id),
        )
        return tuple(self._alias_from_row(row) for row in rows)

    async def _offers_for_snapshot(self, snapshot_id: str) -> tuple[ProviderOffer, ...]:
        rows = await self._all(
            "SELECT * FROM provider_offers WHERE snapshot_id = ? ORDER BY route_id", (snapshot_id,)
        )
        return tuple(self._offer_from_row(row) for row in rows)

    @staticmethod
    def _snapshot_from_row(
        row, offers: tuple[ProviderOffer, ...], *, stale: bool
    ) -> InventorySnapshot:
        return InventorySnapshot(
            snapshot_id=row["snapshot_id"],
            provider=row["provider"],
            credential_scope=CredentialScopeRef(kind=row["scope_kind"], scope_id=row["scope_id"]),
            status="stale" if stale else row["status"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            expires_at=row["expires_at"],
            pagination_complete=bool(row["pagination_complete"]),
            adapter_revision=row["adapter_revision"],
            source_metadata=json.loads(row["source_metadata_json"]),
            error_summary=row["error_summary"],
            offers=offers,
        )

    @staticmethod
    def _identity_from_row(row) -> ModelIdentity:
        return ModelIdentity(
            identity_id=row["identity_id"],
            vendor=row["vendor"],
            family=row["family"],
            release=row["release"],
            reasoning_variant=row["reasoning_variant"],
            quantization=row["quantization"],
            fine_tune=row["fine_tune"],
            context_variant=row["context_variant"],
            display_name=row["display_name"],
            provisional=bool(row["provisional"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _alias_from_row(row) -> IdentityAliasDecision:
        return IdentityAliasDecision(
            alias_id=row["alias_id"],
            provider=row["provider"],
            source_id=row["source_id"],
            identity_id=row["identity_id"],
            method=row["method"],
            confidence=row["confidence"],
            evidence=json.loads(row["evidence_json"]),
            reviewer=row["reviewer"],
            decided_at=row["decided_at"],
            superseded_at=row["superseded_at"],
        )

    @staticmethod
    def _offer_from_row(row) -> ProviderOffer:
        return ProviderOffer(
            snapshot_offer_id=row["snapshot_offer_id"],
            offer_id=row["offer_id"],
            identity_id=row["identity_id"],
            route_id=row["route_id"],
            provider=row["provider"],
            upstream_model_id=row["upstream_model_id"],
            base_url=row["base_url"],
            credential_scope=CredentialScopeRef(kind=row["scope_kind"], scope_id=row["scope_id"]),
            capabilities=OfferCapabilities.model_validate(json.loads(row["capabilities_json"])),
            pricing=OfferPricing.model_validate(json.loads(row["pricing_json"])),
            adapter_revision=row["adapter_revision"],
            raw_metadata=json.loads(row["raw_metadata_json"]),
        )

    @staticmethod
    def _selection_clause(
        provider_scopes: Sequence[tuple[ProviderName, CredentialScopeRef]],
        *,
        table_alias: str = "",
    ) -> tuple[str, tuple[str, ...]]:
        prefix = f"{table_alias}." if table_alias else ""
        clauses = []
        params = []
        for provider, scope in provider_scopes:
            clauses.append(
                f"({prefix}provider = ? AND {prefix}scope_kind = ? AND {prefix}scope_id = ?)"
            )
            params.extend((provider, scope.kind, scope.scope_id))
        return " OR ".join(clauses), tuple(params)

    @staticmethod
    def _latest_selected_offers_cte(scope_sql: str) -> str:
        return (
            "WITH ranked_snapshots AS ("
            "SELECT snapshot_id, ROW_NUMBER() OVER ("
            "PARTITION BY provider, scope_kind, scope_id "
            "ORDER BY completed_at DESC, snapshot_id DESC) AS snapshot_rank "
            "FROM inventory_snapshots WHERE status = 'success' "
            f"AND pagination_complete = 1 AND ({scope_sql})), "
            "selected_offers AS (SELECT offers.* FROM provider_offers offers "
            "JOIN ranked_snapshots snapshots ON offers.snapshot_id = snapshots.snapshot_id "
            "WHERE snapshots.snapshot_rank = 1)"
        )

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
