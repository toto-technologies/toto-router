"""Sessions API — the live-routing plane (docs/plans/2026-07-01-live-routing-e2e.md, B2).

POST creates a session and launches the driver run asynchronously; GET /events streams the
run's spans over SSE with exact Last-Event-ID resume; the snapshot endpoint makes a mid-run
page refresh lossless (snapshot + tail covers every event exactly once).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from collections import deque

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..benchmarks import OPTIMIZE
from ..runs import TERMINAL_KINDS, TERMINAL_STATUSES, RunStore
# The launcher + its in-flight/cancel state live in the sessions service (importable by the
# companion without a routes dependency); this module re-exports the historical names below.
from ..sessions_service import (  # noqa: F401 — re-exports for routes/companion.py + tests
    _live_run_ids, _live_runs, _public_tasks, _write_card, cancel_boundary, clear_cancel,
    execute_run, request_cancel, track_run,
)
from .deps import Idem, Identity, _resolve_identity, idempotency, require_auth

router = APIRouter()

# Historical underscore names — other routes/tests import these from here.
_execute = execute_run
_track_run = track_run

# Each session spends real money on real lanes — cheap in-proc limiter.
# ponytail: fixed window over a deque; per-user buckets when there are users.
RATE_LIMIT, RATE_WINDOW = 12, 60.0
_recent: deque[float] = deque()

_draining = False  # set on SIGTERM/shutdown → POST /v1/sessions[/turns] returns 503
_sse_connections = 0  # live SSE subscribe generators on this replica (utilization signal)


def is_draining() -> bool:
    return _draining


def _at_capacity(settings) -> bool:
    """Per-replica surge backpressure: True when in-flight runs hit max_concurrent_runs (0 = off).
    ponytail: reuses _live_run_ids (already tracked for drain) — no separate semaphore. Per-process
    cap; a global cap would need Postgres, which is P1's rate-limit-table work."""
    cap = settings.max_concurrent_runs
    return cap > 0 and len(_live_run_ids) >= cap


async def drain(store, timeout: float, log) -> int:
    """Graceful-shutdown half of plan D3: stop taking new runs, wait up to `timeout` for in-flight
    runs, then fail whatever's left so SSE clients get a terminal event instead of silence.
    Returns the count of stragglers failed. Only touches THIS process's runs (never a sibling
    replica's) because it works off the local _live_run_ids set, not a DB sweep."""
    global _draining
    _draining = True
    log.info("drain start", extra={"in_flight": len(_live_run_ids), "timeout_s": timeout})
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while _live_runs and loop.time() < deadline:
        await asyncio.sleep(0.2)
    stragglers = list(_live_run_ids)
    for run_id in stragglers:
        if store is not None:
            try:
                await store.finish(run_id, status="failed", error="interrupted by deploy")
            except Exception:
                log.exception("drain: could not finish straggler", extra={"run_id": run_id})
    log.info("drain complete", extra={"failed_stragglers": len(stragglers)})
    return len(stragglers)


