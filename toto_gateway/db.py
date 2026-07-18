"""Dual-mode store connection: SQLite (default) or Postgres via TOTO_GW_DATABASE_URL.

The store classes (RunStore, AuthStore) speak hand-written SQL against `self._db`, which is
either a stdlib sqlite3 connection or the `_PgConn` shim below over one psycopg3 connection.
The shim exposes just the sqlite3.Connection slice the stores use, translating the dialect so
the ~60 statements stay written once.

ponytail: single persistent connection + the store's existing lock — today's model, on
Postgres. That already delivers durable, SHARED (multi-replica-capable), non-ephemeral storage.
An async psycopg_pool + `async def` methods is the P1 throughput upgrade, bundled with the
LISTEN/NOTIFY fan-out that actually needs async — the seam is here (swap `_PgConn` for a pool,
methods gain `await`). Reconnect-once covers idle drops; keepalives hold the link open.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path


def _pg_search_path(schema: str) -> str:
    # `public` stays on the path so shared types (the pgvector `vector` type, created in public)
    # and any primary-DB objects still resolve. schema is an identifier we control (config), not
    # user input, but keep it to a safe charset as a belt-and-suspenders guard.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema or ""):
        raise ValueError(f"unsafe content schema name {schema!r}")
    return f"{schema}, public"


def _make_configure(schema: str | None):
    async def _configure(conn) -> None:
        """Pool configure hook: read JSONB as raw text (stores keep json.loads at the boundary),
        and pin the search_path when the content plane rides a schema in the primary DB."""
        from psycopg.types.string import TextLoader

        conn.adapters.register_loader("jsonb", TextLoader)
        if schema:
            await conn.execute(f"SET search_path TO {_pg_search_path(schema)}")

    return _configure


def make_async_pool(database_url: str, schema: str | None = None, *,
                    pool_min: int = 2, pool_max: int = 10, pool_timeout: float | None = None):
    """An UNOPENED AsyncConnectionPool (opened lazily on first use — no running loop at __init__).
    None in SQLite mode. `schema` pins search_path so a co-located content plane lives in its own
    Postgres schema. Sizing/timeout are threaded from Settings (TOTO_GW_POOL_*); the defaults here
    preserve the historical behavior for any direct caller that doesn't pass them.

    `check` validates a pooled conn (SELECT 1) before hand-out, so a failover/idle-dropped conn is
    discarded and replaced instead of failing the query — the pool-path equivalent of _PgConn's
    reconnect-once. `timeout` bounds the acquire wait; on exhaustion psycopg raises PoolTimeout,
    which app.create_app maps to a clean 503 capacity_error."""
    if not database_url:
        return None
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    kw = {} if pool_timeout is None else {"timeout": pool_timeout}
    return AsyncConnectionPool(
        database_url, min_size=pool_min, max_size=pool_max, open=False,
        check=AsyncConnectionPool.check_connection,
        configure=_make_configure(schema),
        kwargs={"autocommit": True, "row_factory": dict_row},
        **kw,
    )


class AsyncStoreMixin:
    """Async DB surface over either a sync sqlite3 connection (inline — no real await) or an async
    psycopg pool (PG). Bodies call `await self._one/_all/_exec/_many`; the sync-vs-async + ?→%s
    branch lives here once. Atomic multi-statement methods dual-branch inline (PG: one statement
    via RETURNING; SQLite: both under self._lock)."""

    _pool = None          # AsyncConnectionPool | None
    _db = None            # sqlite3.Connection | _PgConn (SQLite runtime + PG init DDL)
    _lock = None
    _pool_opened = False

    async def _open_pool(self) -> None:
        if not self._pool_opened:
            await self._pool.open()
            self._pool_opened = True

    async def _one(self, sql: str, params=()):
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                return await (await c.execute(_PgConn._t(sql), params)).fetchone()
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    async def _all(self, sql: str, params=()):
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                return await (await c.execute(_PgConn._t(sql), params)).fetchall()
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    async def _exec(self, sql: str, params=()):
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                await c.execute(_PgConn._t(sql), params)
        else:
            with self._lock:
                self._db.execute(sql, params)
                self._db.commit()

    async def _exec_count(self, sql: str, params=()) -> int:
        """Like _exec but returns the affected-row count (cursor.rowcount) -- both dialects populate
        it for DELETE/UPDATE. Used where the caller reports how many rows a write touched."""
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_PgConn._t(sql), params)
                return cur.rowcount
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur.rowcount

    async def _many(self, sql: str, rows) -> None:
        rows = list(rows)
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                await c.cursor().executemany(_PgConn._t(sql), rows)
        else:
            with self._lock:
                self._db.executemany(sql, rows)
                self._db.commit()

    async def close_pool(self) -> None:
        if self._pool is not None and self._pool_opened:
            await self._pool.close()
            self._pool_opened = False

    async def check_rate_limit(self, scope: str, limit: int, window_seconds: int) -> bool:
        """Atomic fixed-window counter, correct across replicas. True if within limit. Shared by
        both stores. table-qualified RHS: unqualified 'count' is ambiguous in PG."""
        window_start = int(time.time() // window_seconds) * window_seconds
        row = await self._one(
            "INSERT INTO rate_limits (scope, window_start, count) VALUES (?, ?, 1) "
            "ON CONFLICT (scope, window_start) DO UPDATE SET count = rate_limits.count + 1 "
            "RETURNING count",
            (scope, window_start),
        )
        return row["count"] <= limit


class _PgConn:
    """The sqlite3.Connection slice the stores use, over one psycopg3 connection (autocommit,
    dict rows). Placeholder + type dialect is translated here so store SQL stays sqlite-shaped."""

    def __init__(self, url: str, schema: str | None = None) -> None:
        self._url = url
        self._schema = schema
        self._c = None
        self._connect()

    def _connect(self) -> None:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.string import TextLoader

        self._c = psycopg.connect(self._url, autocommit=True, row_factory=dict_row,
                                  keepalives=1, keepalives_idle=30)
        # Read JSONB columns as raw text so the stores keep json.loads at the boundary (identical
        # Python surface); the column is JSONB for indexing/containment queries later.
        self._c.adapters.register_loader("jsonb", TextLoader)
        if self._schema:
            # Co-located content plane: its own schema in the primary DB. Create + pin it so DDL
            # and every query land there, not in public (which the primary stores own).
            self._c.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
            self._c.execute(f"SET search_path TO {_pg_search_path(self._schema)}")

    @staticmethod
    def _t(sql: str) -> str:
        # named :kind -> %(kind)s, but NOT the second colon of a ::type cast (lookbehind for ':').
        # ponytail: a LITERAL % in store SQL (e.g. LIKE 'x%') would break psycopg's client parser —
        # no store statement has one today (grep-verified); double it to %% if one is ever added.
        sql = re.sub(r"(?<!:):(\w+)", r"%(\1)s", sql)
        return sql.replace("?", "%s")             # positional ? -> %s

    def _run(self, fn):
        import psycopg

        try:
            return fn(self._c)
        except psycopg.OperationalError:
            self._connect()   # idle drop / server restart → reconnect once and retry
            return fn(self._c)

    def execute(self, sql: str, params=()):
        return self._run(lambda c: c.execute(self._t(sql), params))

    def executemany(self, sql: str, seq):
        def go(c):
            cur = c.cursor()
            cur.executemany(self._t(sql), list(seq))
            return cur
        return self._run(go)

    def executescript(self, script: str) -> None:
        # PG REAL is float4 (~7 digits) → epoch floats lose precision; sqlite REAL is float8.
        script = script.replace(" REAL", " DOUBLE PRECISION")
        # Strip `--` line comments before splitting on ';': a semicolon inside a comment must not
        # sever a statement (the naive split otherwise runs comment text as SQL → syntax error).
        # This kills that bug class for good; the schemas' DDL has no string literal containing
        # '--', so per-line stripping is safe. ponytail: simplest robust fix, no SQL parser.
        script = "\n".join(line.split("--", 1)[0] for line in script.splitlines())

        def go(c):
            for stmt in script.split(";"):
                if stmt.strip():
                    c.execute(stmt)
        self._run(go)

    def commit(self) -> None:
        pass  # autocommit

    def close(self) -> None:
        if self._c is not None:
            self._c.close()


def connect(database_url: str, sqlite_path: str, schema: str | None = None):
    """Return (db, is_pg). db exposes .execute/.executemany/.executescript/.commit either way.
    `schema` pins a Postgres schema (co-located content plane); ignored in SQLite mode."""
    if database_url:
        return _PgConn(database_url, schema), True
    if sqlite_path != ":memory:":
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(sqlite_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    if sqlite_path != ":memory:":
        db.execute("PRAGMA journal_mode=WAL")
    return db, False
