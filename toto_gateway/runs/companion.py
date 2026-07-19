"""Companion-plane state: user memory, tool receipts, custom tools, and spend ledgers.

Everything here is STRICTLY user-scoped via `_mem_scope` (two users share nothing, no NULL
grandfathering — memory is the most sensitive plane); NULL owner = the open-mode anon user.
"""

from __future__ import annotations

import json
import time
import uuid

from .. import db as _db_mod
from .scoping import _mem_scope, _scope

# Companion memory caps: ~100 rows / 8k chars per user; oldest evictable rows go first;
# preference/instruction are never auto-evicted.
MEMORY_KINDS = ("preference", "fact", "context", "instruction")
MEMORY_MAX_ROWS, MEMORY_MAX_CHARS = 100, 8000
_MEMORY_EVICTABLE = ("context", "fact")  # eviction order: oldest context first, then oldest fact


class CompanionMixin:
    """Companion surface of RunStore: memory rows, tool-call receipts, custom tools, the eternal
    chat conversation, and the TTS/Pipedream spend ledgers."""

    # --- memory ---------------------------------------------------------------

    async def memory_rows(self, user_id: str | None = None) -> list[dict]:
        """The user's whole memory block, oldest-first (it all fits under the cap — no
        retrieval). Strictly scoped: two users share nothing."""
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

    # --- tool-call receipts ----------------------------------------------------

    async def log_tool_call(self, run_id: str, user_id: str | None, tool: str, args: dict,
                      result: str, artifact: dict | None = None) -> None:
        """Audit one companion tool call — receipts are the brand. `result` is a short outcome
        summary, truncated by the caller. `artifact` is the typed envelope
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

    # --- custom tools -----------------------------------------------------------
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

    # --- eternal conversation + live-work context -------------------------------

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

    # --- spend ledgers ----------------------------------------------------------

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
        """Receipt one Pipedream sync pull — the audit row (call count + estimated $). No
        cap/gating (count exactly, estimate $, reconcile monthly against the invoice — their
        credit model is opaque). Mirrors log_tts."""
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
