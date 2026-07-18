"""FastAPI application factory.

`create_app()` builds the catalog, runner registry, trace writer, and gateway, and wires them
into `app.state`. Tests can inject a pre-built `gateway` (e.g. with a fake runner factory) to
exercise the full HTTP surface offline.
"""

from __future__ import annotations

from fastapi import FastAPI

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from . import __version__, pipedream
from .catalog import Catalog
from .config import Settings, get_settings
from .gateway import Gateway
from .obs import (
    RequestContextMiddleware, _mb, init_sentry, peak_rss_bytes, redact_settings, request_id_var,
    rss_bytes,
)
# Core (gateway-plane) routers only. App-plane routers (companion, canvas, lists, …) are
# lazy-imported behind the plane/edition gate below — the OSS export deletes those modules
# wholesale (edition seam), so nothing here may import them at module top.
from .routes import (
    admin_analytics, admin_catalog, admin_catalog_adoptions, admin_catalog_sync, admin_labeling,
    admin_latency, admin_providers, admin_requests, admin_routing, admin_usage, auth, chat,
    credentials, custom_tools, health, metrics, models, prewarm, route, routing, sessions, tokens,
)
from .runners.registry import RunnerRegistry
from .trace import build_writer_from_settings

log = logging.getLogger("toto_gateway")


def build_gateway(settings: Settings) -> Gateway:
    from .driver import prompts

    # Config → prompts seam: the classifier prompt variant both live call sites (driver label
    # node, /v1 smart route) build with. Raises on an unknown value — boot-time, never mid-request.
    prompts.set_label_variant(settings.label_prompt_variant)
    # Same seam for the subagent runners flag: on → _clean_task admits requires.runner ∈
    # {pi, claude_code}; off (default) → the pin is stripped, exactly as before C2.
    prompts.set_subagent_runners(settings.subagent_runners)
    catalog = Catalog.load(settings.catalog)
    if settings.fake_exec:
        from .runners.fake import FakeRunner

        registry = RunnerRegistry(factory=FakeRunner)  # real routing, fake (offline) execution
    else:
        registry = RunnerRegistry()
    writer = build_writer_from_settings(settings)

    # Phase 1 brain, gated by config (default off → exact Phase-0 passthrough).
    extractor = guard = router = cache = None
    if settings.routing:
        from .routing.decision import GuardRouter
        from .signals.extractor import HeuristicExtractor
        from .signals.guards import RuleGuard

        # The raw passthrough's safety floor: guard (fail-closed) + policy, honoring the
        # requested model otherwise. Content-based routing lives in the driver (/v1/route).
        extractor, guard, router = HeuristicExtractor(), RuleGuard(), GuardRouter()
    if settings.cache:
        from .cache.exact import ExactCache

        cache = ExactCache()

    # Smart auto-routing (SR1): the `smart` sentinel classifies + routes on the passthrough plane.
    # Reuses the SAME label bindings + classifier model the driver uses; no TOTO_GW_DRIVER needed
    # (the gateway makes the classify call itself). labels=None (routing off / soft-disabled) →
    # smart still answers, degrading to the benchmark default.
    from .benchmarks import Benchmarks
    from .routing.smart import TotoStickiness

    # One Redis client (when TOTO_GW_REDIS_URL is set) shared by the breaker's cross-replica OPEN
    # state (Wave 2 R1) AND the label-memo L2 (S4). None → per-replica behaviour for both.
    redis_client = _breaker_redis(settings)

    return Gateway(
        catalog=catalog, registry=registry, writer=writer,
        extractor=extractor, guard=guard, router=router, cache=cache,
        labels=_build_labels(settings, catalog),
        benchmarks=Benchmarks.load(settings.benchmarks),
        # Stickiness ladder (S1 seam → S4 composite). TotoStickiness is one class, rungs in
        # precedence: DeclaredSession (declared:<hash> → long eager hold) > LabelAwareTTL (per-task-
        # type: org `stick_ttls` > TOTO_GW_STICK_TTLS default > flat 900s) > WarmthHold floor (a hot
        # conversation extends the pin). With no maps and cold conversations it is the flat 900s slide.
        # Bare Gateway() in tests stays None.
        stick=TotoStickiness(settings.stick_ttls_map),
        # Smart-route tagging model: the dedicated smart_classifier_model when set (points ONLY the
        # /v1 smart route at e.g. or-gemini-2.5-flash), else the shared label_classifier_model. A configured id
        # absent from the catalog degrades gracefully in smart_route (never 500).
        classifier_model=(settings.smart_classifier_model or settings.label_classifier_model),
        label_timeout_ms=settings.label_timeout_ms,
        max_concurrent_llm=settings.max_concurrent_llm_calls,
        retries=settings.provider_retries,
        backoff_base=settings.provider_backoff_base,
        backoff_cap=settings.provider_backoff_cap,
        passthrough_fallback=settings.passthrough_fallback,
        breaker_fail_threshold=settings.breaker_fail_threshold,
        breaker_reset_seconds=settings.breaker_reset_seconds,
        # Wave 2 R1: cross-replica shared breaker OPEN state when a Redis URL is set (else None →
        # per-replica). Lazy client (connects on first command); fail-open on any Redis error.
        breaker_redis=redis_client,
        # S4: the same client backs the label-memo L2 (cross-replica classification sharing).
        memo_redis=redis_client,
        # Chunk B: hold a conversation's warm model over a fresh pick while its provider prefix cache
        # is live (kill-switch TOTO_GW_WARMTH_ROUTING, default on). Off → fresh resolution every turn.
        warmth_routing=settings.warmth_routing,
        stream_stall_timeout=settings.stream_stall_timeout,
        # Observability content-capture (TOTO_GW_LOG_CONTENT, default ON): capture prompt+response
        # per request into request_content, for the activity-log drill-down. Access-scoped on read.
        log_content=settings.log_content,
        # Breaker transitions (circuit_open/circuit_close) land in the same JSONL provenance floor
        # as driver spans, so an operator sees a provider trip without extra plumbing.
        observe=_make_span_observer(settings),
    )


