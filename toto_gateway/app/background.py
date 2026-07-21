"""Background loops the lifespan spawns: memory watermark, run reaper (+ content pruning),
dreamer, calendar sync, boot backfills, and the scheduled refreshers/exporters. Every loop
contains its own failures — a bad tick never kills the loop or the app."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import FastAPI

from ..config import Settings
from ..obs import _mb, peak_rss_bytes, rss_bytes

log = logging.getLogger("toto_gateway")


async def _memory_watermark(settings: Settings, interval: int = 30, poll: int = 10) -> None:
    """Log RSS + peak every `interval`s, or immediately on a >20% jump (a silent SIGKILL/OOM
    then reads as a startup line with no watermark rise before it). Poll is finer than the emit
    cadence so a fast balloon is caught. ponytail: /proc RSS on Linux only."""
    mlog = logging.getLogger("toto_gateway.mem")
    last_emit, last_rss = 0.0, None
    while True:
        await asyncio.sleep(poll)
        cur = rss_bytes()
        jump = bool(last_rss and cur and cur > last_rss * 1.2)
        now = time.monotonic()
        if now - last_emit >= interval or jump:
            mlog.info("memory", extra={"rss_mb": _mb(cur), "peak_mb": _mb(peak_rss_bytes()),
                                       "jump": jump})
            last_emit, last_rss = now, cur


def _prune_request_content(app: FastAPI, settings: Settings, rlog) -> None:
    """Age out captured request_content past content_retention_days (sibling of delta retention).
    Reads the trace-DB engine off the gateway's writer; no SQL sink or retention<=0 → no-op.
    ponytail: lives in _reaper, which needs a runs store to tick — a trace-only deploy (no runs)
    won't prune; move this to its own tick if that deploy shape ever ships content-capture."""
    from ..trace import prune_request_content, sql_engine

    engine = sql_engine(getattr(getattr(app.state, "gateway", None), "writer", None))
    if engine is None:
        return
    pruned = prune_request_content(engine, settings.content_retention_days)
    if pruned:
        rlog.info("pruned request_content rows", extra={"count": pruned})


async def _benchmark_refresher(app: FastAPI, settings: Settings) -> None:
    """Scheduled benchmark refresh: every benchmark_refresh_hours, run the SAME ingest+overlay the
    admin endpoint calls (admin_benchmarks.run_refresh) — one code path, no drift. First tick
    delayed one interval (boot already overlays). A failed tick logs once and keeps ticking; the
    lifespan cancels this task cleanly on shutdown."""
    from ..routes.admin_benchmarks import run_refresh

    blog = logging.getLogger("toto_gateway.benchmarks")
    interval = settings.benchmark_refresh_hours * 3600
    while True:
        await asyncio.sleep(interval)
        try:
            report = await run_refresh(app)
            blog.info("scheduled benchmark refresh", extra={"overlay": report.get("_overlay")})
        except Exception as e:  # noqa: BLE001 — never kill the loop or the app
            blog.warning("scheduled benchmark refresh failed", extra={"err": str(e)})


async def _inventory_refresher(app: FastAPI, settings: Settings) -> None:
    """Delayed scheduled inventory refresh through the same compile/submit path as the API."""
    from ..benchmarking.platform import InventoryRefreshIntent, PlatformActor
    from ..catalog_sync import probe_availability

    ilog = logging.getLogger("toto_gateway.inventory")
    interval = settings.inventory_refresh_hours * 3600
    actor = PlatformActor(actor_id="scheduled-inventory", is_operator=True, kind="system")
    while True:
        await asyncio.sleep(interval)
        try:
            plan = await app.state.benchmark_platform.compile(
                InventoryRefreshIntent(providers=("openrouter", "fireworks"), scope="platform"),
                actor,
            )
            operation = await app.state.benchmark_platform.submit(
                plan,
                actor,
                idempotency_key=f"scheduled:{int(time.time() // interval)}",
            )
            await app.state.benchmark_platform.wait(operation.operation_id)
        except Exception as error:  # noqa: BLE001 - one failed tick never kills scheduling
            ilog.warning("scheduled inventory refresh failed", extra={"err": str(error)})
        try:  # availability probe rides the same cadence; its failure never kills the loop
            app.state.catalog_availability = await probe_availability(app.state.gateway.catalog.models)
        except Exception as error:  # noqa: BLE001
            ilog.warning("scheduled availability probe failed", extra={"err": str(error)})


