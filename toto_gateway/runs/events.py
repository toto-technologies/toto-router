"""Event log + live fan-out: publish/subscribe, board channels, and retention pruning.

`publish()` appends a seq-numbered event and wakes live subscribers through the wake bus;
`subscribe()` replays from the DB then live-tails. Correctness always comes from the DB — the
bus is only a latency hint (a missed wake is healed by the repoll floor).
"""

from __future__ import annotations

import asyncio
import json
import time
from contextvars import ContextVar
from typing import AsyncIterator

# Set by the sessions route around driver.run(); the span fan-out reads it to address events.
# Contextvars flow through asyncio.gather, so concurrent runs never cross streams.
CURRENT_RUN_ID: ContextVar[str | None] = ContextVar("toto_gw_run_id", default=None)

TERMINAL_KINDS = ("run_done", "run_failed", "run_cancelled")
# Terminal run STATUSES (sessions.status), mirroring TERMINAL_KINDS. cancelled IS terminal — the
# same-lane "still thinking" guards must treat it so, else one Stop deadlocks the lane forever.
TERMINAL_STATUSES = ("done", "failed", "cancelled")
_REPOLL_SECONDS = 5.0  # subscribe() re-reads on each wake; also every 5s as a missed-notify floor
_WAKE_CHANNEL = "toto_run"  # == wake.CHANNEL; publish()'s CTE pg_notify fires on it (kept in sync)


class EventsMixin:
    """Event-log surface of RunStore. Relies on the host class for the connection seam
    (_db/_pool/_lock/_pg + the read/write helpers), the wake bus (_wake), the lease identity
    (_holder/_lease_ttl), and _run_month_of (PG events partition key)."""

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
            # Allocate seq AND fire the cross-replica pg_notify in ONE round-trip (nothing on a
            # separate sync conn). The (run_id, seq, run_month) PK makes a cross-replica race
            # safe-by-conflict — retry on the loser. run_month is run-constant, so MAX(seq)
            # WHERE run_id AND run_month prunes to the run's single partition and (run_id, seq)
            # uniqueness holds. Autocommit pool → the pg_notify commits immediately.
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
            # Renew the lease on every event — activity keeps the run alive. Fold the cost bump
            # into the same statement so it stays one round-trip. ponytail: one PK UPDATE per
            # event (incl. deltas); throttle if the delta storm ever dominates writes.
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
                lexp = ts + self._lease_ttl  # renew the lease on every event
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

    # --- retention ------------------------------------------------------------

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
        """Retention: delete this user's explicit memory facts created before `older_than`,
        bounded to `limit` rows this call. Returns rows deleted. Product data (memory_write / REST),
        NOT the auto-capture lane — this is the sink zero_retention leaves alone. IN-subquery with
        LIMIT is bounded on both dialects; a backlog drains over ticks."""
        return await self._exec_count(
            "DELETE FROM user_memory WHERE memory_id IN "
            "(SELECT memory_id FROM user_memory WHERE user_id = ? AND created_at < ? "
            "ORDER BY created_at LIMIT ?)",
            (user_id, float(older_than), int(limit)))

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