def _build_labels(settings: Settings, catalog):
    """LabelBindings for BOTH planes (gateway smart route + driver label routing). Any incoherence
    between the bindings and THIS catalog — a bound id the catalog doesn't carry (normal on
    reduced/dev catalogs), a fake-lane binding, or the classifier model missing — soft-disables
    the feature (None) with a loud log; routing falls back to benchmarks. Boot never blocks."""
    if not settings.label_routing:
        return None
    from .routing.labels import LabelBindings

    labels = LabelBindings(settings.label_bindings or None)
    errs = labels.validate(catalog)
    if catalog.get(settings.label_classifier_model) is None:
        errs.append(f"classifier model {settings.label_classifier_model!r} not in catalog")
    if errs:
        logging.getLogger("toto_gateway.routing").error(
            "label routing disabled: %s", "; ".join(errs))
        return None
    return labels


def _breaker_redis(settings: Settings):
    """The optional redis.asyncio client for cross-replica breaker state (Wave 2 R1). None unless a
    Redis URL is set. Lazy (from_url connects on first command), so no loop needed here at build."""
    if not settings.redis_url:
        return None
    import redis.asyncio as redis
    return redis.from_url(settings.redis_url)


def _make_span_observer(settings: Settings):
    """Append each driver span as one JSON line — the always-on local provenance floor.

    ponytail: open-per-span is fine at the driver's request rate; add a buffered handle if
    span volume ever dominates. Failures are swallowed — observability never breaks a run.
    """
    path = settings.driver_spans_jsonl

    def observe(span: dict) -> None:
        # W1-C4: driver spans carry task text (a durable payload sink). A zero-retention org's run
        # leaves no span line on disk; its routing decision still lives, payload-free, on the
        # gateway_events trace row. Identity-thin runs (no org) write spans exactly as before.
        from .routes.deps import current_identity

        if getattr(current_identity(), "zero_retention", False):
            return
        try:
            with open(path, "a") as f:
                f.write(json.dumps(span, default=str) + "\n")
        except Exception:
            pass

    return observe


async def audit_driver_denial(auth, identity, exc) -> None:
    """W2-C4 small-fix: write a catalog.model_denied audit row for an org-allowlist denial on the
    driver plane (chat.py already does this on its plane; the gateway has no store handle, so the
    driver's gateway-bridge closure calls this). ONLY the allowlist gate — the C2 per-team deny
    (exc.allowlist False) keeps its policy_violation shape and is not an org-governance event. No-op
    when the store is absent (offline/test gateways) or it isn't an allowlist denial."""
    from . import audit

    if auth is None or not getattr(exc, "allowlist", False):
        return
    await audit.record(
        auth, "catalog.model_denied",
        actor_user_id=getattr(identity, "user_id", None),
        org_id=getattr(identity, "org_id", None),
        target_type="model", target_id=getattr(exc, "model_id", None),
        meta={"reason": "allowlist", "plane": "driver"})