async def _audit_exporter(app: FastAPI, settings: Settings) -> None:
    """Scheduled audit export: every audit_export_tick_seconds, for each org with export enabled
    whose per-org cadence is due, run the SAME engine the manual .../run route calls (one code
    path, no drift). Each org's failure is contained — recorded as last_error and retried next
    cycle — and NEVER touches the serving path. Retention pruning rides the same per-org run."""
    from ..audit_export import run_export_for_org
    from ..credentials import credentials_secret
    from ..storage import get_object_store
    from ..trace import sql_engine

    xlog = logging.getLogger("toto_gateway.audit_export")
    auth = getattr(app.state, "auth", None)
    if auth is None:
        return
    interval = settings.audit_export_tick_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            orgs = await auth.list_audit_export_orgs()
        except Exception:  # noqa: BLE001 — a bad tick never kills the loop
            xlog.exception("audit-export: could not list orgs")
            continue
        engine = sql_engine(getattr(getattr(app.state, "gateway", None), "writer", None))
        store = get_object_store(settings)
        secret = credentials_secret(settings)
        now = time.time()
        for cfg in orgs:
            if now - (cfg.get("last_run") or 0) < (cfg.get("cadence_hours") or 24) * 3600:
                continue  # not yet due
            org = cfg["org_id"]
            try:
                await run_export_for_org(auth, engine, store, org_id=org, cfg=cfg, secret_key=secret)
                await auth.set_audit_export_run(org, last_run=time.time(), last_error=None)
            except Exception as exc:  # noqa: BLE001 — one org's failure never stops the others
                await auth.set_audit_export_run(org, last_run=time.time(), last_error=str(exc))
                xlog.warning("audit-export failed", extra={"org_id": org, "err": str(exc)})


async def _retention_sweeper(app: FastAPI, settings: Settings) -> None:
    """Content-plane retention: every retention_sweep_tick_seconds, age out USER-INVOKED PRODUCT
    storage (content-plane documents + doc_embeddings, explicit user_memory facts) per each org's
    retention policy — the sinks zero-retention deliberately excludes. Same code path as the
    manual POST .../retention/run route (one engine, no drift). Deletes are batch-bounded per tick;
    each org's failure is contained (retention.run_retention_sweep logs and continues). Never
    touches the serving path or trace/telemetry metadata."""
    from ..retention import run_retention_sweep

    slog = logging.getLogger("toto_gateway.retention")
    auth = getattr(app.state, "auth", None)
    if auth is None:
        return
    interval = settings.retention_sweep_tick_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            summary = await run_retention_sweep(
                auth, getattr(app.state, "content", None), getattr(app.state, "runs", None),
                batch_limit=settings.retention_batch_limit)
            if summary:
                slog.info("retention sweep", extra={"orgs": len(summary)})
        except Exception:  # noqa: BLE001 — a bad tick never kills the loop
            slog.exception("retention sweep tick failed")


async def _reaper(app: FastAPI, settings: Settings, interval: int = 60) -> None:
    """Every `interval`s, reclaim any run whose lease has expired (no renewing event for
    run_timeout) and fail it — fixes stuck-forever crash state and hung runs. The lease reclaim
    is atomic across replicas (UPDATE ... RETURNING), so multiple reapers never double-fail a
    run. finish() publishes the terminal event so SSE clients unstick."""
    rlog = logging.getLogger("toto_gateway.reaper")
    store = getattr(app.state, "runs", None)
    if store is None:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            for run_id in await store.reclaim_expired_leases():
                await store.finish(run_id, status="failed", error="run timed out (reaped)")
                rlog.warning("reaped stale run", extra={"run_id": run_id})
            pruned = await store.prune_deltas(settings.delta_retention_days)  # delta retention
            if pruned:
                rlog.info("pruned answer_delta events", extra={"count": pruned})
            store.ensure_event_partitions()  # keep current+next month partitions ahead (PG only, sync init conn)
            _prune_request_content(app, settings, rlog)  # observability content retention (sibling)
        except Exception:
            rlog.exception("reaper error")


