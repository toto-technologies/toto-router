"""Sessions and conversations: run CRUD, multi-turn lineage, and lease-based recovery.

A run holds a lease renewed on every publish(); the reaper reclaims any lease that expires
(silent run = dead process) — see `reclaim_expired_leases` for the cross-replica atomicity.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .. import db as _db_mod
from .scoping import _scope


class SessionsMixin:
    """Session/conversation surface of RunStore. Relies on the host class for the connection
    seam, the lease identity (_holder/_lease_ttl), _run_month (PG partition-key cache), and
    publish() (terminal events)."""

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
             self._holder, now + self._lease_ttl),  # run starts holding a lease
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
        # A cooperative interrupt finishes status='cancelled' → its own terminal kind
        # (replay-safe: run_cancelled is in TERMINAL_KINDS, so subscribe stops after it too).
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
        """Atomically claim every running run whose lease has expired, transferring it to
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
        the cap in one block (hysteresis) — the prefix then stays byte-stable for many turns
        between evictions instead of sliding every turn.
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