def build_driver(settings: Settings, gateway: Gateway, runs=None, auth=None):
    """The Sonnet-class driver on top of the passthrough gateway. The gateway is a pure
    executor here — the driver owns routing (its metadata classifier) and guarding.
    With a RunStore, spans also fan out to the live SSE plane (JSONL sink is untouched)."""
    from .driver.core import Driver, Exec
    from .pipeline import ModelNotPermittedError

    if settings.prompts_file:  # dev prompt overrides — no-op when unset/missing, loud when malformed
        from .driver import prompts

        prompts.load_overrides_file(settings.prompts_file)

    from .routes.deps import current_identity

    async def complete_fn(req) -> "Exec":
        # Enforce the caller's team catalog policy on the driver plane too (C2): the identity was
        # stashed by require_auth and rides the request context into this boot-time closure.
        try:
            res = await gateway.complete(req, harness="driver", identity=current_identity())
        except ModelNotPermittedError as exc:
            # W2-C4 small-fix: mirror chat.py — an org-allowlist denial on the driver plane also
            # writes a catalog.model_denied audit row (the gateway itself has no store handle, so
            # this driver-plane choke does it). Only the allowlist gate, not the C2 per-team deny.
            await audit_driver_denial(auth, current_identity(), exc)
            raise
        t = res.trace
        txt = res.response.choices[0].message.content if res.response.choices else ""
        r = res.response  # carries what the upstream actually served (absent on fakes → None)
        return Exec(
            text=txt or "", model=t.model, lane=t.lane,
            tokens_prompt=t.tokens_prompt or 0, tokens_completion=t.tokens_completion or 0,
            tokens_cached=t.tokens_cached or 0,
            cost_usd=t.cost_usd, latency_ms=t.latency_ms_total or 0,
            upstream_model=getattr(r, "upstream_model", None) or "",
            provider=getattr(r, "provider", None) or "",
            generation_id=getattr(r, "generation_id", None) or "",
        )

    async def stream_fn(req, on_delta) -> "Exec":
        """Stream a driver answer: forward text deltas to on_delta, recover provenance from the
        finished trace (gateway writes it on close)."""
        box: dict = {}
        acc: list[str] = []
        async for chunk in gateway.stream(req, harness="driver", identity=current_identity(),
                                          on_trace=lambda tr: box.__setitem__("t", tr)):
            for ch in chunk.choices:
                if ch.delta.content:
                    acc.append(ch.delta.content)
                    await on_delta(ch.delta.content)  # publishes the batch to the async run store
        t = box.get("t")
        return Exec(
            text="".join(acc), model=t.model if t else req.model, lane=t.lane if t else "",
            tokens_prompt=(t.tokens_prompt or 0) if t else 0,
            tokens_completion=(t.tokens_completion or 0) if t else 0,
            tokens_cached=(t.tokens_cached or 0) if t else 0,
            cost_usd=t.cost_usd if t else None, latency_ms=(t.latency_ms_total or 0) if t else 0,
        )

    toto = None
    if settings.toto_enabled:
        from .driver.toto_client import TotoClient

        toto = TotoClient(settings.toto_url, settings.toto_token)

    jsonl_observe = _make_span_observer(settings)

    def _stamp(span: dict) -> dict:
        # Correlate every span back to the request that started the run (obs.request_id_var is
        # set by the request middleware and copied into this run's task at create_task time).
        rid = request_id_var.get()
        if rid:
            span.setdefault("request_id", rid)
        return span

    if runs is None:
        async def observe(span: dict) -> None:
            jsonl_observe(_stamp(span))
    else:
        async def observe(span: dict) -> None:
            _stamp(span)
            jsonl_observe(span)  # sync file append (always-on local floor)
            try:
                await runs.span_observer(span)  # async fan-out to the SSE plane
            except Exception:
                pass  # observability must never break the run

    # Embedding routing + experience corpus (docs/plans/2026-07-02-embedding-rag-routing.md).
    # Embedder is None without an OpenRouter key → both silently degrade to the keyword path.
    from .embeddings import build_embedder

    # fake_exec = fully offline (tests, no-key quickstart) → no real embedding calls either.
    embedder = None if settings.fake_exec else build_embedder(settings, store=runs)
    corpus_sink = None
    if runs is not None and embedder is not None and settings.embed_corpus:
        from .runs import CURRENT_RUN_ID

        async def corpus_sink(task_id, text, skill, model_id, outcome, cost_usd, latency_ms):
            run_id = CURRENT_RUN_ID.get()
            if not run_id:
                return
            # W1-C4: the experience corpus persists task text (a durable payload sink). A
            # zero-retention org contributes no rows — routing for its traffic falls back to the
            # payload-free paths (benchmark/label), exactly as when the corpus is empty.
            if getattr(current_identity(), "zero_retention", False):
                return
            vec = await embedder.embed_one(text)  # cache hit if routing already embedded it
            if vec is None:
                return
            uid = (await runs.get_session(run_id) or {}).get("user_id")
            await runs.write_task_embedding(run_id, task_id, text, vec, skill=skill, model_id=model_id,
                                      outcome=outcome, cost_usd=cost_usd, latency_ms=latency_ms,
                                      user_id=uid)

    # Experience-kNN proposer — None unless TOTO_GW_EXPERIENCE_KNN is on AND an embedder exists.
    from .driver.knn import build_experience_knn

    knn = build_experience_knn(settings, runs, embedder, gateway.catalog)

    # Label routing (default ON). Any incoherence between bindings and the running catalog —
    # a bound id this catalog doesn't carry (normal on reduced/dev catalogs), a fake-lane
    # binding, or a missing classifier model — soft-disables the feature with a loud log and
    # routing falls back to classify(). Boot never blocks: the ladder below is always correct
    # (the Benchmarks.load missing-file semantics).
    # Shared with the gateway's smart route (SR1): one soft-disable path, one loud log. ERROR, not
    # warning — on the full catalog a typo'd labels.yaml silently reverts the fleet to benchmark
    # routing. The shipped file is CI-guarded (test_shipped_bindings_are_valid_against_openrouter_
    # catalog); the log is the tripwire for custom TOTO_GW_LABEL_BINDINGS files and reduced catalogs.
    labels = _build_labels(settings, gateway.catalog)

    # Subagent runners (C2): flag on → live pi/claude_code adapters register before the gateway
    # catch-all; pi's callback provider is THIS gateway's /v1 (loopback), authing with the
    # operator token when auth is on. Flag off → None → default gateway-only registry.
    adapters = None
    if settings.subagent_runners:
        from .driver.adapters import AdapterRegistry

        adapters = AdapterRegistry.with_subagents(
            complete_fn, gateway_base_url=f"http://127.0.0.1:{settings.port}/v1",
            gateway_api_key=settings.auth_token, timeout=settings.subagent_timeout)

    return Driver(
        catalog=gateway.catalog, complete_fn=complete_fn,
        driver_model=settings.driver_model, triage_model=settings.triage_model,
        toto=toto, observe=observe, adapters=adapters,
        # Share the gateway's Benchmarks object (not a second load) so the boot overlay + a
        # POST /v1/admin/benchmarks/refresh hot-swap are seen by BOTH planes at once (B3).
        benchmarks=gateway.benchmarks,
        preferences=(runs.get_preferences if runs is not None else None),
        max_tokens={
            "triage": settings.max_tokens_triage,
            "answer": settings.max_tokens_answer,
            "decompose": settings.max_tokens_decompose,
            "dispatch": settings.max_tokens_dispatch,
            "synthesize": settings.max_tokens_synthesize,
        },
        # Stream answers to the SSE plane only when there's a run store to publish into.
        stream_fn=(stream_fn if runs is not None else None),
        emit_delta=(runs.publish_delta if runs is not None else None),
        provider_retries=settings.provider_retries,
        provider_backoff_base=settings.provider_backoff_base,
        embedder=embedder,
        embed_routing=settings.embed_routing,
        corpus_sink=corpus_sink,
        knn=knn,
        labels=labels,
        label_model=settings.label_classifier_model,
        label_timeout_ms=settings.label_timeout_ms,
        delta_flush_chars=settings.delta_flush_chars,
        delta_flush_ms=settings.delta_flush_ms,
    )


async def _memory_watermark(settings: Settings, interval: int = 30, poll: int = 10) -> None:
    """Task #17 diagnostic: log RSS + peak every `interval`s, or immediately on a >20% jump (a
    silent SIGKILL/OOM then reads as a startup line with no watermark rise before it). Poll is
    finer than the emit cadence so a fast balloon is caught. ponytail: /proc RSS on Linux only."""
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
    from .trace import prune_request_content, sql_engine

    engine = sql_engine(getattr(getattr(app.state, "gateway", None), "writer", None))
    if engine is None:
        return
    pruned = prune_request_content(engine, settings.content_retention_days)
    if pruned:
        rlog.info("pruned request_content rows", extra={"count": pruned})