_DREAM_LOCK = 0x70D0  # arbitrary fixed pg advisory-lock key for the dreamer tick (one per app)


async def _dreamer(app: FastAPI, settings: Settings, interval: int = 3600) -> None:
    """Nightly memory consolidation: sibling of _reaper. Ticks hourly; when the UTC hour matches
    memory_dream_hour, one replica (advisory-lock leader) runs dream_tenant for each active
    tenant that hasn't been claimed today. Per-tenant idempotency + cross-replica election both
    come from the claim_dream_run row. Every failure is contained — a bad tenant pass is
    recorded 'failed' and the tick moves on."""
    dlog = logging.getLogger("toto_gateway.dreamer")
    runs = getattr(app.state, "runs", None)
    content = getattr(app.state, "content", None)
    gateway = getattr(app.state, "gateway", None)
    memory = getattr(app.state, "memory", None)
    if runs is None or content is None or gateway is None:
        return
    from datetime import datetime, timezone

    from ..dreams import dream_tenant
    from ..routes.deps import _resolve_tenant

    model = settings.memory_extract_model or settings.triage_model
    while True:
        await asyncio.sleep(interval)
        try:
            now = datetime.now(timezone.utc)
            if now.hour != settings.memory_dream_hour:
                continue
            date = now.strftime("%Y-%m-%d")
            locked, release = await runs.try_advisory_lock(_DREAM_LOCK)
            if not locked:
                continue  # another replica has the tick
            try:
                active = await runs.active_tenants(time.time() - settings.memory_dream_stale_days * 86400)
                for uid in active:
                    tenant_id = _resolve_tenant(uid)
                    if not tenant_id or not await runs.claim_dream_run(tenant_id, date):
                        continue  # already claimed today (idempotent) or unresolvable tenant
                    try:
                        out = await dream_tenant(
                            tenant_id, uid, gateway=gateway, content=content, memory=memory,
                            runs=runs, budget_usd=settings.memory_dream_daily_usd,
                            stale_days=settings.memory_dream_stale_days,
                            merge_sim=settings.memory_dream_merge_sim, model=model)
                        await runs.finish_dream_run(tenant_id, date, merged=out["merged"],
                                                    archived=out["archived"], cost_usd=out["cost_usd"])
                        dlog.info("dreamed", extra={"tenant": tenant_id[:8], **out})
                    except Exception:
                        await runs.finish_dream_run(tenant_id, date, merged=0, archived=0,
                                                    cost_usd=0.0, status="failed")
                        dlog.exception("dream pass failed", extra={"tenant": tenant_id[:8]})
            finally:
                await release()
        except Exception:
            dlog.exception("dreamer tick error")


_CAL_SYNC_LOCK = 0xCA1E  # fixed pg advisory-lock key for the calendar sync tick (one leader)