def _error(status: int, message: str, err_type: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": err_type}}, headers=headers)


def _plane(request: Request) -> tuple[RunStore | None, object | None]:
    return getattr(request.app.state, "runs", None), getattr(request.app.state, "driver", None)


class CreateSession(BaseModel):
    query: str
    optimize: str | None = None
    # Pre-triage: a harness whose own loop already decided (pi door) can skip driver triage.
    # Honored only for AUTHENTICATED callers (cost optimization, not a privilege — but never
    # steerable by drive-by anonymous traffic); silently ignored otherwise.
    kind: str | None = None


SESSION_KINDS = ("multistep", "trivial")


def _kind_or_error(body: CreateSession, identity: Identity):
    """(kind, error): validate body.kind and gate it on authentication. Anonymous callers'
    kind is dropped (run goes through normal triage), never a hard failure."""
    if body.kind is None:
        return None, None
    if body.kind not in SESSION_KINDS:
        return None, _error(400, f"kind must be one of {SESSION_KINDS}", "invalid_request_error")
    return (body.kind if identity.authenticated else None), None


def _rate_limited() -> bool:
    """SQLite / single-replica: in-proc fixed-window limiter for run creation."""
    now = time.monotonic()
    while _recent and now - _recent[0] > RATE_WINDOW:
        _recent.popleft()
    if len(_recent) >= RATE_LIMIT:
        return True
    _recent.append(now)
    return False


async def _run_limit_error(request: Request, store, identity):
    """Rate + daily-quota gate for run/turn creation. PG mode: per-USER counters in the shared
    rate_limits table (correct across replicas; a global limiter is a one-user-DoS-everyone bug
    at multi-user scale). SQLite mode: the in-proc deque. Operator bypasses."""
    settings = request.app.state.settings
    if identity.is_operator:
        return None
    if not settings.database_url:
        return _error(429, "session rate limit reached — try again shortly",
                      "rate_limit_error") if _rate_limited() else None
    uid = identity.user_id  # operator bypassed above; every remaining caller is a logged-in user
    if not await store.check_rate_limit(f"run:{uid}", RATE_LIMIT, int(RATE_WINDOW)):
        return _error(429, "session rate limit reached — try again shortly", "rate_limit_error")
    if settings.daily_run_quota > 0 and \
            not await store.check_rate_limit(f"quota:{uid}", settings.daily_run_quota, 86400):
        return _error(429, "daily run quota reached — resets tomorrow", "rate_limit_error")
    return None


@router.post("/v1/sessions", status_code=202)
async def create_session(
    body: CreateSession, request: Request, identity: Identity = Depends(require_auth),
    idem: Idem = Depends(idempotency),
):
    store, driver = _plane(request)
    if store is None or driver is None:
        return _error(503, "driver disabled — start the gateway with TOTO_GW_DRIVER=1", "config_error")
    if is_draining():
        return _error(503, "server is draining for deploy — retry shortly", "unavailable")
    if _at_capacity(request.app.state.settings):
        return _error(429, "server at capacity — retry shortly", "capacity_error", retry_after=5)
    if not body.query.strip():
        return _error(400, "query must be non-empty", "invalid_request_error")
    if body.optimize is not None and body.optimize not in OPTIMIZE:
        return _error(400, f"optimize must be one of {OPTIMIZE}", "invalid_request_error")
    kind, err = _kind_or_error(body, identity)
    if err is not None:
        return err

    limited = await _run_limit_error(request, store, identity)
    if limited is not None:
        return limited

    # Idempotency (opt-in): a retry replays the ORIGINAL run_id so the client re-polls the SAME
    # run — no second driver execution, no double token spend. Stored BEFORE the run starts.
    if (hit := await idem.replay()) is not None:
        return hit
    run_id = uuid.uuid4().hex[:12]
    await store.create(run_id, body.query, user_id=identity.user_id)  # turn 1: conv_id defaults to run_id
    await store.publish(run_id, "run_created",
                        {"query": body.query, "optimize": body.optimize, "kind": kind})
    task = asyncio.create_task(_execute(store, driver, run_id, body.query, body.optimize,
                                        kind=kind, user_id=identity.user_id))
    _track_run(task, run_id)
    return await idem.store({"run_id": run_id, "status": "running"}, 202)


@router.post("/v1/sessions/{run_id}/turns", status_code=202)
async def create_turn(
    run_id: str, body: CreateSession, request: Request, identity: Identity = Depends(require_auth),
    idem: Idem = Depends(idempotency),
):
    """Continue a conversation: a new run in the same conv chain. 409 while a turn is live —
    turns are strictly sequential, so only one is ever running."""
    store, driver = _plane(request)
    if store is None or driver is None:
        return _error(503, "driver disabled — start the gateway with TOTO_GW_DRIVER=1", "config_error")
    if is_draining():
        return _error(503, "server is draining for deploy — retry shortly", "unavailable")
    if _at_capacity(request.app.state.settings):
        return _error(429, "server at capacity — retry shortly", "capacity_error", retry_after=5)
    if not body.query.strip():
        return _error(400, "query must be non-empty", "invalid_request_error")
    if body.optimize is not None and body.optimize not in OPTIMIZE:
        return _error(400, f"optimize must be one of {OPTIMIZE}", "invalid_request_error")
    kind, err = _kind_or_error(body, identity)  # same body model → same gate on turns
    if err is not None:
        return err

    # Scope guard: the conversation must be reachable by this caller (own row or legacy NULL).
    if await store.get_session(run_id, user_id=identity.user_id) is None:
        return _error(404, f"unknown session {run_id}", "not_found")
    turns = await store.get_turns(run_id, user_id=identity.user_id)  # accepts any turn's id
    if not turns:
        return _error(404, f"unknown session {run_id}", "not_found")
    latest = turns[-1]
    # Same-lane 409 (companion plan Decision 4): API turns are lane='work'; only a live WORK
    # turn blocks a new one. A live chat turn (companion thinking) never blocks work.
    work = [t for t in turns if t["lane"] == "work"]
    if work and work[-1]["status"] not in TERMINAL_STATUSES:
        return _error(409, "the conversation's latest turn is still running", "conflict")
    limited = await _run_limit_error(request, store, identity)
    if limited is not None:
        return limited

    if (hit := await idem.replay()) is not None:  # opt-in: replay the original turn's run_id
        return hit
    conv_id = turns[0]["conv_id"]                 # = turn-1 run_id
    turn = (latest["turn"] or 1) + 1
    new_run = uuid.uuid4().hex[:12]
    await store.create(new_run, body.query, conv_id=conv_id, turn=turn, user_id=identity.user_id)
    await store.publish(new_run, "run_created",
                  {"query": body.query, "optimize": body.optimize, "conv_id": conv_id, "turn": turn})
    history = await store.get_history(conv_id, turn, max_chars=request.app.state.settings.history_chars,
                  user_id=identity.user_id)
    task = asyncio.create_task(
        _execute(store, driver, new_run, body.query, body.optimize, history,
                 kind=kind, user_id=identity.user_id))
    _track_run(task, new_run)
    return await idem.store(
        {"run_id": new_run, "conv_id": conv_id, "turn": turn, "status": "running"}, 202)


@router.get("/v1/sessions/{conv_id}/turns")
async def get_conversation(conv_id: str, request: Request,
                           identity: Identity = Depends(require_auth)):
    """The conversation snapshot: every turn's full session snapshot ordered by turn, plus the
    summed cost. The dialogue page's one fetch on open. Accepts any turn's id."""
    store, _ = _plane(request)
    if store is None:
        return _error(503, "driver disabled", "config_error")
    if await store.get_session(conv_id, user_id=identity.user_id) is None:
        return _error(404, f"unknown conversation {conv_id}", "not_found")
    turns = await store.get_turns(conv_id, user_id=identity.user_id)
    if not turns:
        return _error(404, f"unknown conversation {conv_id}", "not_found")
    cost = sum(t.get("cost_total") or 0.0 for t in turns)
    return {"conv_id": turns[0]["conv_id"], "cost_total": cost, "turns": turns}


@router.get("/v1/sessions")
async def list_sessions(request: Request, identity: Identity = Depends(require_auth)):
    store, _ = _plane(request)
    if store is None:
        return _error(503, "driver disabled", "config_error")
    return {"sessions": await store.list_sessions(user_id=identity.user_id)}


@router.get("/v1/sessions/{run_id}")
async def get_session(run_id: str, request: Request, identity: Identity = Depends(require_auth)):
    store, _ = _plane(request)
    if store is None:
        return _error(503, "driver disabled", "config_error")
    session = await store.get_session(run_id, user_id=identity.user_id)
    if session is None:
        return _error(404, f"unknown session {run_id}", "not_found")
    session["last_seq"] = (await store.events_after(run_id) or [{"seq": 0}])[-1]["seq"]
    return session


@router.get("/v1/sessions/{run_id}/events")
async def stream_events(run_id: str, request: Request):
    store, _ = _plane(request)
    if store is None:
        return _error(503, "driver disabled", "config_error")
    settings = request.app.state.settings
    # Browser EventSource rides the session cookie automatically (same-origin); scripts/curl use
    # the operator bearer in the Authorization header. The operator token is NOT accepted in the
    # query string — it would leak into access logs and Referer headers (#26).
    identity = await _resolve_identity(request)
    if not identity.authenticated:
        return _error(401, "missing or invalid token", "auth_error")
    if await store.get_session(run_id, user_id=identity.user_id) is None:
        return _error(404, f"unknown session {run_id}", "not_found")

    after = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        after_seq = int(after)
    except ValueError:
        after_seq = 0

    heartbeat = settings.sse_heartbeat_seconds
    async def gen():
        global _sse_connections
        events = store.subscribe(run_id, after_seq).__aiter__()
        _sse_connections += 1
        try:
            while True:
                try:
                    event = await asyncio.wait_for(events.__anext__(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": heartbeat\n\n"
                    continue
                except StopAsyncIteration:
                    return
                data = json.dumps({"ts": event["ts"], **event["data"]}, default=str)
                yield f"id: {event['seq']}\nevent: {event['kind']}\ndata: {data}\n\n"
                if event["kind"] in TERMINAL_KINDS:
                    return
        finally:
            _sse_connections -= 1
            await events.aclose()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _board_channels(user_id: str | None) -> list[str]:
    """The board channels a viewer tails — mirrors the read scope (runs._scope) EXACTLY: a viewer
    gets live deltas for precisely the rows a snapshot GET would return, never more. Each identity
    tails exactly one channel — its own: a logged-in user tails `board:<uid>`, the operator/anon
    service credential tails `board:anon` (where its NULL-owner writes land).

    History: this once unioned `board:anon` into every logged-in viewer's set, because the read
    scope of the day was `user_id = ? OR user_id IS NULL` — NULL-owner rows were public. The
    2026-07-04 strict-IDOR ruling reversed that (fail-closed, own-rows-only): a logged-in user can
    no longer read a NULL-owner row, so tailing `board:anon` only over-delivered deltas it could
    never apply. Do NOT re-add the union without first re-adding the OR-NULL read premise it
    mirrors — the stream must match `_scope`, and `_scope` is strict."""
    return [f"board:{user_id or 'anon'}"]


@router.get("/v1/board/events")
async def stream_board(request: Request):
    """Live board channel: every canvas/list mutation for the caller's user broadcasts here (the
    store publishes on `board:{user}` at each write — one chokepoint for HTTP routes AND the
    companion's store-direct calls). Frontend gets full state from the snapshot GETs and only
    tails NEW mutations from this stream. Mirrors stream_events (same SSE generator + meter +
    heartbeat); the channel never emits a terminal event, so subscribe tails forever."""
    store, _ = _plane(request)
    if store is None:
        return _error(503, "driver disabled", "config_error")
    settings = request.app.state.settings
    # Same auth as stream_events: cookie for the browser, operator ?token= since EventSource
    # can't set an Authorization header.
    identity = await _resolve_identity(request)
    if settings.auth_token and hmac.compare_digest(
            request.query_params.get("token", ""), settings.auth_token):
        identity = OPERATOR
    # Login is the posture now (anon/open-mode removed in the IDOR sweep); the operator ?token=
    # path above authenticates EventSource. Anyone still unauthenticated is turned away.
    if not identity.authenticated:
        return _error(401, "missing or invalid token", "auth_error")

    channels = _board_channels(identity.user_id)

    # Tail-from-now: no resume token → start each channel at its current max seq (NOT 0), so a
    # fresh connection doesn't replay history the snapshot GETs already delivered. seq spaces are
    # per-channel, so the resume cursor is one seq per channel joined by "." (a single channel
    # stays a plain int — the original format). The frontend echoes this back verbatim as
    # Last-Event-ID, so the encoding is private to this endpoint. ponytail: 1–2 channels only.
    after = request.headers.get("last-event-id") or request.query_params.get("after")
    if after is None:
        cursors = [await store.board_latest_seq(ch) for ch in channels]
    else:
        parts = after.split(".")
        cursors = []
        for i in range(len(channels)):
            try:
                cursors.append(int(parts[i]))
            except (ValueError, IndexError):
                cursors.append(0)

    heartbeat = settings.sse_heartbeat_seconds
    async def gen():
        global _sse_connections
        # Merge the per-channel subscriptions into one SSE stream: race each channel's next event,
        # emit as they arrive (per-channel seq order is preserved — one iterator per channel, only
        # re-armed after its event is emitted). Board channels never emit a terminal event, so this
        # tails forever; StopAsyncIteration handling is just defensive.
        iters = [store.subscribe(ch, cur).__aiter__() for ch, cur in zip(channels, cursors)]
        cur = list(cursors)
        pending = {i: asyncio.ensure_future(it.__anext__()) for i, it in enumerate(iters)}
        _sse_connections += 1
        try:
            while pending:
                idx_of = {id(t): i for i, t in pending.items()}
                done, _ = await asyncio.wait(
                    pending.values(), timeout=heartbeat, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    if await request.is_disconnected():
                        return
                    yield ": heartbeat\n\n"
                    continue
                for task in done:
                    i = idx_of[id(task)]
                    try:
                        event = task.result()
                    except StopAsyncIteration:
                        del pending[i]
                        continue
                    cur[i] = event["seq"]
                    pending[i] = asyncio.ensure_future(iters[i].__anext__())
                    data = json.dumps({"ts": event["ts"], **event["data"]}, default=str)
                    eid = ".".join(str(c) for c in cur)
                    yield f"id: {eid}\nevent: {event['kind']}\ndata: {data}\n\n"
        finally:
            _sse_connections -= 1
            for t in pending.values():
                t.cancel()
            for it in iters:
                await it.aclose()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/statusz")
async def statusz(request: Request, identity: Identity = Depends(require_auth)):
    """Replica-local utilization for autoscalers / manual scaling decisions (docs/ops/scaling.md).
    Operator-token only — not public."""
    if not identity.is_operator:
        return _error(403, "operator token required", "authentication_error")
    from .. import __version__
    from ..obs import _mb, peak_rss_bytes, rss_bytes

    cap = request.app.state.settings.max_concurrent_runs
    in_flight = len(_live_run_ids)
    started = getattr(request.app.state, "started_at", None)
    # Pool saturation (PG only): the deeper concurrency ceiling behind the run cap. psycopg_pool's
    # get_stats() is empty until the pool has opened; None in SQLite mode (no pool).
    _pool = getattr(request.app.state.runs, "_pool", None) if request.app.state.runs else None
    _stats = _pool.get_stats() if _pool is not None else {}
    return {
        "in_flight_runs": in_flight,
        "max_concurrent_runs": cap,
        "run_saturation": round(in_flight / cap, 3) if cap > 0 else None,  # null = unlimited
        "pool_size": _stats.get("pool_size"),          # conns the pool currently holds (null=SQLite)
        "pool_available": _stats.get("pool_available"),  # idle conns ready to hand out
        "sse_connections": _sse_connections,
        # Active SSE fan-out backend (PgWakeBus cross-replica | InProcWakeBus single-replica). Lets
        # the scaling runbook confirm PG mode is live at >1 replica without shell access (fanout.md G3).
        "wake_backend": type(request.app.state.runs._wake).__name__ if request.app.state.runs else None,
        "rss_mb": _mb(rss_bytes()),
        "peak_mb": _mb(peak_rss_bytes()),
        "uptime_s": round(time.time() - started, 1) if started else None,
        "draining": is_draining(),
        "version": __version__,
        # Schema-version anchor (PT-D): the forward-only DDL generation this replica booted against.
        # Lets an operator confirm all replicas agree before running a non-additive migration.
        "schema_version": getattr(request.app.state.runs, "schema_version", None)
        if request.app.state.runs else None,
        # Distribution-license posture (W3-C7): full snapshot (org + entitlements + grace/blocked)
        # for the operator. Unlicensed dev deploys report {"state": "unlicensed", ...}.
        "license": (lambda l: l.snapshot() if l is not None else None)(
            getattr(request.app.state, "license_status", None)),
    }