async def _benchmark_refresher(app: FastAPI, settings: Settings) -> None:
    """B5 scheduled refresh: every benchmark_refresh_hours, run the SAME ingest+overlay the admin
    endpoint calls (admin_benchmarks.run_refresh) — one code path, no drift. First tick delayed one
    interval (boot already overlays). A failed tick logs once and keeps ticking; the lifespan
    cancels this task cleanly on shutdown."""
    from .routes.admin_benchmarks import run_refresh

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
    from .benchmarking.platform import InventoryRefreshIntent, PlatformActor
    from .catalog_sync import probe_availability

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
    """W2-C4 scheduled audit export: every audit_export_tick_seconds, for each org with export
    enabled whose per-org cadence is due, run the SAME engine the manual .../run route calls (one
    code path, no drift). Each org's failure is contained — recorded as last_error and retried next
    cycle — and NEVER touches the serving path. Retention pruning rides the same per-org run."""
    from .audit_export import run_export_for_org
    from .credentials import credentials_secret
    from .storage import get_object_store
    from .trace import sql_engine

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
    """W3-C6 content-plane retention: every retention_sweep_tick_seconds, age out USER-INVOKED
    PRODUCT storage (content-plane documents + doc_embeddings, explicit user_memory facts) per each
    org's retention policy — the sinks zero-retention deliberately excludes. Same code path as the
    manual POST .../retention/run route (one engine, no drift). Deletes are batch-bounded per tick;
    each org's failure is contained (retention.run_retention_sweep logs and continues). Never touches
    the serving path or trace/telemetry metadata."""
    from .retention import run_retention_sweep

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
    """Plan D3 + steal 3: every `interval`s, reclaim any run whose lease has expired (no renewing
    event for run_timeout) and fail it — fixes stuck-forever crash state and hung runs. The lease
    reclaim is atomic across replicas (UPDATE ... RETURNING), so multiple reapers never double-fail
    a run. finish() publishes the terminal event so SSE clients unstick."""
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
            pruned = await store.prune_deltas(settings.delta_retention_days)  # A4.2 delta retention
            if pruned:
                rlog.info("pruned answer_delta events", extra={"count": pruned})
            store.ensure_event_partitions()  # keep current+next month partitions ahead (PG only, sync init conn)
            _prune_request_content(app, settings, rlog)  # observability content retention (sibling)
        except Exception:
            rlog.exception("reaper error")


_DREAM_LOCK = 0x70D0  # arbitrary fixed pg advisory-lock key for the dreamer tick (one per app)


async def _dreamer(app: FastAPI, settings: Settings, interval: int = 3600) -> None:
    """Nightly memory consolidation (memory-lifecycle P1): sibling of _reaper. Ticks hourly; when
    the UTC hour matches memory_dream_hour, one replica (advisory-lock leader) runs dream_tenant
    for each active tenant that hasn't been claimed today. Per-tenant idempotency + cross-replica
    election both come from the claim_dream_run row. Every failure is contained — a bad tenant
    pass is recorded 'failed' and the tick moves on."""
    dlog = logging.getLogger("toto_gateway.dreamer")
    runs = getattr(app.state, "runs", None)
    content = getattr(app.state, "content", None)
    gateway = getattr(app.state, "gateway", None)
    memory = getattr(app.state, "memory", None)
    if runs is None or content is None or gateway is None:
        return
    from datetime import datetime, timezone

    from .dreams import dream_tenant
    from .routes.deps import _resolve_tenant

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
    from .ics import merge_events, parse_ics

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

        # ICS subscriptions (rung P1) — no OAuth, covers Google/Apple/Outlook.
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

        # Pipedream Connect (calendar-login pilot) — the owner's connected Google Calendar.
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
    """Calendar ICS subscribe (calendar kind, rung P1): sibling of _reaper/_dreamer. Every
    cal_sync_interval seconds, one replica (advisory-lock leader) runs calsync_tick over every
    calendar object. Iterates objects per user like _dreamer iterates tenants."""
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
    """Boot backfill (brain-markdown Phase-2): move any user-owned note bodies still in
    canvas_objects.payload into the content plane. Backgrounded so it NEVER blocks readiness, with
    an EXISTS short-circuit that returns instantly on a clean table. Idempotent; a content-plane
    outage here can't stop the gateway (unmoved rows keep their payload body, served by the read
    fallback) — it logs and retries next boot."""
    try:
        from .content import backfill_note_bodies

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
        from .content import backfill_embeddings

        n = await backfill_embeddings(app.state.content)
        if n:
            log.info("content embedding backfill", extra={"embedded": n})
    except Exception:
        log.exception("content embedding backfill failed — will retry next boot")


# App-wide CSP for the same-origin SPA + API. 'unsafe-inline' covers SvelteKit's hydration
# bootstrap script/style; everything else is clamped to 'self' so injected external scripts
# can't load and exfil is blocked. ponytail: tighten to nonces/hashes if we drop unsafe-inline.
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
        # media-src: companion TTS plays blob: object-URLs; without an explicit media-src,
        # <audio> falls back to default-src and the browser SILENTLY drops the source
        # (play() resolves, no error, no sound). blob: for media only — never scripts.
        "media-src 'self' blob:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def _apply_security_headers(headers) -> None:
    """Set the app-wide security headers on a response's (Mutable)Headers in place. Routes that
    ship their own Content-Security-Policy (the bindle sandbox, deliberately frameable
    same-origin) opt out of the app policy AND its frame ban — we never clobber their CSP."""
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if "content-security-policy" not in headers:
        headers["Content-Security-Policy"] = _CSP
        headers["X-Frame-Options"] = "DENY"