async def calsync_tick(store, settings: Settings, client) -> int:
    """One calendar-sync pass (extracted so a test can drive it without the loop/sleep). For each
    calendar object, fold in its two external sources — ICS subscriptions (always) and, when the
    Pipedream pilot is enabled, the owner's connected Google Calendar — then write the object back
    scoped to its owner (put_object re-scopes via user_id). Each merge REPLACES only its own source
    (source:"toto" local events are never touched). Returns the count of objects written. Every
    per-source failure is contained (logged + skipped) so one dead feed/account can't stall the rest.

    Pipedream is metered per real pull (pd-metering) and cached per user within the tick so N
    calendars owned by one user cost one accounts-lookup + one events-pull, not N of each."""
    from .. import pipedream  # app-plane calendar connector; imported here so the OSS export can drop it
    from ..ics import merge_events, parse_ics

    clog = logging.getLogger("toto_gateway.calsync")
    pd_on = pipedream.enabled(settings)
    pd = pipedream.PipedreamClient(settings, client) if pd_on else None
    # Per-tick caches so a paid Pipedream call happens at most once per (user) / (account).
    pd_accounts: dict = {}   # user_id -> [account, ...]
    pd_events: dict = {}     # account_id -> [event, ...]
    written = 0
    for obj in await store.all_objects_of_kind("calendar"):
        events = list(obj["payload"].get("events") or [])
        changed = False

        # ICS subscriptions — no OAuth, covers Google/Apple/Outlook.
        for sub in obj["payload"].get("subscriptions") or []:
            url = sub.get("url") if isinstance(sub, dict) else sub
            label = (sub.get("label") if isinstance(sub, dict) else None) or url
            if not url:
                continue
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                fetched = parse_ics(resp.text)
            except Exception:
                clog.warning("calendar feed fetch failed", extra={"url": str(url)[:120]})
                continue
            events = merge_events(events, label, fetched, max_events=settings.cal_sync_max_events)
            changed = True

        # Pipedream Connect — the owner's connected Google Calendar.
        uid = obj.get("user_id")
        if pd is not None and uid:
            try:
                if uid not in pd_accounts:
                    pd_accounts[uid] = await pd.list_accounts(uid)
                acct = next((a for a in pd_accounts[uid]
                             if (a.get("app") or {}).get("name_slug") == pipedream.GCAL_SLUG), None)
                if acct and acct.get("id"):
                    aid = acct["id"]
                    if aid not in pd_events:
                        pd_events[aid] = await pd.calendar_events(uid, aid)
                        await store.log_pipedream(uid, 1, pipedream.EST_USD_PER_CALL)
                    events = merge_events(events, "google", pd_events[aid],
                                          max_events=settings.cal_sync_max_events)
                    changed = True
            except Exception:
                clog.warning("pipedream calendar sync failed", extra={"user": str(uid)[:8]})

        if not changed:
            continue
        await store.put_object("calendar", obj["object_id"], {**obj["payload"], "events": events},
                               user_id=uid)
        written += 1
        clog.info("calendar synced", extra={"object_id": obj["object_id"], "events": len(events)})
    return written


async def _calsync(app: FastAPI, settings: Settings) -> None:
    """Calendar sync loop: sibling of _reaper/_dreamer. Every cal_sync_interval seconds, one
    replica (advisory-lock leader) runs calsync_tick over every calendar object. Iterates
    objects per user like _dreamer iterates tenants."""
    clog = logging.getLogger("toto_gateway.calsync")
    store = getattr(app.state, "runs", None)
    if store is None:
        return
    import httpx

    while True:
        await asyncio.sleep(settings.cal_sync_interval)
        try:
            locked, release = await store.try_advisory_lock(_CAL_SYNC_LOCK)
            if not locked:
                continue  # another replica owns the tick
            try:
                timeout = httpx.Timeout(settings.cal_sync_timeout, connect=settings.cal_sync_timeout)
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    await calsync_tick(store, settings, client)
            finally:
                await release()
        except Exception:
            clog.exception("calsync tick error")


async def _backfill_notes(app: FastAPI) -> None:
    """Boot backfill: move any user-owned note bodies still in canvas_objects.payload into the
    content plane. Backgrounded so it NEVER blocks readiness, with an EXISTS short-circuit that
    returns instantly on a clean table. Idempotent; a content-plane outage here can't stop the
    gateway (unmoved rows keep their payload body, served by the read fallback) — it logs and
    retries next boot."""
    try:
        from ..content import backfill_note_bodies

        moved = await backfill_note_bodies(app.state.runs, app.state.content)
        if moved:
            log.info("note-body backfill", extra={"moved": moved})
    except Exception:
        log.exception("note-body backfill failed — will retry next boot")


async def _backfill_embeddings(app: FastAPI) -> None:
    """Boot backfill: embed any content-plane documents that have no embedding rows yet (written
    while the memory plane was off, or before an embedding key existed). Idempotent + a no-op once
    the corpus is indexed — embeddings are durable now. Failure logs and retries next boot."""
    try:
        from ..content import backfill_embeddings

        n = await backfill_embeddings(app.state.content)
        if n:
            log.info("content embedding backfill", extra={"embedded": n})
    except Exception:
        log.exception("content embedding backfill failed — will retry next boot")
