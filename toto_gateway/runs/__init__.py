"""RunStore — per-session event log + live pub/sub, backing the sessions API and SSE.

The driver emits one span per graph node through its observer seam; this package gives those
spans an address. Each session (run) gets a monotonically seq-numbered event log: the DB is the
durable replay/restart path, the wake bus is the live path. SSE resume is exact: a subscriber
names the last seq it saw and gets everything after.

One store holds every table of the live-routing plane (sessions/events/feedback/preferences plus
the canvas and companion planes). Dual-dialect behind one interface: stdlib sqlite3 (WAL mode,
single lock-guarded connection — single-operator scale, no new dependencies) or Postgres (async
pool, partitioned events).

Modules:
    store.py     schema DDL, migrations, PG partitioning, and the composed RunStore class
    sessions.py  session/conversation CRUD + lease-based recovery
    events.py    publish/subscribe event log, board channels, retention pruning
    companion.py user memory, tool receipts, custom tools, spend ledgers
    claims.py    cross-replica atomic claims: dream runs, idempotency keys, advisory locks
    signals.py   preferences, feedback verdicts, task-embedding corpus
    canvas.py    lists, positions, desks, bindles, documents, generic objects
    scoping.py   strict per-user WHERE predicates (fail closed)
"""

from __future__ import annotations

from .canvas import (DEFAULT_DESK_MATERIAL, DEFAULT_DESK_TIER, DESK_TIER_NAMES, DESK_TIERS,
                     desk_dims)
from .companion import MEMORY_KINDS, MEMORY_MAX_CHARS, MEMORY_MAX_ROWS
from .events import CURRENT_RUN_ID, TERMINAL_KINDS, TERMINAL_STATUSES
from .scoping import _mem_scope, _scope
from .store import _SCHEMA, SCHEMA_VERSION, RunStore

__all__ = [
    "CURRENT_RUN_ID", "DEFAULT_DESK_MATERIAL", "DEFAULT_DESK_TIER", "DESK_TIER_NAMES",
    "DESK_TIERS", "MEMORY_KINDS", "MEMORY_MAX_CHARS", "MEMORY_MAX_ROWS", "RunStore",
    "SCHEMA_VERSION", "TERMINAL_KINDS", "TERMINAL_STATUSES", "desk_dims",
]
