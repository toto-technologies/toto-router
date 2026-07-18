"""SSE wake-up seam (infra plan A4.1). notify(run_id) wakes local subscribers, who then re-read
events_after — correctness ALWAYS from the DB, never the bus, so at-most-once delivery is fine.

Three backends behind one interface:
  - InProcWakeBus: asyncio queues (SQLite / single replica — today's _subs semantics).
  - PgWakeBus:     Postgres LISTEN/NOTIFY across replicas (dedicated listener conn + reconnect).
  - RedisWakeBus:  Redis pub/sub across replicas (Wave 2 R1). SAME notify+subscribe seam, SAME
                   re-read-from-DB contract — the PUBLISH carries only the run_id, never the body.

RedisWakeBus is the escape hatch the fan-out plan reserved (rung-4 swap): a config flip
(TOTO_GW_REDIS_URL) + one small backend, never a publish()/subscribe() rewrite.
"""

from __future__ import annotations

import asyncio
import contextlib

CHANNEL = "toto_run"


class InProcWakeBus:
    """Single-replica wake-up: notify puts a signal on each local subscriber's queue."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = {}

    def notify(self, run_id: str) -> None:
        for q in tuple(self._subs.get(run_id, ())):
            q.put_nowait(None)  # payload ignored — the wake means "re-read events_after"

    @contextlib.contextmanager
    def subscribe(self, run_id: str):
        q: asyncio.Queue = asyncio.Queue()
        self._subs.setdefault(run_id, set()).add(q)
        try:
            yield q
        finally:
            subs = self._subs.get(run_id)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    self._subs.pop(run_id, None)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def armed(self) -> bool:
        return True  # single-replica in-proc bus is always ready (no listener to arm)


class PgWakeBus:
    """Cross-replica wake-up via Postgres LISTEN/NOTIFY. notify() only wakes THIS replica's local
    queues — the cross-replica pg_notify now rides RunStore.publish()'s write in a single CTE
    round-trip (addendum #1: nothing on a separate sync conn off the write path). The echo of that
    pg_notify back to this replica's listener is a harmless double-wake (re-read finds nothing
    new). One dedicated LISTEN conn with a reconnect loop; no publisher-side connection at all."""

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._task: asyncio.Task | None = None

    def _wake_local(self, run_id: str) -> None:
        for q in tuple(self._subs.get(run_id, ())):
            q.put_nowait(None)

    def notify(self, run_id: str) -> None:
        self._wake_local(run_id)  # same-replica fast path; other replicas wake via publish()'s CTE

    subscribe = InProcWakeBus.subscribe  # identical local-queue registration

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def armed(self) -> bool:
        # The reconnect loop catches dropped conns and re-arms LISTEN, so a live task == armed.
        # A crashed/never-started listener (task None or done) means this replica gets no
        # cross-replica wakes → SSE fan-out is dead and /readyz must fail closed.
        return self._task is not None and not self._task.done()

    async def _listen_loop(self) -> None:
        import psycopg

        while True:
            try:
                aconn = await psycopg.AsyncConnection.connect(self._url, autocommit=True)
                async with aconn:
                    await aconn.execute(f"LISTEN {CHANNEL}")
                    async for note in aconn.notifies():
                        self._wake_local(note.payload)  # payload = run_id
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)                 # dropped conn → reconnect (LISTEN re-armed)


class RedisWakeBus:
    """Cross-replica wake via Redis pub/sub (Wave 2 R1). notify() wakes THIS replica's queues
    immediately (same-replica fast path, like PgWakeBus) and fire-and-forgets a PUBLISH so peer
    replicas' listeners re-read. The payload is only the run_id — correctness still re-reads from
    the DB (the loopback of our own PUBLISH is a harmless double-wake). One dedicated pub/sub
    listener with a reconnect loop; the PUBLISH client is lazy and off the listen path."""

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._subs: dict[str, set[asyncio.Queue]] = {}
        self._task: asyncio.Task | None = None
        self._pub = None            # lazy redis.asyncio client for PUBLISH
        self._pending: set = set()  # keep fire-and-forget publish tasks alive until done

    _wake_local = PgWakeBus._wake_local     # identical local-queue fan-out
    subscribe = InProcWakeBus.subscribe     # identical local-queue registration
    armed = PgWakeBus.armed                 # live listener task == armed; same _task semantics as PG bus

    def notify(self, run_id: str) -> None:
        self._wake_local(run_id)            # same-replica fast path
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                          # no loop (sync caller/test) → local-only
        t = loop.create_task(self._publish(run_id))
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    async def _publish(self, run_id: str) -> None:
        try:
            if self._pub is None:
                import redis.asyncio as redis
                self._pub = redis.from_url(self._url)
            await self._pub.publish(CHANNEL, run_id)
        except Exception:
            pass  # ponytail: fan-out is best-effort; the subscribe() _REPOLL floor self-heals a drop

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._pub is not None:
            with contextlib.suppress(Exception):
                await self._pub.aclose()

    async def _listen_loop(self) -> None:
        import redis.asyncio as redis

        while True:
            try:
                r = redis.from_url(self._url)
                async with r.pubsub() as ps:
                    await ps.subscribe(CHANNEL)
                    async for msg in ps.listen():
                        if msg.get("type") == "message":
                            data = msg["data"]
                            self._wake_local(data.decode() if isinstance(data, (bytes, bytearray))
                                             else data)  # payload = run_id
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)  # dropped conn → reconnect (re-subscribe)


def make_wake_bus(database_url: str, redis_url: str = ""):
    if redis_url:
        return RedisWakeBus(redis_url)
    return PgWakeBus(database_url) if database_url else InProcWakeBus()
