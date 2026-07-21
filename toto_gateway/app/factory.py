"""`create_app`: lifespan, middleware, app.state wiring, plane/edition router mounting, and
the SPA static mounts."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .. import __version__
from ..config import Settings, get_settings
from ..gateway import Gateway
from ..obs import RequestContextMiddleware, init_sentry, redact_settings
# Core (gateway-plane) routers only. App-plane routers (companion, canvas, lists, …) are
# lazy-imported behind the plane/edition gate below — the OSS export deletes those modules
# wholesale (edition seam), so nothing here may import them at module top.
from ..routes import (
    admin_catalog, admin_catalog_adoptions, admin_catalog_sync, admin_providers, admin_requests,
    admin_routing, admin_usage, auth, chat, credentials, health, messages, metrics, models,
    prewarm, provider_keys, route, routing, sessions, tokens,
)
from .background import (
    _audit_exporter, _backfill_embeddings, _backfill_notes, _benchmark_refresher, _calsync,
    _dreamer, _inventory_refresher, _memory_watermark, _reaper, _retention_sweeper,
)
from .build import build_driver, build_gateway
from .http import SpaStaticFiles, _apply_security_headers, _find_build

log = logging.getLogger("toto_gateway")


def create_app(settings: Settings | None = None, gateway: Gateway | None = None) -> FastAPI:
    settings = settings or get_settings()
    # Edition + plane gates, computed once (the open-core seam). In the OSS edition the app plane
    # (the Toto product surface) simply does not exist: its modules are deleted from the export,
    # so nothing below may even import them.
    oss = settings.edition.strip().lower() == "oss"
    app_plane = "app" in settings.plane_set and not oss
    init_sentry(settings)  # no-op unless TOTO_GW_SENTRY_DSN is set
    if oss:
        # Zero-config single-tenant boot, resolved BEFORE build_gateway so both seams see truth:
        #  - at-rest secret: none configured → generate one and persist it beside the SQLite DB, so
        #    pasting a provider key in Settings works with zero env vars (tradeoff documented on
        #    bootstrap_local_secret). Stored keys themselves are LIVE at dispatch per request.
        #  - default catalog: a stored OpenRouter key counts as configured, so a DEFAULTED catalog
        #    pick upgrades to catalog.openrouter.yaml — this one takes effect at boot, not live,
        #    because the catalog is built once here. An explicit TOTO_GW_CATALOG is never touched.
        from ..credentials import (
            bootstrap_local_secret, compose_default_catalog, configured_key_providers,
        )
        from ..routes.deps import OSS_LOCAL_ORG

        if settings.kms_provider == "env" and not settings.credentials_secret:
            settings.credentials_secret = bootstrap_local_secret(settings)
        # Compose ALL keyed providers' fragments (env + stored), so a fresh boot with a stored
        # Cloudflare key AND OPENROUTER_API_KEY lights up both. Only when the catalog was DEFAULTED
        # (an explicit TOTO_GW_CATALOG stays the operator's override, untouched).
        if settings._catalog_defaulted:
            composed = compose_default_catalog(
                configured_key_providers(settings, OSS_LOCAL_ORG))
            if composed != settings.catalog:
                settings.catalog = composed
                log.info("stored/env provider keys → default catalog composed to %s", composed)
    if settings.kms_provider != "env":
        # Fail-closed at startup: resolve the at-rest key material NOW so a vault-unreachable /
        # missing-key deploy crashes on boot instead of 500ing writes later. env mode skips this
        # (byte-identical behavior). The read is cached for the hot path.
        from ..credentials import credentials_secret as _resolve_kms

        _resolve_kms(settings)
    gateway = gateway or build_gateway(settings)
    if settings.prompts_file:  # apply even with the driver off, so /v1/dev/prompts reads truth
        from ..driver import prompts

        prompts.load_overrides_file(settings.prompts_file)
    if settings.scopes_file and not oss:  # tool-scope overrides for the custom-tools plane (dropped in oss)
        from .. import tool_scopes

        tool_scopes.load_scope_overrides_file(settings.scopes_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A startup line with no preceding shutdown line = SIGKILL/OOM.
        log.info("startup", extra={"version": __version__, "driver": settings.driver})
        log.info("settings snapshot", extra={"settings": redact_settings(settings)})
        runs = getattr(app.state, "runs", None)
        if runs is not None:
            await runs.wake_start()  # arm the PG LISTEN listener (no-op in SQLite mode)
            # Name the active wake backend so a misconfigured multi-replica-on-in-proc deploy is one
            # grep away, not a silent board blackout. No assert — a single-replica dev box legitimately
            # runs InProcWakeBus. Same value is surfaced in /statusz (wake_backend).
            log.info("wake backend", extra={"wake_backend": type(runs._wake).__name__})
        # Inventory startup is database-only: migrate and hydrate the immutable request index,
        # never call provider HTTP before serving.
        await app.state.benchmark_platform.start()
        # Benchmark overlay: merge store-derived per-category scores onto the yaml-loaded
        # Benchmarks so smart/driver routing uses real leaderboard data. One DB read at startup, no
        # network. Empty store → yaml-only, byte-identical routing. Best-effort — never blocks boot.
        # The store is stashed on app.state so the refresh route reuses it and hot-swaps THIS same
        # (gateway+driver shared) Benchmarks object. Unconditional: smart routing works without the
        # driver plane.
        try:
            from ..benchmarking import registry as _bench_registry
            from ..benchmarking.aggregate import overlay_benchmarks
            from ..benchmarking.store import BenchmarkStore

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
        if settings.benchmark_refresh_hours > 0:
            bg.append(asyncio.create_task(_benchmark_refresher(app, settings)))
        if settings.inventory_refresh_hours > 0:
            bg.append(asyncio.create_task(_inventory_refresher(app, settings)))
        # App/enterprise background loops. Each rides a module the OSS export drops — note/embedding
        # backfills and the dreamer on the content/recall plane, calendar sync on pipedream/ics,
        # audit export, content retention — so they spawn only outside the oss edition. This branch
        # is a no-op for every existing deploy (oss is False there); pipedream is imported inside it
        # so nothing here references a dropped module at import time.
        if not oss:
            from .. import pipedream

            if runs is not None:
                # Note-body backfill runs in the BACKGROUND: pre-yield it would full-table-scan
                # canvas_objects at boot, holding /readyz at 503 past the Docker start-period and
                # failing healthcheck-gated rollouts on large/slow tables. It's idempotent and a
                # no-op once clean (EXISTS short-circuit inside), so serving never waits on it.
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
            if getattr(app.state, "auth", None) is not None and settings.audit_export_tick_seconds > 0:
                bg.append(asyncio.create_task(_audit_exporter(app, settings)))
            if getattr(app.state, "auth", None) is not None and settings.retention_sweep_tick_seconds > 0:
                bg.append(asyncio.create_task(_retention_sweeper(app, settings)))
        # Egress allowlist: derive the allowed host set from config + configured SSO issuers,
        # then patch the httpx transport chokepoint. Best-effort — an install failure never blocks boot.
        try:
            from .. import egress

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
    from ..egress import EgressBlockedError

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

    # Distribution license gate. Past grace (or missing/invalid when required), refuse the chat
    # plane with a clean 503 while the paths an operator needs to SEE and FIX the license stay
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
    if oss:
        # No distribution-license plane in the open edition (license.py isn't shipped). All three
        # readers — the gate middleware above, /statusz, /healthz — already treat a None status as
        # "unlicensed / gate skipped", identical to what evaluate() returns for a keyless deploy.
        app.state.license_status = None
    else:
        from .. import license as _license

        app.state.license_status = _license.evaluate(settings)
    # Wire the live USE gauges to the counts already tracked elsewhere (fail-open at collect time).
    from ..metrics import METRICS

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
    from ..auth import AuthStore

    app.state.auth = AuthStore(settings.db, settings.database_url, pool=pool_cfg)
    # BYOS: register the org-connector source so the object-store resolvers (storage.py) can home
    # a user's writes on their org's private bucket — including from paths below the route plane.
    from ..storage import set_org_config_source

    set_org_config_source(app.state.auth)
    # Team/org monthly budget enforcer. Wired here (not in build_gateway) because it needs the
    # AuthStore, which exists only now. Reads spend off the SAME trace engine the writer uses. No
    # trace DB / no budget rows → decide() no-ops, so this is free until an org sets a budget.
    from ..budgets import BudgetEnforcer
    from ..trace import sql_engine

    gateway.budget = BudgetEnforcer(app.state.auth, lambda: sql_engine(gateway.writer))
    from ..benchmarking.platform import BenchmarkPlatform
    from ..benchmarking.platform_store import BenchmarkPlatformStore

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
        from ..runs import RunStore

        app.state.runs = RunStore(settings.db, settings.database_url, lease_ttl=settings.run_timeout,
                                  pool=pool_cfg, redis_url=settings.redis_url)
        if settings.db == ":memory:" or settings.db.startswith(("/tmp", "/var/tmp")):
            # Never lose labeled data SILENTLY: ephemeral is allowed, but loudly.
            log.warning("ephemeral sessions/feedback DB — labels won't survive a redeploy; "
                        "mount a volume and set TOTO_GW_DB", extra={"db": settings.db})
        app.state.driver = build_driver(settings, gateway, runs=app.state.runs,
                                        auth=getattr(app.state, "auth", None))
        # Content plane: tenant → ContentStore resolver, holding authored markdown AND the memory
        # recall index (doc_embeddings). Resolution: a dedicated CONTENT_DATABASE_URL wins
        # (sole-tenant customer), else the primary DATABASE_URL under a `content` schema (the
        # enterprise default — one Postgres, two schemas), else SQLite (dev only). HARD RULE: a
        # set DATABASE_URL never silently falls back to ephemeral SQLite. Ephemeral primary
        # (tests, :memory:) → ephemeral content plane, no scattered file.
        #
        # The content/recall plane, memory extraction, and the companion below are the app/enterprise
        # product built on the router — their modules (content, memory, memory_extract, companion) are
        # dropped by the OSS export, so the whole block is edition-gated. Enterprise (default edition,
        # oss False) wires exactly what it always did; the oss branch leaves the attrs None, which the
        # shared readers already guard for (getattr(driver, "memory", None), app.state.content).
        if not oss:
            from ..content import ContentIndexer, ContentResolver

            if settings.content_database_url:
                _c_url, _c_schema, _c_path = settings.content_database_url, None, ""
            elif settings.database_url:
                _c_url, _c_schema, _c_path = settings.database_url, settings.content_schema, ""
            else:
                _c_url, _c_schema = "", None
                _c_path = settings.content_db if settings.db != ":memory:" else ":memory:"
            # The embed-on-write indexer is the ONE seam: every content put/delete embeds into
            # doc_embeddings from inside ContentStore — no per-route index code. Built only when the
            # memory plane is on (TOTO_GW_MEMORY=1), else None → mirroring is a silent no-op. It
            # reuses the driver's embedder (same OpenRouter path as routing).
            app.state.content = ContentResolver(_c_path, _c_url, schema=_c_schema, pool=pool_cfg)
            if settings.memory:
                _embedder = getattr(app.state.driver, "_embedder", None)  # reuse the routing embedder
                app.state.content.indexer = ContentIndexer(app.state.content, _embedder)
                # The RECALL plane: a thin adapter over the content plane. None when the flag is off →
                # the companion degrades to declared-memory-only. Attached to the driver too so the
                # session-completion capture hook (routes/sessions) can reach it.
                from ..memory import build_memory

                # rerank runs on OUR gateway via the driver's own complete seam (retry/fallback/trace,
                # cost in our metering) — the economy model, one batched call, degrade-to-fused-order.
                app.state.memory = build_memory(settings, app.state.content, _embedder,
                                                llm_fn=app.state.driver._llm)
            else:
                app.state.memory = None
            app.state.driver.memory = app.state.memory
            # Post-capture distiller: turns raw captures into durable typed facts in user_memory.
            # Present == enabled; attached to BOTH capture sites (companion chat + session outcome).
            # Needs the recall plane on (the dedupe consults it).
            if settings.memory and settings.memory_extract:
                from ..memory_extract import MemoryExtractor

                app.state.extractor = MemoryExtractor(
                    gateway=gateway, runs=app.state.runs, memory=app.state.memory,
                    model=settings.memory_extract_model or settings.triage_model,
                    every=settings.memory_extract_every, dedupe_sim=settings.memory_extract_dedupe_sim,
                    daily_usd=settings.memory_extract_daily_usd)
            else:
                app.state.extractor = None
            app.state.driver.extractor = app.state.extractor  # session-outcome capture site reaches it
        else:
            app.state.content = None
            app.state.memory = None
            app.state.extractor = None
            app.state.driver.memory = None
            app.state.driver.extractor = None
        # The companion rides the driver plane AND the app plane: it exists only when the app
        # plane is mounted (and never in the OSS edition — companion/ is absent from that tree).
        if app_plane:
            from .. import pipedream
            from ..companion.core import Companion
            from ..routes.admin_benchmarks import _get_store

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
        from ..routes import dev, dev_experiments

        app.include_router(dev.router)
        app.include_router(dev_experiments.router)
        log.warning("dev dashboard mounted at /dev — sandbox/dev only, never enable in prod")

    # --- Plane gating ---------------------------------------------------------------------------
    # Data-driven map of plane → routers; TOTO_GW_PLANES selects which mount (default: both).
    #   gateway (always on) — the pure API/gateway (chat/models), the driver (route/routing/
    #     sessions), and gateway features (credentials = BYOK).
    #   app (only when "app" in planes) — the Toto product surface: companion, canvas, tasks,
    #     objects, bindles, calendar, integrations + the SPA static mount below.
    # Ambiguous routers default to the app plane (keep the gateway minimal); sessions/credentials
    # sit in gateway. custom_tools (the tool contract) mounts in the not-oss block below — its
    # executor lives on the excluded companion plane, so the open edition drops it.
    plane_routers = {
        "gateway": [health, metrics, auth, tokens, models, chat, messages, prewarm, route, routing,
                    sessions, credentials, provider_keys,
                    admin_catalog, admin_catalog_adoptions, admin_catalog_sync,
                    admin_providers, admin_requests, admin_routing, admin_usage],
    }
    # App plane (the Toto product surface). Lazy-imported behind the same gate that mounts it:
    # the OSS export deletes these modules wholesale (edition seam), so in the oss edition the app
    # plane must simply not exist — not even as an import. Enterprise deploys (default planes,
    # default edition) mount exactly what they always did.
    if app_plane:
        from ..routes import (
            bindles, calendar, canvas, companion, documents, feedback, integrations, lists,
            objects, preferences,
        )

        plane_routers["app"] = [companion, preferences, feedback, lists, canvas, bindles,
                                objects, calendar, integrations, documents]
    # Edition gate (open-core seam): everything scoped to a single user's own keys mounts above;
    # everything org-shaped mounts only when TOTO_GW_EDITION is enterprise (the default — this
    # branch is a no-op for every existing deploy). oss → these routes are plain 404s, same
    # pattern as TOTO_GW_PLANES. Imported here, not at module top, so the OSS export can drop
    # the modules wholesale.
    if not oss:
        from ..routes import (
            admin_analytics, admin_audit, admin_audit_export, admin_benchmark_platform,
            admin_benchmarks, admin_budgets, admin_egress, admin_labeling, admin_latency,
            admin_license, admin_observability, admin_sso, admin_storage, admin_tenancy,
            admin_tokens, admin_tuning, admin_workmap, custom_tools, org_credentials, scim,
        )

        plane_routers["gateway"] += [
            admin_analytics, admin_labeling, admin_latency,
            custom_tools, scim, org_credentials, admin_audit, admin_audit_export,
            admin_benchmark_platform, admin_benchmarks, admin_budgets, admin_egress, admin_license,
            admin_observability, admin_sso, admin_storage, admin_tenancy, admin_tokens, admin_tuning,
            admin_workmap,
        ]
    active_planes = settings.plane_set
    log.info("planes active: %s", ",".join(sorted(active_planes)))
    for plane, mods in plane_routers.items():
        if plane in active_planes:
            for mod in mods:
                app.include_router(mod.router)

    # Serve built SvelteKit SPAs same-origin — cookie sessions need one origin.
    # Two surfaces, same adapter-static pattern:
    #   /console — the Control Surface admin console (gateway admin). Mounted whenever a build
    #              exists, REGARDLESS of the app plane, because the /v1/admin API it drives lives in
    #              the always-on gateway plane. Same-origin ⇒ its httpOnly toto_session cookie flows.
    #   /svelte  — the Toto product SPA, the app plane's front door (skipped in gateway-only mode).
    from fastapi.responses import RedirectResponse

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
