"""RunStore: schema DDL, boot-time migrations, PG partitioning, and the composed class.

The schema contract is boot-time idempotent DDL (CREATE IF NOT EXISTS + guarded ALTERs) — no
migration framework. Dual-dialect throughout: stdlib sqlite3 (WAL, one lock-guarded connection)
or Postgres (async pool, LIST-partitioned events) behind the same interface.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid

from .. import db as _db_mod
from .canvas import CanvasMixin, _OWN_TABLE_KINDS
from .claims import ClaimsMixin
from .companion import CompanionMixin
from .events import EventsMixin
from .sessions import SessionsMixin
from .signals import SignalsMixin

# NOTE: keep semicolons out of the comments below — the PG shim's executescript splits
# statements on them (guarded by tests/test_runs.py::test_schema_comments_have_no_semicolons).
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
-- events is the one UNBOUNDED table (every span of every run, forever). Retention today is
-- prune_deltas (answer_delta rows of old terminal runs) -- whole-partition drops on PG are the
-- upgrade. payload stays TEXT here (blob, replayed wholesale, never queried into).
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
-- Desk identity per surface: tier + material are SHARED truth, not device-local localStorage --
-- agents read the same desk every client draws onto. Keyed on (user_id, surface): '' surface =
-- the world, else a container object_id. w/h are the custom-tier dims (NULL for named tiers,
-- which derive from DESK_TIERS server-side).
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
-- Companion memory: typed rows, one fact per row, whole block injected at every wake. STRICTLY
-- user-scoped -- no NULL grandfathering (memory is the most sensitive plane), NULL owner = the
-- open-mode anon user. Caps enforced in memory_write, never trusted to the model.
CREATE TABLE IF NOT EXISTS user_memory (
  memory_id  TEXT PRIMARY KEY,
  user_id    TEXT,
  kind       TEXT NOT NULL,           -- preference | fact | context | instruction
  content    TEXT NOT NULL,
  source_run TEXT NOT NULL DEFAULT '',-- provenance: the chat turn that wrote it
  created_at REAL NOT NULL
);
-- Tenant registry: content-plane routing metadata, living in the OPERATIONAL DB. content_dsn_ref
-- is a secret-manager REFERENCE, never an inline DSN. v1 holds registered rows only -- nothing
-- reads it yet, the content resolver consults it when dedicated tenants land.
CREATE TABLE IF NOT EXISTS tenants (
  tenant_id       TEXT PRIMARY KEY,
  content_dsn_ref TEXT NOT NULL DEFAULT '',
  region          TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'active',
  epoch           INTEGER NOT NULL DEFAULT 0
);
-- Companion tool-call audit trail (receipts are the brand). Gateway DB only -- same line the
-- session prompts/answers already sit behind.
CREATE TABLE IF NOT EXISTS companion_tool_calls (
  call_id    TEXT PRIMARY KEY,
  run_id     TEXT NOT NULL,           -- the chat turn that made the call
  user_id    TEXT,
  tool       TEXT NOT NULL,
  args_json  TEXT NOT NULL DEFAULT '{}',
  result     TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
-- Companion voice (TTS) spend ledger. One row per /speak call -- it is BOTH the receipt (chars +
-- cost) AND the per-user daily-cap input, summed by tts_spend_today exactly as chat_spend_today
-- sums sessions. User-scoped like every companion plane.
CREATE TABLE IF NOT EXISTS companion_tts_usage (
  call_id    TEXT PRIMARY KEY,
  user_id    TEXT,
  chars      INTEGER NOT NULL,
  cost_usd   REAL NOT NULL,
  created_at REAL NOT NULL
);
-- Pipedream Connect spend ledger. One row per external sync PULL -- the receipt (call count +
-- ESTIMATED $, their credit model is opaque so it is reconciled monthly against the invoice,
-- no gating). User-scoped like every companion plane.
CREATE TABLE IF NOT EXISTS pipedream_usage (
  call_id    TEXT PRIMARY KEY,
  user_id    TEXT,
  calls      INTEGER NOT NULL,
  est_usd    REAL NOT NULL,
  created_at REAL NOT NULL
);
-- Custom tools. Ownable, STRICTLY user-scoped like user_memory -- two users share nothing, no
-- NULL grandfathering. spec is the sharable JSON artifact (the whole contract). unique
-- (user_id, name) makes a re-PUT of the same name an owner-scoped overwrite (import == upsert).
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
-- Dream pass audit + idempotency. The PK (tenant_id, run_date) IS the once-per-tenant-per-night
-- guard AND the cross-replica leader election -- the winning claim_dream_run insert owns that
-- tenant for the day. shown gates the companion's one sparing next-wake mention.
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
-- Idempotency keys. A client retry after a network blip replays the first response instead of
-- double-executing a create (double token spend, duplicate lists). The winning claim_idempotency
-- insert is BOTH the dedup guard AND the cross-replica in-flight marker (status_code NULL =
-- still running -- a second claim before the result lands gets 409 retry). user_id is coalesced
-- to '' for the NULL-owner operator so the composite PK dedups on both dialects (SQLite treats
-- NULL PK parts as distinct).
-- ponytail no TTL/reaper -- keys are tiny at single-operator scale, add a created_at sweep later.
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
-- Schema-version anchor (contract + ceiling in docs/ops/migrations.md). The boot-time idempotent
-- DDL has no ordering anchor -- this one row gives the first non-additive migration a
-- replica-safe guard and a version to read at /statusz. Stamped once with ON CONFLICT DO NOTHING
-- so concurrent replica boots race safely (first writer wins, the rest no-op).
CREATE TABLE IF NOT EXISTS meta (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""

# Forward-only schema generation. Bumped by hand when the boot DDL changes in a way a future
# migration needs to reason about (the contract + ceiling live in docs/ops/migrations.md). It is
# an ANCHOR, not a runner: it does not gate boot or trigger backfills -- it records what this
# replica stamped so the first destructive migration has an ordered starting point.
# "2" = the control-plane tenancy tables (organizations/teams/memberships in auth.py's _SCHEMA).
SCHEMA_VERSION = "2"


class RunStore(SessionsMixin, EventsMixin, CompanionMixin, ClaimsMixin, SignalsMixin,
               CanvasMixin, _db_mod.AsyncStoreMixin):
    """Per-session event log + live pub/sub + the whole app-plane store, split by concern into
    the mixins above. This class owns the connection seam and the boot-time schema work."""

    def __init__(self, path: str = ":memory:", database_url: str = "",
                 lease_ttl: float = 600, pool: dict | None = None, redis_url: str = "") -> None:
        from .. import db as _db

        self._db, self._pg = _db.connect(database_url, path)  # sync conn: init DDL + PG partitions
        self._pool = _db.make_async_pool(database_url, **(pool or {}))  # async pool: runtime queries
        self._partitions_month = None  # month-gate for ensure_event_partitions
        self._database_url = database_url                     # kept for the dream advisory-lock conn
        self._run_month: dict[str, str] = {}  # run_id → 'YYYY-MM' (PG events partition key cache)
        # Lease-based recovery: this replica's holder id + how long a lease lives without a
        # renewing event. A run holds a lease renewed on every publish(); the reaper reclaims any
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
        # Multi-turn lineage: conv_id = the run_id of turn 1.
        # lane: 'work' (driver runs, NULL = legacy work) | 'chat' (companion turns) — the 409
        # turn guard fires same-lane only.
        # model: the answering model for a chat turn (per-turn provenance for the chat surface —
        # work turns keep model_id per-task in tasks_json, so this stays NULL for them).
        for col, decl in (("conv_id", "TEXT"), ("turn", "INTEGER"), ("lane", "TEXT"),
                          ("model", "TEXT")):
            self._alter(f"ALTER TABLE sessions ADD COLUMN {ine}{col} {decl}")
        # Per-user scoping: every row carries its owner; legacy rows read NULL.
        for table in ("sessions", "lists", "canvas_objects", "canvas_positions", "bindles",
                      "feedback"):
            self._alter(f"ALTER TABLE {table} ADD COLUMN {ine}user_id TEXT")
        # Canvas structure: a position's surface is a parent key ('' = default world).
        self._alter(f"ALTER TABLE canvas_positions ADD COLUMN {ine}parent TEXT NOT NULL DEFAULT ''")
        # Optional card width (list cards send it; NULL = the kind's default width in the UI).
        self._alter(f"ALTER TABLE canvas_positions ADD COLUMN {ine}w REAL")
        # Optional card height — same semantics as w.
        self._alter(f"ALTER TABLE canvas_positions ADD COLUMN {ine}h REAL")
        # Write provenance: who last wrote this row — 'user' (a hand), 'agent' (Toto/MCP/pi),
        # 'operator'. Nullable; legacy rows read NULL (provenance unknown, not a lie).
        self._alter(f"ALTER TABLE canvas_positions ADD COLUMN {ine}actor TEXT")
        self._alter(f"ALTER TABLE canvas_objects ADD COLUMN {ine}actor TEXT")
        # List-item done state ('' | 'doing' | 'done') for the prod list done-toggles.
        self._alter(f"ALTER TABLE list_items ADD COLUMN {ine}status TEXT NOT NULL DEFAULT ''")
        # Typed artifact envelope on companion tool receipts: sha256/evidence/confidence/
        # produced_by alongside the short result summary — content-addressable receipts.
        self._alter(f"ALTER TABLE companion_tool_calls ADD COLUMN {ine}artifact TEXT NOT NULL DEFAULT '{{}}'")
        # Lease columns on sessions. lease_expires is epoch seconds → DOUBLE on PG:
        # its float4 REAL keeps only ~7 digits and would round a ~1.7e9 epoch to ~100s buckets.
        self._alter(f"ALTER TABLE sessions ADD COLUMN {ine}lease_holder TEXT")
        _leasecol = "DOUBLE PRECISION" if self._pg else "REAL"
        self._alter(f"ALTER TABLE sessions ADD COLUMN {ine}lease_expires {_leasecol}")
        # preferences went global (key PK) -> per-user ((user_id, key) PK). Can't reshape a PK via
        # ALTER; no legacy data to preserve, so drop+recreate an old single-column-PK table once.
        self._migrate_preferences_per_user(ine)
        self.schema_version = self._stamp_schema_version()  # forward-only version anchor
        self._db.commit()
        self._cleanup_orphan_positions()  # purge position rows whose object is already gone
        if self._pg:
            self._pg_optimize()
        self._lock = threading.Lock()
        from ..wake import make_wake_bus

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
        longer exists (an orphan row renders as a phantom Mission Control dot / pile badge, the
        screen lying about state). Only GENERIC kinds are checked against canvas_objects; own-table
        kinds (list/session/bindle) are left alone — they cascade in their own delete paths and
        don't live in canvas_objects, so they'd all look 'orphaned' here. New generic kinds are
        covered automatically (they're just 'not an own-table kind'). Runs sync at init like the
        migrations, using self._db directly (before the async lock exists)."""
        own = ",".join(["?"] * len(_OWN_TABLE_KINDS))
        self._db.execute(
            f"DELETE FROM canvas_positions WHERE kind NOT IN ({own}) AND NOT EXISTS "
            "(SELECT 1 FROM canvas_objects o WHERE o.kind = canvas_positions.kind "
            "AND o.object_id = canvas_positions.object_id)",
            tuple(_OWN_TABLE_KINDS),
        )
        self._db.commit()

    # --- PG events partitioning ------------------------------------------------

    def _pg_create_events(self) -> None:
        """PG only: events is LIST-partitioned by run_month (the run's CREATED month, constant per
        run). The partition key MUST be in the PK — but because run_month is run-constant, ALL of a
        run's events live in ONE partition, so (run_id, seq) stays globally unique and the
        seq-conflict-retry in publish() still detects races. Created BEFORE executescript so its
        plain `CREATE TABLE IF NOT EXISTS events` no-ops. Assumes a fresh PG (day-0 cutover); an
        existing plain-events DB would need a manual re-partition.
        Retention keeps spans, so it's a plain DELETE of deltas; whole-partition DROP (drops
        spans too) is the upgrade — the partitions exist now so that's a one-liner later."""
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

        Month-gate: the 60s reaper calls this every tick, but the partitions only change on a
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
