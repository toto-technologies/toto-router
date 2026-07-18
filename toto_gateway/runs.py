"""RunStore — per-session event log + live pub/sub, backing the sessions API and SSE.

The driver already emits one span per graph node through its observer seam; this module gives
those spans an address. Each session (run) gets a monotonically seq-numbered event log:
SQLite is the durable replay/restart path, an in-process asyncio fan-out is the live path.
SSE resume is exact: a subscriber names the last seq it saw and gets everything after.

One SQLite file holds all four tables of the live-routing plane (sessions/events/feedback/
preferences — see docs/plans/2026-07-01-live-routing-e2e.md). stdlib sqlite3, WAL mode,
single connection guarded by a lock: single-operator scale, no new dependencies.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any, AsyncIterator

from . import db as _db_mod

# Set by the sessions route around driver.run(); the span fan-out reads it to address events.
# Contextvars flow through asyncio.gather, so concurrent runs never cross streams.
CURRENT_RUN_ID: ContextVar[str | None] = ContextVar("toto_gw_run_id", default=None)

TERMINAL_KINDS = ("run_done", "run_failed", "run_cancelled")
# Terminal run STATUSES (sessions.status), mirroring TERMINAL_KINDS. cancelled IS terminal — the
# same-lane "still thinking" guards must treat it so, else one Stop deadlocks the lane forever.
TERMINAL_STATUSES = ("done", "failed", "cancelled")
_REPOLL_SECONDS = 5.0  # subscribe() re-reads on each wake; also every 5s as a missed-notify floor
_WAKE_CHANNEL = "toto_run"  # == wake.CHANNEL; publish()'s CTE pg_notify fires on it (kept in sync)


def _scope(user_id: str | None) -> tuple[str, tuple]:
    """A WHERE fragment restricting a real user to STRICTLY their own rows — never another
    user's, never NULL-owner (fail closed). Empty (no restriction) when user_id is None —
    operator or open-mode, which is the service-credential path and sees everything."""
    if user_id is None:
        return "", ()
    return "(user_id = ?)", (user_id,)


# Desk size tiers (canvas-object-rules Axiom 2 / V16): a desk is a predictable size, chosen not
# emergent. World dims per tier live SERVER-SIDE (shared truth) so agents reason about the same
# surface every client draws. Custom carries its own w/h; named tiers derive from here.
DESK_TIERS: dict[str, tuple[float, float]] = {
    "small":  (1920.0, 1200.0),   # one task's worth of work
    "medium": (2560.0, 1600.0),   # a project's active set — the default
    "large":  (4096.0, 2560.0),   # a wall — many clusters/piles
}
DESK_TIER_NAMES = frozenset(DESK_TIERS) | {"custom"}
DEFAULT_DESK_TIER = "medium"
DEFAULT_DESK_MATERIAL = "guilloche"  # materials-v2 default (Alex ruling) — NOT MATERIALS[0]; shared identity, not device taste


def desk_dims(tier: str, w: float | None, h: float | None) -> tuple[float, float]:
    """Effective (w, h) for a desk record: custom uses its stored dims (falling back to the
    default tier if either is missing — a custom row can't render dimensionless); a named tier
    always derives from DESK_TIERS, ignoring any stale stored w/h."""
    if tier == "custom" and w and h:
        return float(w), float(h)
    return DESK_TIERS.get(tier, DESK_TIERS[DEFAULT_DESK_TIER])


# A canvas position points at an object living in ONE of four tables. Three kinds keep their own
# table (kind -> (table, id column)); every OTHER kind is a generic row in canvas_objects. Naming
# only the stable own-table kinds keeps the existence check + orphan cleanup drift-free as new
# generic kinds land (V14: a position may only back a real object).
_OWN_TABLE_KINDS: dict[str, tuple[str, str]] = {
    "list":    ("lists", "list_id"),
    "session": ("sessions", "run_id"),
    "bindle":  ("bindles", "bindle_id"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  run_id     TEXT PRIMARY KEY,
  query      TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'running',   -- running | done | failed
  answer     TEXT NOT NULL DEFAULT '',
  error      TEXT NOT NULL DEFAULT '',
  tasks_json TEXT NOT NULL DEFAULT '[]',
  cost_total REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
-- events is the one UNBOUNDED table (every span of every run, forever). RETENTION TODO (not
-- built): once on Postgres, prune or range-partition by ts and drop partitions for TERMINAL
-- runs older than ~90d. payload stays TEXT here (blob, replayed wholesale, never queried into).
CREATE TABLE IF NOT EXISTS events (
  run_id  TEXT NOT NULL,
  seq     INTEGER NOT NULL,
  ts      REAL NOT NULL,
  kind    TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS feedback (
  run_id     TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  model_id   TEXT NOT NULL,
  skill      TEXT NOT NULL,
  verdict    TEXT NOT NULL,                     -- up | down
  created_at REAL NOT NULL,
  PRIMARY KEY (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS preferences (
  user_id TEXT,
  key     TEXT NOT NULL,
  value   TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS lists (
  list_id    TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS list_items (
  list_id        TEXT NOT NULL,
  item_id        TEXT NOT NULL,
  task           TEXT NOT NULL,
  description    TEXT NOT NULL DEFAULT '',
  metadata       TEXT NOT NULL DEFAULT '{}',
  enriched_model TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL DEFAULT '',        -- '' | 'doing' | 'done' (prod list done-toggles)
  position       INTEGER NOT NULL,
  created_at     REAL NOT NULL,
  PRIMARY KEY (list_id, item_id)
);
CREATE TABLE IF NOT EXISTS canvas_positions (
  kind       TEXT NOT NULL,
  object_id  TEXT NOT NULL,
  x REAL NOT NULL, y REAL NOT NULL,
  z INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (kind, object_id)
);
CREATE TABLE IF NOT EXISTS bindles (
  bindle_id TEXT PRIMARY KEY,
  edition   TEXT NOT NULL,
  subtitle  TEXT NOT NULL DEFAULT '',
  pages     INTEGER NOT NULL DEFAULT 0,
  html      TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS canvas_objects (
  kind       TEXT NOT NULL,
  object_id  TEXT NOT NULL,
  payload    TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (kind, object_id)
);
-- Session documents: a completed work session's synthesized answer, persisted as a markdown FILE
-- in the ObjectStore (key = documents/<doc_id>.md). This table is only the INDEX (the store has no
-- list op) while the bytes live in the residency-flexible store (filesystem or S3). user_id is the
-- owner (STRICT per-user scope, NULL = legacy/invisible). sha256/bytes describe the stored object.
CREATE TABLE IF NOT EXISTS documents (
  doc_id     TEXT PRIMARY KEY,
  user_id    TEXT,
  run_id     TEXT NOT NULL,
  title      TEXT NOT NULL,
  key        TEXT NOT NULL,
  sha256     TEXT NOT NULL,
  bytes      INTEGER NOT NULL,
  created_at REAL NOT NULL
);
-- Desk identity per surface (canvas-object-rules Axiom 8 / V16): tier + material are SHARED
-- truth, not device-local localStorage — agents read the same desk every client draws onto.
-- Keyed on (user_id, surface): '' surface = the world, else a container object_id. w/h are the
-- custom-tier dims (NULL for named tiers, which derive from DESK_TIERS server-side).
CREATE TABLE IF NOT EXISTS canvas_desks (
  user_id    TEXT,
  parent     TEXT NOT NULL DEFAULT '',
  tier       TEXT NOT NULL DEFAULT 'medium',
  w REAL, h REAL,
  material   TEXT NOT NULL DEFAULT 'oak',
  updated_at REAL NOT NULL,
  PRIMARY KEY (user_id, parent)
);
CREATE TABLE IF NOT EXISTS task_embeddings (
  run_id      TEXT NOT NULL,
  task_id     TEXT NOT NULL,
  text        TEXT NOT NULL,          -- task+description (NEVER the answer)
  vector      TEXT NOT NULL,          -- JSON float array
  skill       TEXT NOT NULL DEFAULT '',
  model_id    TEXT NOT NULL DEFAULT '',
  outcome     TEXT NOT NULL DEFAULT '',
  cost_usd    REAL,
  latency_ms  INTEGER,
  user_id     TEXT,
  created_at  REAL NOT NULL,
  PRIMARY KEY (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS embedding_cache (
  hash       TEXT PRIMARY KEY,        -- sha256(model + text)
  vector     TEXT NOT NULL,           -- JSON float array
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_limits (
  scope        TEXT NOT NULL,         -- 'run:<user>' | 'auth:<ip>' | 'quota:<user>'
  window_start INTEGER NOT NULL,      -- unix-floor of the fixed window
  count        INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (scope, window_start)
);
-- Companion memory (docs/plans/2026-07-03-toto-companion-agent.md, Decision 3): typed rows,
-- one fact per row, whole block injected at every wake. STRICTLY user-scoped — no NULL
-- grandfathering (memory is the most sensitive plane), NULL owner = the open-mode anon user.
-- Caps enforced in memory_write, never trusted to the model. NOTE: keep semicolons out of
-- these comments — the PG shim's executescript splits statements on them.
CREATE TABLE IF NOT EXISTS user_memory (
  memory_id  TEXT PRIMARY KEY,
  user_id    TEXT,
  kind       TEXT NOT NULL,           -- preference | fact | context | instruction
  content    TEXT NOT NULL,
  source_run TEXT NOT NULL DEFAULT '',-- provenance: the chat turn that wrote it
  created_at REAL NOT NULL
);
-- Tenant registry (docs/plans/2026-07-04-brain-markdown-filesystem.md, phase 1): content-plane
-- routing metadata, living in the OPERATIONAL DB. content_dsn_ref is a secret-manager
-- REFERENCE, never an inline DSN (Security HIGH-5). v1 holds registered rows only — nothing
-- reads it yet, the content resolver consults it when dedicated tenants land.
CREATE TABLE IF NOT EXISTS tenants (
  tenant_id       TEXT PRIMARY KEY,
  content_dsn_ref TEXT NOT NULL DEFAULT '',
  region          TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'active',
  epoch           INTEGER NOT NULL DEFAULT 0
);
-- Companion tool-call audit trail (Decision 2: receipts are the brand, Decision 6: SOC2).
-- Gateway DB only — same line the session prompts/answers already sit behind.
CREATE TABLE IF NOT EXISTS companion_tool_calls (
  call_id    TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL,           -- the chat turn that made the call
  user_id    TEXT,
  tool       TEXT NOT NULL,
  args_json  TEXT NOT NULL DEFAULT '{}',
  result     TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
-- Dream pass audit + idempotency (docs/plans/2026-07-05-memory-lifecycle.md P1). The PK
-- (tenant_id, run_date) IS the once-per-tenant-per-night guard AND the cross-replica leader
-- election -- the winning claim_dream_run insert owns that tenant for the day. shown gates the
-- companion's one sparing next-wake mention (D5). NOTE keep semicolons out of these comments --
-- the PG shim executescript splits on them.
-- Companion voice (TTS) spend ledger (docs/plans/2026-07-05-voice-companion.md P0). One row per
-- /speak call -- it is BOTH the receipt (chars + cost) AND the per-user daily-cap input, summed
-- by tts_spend_today exactly as chat_spend_today sums sessions. User-scoped like every companion
-- plane. NOTE keep semicolons out of these comments -- the PG shim executescript splits on them.
CREATE TABLE IF NOT EXISTS companion_tts_usage (
  call_id    TEXT PRIMARY KEY,
  user_id    TEXT,
  chars      INTEGER NOT NULL,
  cost_usd   REAL NOT NULL,
  created_at REAL NOT NULL
);
-- Pipedream Connect spend ledger (docs/plans/2026-07-06-pipedream-assessment.md pd-metering). One
-- row per external sync PULL -- the receipt (call count + ESTIMATED $, their credit model is opaque
-- so it is reconciled monthly against the invoice, no gating). User-scoped like every companion
-- plane. NOTE keep semicolons out of these comments -- the PG shim executescript splits on them.
CREATE TABLE IF NOT EXISTS pipedream_usage (
  call_id    TEXT PRIMARY KEY,
  user_id    TEXT,
  calls      INTEGER NOT NULL,
  est_usd    REAL NOT NULL,
  created_at REAL NOT NULL
);
-- Custom tools (docs/plans/2026-07-06-tool-contract.md §3). Ownable, STRICTLY user-scoped like
-- user_memory -- two users share nothing, no NULL grandfathering. spec is the sharable JSON
-- artifact (the whole contract). unique (user_id, name) makes a re-PUT of the same name an
-- owner-scoped overwrite (import == upsert). NOTE keep semicolons out of these comments -- the PG
-- shim executescript splits on them.
CREATE TABLE IF NOT EXISTS custom_tools (
  tool_id    TEXT PRIMARY KEY,
  user_id    TEXT,
  name       TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  spec       TEXT NOT NULL,
  version    INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS dream_runs (
  tenant_id   TEXT NOT NULL,
  run_date    TEXT NOT NULL,           -- UTC YYYY-MM-DD
  status      TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
  merged      INTEGER NOT NULL DEFAULT 0,
  archived    INTEGER NOT NULL DEFAULT 0,
  cost_usd    REAL NOT NULL DEFAULT 0,
  shown       INTEGER NOT NULL DEFAULT 0,       -- 1 once the companion has volunteered it
  started_at  REAL NOT NULL,
  finished_at REAL,
  PRIMARY KEY (tenant_id, run_date)
);
-- Idempotency keys (engine-hardening Wave 2, docs/plans/engine-hardening/caching.md chunk A). A
-- client retry after a network blip replays the first response instead of double-executing a
-- create (double token spend, duplicate lists). The winning claim_idempotency insert is BOTH the
-- dedup guard AND the cross-replica in-flight marker (status_code NULL = still running -- a second
-- claim before the result lands gets 409 retry). user_id is coalesced to '' for the NULL-owner
-- operator so the composite PK dedups on both dialects (SQLite treats NULL PK parts as distinct).
-- ponytail no TTL/reaper -- keys are tiny at single-operator scale, add a created_at sweep later.
-- NOTE keep semicolons out of these comments -- the PG shim executescript splits on them.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  user_id       TEXT NOT NULL DEFAULT '',
  idem_key      TEXT NOT NULL,
  method        TEXT NOT NULL,
  path          TEXT NOT NULL,
  status_code   INTEGER,                        -- NULL while in-flight, set once the result lands
  response_json TEXT,
  created_at    REAL NOT NULL,
  PRIMARY KEY (user_id, idem_key)
);
-- Schema-version anchor (engine-hardening PT-D, docs/ops/migrations.md). The boot-time idempotent
-- DDL contract (CREATE IF NOT EXISTS + guarded ALTERs) has no ordering anchor -- this one row gives
-- the first non-additive migration a replica-safe guard and a version to read at /statusz. Stamped
-- once with ON CONFLICT DO NOTHING so concurrent replica boots race safely (first writer wins, the
-- rest no-op). NOTE keep semicolons out of these comments -- the PG shim executescript splits on them.
CREATE TABLE IF NOT EXISTS meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""

# Forward-only schema generation. Bumped by hand when the boot DDL changes in a way a future
# migration needs to reason about (the contract + ceiling live in docs/ops/migrations.md). It is an
# ANCHOR, not a runner: it does not gate boot or trigger backfills -- it records what this replica
# stamped so the first destructive migration has an ordered starting point.
# Bumped to "2" for the control-plane tenancy tables (organizations/teams/memberships in auth.py
# _SCHEMA). Anchor only — records what this replica stamped; does not gate boot or run migrations.
SCHEMA_VERSION = "2"

# Companion memory caps (plan Decision 3): ~100 rows / 8k chars per user; oldest evictable rows
# go first; preference/instruction are never auto-evicted.
MEMORY_KINDS = ("preference", "fact", "context", "instruction")
MEMORY_MAX_ROWS, MEMORY_MAX_CHARS = 100, 8000
_MEMORY_EVICTABLE = ("context", "fact")  # eviction order: oldest context first, then oldest fact


def _mem_scope(user_id: str | None) -> tuple[str, tuple]:
    """STRICT owner predicate for memory rows — no NULL grandfathering (two users share
    nothing). user_id None = the open-mode anonymous user's own rows."""
    if user_id is None:
        return "user_id IS NULL", ()
    return "user_id = ?", (user_id,)


class RunStore(_db_mod.AsyncStoreMixin):
    def __init__(self, path: str = ":memory:", database_url: str = "",
                 lease_ttl: float = 600, pool: dict | None = None, redis_url: str = "") -> None:
        from . import db as _db

        self._db, self._pg = _db.connect(database_url, path)  # sync conn: init DDL + PG partitions
        self._pool = _db.make_async_pool(database_url, **(pool or {}))  # async pool: runtime queries
        self._partitions_month = None  # month-gate for ensure_event_partitions (C5)
        self._database_url = database_url                     # kept for the dream advisory-lock conn
        self._run_month: dict[str, str] = {}  # run_id → 'YYYY-MM' (PG events partition key cache)
        # Lease-based recovery (steal 3): this replica's holder id + how long a lease lives without
        # a renewing event. A run holds a lease renewed on every publish(); the reaper reclaims any
        # lease that expires (silent run = dead process). ttl == run_timeout so staleness semantics
        # match the old reaper exactly (no event for run_timeout → reclaimed).
        self._holder = uuid.uuid4().hex[:8]
        self._lease_ttl = lease_ttl
        if self._pg:
            self._pg_create_events()  # partitioned events BEFORE executescript no-ops the plain one
        self._db.executescript(_SCHEMA)
        # Guarded ALTERs (no migration framework; DB is often ephemeral). Postgres supports
        # ADD COLUMN IF NOT EXISTS; sqlite raises OperationalError on a dup, which we swallow.
        ine = "IF NOT EXISTS " if self._pg else ""
        # Multi-turn lineage: conv_id = the run_id of turn 1 (see docs/plans multi-turn).
        # lane (companion plan Decision 4): 'work' (driver runs, NULL = legacy work) | 'chat'
        # (companion turns) — the 409 turn guard fires same-lane only.
        # model: the answering model for a chat turn (per-turn provenance for the chat surface —
        # work turns keep model_id per-task in tasks_json, so this stays NULL for them).
        for col, decl in (("conv_id", "TEXT"), ("turn", "INTEGER"), ("lane", "TEXT"),
                          ("model", "TEXT")):
            self._alter(f"ALTER TABLE sessions ADD COLUMN {ine}{col} {decl}")
        # Per-user scoping (Decision 6): every row carries its owner; legacy rows read NULL.
        for table in ("sessions", "lists", "canvas_objects", "canvas_positions", "bindles",
                      "feedback"):
            self._alter(f"ALTER TABLE {table} ADD COLUMN {ine}user_id TEXT")
        # Canvas structure: a position's surface is a parent key ('' = default world).
        self._alter(f"ALTER TABLE canvas_positions ADD COLUMN {ine}parent TEXT NOT NULL DEFAULT ''")
        # Optional card width (list cards send it; NULL = the kind's default width in the UI).
        self._alter(f"ALTER TABLE canvas_positions ADD COLUMN {ine}w REAL")
        # Optional card height (2026-07-04, list internal-scroll + resize) — same semantics as w.
        self._alter(f"ALTER TABLE canvas_positions ADD COLUMN {ine}h REAL")
        # Write provenance (V17 / Axiom 9): who last wrote this row — 'user' (a hand), 'agent'
        # (Toto/MCP/pi), 'operator'. Nullable; legacy rows read NULL (provenance unknown, not a lie).
        self._alter(f"ALTER TABLE canvas_positions ADD COLUMN {ine}actor TEXT")
        self._alter(f"ALTER TABLE canvas_objects ADD COLUMN {ine}actor TEXT")
        # List-item done state ('' | 'doing' | 'done') for the prod list done-toggles.
        self._alter(f"ALTER TABLE list_items ADD COLUMN {ine}status TEXT NOT NULL DEFAULT ''")
        # Typed artifact envelope on companion tool receipts (steal 1): sha256/evidence/confidence/
        # produced_by alongside the short result summary — content-addressable receipts.
        self._alter(f"ALTER TABLE companion_tool_calls ADD COLUMN {ine}artifact TEXT NOT NULL DEFAULT '{{}}'")
        # Lease columns on sessions (steal 3). lease_expires is epoch seconds → DOUBLE on PG:
        # its float4 REAL keeps only ~7 digits and would round a ~1.7e9 epoch to ~100s buckets.
        self._alter(f"ALTER TABLE sessions ADD COLUMN {ine}lease_holder TEXT")
        _leasecol = "DOUBLE PRECISION" if self._pg else "REAL"
        self._alter(f"ALTER TABLE sessions ADD COLUMN {ine}lease_expires {_leasecol}")
        # preferences went global (key PK) -> per-user ((user_id, key) PK). Can't reshape a PK via
        # ALTER; no legacy data to preserve, so drop+recreate an old single-column-PK table once.
        self._migrate_preferences_per_user(ine)
        self.schema_version = self._stamp_schema_version()  # PT-D: forward-only version anchor
        self._db.commit()
        self._cleanup_orphan_positions()  # V14: purge position rows whose object is already gone
        if self._pg:
            self._pg_optimize()
        self._lock = threading.Lock()
        from .wake import make_wake_bus

        self._wake = make_wake_bus(database_url, redis_url)  # fan-out seam: in-proc | PG | Redis

    async def wake_start(self) -> None:
        await self._wake.start()  # app lifespan: arms the PG listener (no-op in SQLite mode)

    async def wake_stop(self) -> None:
        await self._wake.stop()

    def wake_armed(self) -> bool:
        """True when the SSE fan-out listener is live (in-proc: always; PG: LISTEN task alive)."""
        return self._wake.armed()

    def _stamp_schema_version(self) -> str:
        """Stamp SCHEMA_VERSION into meta on first boot; a re-boot is a no-op (ON CONFLICT DO
        NOTHING — the first writer across a racing replica set wins). Returns the value now on
        record (this replica's stamp, or an existing older/newer one). Dual-dialect: ON CONFLICT
        DO NOTHING is valid on both SQLite and Postgres."""
        self._db.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES ('schema_version', ?, ?) "
            "ON CONFLICT (key) DO NOTHING",
            (SCHEMA_VERSION, time.time()),
        )
        row = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return row["value"] if row else SCHEMA_VERSION

    def _alter(self, sql: str) -> None:
        try:
            self._db.execute(sql)
        except sqlite3.OperationalError:
            pass  # sqlite: column already exists (PG uses IF NOT EXISTS → no error)

    def _migrate_preferences_per_user(self, ine: str) -> None:
        """One-shot: an old preferences table (key PK, no user_id) → new (user_id, key) PK. Fresh
        DBs already have user_id via _SCHEMA, so this no-ops. No legacy data preserved (Decision)."""
        if self._pg:
            has_user = self._db.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'preferences' AND column_name = 'user_id'").fetchone()
        else:
            has_user = any(c["name"] == "user_id"
                           for c in self._db.execute("PRAGMA table_info(preferences)").fetchall())
        if has_user:
            return
        self._db.execute("DROP TABLE preferences")
        self._db.execute(f"CREATE TABLE {ine}preferences (user_id TEXT, key TEXT NOT NULL, "
                         "value TEXT NOT NULL, PRIMARY KEY (user_id, key))")

    def _cleanup_orphan_positions(self) -> None:
        """One-shot on every boot (idempotent, cheap): delete canvas_positions rows whose object no
        longer exists (V14 / Axiom 7 — an orphan row renders as a phantom Mission Control dot / pile
        badge, the screen lying about state). Only GENERIC kinds are checked against canvas_objects;
        own-table kinds (list/session/bindle) are left alone — they cascade in their own delete
        paths and don't live in canvas_objects, so they'd all look 'orphaned' here. New generic kinds
        are covered automatically (they're just 'not an own-table kind'). Runs sync at init like the
        migrations, using self._db directly (before the async lock exists)."""
        own = ",".join(["?"] * len(_OWN_TABLE_KINDS))
        self._db.execute(
            f"DELETE FROM canvas_positions WHERE kind NOT IN ({own}) AND NOT EXISTS "
            "(SELECT 1 FROM canvas_objects o WHERE o.kind = canvas_positions.kind "
            "AND o.object_id = canvas_positions.object_id)",
            tuple(_OWN_TABLE_KINDS),
        )
        self._db.commit()

    def _pg_create_events(self) -> None:
        """PG only: events is LIST-partitioned by run_month (the run's CREATED month, constant per
        run). The partition key MUST be in the PK — but because run_month is run-constant, ALL of a
        run's events live in ONE partition, so (run_id, seq) stays globally unique and the
        seq-conflict-retry in publish() still detects races. Created BEFORE executescript so its
        plain `CREATE TABLE IF NOT EXISTS events` no-ops. Assumes a fresh PG (day-0 cutover); an
        existing plain-events DB would need a manual re-partition (not a concern per Alex).
        Retention: A4.2 keeps spans, so it's a plain DELETE of deltas; whole-partition DROP (drops
        spans too) is the rung-3 upgrade — the partitions exist now so that's a one-liner later."""
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts DOUBLE PRECISION NOT NULL, "
            "kind TEXT NOT NULL, payload TEXT NOT NULL, run_month TEXT NOT NULL, "
            "PRIMARY KEY (run_id, seq, run_month)) PARTITION BY LIST (run_month)"
        )
        self.ensure_event_partitions()

    def ensure_event_partitions(self) -> None:
        """Create the current + next month partitions (idempotent). Called at init and each reaper
        tick — no pg_partman, no cron. No-op in SQLite mode.

        C5 month-gate: the 60s reaper calls this every tick, but the partitions only change on a
        month rollover. Cache the (current, next) months and short-circuit when unchanged, so the
        common tick does ZERO blocking PG DDL on the event loop — the DDL round-trip runs only at
        init and the ~monthly rollover, not every 60s."""
        if not self._pg:
            return
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        nxt = (now.replace(day=1) + datetime.timedelta(days=32))
        months = (now.strftime("%Y-%m"), nxt.strftime("%Y-%m"))
        if months == self._partitions_month:
            return  # steady-state reaper tick: partitions already ensured this month, no DDL
        for month in set(months):
            part = "events_" + month.replace("-", "_")
            self._db.execute(f"CREATE TABLE IF NOT EXISTS {part} PARTITION OF events "
                             f"FOR VALUES IN ('{month}')")
        # Forever-lived partition for the synthetic board:{user} channels (see _run_month_of).
        self._db.execute("CREATE TABLE IF NOT EXISTS events_board PARTITION OF events "
                         "FOR VALUES IN ('board')")
        self._db.commit()
        self._partitions_month = months

    async def _run_month_of(self, run_id: str) -> str:
        """The run's created month 'YYYY-MM' (the events partition key). Cache, DB fallback (the
        reaper may finish a run it didn't create)."""
        import datetime

        # board:{user} is a synthetic, forever-lived partition key — it has no session row and
        # must NOT rotate monthly, or its seq counter would reset and break Last-Event-ID de-dupe.
        # Pin it to a constant bucket (matching the events_board partition ensure_event_partitions makes).
        if run_id.startswith("board:"):
            return "board"
        m = self._run_month.get(run_id)
        if m is None:
            row = await self._one("SELECT created_at FROM sessions WHERE run_id = ?", (run_id,))
            ts = row["created_at"] if row else time.time()
            m = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m")
            self._run_month[run_id] = m
        return m

    def _pg_optimize(self) -> None:
        """Postgres-only scale prep (SQLite untouched): JSONB for the queried-into payload +
        indexes matched to REAL store queries. All idempotent (guarded / IF NOT EXISTS)."""
        # canvas_objects.payload → JSONB (member filters / containment queries land later). Reads
        # stay str via the jsonb→text loader in db.py, so json.dumps/loads at the boundary is unchanged.
        dt = self._db.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'canvas_objects' AND column_name = 'payload'"
        ).fetchone()
        if dt and dt["data_type"] != "jsonb":
            self._db.execute("ALTER TABLE canvas_objects ALTER COLUMN payload TYPE JSONB "
                             "USING payload::jsonb")
        for idx in (
            # get_positions(parent=) + count_children(parent): filter by surface
            "CREATE INDEX IF NOT EXISTS ix_positions_parent ON canvas_positions (parent)",
            # get_turns / get_history / list_sessions: group a conversation by its resolved key
            # (the WHERE uses COALESCE(NULLIF(conv_id,''), run_id) — an EXPRESSION index matches it,
            #  which a plain conv_id index would not; that's the actual query, so index the expression)
            "CREATE INDEX IF NOT EXISTS ix_sessions_conv "
            "ON sessions ((COALESCE(NULLIF(conv_id, ''), run_id)))",
            # list_sessions: WHERE (user_id ...) ORDER BY created_at
            "CREATE INDEX IF NOT EXISTS ix_sessions_user_created ON sessions (user_id, created_at DESC)",
            # list_lists: WHERE user_id
            "CREATE INDEX IF NOT EXISTS ix_lists_user ON lists (user_id)",
            # get_objects: WHERE user_id
            "CREATE INDEX IF NOT EXISTS ix_objects_user ON canvas_objects (user_id)",
            # documents_for: WHERE user_id ORDER BY created_at DESC
            "CREATE INDEX IF NOT EXISTS ix_documents_user_created ON documents (user_id, created_at DESC)",
            # stale_running_sessions (reaper) + turn-409 checks touch only the hot 'running' subset
            "CREATE INDEX IF NOT EXISTS ix_sessions_running ON sessions (status) WHERE status = 'running'",
        ):
            self._db.execute(idx)
        self._db.commit()

    # --- sessions -------------------------------------------------------------

    async def create(self, run_id: str, query: str, conv_id: str | None = None, turn: int = 1,
               user_id: str | None = None, lane: str = "work") -> None:
        now = time.time()
        if self._pg:  # cache the run's month so publish() needn't look it up (events partition key)
            import datetime

            self._run_month[run_id] = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m")
        await self._exec(
            "INSERT INTO sessions (run_id, query, created_at, conv_id, turn, user_id, lane, "
            "lease_holder, lease_expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, query, now, conv_id or run_id, turn, user_id, lane,
             self._holder, now + self._lease_ttl),  # steal 3: run starts holding a lease
        )

    async def finish(self, run_id: str, *, status: str, answer: str = "", error: str = "",
               tasks: list[dict] | None = None, cost_total: float | None = None,
               model: str | None = None) -> None:
        """Mark terminal state and publish the terminal event — silence is never the signal."""
        sets = ["status = ?", "answer = ?", "error = ?", "tasks_json = ?"]
        args: list[Any] = [status, answer, error, json.dumps(tasks or [])]
        if cost_total is not None:
            sets.append("cost_total = ?")
            args.append(cost_total)
        if model is not None:  # chat-turn provenance; None leaves the column NULL (fail-open)
            sets.append("model = ?")
            args.append(model)
        args.append(run_id)
        await self._exec(f"UPDATE sessions SET {', '.join(sets)} WHERE run_id = ?", args)
        # A cooperative interrupt (voice-agent plan) finishes status='cancelled' → its own terminal
        # kind (replay-safe: run_cancelled is in TERMINAL_KINDS, so subscribe stops after it too).
        kind = {"done": "run_done", "cancelled": "run_cancelled"}.get(status, "run_failed")
        await self.publish(run_id, kind, {"status": status, "error": error, "cost_total": cost_total})

    async def stale_running_sessions(self, older_than_seconds: float) -> list[str]:
        """run_ids of sessions still 'running' whose latest event (or created_at, if none) is
        older than the cutoff. Kept as a store-level query (tests use it); the reaper itself now
        reclaims by lease (reclaim_expired_leases) — cheaper and multi-replica-atomic.
        ponytail: correlated subquery — fine at session scale, and portable to Postgres."""
        cutoff = time.time() - older_than_seconds
        rows = await self._all(
            "SELECT s.run_id FROM sessions s WHERE s.status = 'running' AND "
            "COALESCE((SELECT MAX(e.ts) FROM events e WHERE e.run_id = s.run_id), s.created_at) < ?",
            (cutoff,),
        )
        return [r["run_id"] for r in rows]

    async def reclaim_expired_leases(self, now: float | None = None) -> list[str]:
        """Steal 3: atomically claim every running run whose lease has expired, transferring it to
        THIS replica, and return the ids won. Multi-replica-correct: the UPDATE ... RETURNING is
        atomic per row, so two reapers can't both reclaim the same run — the loser's WHERE no
        longer matches (lease_expires was pushed into the future). The reaper then finish()es each
        id (marks it failed), which flips status out of 'running' so it's never reclaimed twice.
        Legacy running rows with a NULL lease (created before this column) are reclaimed too."""
        now = time.time() if now is None else now
        exp = now + self._lease_ttl
        sql = ("UPDATE sessions SET lease_holder = ?, lease_expires = ? "
               "WHERE status = 'running' AND (lease_expires < ? OR lease_expires IS NULL) "
               "RETURNING run_id")
        # UPDATE ... RETURNING needs an explicit commit on sqlite (the _all read helper doesn't
        # commit), so dual-branch inline like delete_object rather than routing through _all.
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_db_mod._PgConn._t(sql), (self._holder, exp, now))
                rows = await cur.fetchall()
                return [r["run_id"] for r in rows]
        with self._lock:
            rows = self._db.execute(sql, (self._holder, exp, now)).fetchall()
            self._db.commit()
            return [r["run_id"] for r in rows]

    async def list_sessions(self, user_id: str | None = None) -> list[dict]:
        """One row per CONVERSATION (not per run): turn-1 query, n_turns, summed cost, latest
        status, last_activity. ponytail: group in Python — dozens-of-sessions scale, obviously
        correct vs a window query; swap to SQL windows only if the list ever gets huge."""
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT run_id, query, status, cost_total, created_at, conv_id, turn "
            "FROM sessions " + (f"WHERE {clause} " if clause else "") + "ORDER BY created_at ASC",
            params,
        )
        convs: dict[str, dict] = {}
        for r in rows:
            conv = r["conv_id"] or r["run_id"]  # NULL/'' fallback → run_id (turn 1)
            c = convs.setdefault(conv, {"conv_id": conv, "query": r["query"], "n_turns": 0,
                                        "cost_total": 0.0, "last_activity": r["created_at"],
                                        "status": r["status"]})
            c["n_turns"] += 1
            c["cost_total"] += r["cost_total"] or 0.0
            c["last_activity"] = r["created_at"]  # ASC order → last row wins
            c["status"] = r["status"]             # ASC order → latest turn's status
            if (r["turn"] or 1) == 1:
                c["query"] = r["query"]           # turn-1 query is the conversation title
        return sorted(convs.values(), key=lambda c: c["last_activity"], reverse=True)

    def _session_dict(self, row) -> dict:
        out = dict(row)
        out["tasks"] = json.loads(out.pop("tasks_json"))
        out["conv_id"] = out.get("conv_id") or out["run_id"]  # NULL fallback (old rows)
        out["turn"] = out.get("turn") or 1
        out["lane"] = out.get("lane") or "work"               # NULL fallback = legacy work run
        return out

    async def get_session(self, run_id: str, user_id: str | None = None) -> dict | None:
        clause, params = _scope(user_id)
        row = await self._one(
            "SELECT * FROM sessions WHERE run_id = ?" + (f" AND {clause}" if clause else ""),
            (run_id, *params),
        )
        return self._session_dict(row) if row is not None else None

    async def sft_pairs(self, lane: str = "economy", limit: int = 0) -> list[dict]:
        """Real (query, answer) pairs for fine-tuning, from COMPLETED sessions whose execution
        stayed on `lane` ("economy" = the cheap tier). Historical rows written before the
        lane/tier split hold the old value "local"; those won't match "economy" — pass lane="local"
        to mine them. The ONLY place this content lives — the
        trace sink (TraceRecord) stores tokens/cost/model, never prompt/answer text. A session
        qualifies when it's done, has non-empty query+answer, and every executed (non-blocked)
        task ran on `lane`. Newest first. ponytail: lane filter in Python off tasks_json —
        sessions are dozens-scale; push to SQL only if this ever scans millions."""
        rows = await self._all(
            "SELECT query, answer, tasks_json FROM sessions "
            "WHERE status = 'done' AND answer != '' AND query != '' ORDER BY created_at DESC"
        )
        pairs: list[dict] = []
        for r in rows:
            executed = [t for t in json.loads(r["tasks_json"]) if not t.get("blocked")]
            if not executed or any((t.get("lane") or "") != lane for t in executed):
                continue
            pairs.append({"query": r["query"], "answer": r["answer"]})
            if limit and len(pairs) >= limit:
                break
        return pairs

    # --- conversations (multi-turn) -------------------------------------------

    async def conv_of(self, run_id: str) -> str | None:
        """Resolve any turn's run_id to its conversation id (turn-1 run_id). None if unknown."""
        row = await self._one("SELECT conv_id FROM sessions WHERE run_id = ?", (run_id,))
        if row is None:
            return None
        return row["conv_id"] or run_id  # NULL fallback = this run is its own conversation

    async def get_turns(self, conv_id: str, user_id: str | None = None) -> list[dict]:
        """Full session snapshots for a conversation, ordered by turn. Accepts any turn's id.
        Strictly scoped to the caller's OWN turns — never another user's, never a NULL-owner
        turn (even a shared NULL root turn in the same conv tree is withheld). Fail closed."""
        conv = await self.conv_of(conv_id)
        if conv is None:
            return []
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT * FROM sessions WHERE COALESCE(NULLIF(conv_id, ''), run_id) = ? "
            + (f"AND {clause} " if clause else "") + "ORDER BY COALESCE(turn, 1)",
            (conv, *params)
        )
        return [self._session_dict(r) for r in rows]

    async def get_history(self, conv_id: str, before_turn: int, max_chars: int | None = None,
                    user_id: str | None = None) -> list[dict]:
        """[{query, answer}] for DONE turns before before_turn, ordered. Failed turns are
        excluded from model context. With max_chars: under the cap, ALL turns (append-only —
        provider caches love a stable prefix); over it, evict oldest whole turns down to HALF
        the cap in one block (hysteresis, context-caching plan Decision 1) — the prefix then
        stays byte-stable for many turns between evictions instead of sliding every turn.
        At least the most recent turn is always kept. Strictly scoped to the caller's OWN
        turns — never another user's, never NULL-owner (fail closed)."""
        conv = await self.conv_of(conv_id)
        if conv is None:
            return []
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT query, answer FROM sessions WHERE COALESCE(NULLIF(conv_id, ''), run_id) = ? "
            "AND COALESCE(turn, 1) < ? AND status = 'done' "
            + (f"AND {clause} " if clause else "") + "ORDER BY COALESCE(turn, 1)",
            (conv, before_turn, *params)
        )
        pairs = [{"query": r["query"], "answer": r["answer"]} for r in rows]
        if max_chars is None:
            return pairs
        # Replay the eviction walk over the whole turn sequence: deterministic, so turn N+1's
        # window == turn N's window + the new turn, UNLESS that turn tripped the cap — which
        # is exactly the hysteresis (a naive "trim current total to half" would re-anchor the
        # window every call and slide the prefix each turn).
        kept: list[dict] = []
        total = 0
        for p in pairs:
            kept.append(p)
            total += len(p["query"]) + len(p["answer"])
            if total > max_chars:
                while len(kept) > 1 and total > max_chars // 2:  # block-evict oldest first
                    total -= len(kept[0]["query"]) + len(kept[0]["answer"])
                    kept.pop(0)
        return kept

    # --- companion (memory / tool audit / eternal conv) -------------------------
    # docs/plans/2026-07-03-toto-companion-agent.md — the agent is a row, not a resident.

    async def memory_rows(self, user_id: str | None = None) -> list[dict]:
        """The user's whole memory block, oldest-first (it all fits under the cap — no
        retrieval in P0). Strictly scoped: two users share nothing."""
        clause, params = _mem_scope(user_id)
        rows = await self._all(
            "SELECT memory_id, kind, content, source_run, created_at FROM user_memory "
            f"WHERE {clause} ORDER BY created_at",
            params,
        )
        return [dict(r) for r in rows]

    async def memory_write(self, user_id: str | None, kind: str, content: str,
                     source_run: str = "") -> dict:
        """Insert one memory row, enforcing the caps HERE (never trusted to the model):
        ≤ MEMORY_MAX_ROWS rows and ≤ MEMORY_MAX_CHARS content chars per user. Over cap →
        evict oldest context first, then oldest fact; preference/instruction are never
        auto-evicted — if they alone exceed the cap the write is refused.
        Returns {"memory_id", "evicted": [ids]} or {"error": ...}."""
        if kind not in MEMORY_KINDS:
            return {"error": f"kind must be one of {MEMORY_KINDS}"}
        content = " ".join(content.split())
        if not content:
            return {"error": "content must be non-empty"}
        rows = await self.memory_rows(user_id)
        evictable = [r for k in _MEMORY_EVICTABLE for r in rows if r["kind"] == k]
        evicted: list[dict] = []

        def over(kept: list[dict]) -> bool:
            return (len(kept) + 1 > MEMORY_MAX_ROWS or
                    sum(len(r["content"]) for r in kept) + len(content) > MEMORY_MAX_CHARS)

        kept = list(rows)
        while over(kept) and evictable:
            e = evictable.pop(0)
            evicted.append(e)
            kept = [r for r in kept if r["memory_id"] != e["memory_id"]]
        if over(kept):  # only preference/instruction left — refuse rather than evict them
            return {"error": "memory full: preference/instruction rows are never auto-evicted; "
                             "delete some memories first"}
        for e in evicted:
            await self._exec("DELETE FROM user_memory WHERE memory_id = ?", (e["memory_id"],))
        memory_id = uuid.uuid4().hex[:12]
        await self._exec(
            "INSERT INTO user_memory (memory_id, user_id, kind, content, source_run, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (memory_id, user_id, kind, content, source_run, time.time()),
        )
        return {"memory_id": memory_id, "evicted": [e["memory_id"] for e in evicted]}

    async def memory_delete(self, memory_id: str, user_id: str | None = None) -> bool:
        """Erase one memory row (SOC2: enumerable/erasable). Owner-scoped; True if it existed."""
        clause, params = _mem_scope(user_id)
        # rowcount needs the raw cursor — same dual-branch shape as delete_object.
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_db_mod._PgConn._t(
                    f"DELETE FROM user_memory WHERE memory_id = ? AND {clause}"),
                    (memory_id, *params))
                return cur.rowcount > 0
        with self._lock:
            cur = self._db.execute(
                f"DELETE FROM user_memory WHERE memory_id = ? AND {clause}",
                (memory_id, *params),
            )
            self._db.commit()
            return cur.rowcount > 0

    async def log_tool_call(self, run_id: str, user_id: str | None, tool: str, args: dict,
                      result: str, artifact: dict | None = None) -> None:
        """Audit one companion tool call — receipts are the brand. `result` is a short outcome
        summary, truncated by the caller. `artifact` (steal 1) is the typed envelope
        (sha256/evidence/confidence/produced_by) for the receipt; {} when none."""
        await self._exec(
            "INSERT INTO companion_tool_calls (call_id, run_id, user_id, tool, args_json, "
            "result, created_at, artifact) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], run_id, user_id, tool, json.dumps(args, default=str),
             result, time.time(), json.dumps(artifact or {}, default=str)),
        )

    async def tool_calls_for_run(self, run_id: str) -> list[dict]:
        rows = await self._all(
            "SELECT tool, args_json, result, created_at, artifact FROM companion_tool_calls "
            "WHERE run_id = ? ORDER BY created_at", (run_id,),
        )
        return [{**dict(r), "args": json.loads(r["args_json"]),
                 "artifact": json.loads(r["artifact"] or "{}")} for r in rows]

    # --- custom tools (docs/plans/2026-07-06-tool-contract.md §3) ---------------
    # STRICT per-user scoping via _mem_scope (like user_memory): a cross-user get is a miss, a
    # write stamps + is keyed on the owner. name is unique per (user_id, name) so re-PUT of the
    # same name is an owner-scoped overwrite (import == upsert).

    async def create_custom_tool(self, user_id: str | None, name: str, description: str,
                                 spec: dict, version: int,
                                 max_tools: int = 50) -> dict:
        """Upsert one custom tool. version must not decrease on an existing name; a NEW name is
        refused once the user is at `max_tools`. Returns {"tool_id", "created": bool} or
        {"error": ...}. Owner-scoped throughout (get/count read only this user's rows)."""
        existing = await self.get_custom_tool(user_id, name)
        if existing is not None and version < existing["version"]:
            return {"error": f"version must not decrease (stored {existing['version']}, got {version})"}
        if existing is None and await self.count_custom_tools(user_id) >= max_tools:
            return {"error": f"custom tool limit reached ({max_tools}) — delete one first"}
        now = time.time()
        # Read-then-branch UPDATE/INSERT (ponytail: single-operator scale). Not an ON CONFLICT upsert
        # because the operator path stamps user_id NULL, and SQLite treats NULLs as DISTINCT in the
        # UNIQUE(user_id, name) index — the conflict would never fire and a reused tool_id would hit
        # the PK. The unique index stays as a backstop against a concurrent double-insert for real users.
        if existing is not None:
            await self._exec(
                "UPDATE custom_tools SET description = ?, spec = ?, version = ?, updated_at = ? "
                "WHERE tool_id = ?",
                (description, json.dumps(spec), version, now, existing["tool_id"]),
            )
            return {"tool_id": existing["tool_id"], "created": False}
        tool_id = uuid.uuid4().hex[:12]
        await self._exec(
            "INSERT INTO custom_tools (tool_id, user_id, name, description, spec, version, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tool_id, user_id, name, description, json.dumps(spec), version, now, now),
        )
        return {"tool_id": tool_id, "created": True}

    async def get_custom_tool(self, user_id: str | None, name: str) -> dict | None:
        """One tool by name, owner-scoped (cross-user = None, never a leak). spec is parsed."""
        clause, params = _mem_scope(user_id)
        row = await self._one(
            f"SELECT tool_id, name, description, spec, version, created_at, updated_at "
            f"FROM custom_tools WHERE name = ? AND {clause}", (name, *params))
        if row is None:
            return None
        return {**dict(row), "spec": json.loads(row["spec"])}

    async def list_custom_tools(self, user_id: str | None) -> list[dict]:
        """This user's tools, newest-first, specs parsed — the wake load + the REST list."""
        clause, params = _mem_scope(user_id)
        rows = await self._all(
            f"SELECT tool_id, name, description, spec, version, created_at, updated_at "
            f"FROM custom_tools WHERE {clause} ORDER BY created_at DESC", params)
        return [{**dict(r), "spec": json.loads(r["spec"])} for r in rows]

    async def count_custom_tools(self, user_id: str | None) -> int:
        clause, params = _mem_scope(user_id)
        row = await self._one(
            f"SELECT COUNT(*) AS n FROM custom_tools WHERE {clause}", params)
        return int(row["n"]) if row else 0

    async def delete_custom_tool(self, user_id: str | None, name: str) -> bool:
        """Delete one tool by name, owner-scoped. True iff a row was removed (rowcount, like
        memory_delete's dual-branch shape)."""
        clause, params = _mem_scope(user_id)
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_db_mod._PgConn._t(
                    f"DELETE FROM custom_tools WHERE name = ? AND {clause}"), (name, *params))
                return cur.rowcount > 0
        with self._lock:
            cur = self._db.execute(
                f"DELETE FROM custom_tools WHERE name = ? AND {clause}", (name, *params))
            self._db.commit()
            return cur.rowcount > 0

    async def first_chat_conv(self, user_id: str | None = None) -> str | None:
        """The user's eternal companion conversation (its turn-1 run_id), if one exists.
        users.companion_conv_id (AuthStore) is the fast path for logged-in users; this derive
        covers the open-mode anonymous user and self-heals a lost pointer. Strict scope —
        the companion thread is never shared."""
        clause, params = _mem_scope(user_id)
        row = await self._one(
            "SELECT run_id FROM sessions WHERE lane = 'chat' AND COALESCE(turn, 1) = 1 "
            f"AND {clause} ORDER BY created_at LIMIT 1", params,
        )
        return row["run_id"] if row else None

    async def recent_work_runs(self, user_id: str | None = None, limit: int = 5) -> list[dict]:
        """Newest work-lane runs for the companion's live-work context + check_status listing."""
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT run_id, query, status, cost_total, created_at FROM sessions "
            "WHERE COALESCE(lane, 'work') != 'chat'" + (f" AND {clause}" if clause else "")
            + " ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        )
        return [dict(r) for r in rows]

    async def chat_spend_today(self, user_id: str | None = None) -> float:
        """Summed cost of the user's chat-lane turns since UTC midnight — the
        TOTO_GW_COMPANION_DAILY_USD budget input (past it, turns degrade to economy)."""
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        clause, params = _mem_scope(user_id)
        row = await self._one(
            "SELECT COALESCE(SUM(cost_total), 0) AS c FROM sessions "
            f"WHERE lane = 'chat' AND created_at >= ? AND {clause}", (day0, *params),
        )
        return float(row["c"] or 0.0)

    async def log_tts(self, user_id: str | None, chars: int, cost_usd: float) -> None:
        """Receipt one TTS synthesis — the audit row AND the daily-cap input (tts_spend_today).
        Cost is char-based (known before a byte streams), so it is logged the moment the provider
        accepts the request."""
        await self._exec(
            "INSERT INTO companion_tts_usage (call_id, user_id, chars, cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], user_id, chars, cost_usd, time.time()),
        )

    async def tts_spend_today(self, user_id: str | None = None) -> float:
        """Summed TTS cost since UTC midnight — the companion_tts_daily_usd cap input (mirrors
        chat_spend_today, strict per-user scope: no NULL grandfathering)."""
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        clause, params = _mem_scope(user_id)
        row = await self._one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM companion_tts_usage "
            f"WHERE created_at >= ? AND {clause}", (day0, *params),
        )
        return float(row["c"] or 0.0)

    async def log_pipedream(self, user_id: str | None, calls: int, est_usd: float) -> None:
        """Receipt one Pipedream sync pull — the audit row (call count + estimated $). No cap/gating
        (pd-metering: count exactly, estimate $, reconcile monthly). Mirrors log_tts."""
        await self._exec(
            "INSERT INTO pipedream_usage (call_id, user_id, calls, est_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], user_id, calls, est_usd, time.time()),
        )

    async def pipedream_spend_today(self, user_id: str | None = None) -> float:
        """Summed estimated Pipedream spend since UTC midnight (strict per-user scope). Read-only
        reconciliation input — nothing gates on it (mirrors tts_spend_today)."""
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        clause, params = _mem_scope(user_id)
        row = await self._one(
            "SELECT COALESCE(SUM(est_usd), 0) AS c FROM pipedream_usage "
            f"WHERE created_at >= ? AND {clause}", (day0, *params),
        )
        return float(row["c"] or 0.0)

    # --- dreams (nightly consolidation, docs/plans/2026-07-05-memory-lifecycle.md P1) ----------

    async def active_tenants(self, since: float) -> list[str]:
        """Distinct owners with a session (chat or work turn) since `since` — the dreamer only
        wakes live tenants. Captures follow turns, so a turn is a sound activity signal. NULL-owner
        (operator/anon) rows are excluded: dreams are strictly per-user (no cross-user pass, ever)."""
        rows = await self._all(
            "SELECT DISTINCT user_id FROM sessions WHERE created_at >= ? AND user_id IS NOT NULL",
            (since,))
        return [r["user_id"] for r in rows]

    async def claim_dream_run(self, tenant_id: str, run_date: str) -> bool:
        """Atomically claim (tenant_id, run_date) for a dream pass. The winning INSERT returns the
        row (this replica owns the tenant for the day); a conflict returns nothing (already claimed
        today) → the caller skips. This IS both the idempotency key and the cross-replica leader
        guard — a second run for the same date is a no-op."""
        row = await self._one(
            "INSERT INTO dream_runs (tenant_id, run_date, status, started_at) "
            "VALUES (?, ?, 'running', ?) "
            "ON CONFLICT (tenant_id, run_date) DO NOTHING RETURNING tenant_id",
            (tenant_id, run_date, time.time()))
        return row is not None

    async def finish_dream_run(self, tenant_id: str, run_date: str, *, merged: int, archived: int,
                               cost_usd: float, status: str = "done") -> None:
        """Seal the claimed pass with its receipt (per-leg counts + cost)."""
        await self._exec(
            "UPDATE dream_runs SET status = ?, merged = ?, archived = ?, cost_usd = ?, "
            "finished_at = ? WHERE tenant_id = ? AND run_date = ?",
            (status, merged, archived, cost_usd, time.time(), tenant_id, run_date))

    async def take_dream_hint(self, user_id: str | None) -> str | None:
        """One-shot: the latest MATERIAL, not-yet-shown dream for this user (v1 tenant == user),
        marked shown so the companion volunteers it at most once (D5). None → nothing to mention
        (no dream, or a no-op dream). Never surfaces a failed or zero-change pass."""
        if not user_id:
            return None
        row = await self._one(
            "SELECT run_date, merged, archived FROM dream_runs WHERE tenant_id = ? AND shown = 0 "
            "AND status = 'done' AND (merged > 0 OR archived > 0) ORDER BY run_date DESC LIMIT 1",
            (user_id,))
        if row is None:
            return None
        await self._exec("UPDATE dream_runs SET shown = 1 WHERE tenant_id = ? AND run_date = ?",
                         (user_id, row["run_date"]))
        parts = []
        if row["merged"]:
            parts.append(f"merged {row['merged']} cluster{'' if row['merged'] == 1 else 's'} "
                         "of related notes")
        if row["archived"]:
            parts.append(f"archived {row['archived']} stale one{'' if row['archived'] == 1 else 's'}")
        return "While you were away, I tidied your memory: " + " and ".join(parts) + "."

    # --- idempotency keys (engine-hardening Wave 2, caching.md chunk A) -------------------------

    async def claim_idempotency(self, user_id: str | None, key: str, method: str, path: str):
        """Atomic first-writer claim for an Idempotency-Key. Returns "won" if THIS call inserted the
        placeholder row (the caller runs the handler then store_idempotency_result); else returns the
        existing row (dict) -- a completed row (status_code set) is replayed, an in-flight one
        (status_code NULL) means a concurrent duplicate is still running. Multi-replica correct: the
        INSERT .. ON CONFLICT DO NOTHING RETURNING is one atomic statement across the pool, exactly
        like claim_dream_run. user_id None (operator) maps to '' so the composite PK dedups on both
        dialects (SQLite treats a NULL PK part as distinct -> two operator claims would both win)."""
        row = await self._one(
            "INSERT INTO idempotency_keys (user_id, idem_key, method, path, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id, idem_key) DO NOTHING RETURNING idem_key",
            (user_id or "", key, method, path, time.time()))
        if row is not None:
            return "won"
        return await self.get_idempotency_result(user_id, key)

    async def get_idempotency_result(self, user_id: str | None, key: str):
        """The stored row for (user_id, key) or None. status_code NULL -> still in-flight."""
        return await self._one(
            "SELECT status_code, response_json, method, path FROM idempotency_keys "
            "WHERE user_id = ? AND idem_key = ?",
            (user_id or "", key))

    async def store_idempotency_result(self, user_id: str | None, key: str, status_code: int,
                                       response_json: str) -> None:
        """Seal a claimed key with its response -- subsequent claims replay this instead of executing."""
        await self._exec(
            "UPDATE idempotency_keys SET status_code = ?, response_json = ? "
            "WHERE user_id = ? AND idem_key = ?",
            (status_code, response_json, user_id or "", key))

    async def try_advisory_lock(self, key: int):
        """PG single-leader guard for a whole dreamer tick — a DEDICATED short-lived connection
        holds the session-level advisory lock for exactly the critical section (a pooled connection
        would leak the lock when recycled). Returns (locked, release-coroutine-factory). SQLite is
        single-process → always leader, no-op release. ponytail: the per-tenant claim_dream_run row
        is the real cross-replica idempotency guard; this just stops N replicas scanning in lockstep."""
        async def _noop() -> None:
            return None

        if not self._pg:
            return True, _noop
        import psycopg

        conn = await psycopg.AsyncConnection.connect(self._database_url, autocommit=True)
        try:
            cur = await conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,))
            row = await cur.fetchone()
            locked = bool(row[0] if isinstance(row, (list, tuple)) else list(row.values())[0])
        except Exception:
            await conn.close()
            return False, _noop
        if not locked:
            await conn.close()
            return False, _noop

        async def _release() -> None:
            try:
                await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
            finally:
                await conn.close()

        return True, _release

    async def dev_stats(self) -> dict:
        """Dev-dashboard landing stats (routes/dev.py /v1/dev/stats): today's run count +
        summed cost since UTC midnight, plus total companion memory rows. Read-only."""
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row = await self._one("SELECT COUNT(*) AS n, COALESCE(SUM(cost_total), 0) AS c "
                              "FROM sessions WHERE created_at >= ?", (day0,))
        mem = await self._one("SELECT COUNT(*) AS n FROM user_memory")
        return {"runs": int(row["n"]), "cost_usd": float(row["c"] or 0.0),
                "memory_rows": int(mem["n"])}

    # --- events ---------------------------------------------------------------

    async def publish(self, run_id: str, kind: str, payload: dict) -> dict:
        """Append an event (seq-numbered) and fan out to live subscribers. Costs found in
        payloads accumulate onto the session so the list view can show a live meter."""
        ts = time.time()
        blob = json.dumps(payload, default=str)
        # DO NOT flip synchronous_commit OFF for this write: a lost event is a lost SSE seq,
        # and Last-Event-ID resume would then skip it silently. Durability = resume correctness.
        cost = payload.get("cost")
        add_cost = isinstance(cost, (int, float)) and kind not in TERMINAL_KINDS
        if self._pg:
            # Allocate seq AND fire the cross-replica pg_notify in ONE round-trip (addendum #1:
            # nothing on a separate sync conn). The (run_id, seq, run_month) PK makes a
            # cross-replica race safe-by-conflict — retry on the loser. run_month is run-constant,
            # so MAX(seq) WHERE run_id AND run_month prunes to the run's single partition and
            # (run_id, seq) uniqueness holds. Autocommit pool → the pg_notify commits immediately.
            import psycopg

            run_month = await self._run_month_of(run_id)
            for _ in range(8):
                try:
                    row = await self._one(
                        "WITH ins AS ("
                        "INSERT INTO events (run_id, seq, ts, kind, payload, run_month) "
                        "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ? FROM events "
                        "WHERE run_id = ? AND run_month = ? RETURNING seq) "
                        "SELECT seq, pg_notify(?, ?) FROM ins",
                        (run_id, ts, kind, blob, run_month, run_id, run_month, _WAKE_CHANNEL, run_id),
                    )
                    seq = row["seq"]
                    break
                except psycopg.errors.UniqueViolation:
                    continue
            # Renew the lease on every event — activity keeps the run alive (steal 3). Fold the
            # cost bump into the same statement so it stays one round-trip. ponytail: one PK UPDATE
            # per event (incl. deltas); throttle if the delta storm ever dominates writes.
            lexp = ts + self._lease_ttl
            if add_cost:
                await self._exec(
                    "UPDATE sessions SET cost_total = cost_total + ?, lease_holder = ?, "
                    "lease_expires = ? WHERE run_id = ?", (cost, self._holder, lexp, run_id))
            else:
                await self._exec(
                    "UPDATE sessions SET lease_holder = ?, lease_expires = ? WHERE run_id = ?",
                    (self._holder, lexp, run_id))
        else:
            with self._lock:  # SQLite: seq alloc + insert must be atomic under the one connection
                seq = self._db.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE run_id = ?", (run_id,)
                ).fetchone()["s"] + 1
                self._db.execute(
                    "INSERT INTO events (run_id, seq, ts, kind, payload) VALUES (?, ?, ?, ?, ?)",
                    (run_id, seq, ts, kind, blob),
                )
                lexp = ts + self._lease_ttl  # steal 3: renew the lease on every event
                if add_cost:
                    self._db.execute(
                        "UPDATE sessions SET cost_total = cost_total + ?, lease_holder = ?, "
                        "lease_expires = ? WHERE run_id = ?", (cost, self._holder, lexp, run_id))
                else:
                    self._db.execute(
                        "UPDATE sessions SET lease_holder = ?, lease_expires = ? WHERE run_id = ?",
                        (self._holder, lexp, run_id))
                self._db.commit()
        self._wake.notify(run_id)  # local-wake this replica; PG replicas woke via the CTE pg_notify
        return {"seq": seq, "ts": ts, "kind": kind, "data": payload}

    async def _publish_board(self, user_id: str | None, kind: str, payload: dict) -> None:
        """Fan a canvas/list mutation out to the user's live-board SSE channel. Synthetic run_id
        `board:{user}` — an opaque partition key, so publish/subscribe/the wake bus are reused as-is."""
        await self.publish(f"board:{user_id or 'anon'}", kind, payload)

    async def board_latest_seq(self, key: str) -> int:
        """Current max seq for a channel — the SSE endpoint tails from HERE (not 0) so a fresh
        connection only sees NEW mutations; the snapshot GETs already gave it full state."""
        row = await self._one(
            "SELECT COALESCE(MAX(seq), 0) AS s FROM events WHERE run_id = ?", (key,))
        return row["s"]

    async def prune_deltas(self, older_than_days: float) -> int:
        """Delete answer_delta events of TERMINAL runs older than the cutoff (redundant once
        sessions.answer is stored; spans kept). Also drops stale rate-limit windows. Both modes."""
        cutoff = time.time() - older_than_days * 86400
        row = await self._one(
            "SELECT COUNT(*) AS n FROM events WHERE kind = 'answer_delta' AND run_id IN "
            "(SELECT run_id FROM sessions WHERE status IN ('done', 'failed') AND created_at < ?)",
            (cutoff,),
        )
        n = row["n"]
        await self._exec(
            "DELETE FROM events WHERE kind = 'answer_delta' AND run_id IN "
            "(SELECT run_id FROM sessions WHERE status IN ('done', 'failed') AND created_at < ?)",
            (cutoff,),
        )
        await self._exec("DELETE FROM rate_limits WHERE window_start < ?", (int(cutoff),))
        return n

    async def prune_user_memory(self, user_id: str, older_than: float, limit: int) -> int:
        """W3-C6 retention: delete this user's explicit memory facts created before `older_than`,
        bounded to `limit` rows this call. Returns rows deleted. Product data (memory_write / REST),
        NOT the auto-capture lane — this is the sink zero_retention leaves alone. IN-subquery with
        LIMIT is bounded on both dialects; a backlog drains over ticks."""
        return await self._exec_count(
            "DELETE FROM user_memory WHERE memory_id IN "
            "(SELECT memory_id FROM user_memory WHERE user_id = ? AND created_at < ? "
            "ORDER BY created_at LIMIT ?)",
            (user_id, float(older_than), int(limit)))

    async def events_after(self, run_id: str, after_seq: int = 0) -> list[dict]:
        rows = await self._all(
            "SELECT seq, ts, kind, payload FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, after_seq),
        )
        return [
            {"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "data": json.loads(r["payload"])}
            for r in rows
        ]

    async def subscribe(self, run_id: str, after_seq: int = 0) -> AsyncIterator[dict]:
        """Replay everything after `after_seq`, then live-tail. Ends after a terminal event.
        Register-then-replay closes the race where an event lands between replay and the first
        wake (the wake buffers in the queue). Each wake re-reads events_after — correctness from
        the DB, never the bus. A repoll timeout self-heals any missed wake (LISTEN/NOTIFY is
        at-most-once), bounding delivery latency to _REPOLL_SECONDS."""
        last = after_seq
        with self._wake.subscribe(run_id) as wake:
            for event in await self.events_after(run_id, last):
                last = event["seq"]
                yield event
                if event["kind"] in TERMINAL_KINDS:
                    return
            while True:
                try:
                    await asyncio.wait_for(wake.get(), timeout=_REPOLL_SECONDS)
                except asyncio.TimeoutError:
                    pass  # no wake in the window → re-read anyway (missed-notify safety net)
                for event in await self.events_after(run_id, last):
                    last = event["seq"]
                    yield event
                    if event["kind"] in TERMINAL_KINDS:
                        return

    # --- preferences (P2) -------------------------------------------------------

    async def get_preferences(self, user_id: str | None = None) -> dict:
        """{"optimize": str|None, "pins": {skill: model_id}, "label_models": {label: model_id}}
        — absent keys mean defaults. Per-user: a logged-in caller reads only their own rows
        (strict isolation)."""
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT key, value FROM preferences" + (f" WHERE {clause}" if clause else ""), params)
        out = {r["key"]: json.loads(r["value"]) for r in rows}
        return {"optimize": out.get("optimize"), "pins": out.get("pins") or {},
                "label_models": out.get("label_models") or {}}

    async def set_preferences(self, prefs: dict, user_id: str | None = None) -> None:
        for key in ("optimize", "pins", "label_models"):
            if key in prefs:
                await self._exec(
                    "INSERT INTO preferences (user_id, key, value) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                    (user_id, key, json.dumps(prefs[key])),
                )

    # --- feedback (P3) ------------------------------------------------------------

    async def set_feedback(self, run_id: str, task_id: str, model_id: str, skill: str,
                     verdict: str, user_id: str | None = None) -> None:
        """Idempotent per (run, task): a re-tap flips the verdict. Fail-closed ownership guard on
        the upsert — a re-tap only flips a row the caller owns; a cross-tenant (run,task) collision
        DO-UPDATEs nothing rather than overwriting another user's verdict. Null-safe so operator
        (user_id NULL) can still flip operator-written rows."""
        # SQLite: `IS` is null-safe equality; PG: `IS NOT DISTINCT FROM`. Both compare NULL=NULL true.
        guard = "feedback.user_id IS NOT DISTINCT FROM ?" if self._pg else "feedback.user_id IS ?"
        await self._exec(
            "INSERT INTO feedback (run_id, task_id, model_id, skill, verdict, created_at, "
            "user_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, task_id) DO UPDATE SET "
            "model_id = excluded.model_id, skill = excluded.skill, "
            "verdict = excluded.verdict, created_at = excluded.created_at "
            "WHERE " + guard,
            (run_id, task_id, model_id, skill, verdict, time.time(), user_id, user_id),
        )

    async def feedback_for_run(self, run_id: str, user_id: str | None = None) -> list[dict]:
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT task_id, model_id, skill, verdict FROM feedback WHERE run_id = ?"
            + (f" AND {clause}" if clause else ""),
            (run_id, *params),
        )
        return [dict(r) for r in rows]

    async def feedback_summary(self, user_id: str | None = None) -> list[dict]:
        """Labeled-data aggregates: per (model, skill), how often the user affirmed/rejected."""
        clause, params = _scope(user_id)
        # PG has no SUM(boolean); COUNT(*) FILTER is the portable count-of-matches (sqlite 3.30+
        # supports FILTER too, but SUM(bool) is the established sqlite form — keep both explicit).
        agg = ("COUNT(*) FILTER (WHERE verdict = 'up') AS up, "
               "COUNT(*) FILTER (WHERE verdict = 'down') AS down") if self._pg else \
              "SUM(verdict = 'up') AS up, SUM(verdict = 'down') AS down"
        rows = await self._all(
            "SELECT model_id, skill, " + agg + " FROM feedback "
            + (f"WHERE {clause} " if clause else "")
            + "GROUP BY model_id, skill ORDER BY model_id, skill",
            params,
        )
        return [dict(r) for r in rows]

    # --- lists (canvas Toto lists) -----------------------------------------------

    async def create_list(self, list_id: str, name: str, user_id: str | None = None) -> None:
        await self._exec(
            "INSERT INTO lists (list_id, name, created_at, user_id) VALUES (?, ?, ?, ?)",
            (list_id, name, time.time(), user_id),
        )
        await self._publish_board(user_id, "list_created", {"list_id": list_id, "name": name})

    async def add_item(self, list_id: str, item_id: str, task: str,
                       user_id: str | None = None) -> None:
        # position = MAX+1 in ONE atomic statement (was a lock-guarded read-then-insert; the pool
        # has no single-conn lock, so fold it into the INSERT — correct in both dialects).
        await self._exec(
            "INSERT INTO list_items (list_id, item_id, task, position, created_at) "
            "SELECT ?, ?, ?, COALESCE(MAX(position), 0) + 1, ? FROM list_items WHERE list_id = ?",
            (list_id, item_id, task, time.time(), list_id),
        )
        await self._publish_board(user_id, "item_added",
                                  {"list_id": list_id, "item_id": item_id, "task": task})

    async def enrich_item(self, list_id: str, item_id: str, description: str, metadata: dict,
                    model: str) -> None:
        await self._exec(
            "UPDATE list_items SET description = ?, metadata = ?, enriched_model = ? "
            "WHERE list_id = ? AND item_id = ?",
            (description, json.dumps(metadata), model, list_id, item_id),
        )

    async def set_item_status(self, list_id: str, item_id: str, status: str,
                              user_id: str | None = None) -> None:
        """Set an item's done-state ('' | 'doing' | 'done'). No-op if the item doesn't exist."""
        await self._exec(
            "UPDATE list_items SET status = ? WHERE list_id = ? AND item_id = ?",
            (status, list_id, item_id),
        )
        await self._publish_board(user_id, "item_status",
                                  {"list_id": list_id, "item_id": item_id, "status": status})

    async def delete_item(self, list_id: str, item_id: str,
                          user_id: str | None = None) -> None:
        """Remove one item from a list (the prod list's X-delete). No-op if already gone."""
        await self._exec(
            "DELETE FROM list_items WHERE list_id = ? AND item_id = ?", (list_id, item_id),
        )
        await self._publish_board(user_id, "item_deleted",
                                  {"list_id": list_id, "item_id": item_id})

    async def delete_list(self, list_id: str, user_id: str | None = None) -> bool:
        """Delete a whole list — owner-scoped, with cascade. Removes the list's items and its
        canvas position row (kind='list', if placed), then the `lists` row. Returns True iff a
        list row was removed (mirrors delete_object's scoped-rowcount shape). Ownership is proven
        by the scoped DELETE on `lists` FIRST, so children are only touched once we know the
        caller owns the list (a non-owner gets changed=False and nothing is deleted)."""
        clause, params = _scope(user_id)
        sql = "DELETE FROM lists WHERE list_id = ?" + (f" AND {clause}" if clause else "")
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_db_mod._PgConn._t(sql), (list_id, *params))
                changed = cur.rowcount > 0
        else:
            with self._lock:
                cur = self._db.execute(sql, (list_id, *params))
                self._db.commit()
                changed = cur.rowcount > 0
        if not changed:
            return False
        await self._exec("DELETE FROM list_items WHERE list_id = ?", (list_id,))
        await self._exec("DELETE FROM canvas_positions WHERE kind = 'list' AND object_id = ?",
                         (list_id,))
        await self._publish_board(user_id, "list_deleted", {"list_id": list_id})
        return True

    async def update_item(self, list_id: str, item_id: str, *, task: str | None = None,
                          description: str | None = None, metadata: dict | None = None,
                          user_id: str | None = None) -> bool:
        """Edit an item's task/description/metadata in place — only the provided fields. Owner-
        scoped through the parent list (list_items has no user_id of its own); True if a row
        changed. rowcount needs the raw cursor — same dual-branch shape as delete_object."""
        sets, vals = [], []
        if task is not None:
            sets.append("task = ?"); vals.append(task)
        if description is not None:
            sets.append("description = ?"); vals.append(description)
        if metadata is not None:
            sets.append("metadata = ?"); vals.append(json.dumps(metadata))
        if not sets:
            return False
        clause, params = _scope(user_id)
        scope = f" AND list_id IN (SELECT list_id FROM lists WHERE {clause})" if clause else ""
        sql = f"UPDATE list_items SET {', '.join(sets)} WHERE list_id = ? AND item_id = ?" + scope
        args = (*vals, list_id, item_id, *params)
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_db_mod._PgConn._t(sql), args)
                changed = cur.rowcount > 0
        else:
            with self._lock:
                cur = self._db.execute(sql, args)
                self._db.commit()
                changed = cur.rowcount > 0
        if changed:
            await self._publish_board(user_id, "item_updated",
                                      {"list_id": list_id, "item_id": item_id})
        return changed

    async def get_list(self, list_id: str, user_id: str | None = None) -> dict | None:
        clause, params = _scope(user_id)
        row = await self._one(
            "SELECT * FROM lists WHERE list_id = ?" + (f" AND {clause}" if clause else ""),
            (list_id, *params),
        )
        if row is None:
            return None
        items = await self._all(
            "SELECT * FROM list_items WHERE list_id = ? ORDER BY position", (list_id,)
        )
        out = dict(row)
        out["items"] = [{**dict(i), "metadata": json.loads(i["metadata"])} for i in items]
        return out

    async def list_lists(self, user_id: str | None = None) -> list[dict]:
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT l.*, COUNT(i.item_id) AS n_items, "
            "SUM(CASE WHEN i.enriched_model != '' THEN 1 ELSE 0 END) AS n_enriched "
            "FROM lists l LEFT JOIN list_items i ON i.list_id = l.list_id "
            + (f"WHERE {clause.replace('user_id', 'l.user_id')} " if clause else "")
            + "GROUP BY l.list_id ORDER BY l.created_at DESC",
            params,
        )
        return [dict(r) for r in rows]

    # --- canvas positions (Miro-style spatial canvas) --------------------------

    async def get_positions(self, user_id: str | None = None,
                      parent: str | None = None) -> list[dict]:
        """Positions, optionally filtered to one surface (parent). parent=None → all surfaces
        (backward compatible); parent='' → the default world; parent=<id> → inside a container."""
        clause, params = _scope(user_id)
        wheres = ([clause] if clause else []) + (["parent = ?"] if parent is not None else [])
        args = list(params) + ([parent] if parent is not None else [])
        sql = "SELECT kind, object_id, x, y, z, parent, w, h, actor FROM canvas_positions"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        return [dict(r) for r in await self._all(sql, args)]

    async def set_positions(self, rows: list[dict], user_id: str | None = None,
                            actor: str | None = None) -> None:
        """Batch upsert positions keyed on (kind, object_id); one updated_at for the batch.
        user_id stamps new rows and is preserved on update (owner never reassigned). `parent`
        (default '') is the object's surface — it IS overwritten on update, so a PUT must carry
        the row's current parent or the object jumps back to the world. `w`/`h` (card
        width/height) are the OPPOSITE: absent → PRESERVED (COALESCE keeps the stored value),
        because only list cards send a size and a generic drag PUT shouldn't blow it away.
        Fail-closed owner guard on the upsert: a (kind, object_id) collision only overwrites a
        row the caller owns — a foreign or NULL-owner collision DO-UPDATEs nothing (no
        cross-tenant move/reparent). Null-safe so the operator path (user_id NULL) still
        updates its own NULL-owner rows."""
        now = time.time()
        # SQLite: `IS` is null-safe equality; PG: `IS NOT DISTINCT FROM`. Both compare NULL=NULL true.
        guard = ("canvas_positions.user_id IS NOT DISTINCT FROM excluded.user_id" if self._pg
                 else "canvas_positions.user_id IS excluded.user_id")
        # actor (V17): stamped on insert AND refreshed on update — the row reflects its LATEST
        # writer, which is what a provenance chip renders. ponytail: last-writer-wins on actor too,
        # no conflict resolution (Axiom V1/V2) — this round just lands the field so a future round
        # can arbitrate; the write layer still keeps whoever wrote last.
        await self._many(
            "INSERT INTO canvas_positions (kind, object_id, x, y, z, parent, w, h, updated_at, user_id, actor) "
            "VALUES (:kind, :object_id, :x, :y, :z, :parent, :w, :h, :updated_at, :user_id, :actor) "
            "ON CONFLICT(kind, object_id) DO UPDATE SET "
            "x = excluded.x, y = excluded.y, z = excluded.z, parent = excluded.parent, "
            "w = COALESCE(excluded.w, canvas_positions.w), "
            "h = COALESCE(excluded.h, canvas_positions.h), updated_at = excluded.updated_at, "
            "actor = excluded.actor "
            "WHERE " + guard,
            [{**r, "parent": r.get("parent", ""), "w": r.get("w"), "h": r.get("h"), "updated_at": now,
              "user_id": user_id, "actor": actor} for r in rows],
        )
        await self._publish_board(user_id, "positions_set", {"rows": [
            {"kind": r["kind"], "object_id": r["object_id"], "x": r.get("x"), "y": r.get("y"),
             "z": r.get("z"), "parent": r.get("parent", ""), "w": r.get("w"), "h": r.get("h"),
             "actor": actor}
            for r in rows]})

    async def count_children(self, parent: str, user_id: str | None = None) -> int:
        """How many positions sit inside a container/space — gates its deletion (no orphans)."""
        clause, params = _scope(user_id)
        sql = "SELECT COUNT(*) AS n FROM canvas_positions WHERE parent = ?" + (
            f" AND {clause}" if clause else "")
        return (await self._one(sql, (parent, *params)))["n"]

    async def existing_keys(self, pairs, user_id: str | None = None) -> set[tuple[str, str]]:
        """Which (kind, object_id) pairs actually back a real object, owner-scoped — a position may
        only point at something that exists (V14: no writes into the void). Each kind is checked
        against its backing table: own-table kinds (list/session/bindle) in theirs, every other kind
        in canvas_objects. Per-kind IN queries (portable, no row-value syntax); a handful of kinds,
        so a handful of queries. A pair for an unknown owner simply isn't found → the caller 422s."""
        from collections import defaultdict

        by_kind: dict[str, list[str]] = defaultdict(list)
        for kind, oid in pairs:
            by_kind[kind].append(oid)
        clause, sparams = _scope(user_id)
        found: set[tuple[str, str]] = set()
        for kind, ids in by_kind.items():
            ph = ",".join(["?"] * len(ids))
            if kind in _OWN_TABLE_KINDS:
                table, col = _OWN_TABLE_KINDS[kind]
                sql = (f"SELECT {col} AS oid FROM {table} WHERE "
                       + (f"{clause} AND " if clause else "") + f"{col} IN ({ph})")
                args = (*sparams, *ids)
            else:
                sql = ("SELECT object_id AS oid FROM canvas_objects WHERE "
                       + (f"{clause} AND " if clause else "") + f"kind = ? AND object_id IN ({ph})")
                args = (*sparams, kind, *ids)
            for r in await self._all(sql, args):
                found.add((kind, r["oid"]))
        return found

    # --- desk identity (V16: tier + material are server truth, not device-local) --------------

    async def get_desk(self, user_id: str | None = None, parent: str = "") -> dict:
        """The EFFECTIVE desk for one surface: the stored row if present, else the medium default.
        Dims are resolved server-side (desk_dims) so a client — or an agent placing work — reads the
        same finite surface every time. Owner-scoped read (strict per-user; a real user never sees
        another's or a NULL-owner desk)."""
        clause, params = _scope(user_id)
        row = await self._one(
            "SELECT tier, w, h, material FROM canvas_desks WHERE parent = ?"
            + (f" AND {clause}" if clause else ""), (parent, *params))
        tier = row["tier"] if row else DEFAULT_DESK_TIER
        material = row["material"] if row else DEFAULT_DESK_MATERIAL
        w, h = desk_dims(tier, row["w"] if row else None, row["h"] if row else None)
        return {"parent": parent, "tier": tier, "w": w, "h": h, "material": material}

    async def set_desk(self, user_id: str | None, parent: str, tier: str,
                       w: float | None, h: float | None, material: str) -> None:
        """Upsert a surface's desk identity, then broadcast so live clients converge (Axiom 8).
        Null-safe delete+insert rather than ON CONFLICT: the key is (user_id, parent), and SQLite
        treats a NULL user_id (the operator) as DISTINCT in a PK conflict — so ON CONFLICT would let
        an operator accumulate duplicate rows per surface. The delete matches null-safely (`IS`) and
        the insert replaces, so the operator path is as correct as a real user's. There is no
        cross-owner reach: the delete is owner-matched, so a caller only ever replaces its own row.
        ponytail: two statements, not atomic — a rare user-initiated write, not a hot path; a
        concurrent set could interleave. Add a transaction if desk-set ever contends."""
        now = time.time()
        eq = "IS NOT DISTINCT FROM" if self._pg else "IS"  # null-safe owner match on both engines
        await self._exec(
            f"DELETE FROM canvas_desks WHERE parent = ? AND user_id {eq} ?", (parent, user_id))
        await self._exec(
            "INSERT INTO canvas_desks (user_id, parent, tier, w, h, material, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, parent, tier, w, h, material, now),
        )
        eff_w, eff_h = desk_dims(tier, w, h)
        await self._publish_board(user_id, "desk_set", {
            "parent": parent, "tier": tier, "w": eff_w, "h": eff_h, "material": material})

    # --- bindles (rendered broadsheets as canvas objects) ----------------------

    async def put_bindle(self, bindle_id: str, edition: str, subtitle: str, pages: int,
                   html: str, created_at: float | None = None, user_id: str | None = None) -> None:
        """Upsert a rendered bindle. created_at is preserved on update unless passed; user_id
        stamps a new row and is preserved on update (owner never reassigned). Fail-closed owner
        guard on the upsert: a bindle_id collision only overwrites a row the caller owns — a
        foreign or NULL-owner collision DO-UPDATEs nothing (else a caller could overwrite another
        tenant's bindle HTML → stored XSS). Null-safe so the operator path (user_id NULL) still
        updates its own NULL-owner rows."""
        # SQLite: `IS` is null-safe equality; PG: `IS NOT DISTINCT FROM`. Both compare NULL=NULL true.
        guard = ("bindles.user_id IS NOT DISTINCT FROM excluded.user_id" if self._pg
                 else "bindles.user_id IS excluded.user_id")
        if created_at is None:
            # Keep the original timestamp on update; stamp now on first insert.
            await self._exec(
                "INSERT INTO bindles (bindle_id, edition, subtitle, pages, html, created_at, "
                "user_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(bindle_id) DO UPDATE SET "
                "edition = excluded.edition, subtitle = excluded.subtitle, "
                "pages = excluded.pages, html = excluded.html "
                "WHERE " + guard,
                (bindle_id, edition, subtitle, pages, html, time.time(), user_id),
            )
        else:
            await self._exec(
                "INSERT INTO bindles (bindle_id, edition, subtitle, pages, html, created_at, "
                "user_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(bindle_id) DO UPDATE SET "
                "edition = excluded.edition, subtitle = excluded.subtitle, "
                "pages = excluded.pages, html = excluded.html, created_at = excluded.created_at "
                "WHERE " + guard,
                (bindle_id, edition, subtitle, pages, html, created_at, user_id),
            )

    async def get_bindles(self, user_id: str | None = None) -> list[dict]:
        """Listing rows without the html blob — `bytes` is the html length for a size hint."""
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT bindle_id, edition, subtitle, pages, created_at, LENGTH(html) AS bytes "
            "FROM bindles " + (f"WHERE {clause} " if clause else "") + "ORDER BY created_at DESC",
            params,
        )
        return [dict(r) for r in rows]

    async def get_bindle_html(self, bindle_id: str, user_id: str | None = None) -> str | None:
        clause, params = _scope(user_id)
        row = await self._one(
            "SELECT html FROM bindles WHERE bindle_id = ?" + (f" AND {clause}" if clause else ""),
            (bindle_id, *params),
        )
        return row["html"] if row else None

    # --- session documents (markdown results in the ObjectStore) --------------

    async def document_create(self, doc_id: str, user_id: str | None, run_id: str,
                        title: str, key: str, sha256: str, bytes: int) -> None:
        """Index one saved document. The bytes already live in the ObjectStore under `key`; this
        row is what makes them listable (the store has no list op)."""
        await self._exec(
            "INSERT INTO documents (doc_id, user_id, run_id, title, key, sha256, bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, user_id, run_id, title, key, sha256, bytes, time.time()),
        )

    async def documents_for(self, user_id: str | None, limit: int = 100,
                      before: float | None = None) -> list[dict]:
        """The caller's own documents, newest first. NULL-owner rows never returned (fail-closed
        _scope). `before` (a created_at) paginates: rows strictly older than it."""
        clause, params = _scope(user_id)
        wheres = ([clause] if clause else []) + (["created_at < ?"] if before is not None else [])
        args = list(params) + ([before] if before is not None else []) + [limit]
        sql = "SELECT doc_id, run_id, title, sha256, bytes, created_at FROM documents "
        if wheres:
            sql += "WHERE " + " AND ".join(wheres) + " "
        rows = await self._all(sql + "ORDER BY created_at DESC LIMIT ?", args)
        return [dict(r) for r in rows]

    async def document(self, user_id: str | None, doc_id: str) -> dict | None:
        """Owner-scoped lookup — None for another user's id or a NULL-owner row (existence hidden)."""
        clause, params = _scope(user_id)
        row = await self._one(
            "SELECT * FROM documents WHERE doc_id = ?" + (f" AND {clause}" if clause else ""),
            (doc_id, *params),
        )
        return dict(row) if row is not None else None

    # --- canvas objects (generic data-only kinds: note, clip, ...) -------------

    async def put_object(self, kind: str, object_id: str, payload: dict,
                   user_id: str | None = None, actor: str | None = None) -> None:
        """Upsert a data-only canvas object; created_at is preserved on update
        (omitted from the SET clause), updated_at always bumped. user_id stamps a new row and
        is preserved on update (owner never reassigned). Fail-closed owner guard on the upsert:
        a (kind, object_id) collision only overwrites a row the caller owns — a foreign or
        NULL-owner collision DO-UPDATEs nothing. Null-safe so the operator path (user_id NULL)
        still updates its own NULL-owner rows. actor (V17) records the latest writer's provenance."""
        now = time.time()
        cast = "::jsonb" if self._pg else ""  # payload column is JSONB on PG (see _pg_optimize)
        # SQLite: `IS` is null-safe equality; PG: `IS NOT DISTINCT FROM`. Both compare NULL=NULL true.
        guard = ("canvas_objects.user_id IS NOT DISTINCT FROM excluded.user_id" if self._pg
                 else "canvas_objects.user_id IS excluded.user_id")
        await self._exec(
            "INSERT INTO canvas_objects (kind, object_id, payload, created_at, updated_at, "
            f"user_id, actor) VALUES (?, ?, ?{cast}, ?, ?, ?, ?) "
            "ON CONFLICT(kind, object_id) DO UPDATE SET "
            "payload = excluded.payload, updated_at = excluded.updated_at, actor = excluded.actor "
            "WHERE " + guard,
            (kind, object_id, json.dumps(payload), now, now, user_id, actor),
        )
        await self._publish_board(user_id, "object_put",
                                  {"kind": kind, "object_id": object_id, "actor": actor})

    async def get_objects(self, kind: str | None = None, user_id: str | None = None) -> list[dict]:
        """Light rows [{kind, object_id, payload, created_at, updated_at}], newest-first."""
        clause, params = _scope(user_id)
        wheres = ([f"{clause}"] if clause else []) + (["kind = ?"] if kind is not None else [])
        args = list(params) + ([kind] if kind is not None else [])
        sql = "SELECT kind, object_id, payload, created_at, updated_at, actor FROM canvas_objects "
        if wheres:
            sql += "WHERE " + " AND ".join(wheres) + " "
        rows = await self._all(sql + "ORDER BY created_at DESC", args)
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    async def all_objects_of_kind(self, kind: str) -> list[dict]:
        """Every object of a kind across ALL owners, WITH user_id — the background-job read (the
        calendar ICS sync iterates these like _dreamer iterates tenants, then writes each back
        scoped to its owner via put_object(user_id=...)). Not an API path; callers are trusted
        in-process jobs, so it deliberately ignores the per-user _scope guard."""
        rows = await self._all(
            "SELECT kind, object_id, payload, user_id, created_at, updated_at "
            "FROM canvas_objects WHERE kind = ? ORDER BY created_at DESC", (kind,))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    async def delete_object(self, kind: str, object_id: str, user_id: str | None = None) -> bool:
        clause, params = _scope(user_id)
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_db_mod._PgConn._t(
                    "DELETE FROM canvas_objects WHERE kind = ? AND object_id = ?"
                    + (f" AND {clause}" if clause else "")),
                    (kind, object_id, *params))
                changed = cur.rowcount > 0
        else:
            with self._lock:
                cur = self._db.execute(
                    "DELETE FROM canvas_objects WHERE kind = ? AND object_id = ?"
                    + (f" AND {clause}" if clause else ""),
                    (kind, object_id, *params),
                )
                self._db.commit()
                changed = cur.rowcount > 0
        if changed:
            # Cascade the object's position row(s) — a position must not outlive its object (V14:
            # an orphan row is a phantom Mission Control dot / pile badge). Owner-scoped, same as
            # the delete above. Own-table kinds (list) cascade in their own delete path already.
            clause2, params2 = _scope(user_id)
            await self._exec(
                "DELETE FROM canvas_positions WHERE kind = ? AND object_id = ?"
                + (f" AND {clause2}" if clause2 else ""), (kind, object_id, *params2))
            await self._publish_board(user_id, "object_deleted",
                                      {"kind": kind, "object_id": object_id})
        return changed

    # --- embeddings (routing corpus + cache) -----------------------------------

    async def write_task_embedding(self, run_id: str, task_id: str, text: str, vector: list[float],
                             skill: str = "", model_id: str = "", outcome: str = "",
                             cost_usd: float | None = None, latency_ms: int | None = None,
                             user_id: str | None = None) -> None:
        """Persist one dispatched task's embedding + provenance — the experience corpus for
        future kNN routing. text is task+description only; the answer is never stored."""
        await self._exec(
            "INSERT INTO task_embeddings (run_id, task_id, text, vector, skill, model_id, "
            "outcome, cost_usd, latency_ms, user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, task_id) DO UPDATE SET "
            "text=excluded.text, vector=excluded.vector, skill=excluded.skill, "
            "model_id=excluded.model_id, outcome=excluded.outcome, cost_usd=excluded.cost_usd, "
            "latency_ms=excluded.latency_ms",
            (run_id, task_id, text, json.dumps(vector), skill, model_id, outcome, cost_usd,
             latency_ms, user_id, time.time()),
        )

    async def experience_rows(self, limit: int = 5000) -> list[dict]:
        """The kNN corpus: each dispatched task's embedding + provenance + its feedback verdict
        (LEFT JOIN → verdict None when unlabeled). Newest-first, capped. vector stays a JSON string
        (the caller parses; keeps this a plain read). ponytail: full-table scan client-side is fine
        at thousands of rows — the LIMIT bounds it; add a vector index only when it measurably hurts."""
        rows = await self._all(
            "SELECT te.vector, te.model_id, te.skill, te.outcome, te.cost_usd, f.verdict "
            "FROM task_embeddings te "
            "LEFT JOIN feedback f ON f.run_id = te.run_id AND f.task_id = te.task_id "
            "WHERE te.model_id != '' ORDER BY te.created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def get_cached_embedding(self, hash: str) -> list[float] | None:
        row = await self._one("SELECT vector FROM embedding_cache WHERE hash = ?", (hash,))
        return json.loads(row["vector"]) if row else None

    async def put_cached_embedding(self, hash: str, vector: list[float]) -> None:
        await self._exec(
            "INSERT INTO embedding_cache (hash, vector, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(hash) DO NOTHING",
            (hash, json.dumps(vector), time.time()),
        )

    # --- span fan-out ----------------------------------------------------------

    async def span_observer(self, span: dict) -> None:
        """Observer-seam adapter: address a driver span to the current run, if any. Spans
        emitted outside a session (e.g. the raw /v1/route plane) are simply not ours."""
        run_id = CURRENT_RUN_ID.get()
        if not run_id:
            return
        payload = dict(span)
        kind = payload.pop("node", "span")
        payload.pop("ts", None)  # publish() stamps its own
        await self.publish(run_id, kind, payload)

    async def publish_delta(self, node: str, text: str) -> None:
        """Publish one streamed answer chunk to the current run (addressed like span_observer).
        text is append-only; the client concatenates chunks in seq order. No-op outside a run."""
        run_id = CURRENT_RUN_ID.get()
        if run_id:
            await self.publish(run_id, "answer_delta", {"node": node, "text": text})
