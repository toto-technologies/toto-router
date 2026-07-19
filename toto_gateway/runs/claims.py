"""Cross-replica atomic claims: dream-run leadership, idempotency keys, advisory locks.

Every claim here is a single atomic statement (INSERT ... ON CONFLICT DO NOTHING RETURNING) so
N replicas racing produce exactly one winner — the claim row is both the idempotency key and
the leader election.
"""

from __future__ import annotations

import time


class ClaimsMixin:
    """Claim/coordination surface of RunStore (dreams, idempotency keys, PG advisory locks)."""

    # --- dreams (nightly memory consolidation) ---------------------------------

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
        marked shown so the companion volunteers it at most once. None → nothing to mention
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

    # --- idempotency keys -------------------------------------------------------

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

    # --- advisory locks ---------------------------------------------------------

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