def create_app(settings: Settings | None = None, gateway: Gateway | None = None) -> FastAPI:
    settings = settings or get_settings()
    # Edition + plane gates, computed once (open-core seam — docs/plans/2026-07-14-oss-edition.md).
    # In the OSS edition the app plane (the Toto product surface) simply does not exist: its
    # modules are deleted from the export, so nothing below may even import them.
    oss = settings.edition.strip().lower() == "oss"
    app_plane = "app" in settings.plane_set and not oss
    init_sentry(settings)  # no-op unless TOTO_GW_SENTRY_DSN is set
    if settings.kms_provider != "env":
        # Fail-closed at startup (decision #10): resolve the at-rest key material NOW so a
        # vault=unreachable / missing-key deploy crashes on boot instead of 500ing writes later.
        # env mode skips this (byte-identical to before). The read is cached for the hot path.
        from .credentials import credentials_secret as _resolve_kms

        _resolve_kms(settings)
    gateway = gateway or build_gateway(settings)
    if settings.prompts_file:  # apply even with the driver off, so /v1/dev/prompts reads truth
        from .driver import prompts

        prompts.load_overrides_file(settings.prompts_file)
    if settings.scopes_file:  # tool-scope overrides — same seam, so /v1/dev/tools/scopes reads truth
        from . import tool_scopes

        tool_scopes.load_scope_overrides_file(settings.scopes_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A startup line with no preceding shutdown line = SIGKILL/OOM (task #17 signature).
        log.info("startup", extra={"version": __version__, "driver": settings.driver})
        log.info("settings snapshot", extra={"settings": redact_settings(settings)})
        runs = getattr(app.state, "runs", None)
        if runs is not None:
            await runs.wake_start()  # arm the PG LISTEN listener (no-op in SQLite mode)
            # Name the active wake backend so a misconfigured multi-replica-on-in-proc deploy is one
            # grep away, not a silent board blackout. No assert — a single-replica dev box legitimately
            # runs InProcWakeBus. Same value is surfaced in /statusz (wake_backend). (fanout.md G2/G3)
            log.info("wake backend", extra={"wake_backend": type(runs._wake).__name__})
        # Inventory startup is database-only: migrate and hydrate the immutable request index,
        # never call provider HTTP before serving.
        await app.state.benchmark_platform.start()
        # Benchmark overlay (B3): merge store-derived per-category scores onto the yaml-loaded
        # Benchmarks so smart/driver routing uses real leaderboard data. One DB read at startup, no
        # network. Empty store → yaml-only, byte-identical routing. Best-effort — never blocks boot.
        # The store is stashed on app.state so the refresh route reuses it and hot-swaps THIS same
        # (gateway+driver shared) Benchmarks object. Unconditional: smart routing works without the
        # driver plane.
        try:
            from .benchmarking import registry as _bench_registry
            from .benchmarking.aggregate import overlay_benchmarks
            from .benchmarking.store import BenchmarkStore

            app.state.benchmarks_store = BenchmarkStore(
                settings.db, settings.database_url,
                pool={"pool_min": settings.pool_min, "pool_max": settings.pool_max,
                      "pool_timeout": settings.pool_timeout})
            n = await overlay_benchmarks(
                gateway.benchmarks, settings, app.state.benchmarks_store, _bench_registry,
                catalog_upstreams={e.effective_upstream_model for e in gateway.catalog.models})
            log.info("benchmark overlay", extra={"models_scored": n})
        except Exception as e:  # noqa: BLE001 — overlay is best-effort; yaml routing is the floor
            log.warning("benchmark overlay skipped — routing on yaml scores", extra={"err": str(e)})

        bg = [asyncio.create_task(_memory_watermark(settings)),
              asyncio.create_task(_reaper(app, settings))]
        if runs is not None:
            # Note-body backfill BACKGROUNDED (C3): it once ran pre-yield and full-table-scanned
            # canvas_objects every boot → on a large/slow table /readyz stayed 503 past the Docker
            # start-period and the healthcheck-gated rollout failed the replica. It's idempotent and
            # a no-op once clean (EXISTS short-circuit inside), so serving never waits on it.
            bg.append(asyncio.create_task(_backfill_notes(app)))
        if runs is not None and settings.memory and settings.memory_dreams:
            bg.append(asyncio.create_task(_dreamer(app, settings)))
        if runs is not None and (settings.cal_sync or pipedream.enabled(settings)):
            bg.append(asyncio.create_task(_calsync(app, settings)))
        if runs is not None and getattr(app.state, "content", None) is not None \
                and app.state.content.indexer is not None:
            # Boot embedding backfill in the background (never blocks serving). Idempotent and a
            # no-op once indexed; only runs when the memory plane is on (indexer present).
            bg.append(asyncio.create_task(_backfill_embeddings(app)))
        if settings.benchmark_refresh_hours > 0:
            bg.append(asyncio.create_task(_benchmark_refresher(app, settings)))
        if settings.inventory_refresh_hours > 0:
            bg.append(asyncio.create_task(_inventory_refresher(app, settings)))
        if getattr(app.state, "auth", None) is not None and settings.audit_export_tick_seconds > 0:
            bg.append(asyncio.create_task(_audit_exporter(app, settings)))
        if getattr(app.state, "auth", None) is not None and settings.retention_sweep_tick_seconds > 0:
            bg.append(asyncio.create_task(_retention_sweeper(app, settings)))
        # Egress allowlist (W2-C6): derive the allowed host set from config + configured SSO issuers,
        # then patch the httpx transport chokepoint. Best-effort — an install failure never blocks boot.
        try:
            from . import egress

            issuers = await app.state.auth.all_sso_issuers()
            egress.install(settings, gateway.catalog, app.state.auth,
                           sso_issuers=issuers, loop=asyncio.get_running_loop())
        except Exception as e:  # noqa: BLE001 — egress guard is a floor, never a boot gate
            log.warning("egress allowlist install skipped", extra={"err": str(e)})
        try:
            yield
        finally:
            log.info("shutdown received — draining", extra={"drain_seconds": settings.drain_seconds})
            await sessions.drain(runs, settings.drain_seconds, log)
            for t in bg:
                t.cancel()
            await app.state.benchmark_platform.close()
            if runs is not None:
                await runs.wake_stop()
                await runs.close_pool()  # release the psycopg pool (no-op in SQLite mode)
            auth = getattr(app.state, "auth", None)
            if auth is not None:
                await auth.close_pool()
            content = getattr(app.state, "content", None)
            if content is not None:
                await content.close()  # content-plane pool + conn (no-op if never resolved)
            log.info("shutdown complete")

    # Interactive docs / OpenAPI schema leak the full API surface; login is required now, so
    # closed deploys ship them OFF. The dev-only `cors` flag is our open-deployment signal.
    _docs_off = {} if settings.cors else dict(docs_url=None, redoc_url=None, openapi_url=None)
    app = FastAPI(title="toto-gateway", version=__version__, lifespan=lifespan, **_docs_off)

    # Pool exhaustion → a clean 503, not an unhandled 500. When every pooled conn is checked out
    # and the acquire wait (pool_timeout) elapses, psycopg raises PoolTimeout; map it to the same
    # capacity_error shape sessions._at_capacity returns (429 there, 503 here — the pool is the
    # deeper ceiling), with Retry-After so clients back off instead of hammering.
    from psycopg_pool import PoolTimeout

    @app.exception_handler(PoolTimeout)
    async def _pool_timeout(request, exc):  # noqa: ANN001
        from fastapi.responses import JSONResponse as _JR

        log.warning("db pool exhausted — shedding with 503", extra={"path": request.url.path})
        return _JR(status_code=503, headers={"Retry-After": "5"},
                   content={"error": {"message": "database is at capacity — retry shortly",
                                      "type": "capacity_error"}})

    # Egress allowlist refusal (enforce mode) → a clean 502 on a serving path, never an unhandled
    # 500. Background callers (catalog_sync, benchmark ingest) catch it themselves like provider I/O.
    from .egress import EgressBlockedError

    @app.exception_handler(EgressBlockedError)
    async def _egress_blocked(request, exc: EgressBlockedError):  # noqa: ANN001
        from fastapi.responses import JSONResponse as _JR

        log.error("egress blocked on request path", extra={"host": exc.host, "subsystem": exc.subsystem})
        return _JR(status_code=502,
                   content={"error": {"message": f"egress to {exc.host} is not on the allowlist",
                                      "type": "upstream_error", "code": "egress_blocked"}})

    # Outermost middleware: mint/propagate request_id, echo X-Request-ID, one request log line.
    app.add_middleware(RequestContextMiddleware)

    @app.middleware("http")
    async def _security_headers(request, call_next):
        resp = await call_next(request)
        _apply_security_headers(resp.headers)
        return resp

    # Distribution license gate (W3-C7). Past grace (or missing/invalid when required), refuse the
    # chat plane with a clean 503 while the paths an operator needs to SEE and FIX the license stay
    # up: liveness/status, the admin console + its API-status route, and auth (to log into it). The
    # signature was verified once at boot; blocked() recomputes the time verdict live per request.
    _LICENSE_EXEMPT_PREFIXES = ("/console", "/v1/admin/license", "/v1/auth", "/static", "/assets")
    _LICENSE_EXEMPT_EXACT = frozenset({"/healthz", "/readyz", "/statusz", "/metrics", "/", "/favicon.ico"})

    @app.middleware("http")
    async def _license_gate(request, call_next):
        lic = getattr(request.app.state, "license_status", None)
        if lic is not None and lic.blocked():
            path = request.url.path
            if path not in _LICENSE_EXEMPT_EXACT and not path.startswith(_LICENSE_EXEMPT_PREFIXES):
                from fastapi.responses import JSONResponse as _JR

                snap = lic.snapshot(include_org=False)
                resp = _JR(status_code=503, content={"error": {
                    "message": "This Toto deployment's license is expired or invalid. Chat traffic is "
                               "paused. An operator can view and update the license at /statusz or the "
                               "admin console; see /v1/admin/license.",
                    "type": "license_error", "code": "license_expired", "license": snap}})
                _apply_security_headers(resp.headers)
                return resp
        return await call_next(request)

    if settings.cors or settings.cors_origin:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"] if settings.cors else [settings.cors_origin],
            allow_methods=["*"], allow_headers=["*"],
            # Voice-session burn meter reads the per-call TTS cost off /speak's response header;
            # a cross-origin fetch (dev PUBLIC_API_BASE) can only see it when it's exposed.
            expose_headers=["X-Toto-TTS-Cost"],
        )
    app.state.settings = settings
    app.state.gateway = gateway
    app.state.started_at = time.time()  # for /statusz uptime_s
    # Distribution license: verify TOTO_GW_LICENSE_KEY once at boot; the gate middleware + /statusz +
    # /healthz read the resulting status. Unlicensed dev (no key, not required) is a no-op.
    from . import license as _license

    app.state.license_status = _license.evaluate(settings)
    # Wire the live USE gauges to the counts already tracked elsewhere (fail-open at collect time).
    from .metrics import METRICS

    def _llm_inflight() -> int:
        sem = gateway._llm_sem
        return gateway._max_llm - sem._value if sem is not None else 0

    METRICS.bind_live(
        in_flight_runs=lambda: len(sessions._live_run_ids),
        sse_connections=lambda: sessions._sse_connections,
        llm_semaphore_inflight=_llm_inflight,
    )
    # psycopg pool tunables (PG mode only), threaded into every store's make_async_pool. One dict,
    # sized from Settings; SQLite stores ignore it (make_async_pool returns None without a URL).
    pool_cfg = {"pool_min": settings.pool_min, "pool_max": settings.pool_max,
                "pool_timeout": settings.pool_timeout}
    # Accounts/auth gate the app regardless of the driver flag, so the AuthStore is ALWAYS
    # constructed (shares the same SQLite file as RunStore; WAL handles two connections).
    from .auth import AuthStore

    app.state.auth = AuthStore(settings.db, settings.database_url, pool=pool_cfg)
    # BYOS: register the org-connector source so the object-store resolvers (storage.py) can home
    # a user's writes on their org's private bucket — including from paths below the route plane.
    from .storage import set_org_config_source

    set_org_config_source(app.state.auth)
    # W2-C5: team/org monthly budget enforcer. Wired here (not in build_gateway) because it needs the
    # AuthStore, which exists only now. Reads spend off the SAME trace engine the writer uses. No
    # trace DB / no budget rows → decide() no-ops, so this is free until an org sets a budget.
    from .budgets import BudgetEnforcer
    from .trace import sql_engine

    gateway.budget = BudgetEnforcer(app.state.auth, lambda: sql_engine(gateway.writer))
    from .benchmarking.platform import BenchmarkPlatform
    from .benchmarking.platform_store import BenchmarkPlatformStore

    app.state.benchmark_platform_store = BenchmarkPlatformStore(
        settings.db, settings.database_url, pool=pool_cfg
    )
    app.state.benchmark_platform = BenchmarkPlatform(
        app.state.benchmark_platform_store,
        app.state.auth,
        settings,
        publish_candidates=lambda candidates: setattr(gateway, "candidates", candidates),
    )
    # The driver plane (POST /v1/route + sessions). Routes are always mounted; they return a
    # clean 503 when the driver is disabled, so the surface is discoverable either way.
    if settings.driver:
        from .runs import RunStore

        app.state.runs = RunStore(settings.db, settings.database_url, lease_ttl=settings.run_timeout,
                                  pool=pool_cfg, redis_url=settings.redis_url)
        if settings.db == ":memory:" or settings.db.startswith(("/tmp", "/var/tmp")):
            # Plan rule: never lose labeled data SILENTLY. Ephemeral is allowed, but loudly.
            log.warning("ephemeral sessions/feedback DB — labels won't survive a redeploy; "
                        "mount a volume and set TOTO_GW_DB", extra={"db": settings.db})
        app.state.driver = build_driver(settings, gateway, runs=app.state.runs,
                                        auth=getattr(app.state, "auth", None))
        # Content plane (brain-markdown plan): tenant → ContentStore resolver, holding authored
        # markdown AND the memory recall index (doc_embeddings). Resolution (Decision 2): a
        # dedicated CONTENT_DATABASE_URL wins (sole-tenant customer), else the primary DATABASE_URL
        # under a `content` schema (the enterprise default — one Postgres, two schemas), else
        # SQLite (dev only). HARD RULE: a set DATABASE_URL never silently falls back to ephemeral
        # SQLite. Ephemeral primary (tests, :memory:) → ephemeral content plane, no scattered file.
        from .content import ContentIndexer, ContentResolver

        if settings.content_database_url:
            _c_url, _c_schema, _c_path = settings.content_database_url, None, ""
        elif settings.database_url:
            _c_url, _c_schema, _c_path = settings.database_url, settings.content_schema, ""
        else:
            _c_url, _c_schema = "", None
            _c_path = settings.content_db if settings.db != ":memory:" else ":memory:"
        # The embed-on-write indexer is the ONE seam: every content put/delete embeds into
        # doc_embeddings from inside ContentStore — no per-route index code. Built only when the
        # memory plane is on (TOTO_GW_MEMORY=1), else None → mirroring is a silent no-op (today's
        # behavior). It reuses the driver's embedder (same OpenRouter path as routing).
        app.state.content = ContentResolver(_c_path, _c_url, schema=_c_schema, pool=pool_cfg)
        if settings.memory:
            _embedder = getattr(app.state.driver, "_embedder", None)  # reuse the routing embedder
            app.state.content.indexer = ContentIndexer(app.state.content, _embedder)
            # The RECALL plane: a thin adapter over the content plane. None when the flag is off →
            # the companion degrades to declared-memory-only, exactly as today. Attached to the
            # driver too so the session-completion capture hook (routes/sessions) can reach it.
            from .memory import build_memory

            # rerank runs on OUR gateway via the driver's own complete seam (retry/fallback/trace,
            # cost in our metering) — the economy model, one batched call, degrade-to-fused-order.
            app.state.memory = build_memory(settings, app.state.content, _embedder,
                                            llm_fn=app.state.driver._llm)
        else:
            app.state.memory = None
        app.state.driver.memory = app.state.memory
        # Post-capture distiller (memory-lifecycle P0): turns raw captures into durable typed facts
        # in user_memory. Present == enabled; attached to BOTH capture sites (companion chat +
        # session outcome). Needs the recall plane on (the dedupe consults it).
        if settings.memory and settings.memory_extract:
            from .memory_extract import MemoryExtractor

            app.state.extractor = MemoryExtractor(
                gateway=gateway, runs=app.state.runs, memory=app.state.memory,
                model=settings.memory_extract_model or settings.triage_model,
                every=settings.memory_extract_every, dedupe_sim=settings.memory_extract_dedupe_sim,
                daily_usd=settings.memory_extract_daily_usd)
        else:
            app.state.extractor = None
        app.state.driver.extractor = app.state.extractor  # session-outcome capture site reaches it
        # The companion rides the driver plane AND the app plane: it exists only when the app
        # plane is mounted (and never in the OSS edition — companion/ is absent from that tree).
        if app_plane:
            from .companion.core import Companion
            from .routes.admin_benchmarks import _get_store

            app.state.companion = Companion(driver=app.state.driver, runs=app.state.runs,
                                            model=settings.companion_model,
                                            max_tokens=settings.max_tokens_answer,
                                            memory=app.state.memory, gateway=gateway,
                                            content=app.state.content,
                                            extractor=app.state.extractor,
                                            recall_k=settings.memory_recall_k,
                                            recall_chars=settings.memory_recall_chars,
                                            custom_tools=settings.custom_tools,
                                            external_tools=pipedream.enabled(settings),
                                            settings=settings,
                                            benchmarks_store=_get_store(app))
        else:
            app.state.companion = None
    else:
        app.state.runs = None
        app.state.driver = None
        app.state.companion = None
        app.state.memory = None
        app.state.content = None
        app.state.extractor = None

    # Prompt-tuning dev dashboard — the router is only ever REGISTERED when the flag is on,
    # so with it off (prod default) every /dev + /v1/dev/* path is a plain 404.
    if settings.dev_dashboard:
        from .routes import dev, dev_experiments

        app.include_router(dev.router)
        app.include_router(dev_experiments.router)
        log.warning("dev dashboard mounted at /dev — sandbox/dev only, never enable in prod")

    # --- Plane gating (gateway/driver boundary doc, Q5) ----------------------------------------
    # Data-driven map of plane → routers; TOTO_GW_PLANES selects which mount (default: both).
    #   gateway (always on) — the pure API/gateway (A: chat/models), the driver (B: route/routing/
    #     sessions), and gateway features (credentials = BYOK, custom_tools = the tool contract).
    #     The future `admin` router mounts here too — add it to this list.
    #   app (only when "app" in planes) — the Toto product surface (C): companion, canvas, tasks,
    #     objects, bindles, calendar, integrations + the SPA static mount below.
    # Ambiguous routers default to the app plane (keep the gateway minimal); sessions/credentials/
    # custom_tools sit in gateway per the boundary doc.
    plane_routers = {
        "gateway": [health, metrics, auth, tokens, models, chat, prewarm, route, routing,
                    sessions, credentials, custom_tools,
                    admin_analytics, admin_catalog, admin_catalog_adoptions, admin_catalog_sync,
                    admin_labeling, admin_latency, admin_providers, admin_requests, admin_routing,
                    admin_usage],
    }
    # App plane (the Toto product surface, C). Lazy-imported behind the same gate that mounts it:
    # the OSS export deletes these modules wholesale (edition seam), so in the oss edition the app
    # plane must simply not exist — not even as an import. Enterprise deploys (default planes,
    # default edition) mount exactly what they always did.
    if app_plane:
        from .routes import (
            bindles, calendar, canvas, companion, documents, feedback, integrations, lists,
            objects, preferences,
        )

        plane_routers["app"] = [companion, preferences, feedback, lists, canvas, bindles,
                                objects, calendar, integrations, documents]
    # Edition gate (open-core seam — docs/plans/2026-07-14-oss-edition.md): everything scoped to
    # a single user's own keys mounts above; everything org-shaped mounts only when
    # TOTO_GW_EDITION is enterprise (the default — this branch is a no-op for every existing
    # deploy). oss → these routes are plain 404s, same pattern as TOTO_GW_PLANES. Imported here,
    # not at module top, so the OSS export can drop the modules wholesale.
    if not oss:
        from .routes import (
            admin_audit, admin_audit_export, admin_benchmark_platform, admin_benchmarks,
            admin_budgets, admin_egress, admin_license, admin_observability, admin_sso,
            admin_storage, admin_tenancy, admin_tokens, admin_tuning, admin_workmap,
            org_credentials, scim,
        )

        plane_routers["gateway"] += [
            scim, org_credentials, admin_audit, admin_audit_export, admin_benchmark_platform,
            admin_benchmarks, admin_budgets, admin_egress, admin_license, admin_observability,
            admin_sso, admin_storage, admin_tenancy, admin_tokens, admin_tuning, admin_workmap,
        ]
    active_planes = settings.plane_set
    log.info("planes active: %s", ",".join(sorted(active_planes)))
    for plane, mods in plane_routers.items():
        if plane in active_planes:
            for mod in mods:
                app.include_router(mod.router)

    # Serve built SvelteKit SPAs same-origin (Decision 1): cookie sessions need one origin.
    # Two surfaces, same adapter-static pattern:
    #   /console — the Control Surface admin console (v1.0 gateway admin). Mounted whenever a build
    #              exists, REGARDLESS of the app plane, because the /v1/admin API it drives lives in
    #              the always-on gateway plane. Same-origin ⇒ its httpOnly toto_session cookie flows.
    #   /svelte  — the Toto product SPA, the app plane's front door (skipped in gateway-only mode).
    from pathlib import Path

    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def _find_build(rel: str) -> Path | None:
        # CWD-relative in both dev (repo root) and the Docker runtime (/app, where the node stage's
        # output is copied); fall back to the source-tree location next to the package.
        return next((p for p in (Path(rel), Path(__file__).resolve().parent.parent / rel)
                     if p.is_dir()), None)

    class SpaStaticFiles(StaticFiles):
        """adapter-static writes prerendered pages as <route>.html — map deep links
        (/console/catalog → catalog.html) so hard reloads and shared URLs work. Starlette
        RAISES HTTPException(404) for missing files (it doesn't return a 404 response),
        so the fallback must catch, not inspect status codes."""

        async def get_response(self, path, scope):  # type: ignore[override]
            try:
                resp = await super().get_response(path, scope)
            except StarletteHTTPException as exc:
                if exc.status_code == 404 and path and "." not in path.rsplit("/", 1)[-1]:
                    resp = await super().get_response(f"{path}.html", scope)
                else:
                    raise
            # /_app/immutable/* is content-hashed → cache forever; .html entry points → never.
            if "/immutable/" in path:
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            elif path.endswith(".html") or "." not in path.rsplit("/", 1)[-1]:
                resp.headers["Cache-Control"] = "no-cache"
            return resp

    console_build = _find_build("control-surface/build")
    if console_build is not None:
        app.mount("/console", SpaStaticFiles(directory=str(console_build), html=True), name="console")

    if not app_plane:
        return app

    build_dir = _find_build("frontend/build")

    @app.get("/", include_in_schema=False)
    def _root_redirect() -> RedirectResponse:
        return RedirectResponse("/svelte/", status_code=302)

    if build_dir is not None:
        app.mount("/svelte", SpaStaticFiles(directory=str(build_dir), html=True), name="spa")
    return app


if __name__ == "__main__":
    # Self-check for the security-header logic (#21): normal responses get the full policy +
    # frame ban; a route with its own CSP (the bindle sandbox) keeps it and is NOT frame-banned.
    from starlette.datastructures import MutableHeaders

    normal = MutableHeaders()
    _apply_security_headers(normal)
    assert normal["x-content-type-options"] == "nosniff"
    assert normal["referrer-policy"] == "strict-origin-when-cross-origin"
    assert normal["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in normal["content-security-policy"]

    bindle = MutableHeaders(headers={"content-security-policy": "sandbox allow-scripts",
                                     "x-content-type-options": "nosniff"})
    _apply_security_headers(bindle)
    assert bindle["content-security-policy"] == "sandbox allow-scripts"  # not clobbered
    assert "x-frame-options" not in bindle                              # stays frameable
    assert bindle["referrer-policy"] == "strict-origin-when-cross-origin"
    print("app security-header self-check OK")
