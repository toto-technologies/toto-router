"""Builders for the two engines: `build_gateway` (the passthrough gateway with its routing
brain) and `build_driver` (the agentic driver on top of it), plus the label-bindings and
span-observer helpers both share."""

from __future__ import annotations

import json
import logging

from ..catalog import Catalog
from ..config import Settings
from ..gateway import Gateway
from ..obs import request_id_var
from ..runners.registry import RunnerRegistry
from ..trace import build_writer_from_settings

log = logging.getLogger("toto_gateway")


def build_gateway(settings: Settings) -> Gateway:
    from ..driver import prompts

    # Config → prompts seam: the classifier prompt variant both live call sites (driver label
    # node, /v1 smart route) build with. Raises on an unknown value — boot-time, never mid-request.
    prompts.set_label_variant(settings.label_prompt_variant)
    # Same seam for the subagent runners flag: on → _clean_task admits requires.runner ∈
    # {pi, claude_code}; off (default) → the pin is stripped.
    prompts.set_subagent_runners(settings.subagent_runners)
    catalog = Catalog.load(settings.catalog)
    if settings.fake_exec:
        from ..runners.fake import FakeRunner

        registry = RunnerRegistry(factory=FakeRunner)  # real routing, fake (offline) execution
    else:
        registry = RunnerRegistry()
    writer = build_writer_from_settings(settings)

    # Routing brain, gated by config (default off → exact passthrough).
    extractor = guard = router = cache = None
    if settings.routing:
        from ..routing.decision import GuardRouter
        from ..signals.extractor import HeuristicExtractor
        from ..signals.guards import RuleGuard

        # The raw passthrough's safety floor: guard (fail-closed) + policy, honoring the
        # requested model otherwise. Content-based routing lives in the driver (/v1/route).
        extractor, guard, router = HeuristicExtractor(), RuleGuard(), GuardRouter()
    if settings.cache:
        from ..cache.exact import ExactCache

        cache = ExactCache()

    # Smart auto-routing: the `smart` sentinel classifies + routes on the passthrough plane.
    # Reuses the SAME label bindings + classifier model the driver uses; no TOTO_GW_DRIVER needed
    # (the gateway makes the classify call itself). labels=None (routing off / soft-disabled) →
    # smart still answers, degrading to the benchmark default.
    from ..benchmarks import Benchmarks
    from ..routing.smart import TotoStickiness

    # One Redis client (when TOTO_GW_REDIS_URL is set) shared by the breaker's cross-replica OPEN
    # state AND the label-memo L2. None → per-replica behaviour for both.
    redis_client = _breaker_redis(settings)

    return Gateway(
        catalog=catalog, registry=registry, writer=writer,
        extractor=extractor, guard=guard, router=router, cache=cache,
        labels=_build_labels(settings, catalog),
        benchmarks=Benchmarks.load(settings.benchmarks),
        # Stickiness ladder. TotoStickiness is one class, rungs in precedence: DeclaredSession
        # (declared:<hash> → long eager hold) > LabelAwareTTL (per-task-type: org `stick_ttls` >
        # TOTO_GW_STICK_TTLS default > flat 900s) > WarmthHold floor (a hot conversation extends
        # the pin). With no maps and cold conversations it is the flat 900s slide.
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
        # Cross-replica shared breaker OPEN state when a Redis URL is set (else None →
        # per-replica). Lazy client (connects on first command); fail-open on any Redis error.
        breaker_redis=redis_client,
        # The same client backs the label-memo L2 (cross-replica classification sharing).
        memo_redis=redis_client,
        # Hold a conversation's warm model over a fresh pick while its provider prefix cache
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
    from ..routing.labels import LabelBindings

    labels = LabelBindings(settings.label_bindings or None)
    errs = labels.validate(catalog)
    if catalog.get(settings.label_classifier_model) is None:
        errs.append(f"classifier model {settings.label_classifier_model!r} not in catalog")
    if errs:
        # Expected on the plain default catalog (no provider key) — a degraded mode, not a fault.
        # WARNING with the exact fix, so a fresh clone learns the one env var to set from the log.
        import os

        hint = ("set OPENROUTER_API_KEY and restart to enable smart routing with the bundled "
                "OpenRouter catalog" if not os.environ.get("OPENROUTER_API_KEY")
                else "point TOTO_GW_CATALOG at a catalog that carries these models "
                "(e.g. catalog.openrouter.yaml)")
        logging.getLogger("toto_gateway.routing").warning(
            "smart task-type routing disabled — requests still serve via the fallback router. "
            "Cause: %s. To fix: %s.", "; ".join(errs), hint)
        return None
    return labels


def _breaker_redis(settings: Settings):
    """The optional redis.asyncio client for cross-replica breaker state. None unless a Redis URL
    is set. Lazy (from_url connects on first command), so no loop needed here at build."""
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
        # Driver spans carry task text (a durable payload sink). A zero-retention org's run
        # leaves no span line on disk; its routing decision still lives, payload-free, on the
        # gateway_events trace row. Identity-thin runs (no org) write spans as usual.
        from ..routes.deps import current_identity

        if getattr(current_identity(), "zero_retention", False):
            return
        try:
            with open(path, "a") as f:
                f.write(json.dumps(span, default=str) + "\n")
        except Exception:
            pass

    return observe


async def audit_driver_denial(auth, identity, exc) -> None:
    """Write a catalog.model_denied audit row for an org-allowlist denial on the driver plane
    (chat.py already does this on its plane; the gateway has no store handle, so the driver's
    gateway-bridge closure calls this). ONLY the allowlist gate — a per-team deny
    (exc.allowlist False) keeps its policy_violation shape and is not an org-governance event.
    No-op when the store is absent (offline/test gateways) or it isn't an allowlist denial."""
    from .. import audit

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
    from ..driver.core import Driver, Exec
    from ..pipeline import ModelNotPermittedError

    if settings.prompts_file:  # dev prompt overrides — no-op when unset/missing, loud when malformed
        from ..driver import prompts

        prompts.load_overrides_file(settings.prompts_file)

    from ..routes.deps import current_identity

    async def complete_fn(req) -> "Exec":
        # Enforce the caller's team catalog policy on the driver plane too: the identity was
        # stashed by require_auth and rides the request context into this boot-time closure.
        try:
            res = await gateway.complete(req, harness="driver", identity=current_identity())
        except ModelNotPermittedError as exc:
            # Mirror chat.py — an org-allowlist denial on the driver plane also writes a
            # catalog.model_denied audit row (the gateway itself has no store handle, so this
            # driver-plane choke does it). Only the allowlist gate, not the per-team deny.
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
        from ..driver.toto_client import TotoClient

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

    # Embedding routing + experience corpus. Embedder is None without an OpenRouter key → both
    # silently degrade to the keyword path.
    from ..embeddings import build_embedder

    # fake_exec = fully offline (tests, no-key quickstart) → no real embedding calls either.
    embedder = None if settings.fake_exec else build_embedder(settings, store=runs)
    corpus_sink = None
    if runs is not None and embedder is not None and settings.embed_corpus:
        from ..runs import CURRENT_RUN_ID

        async def corpus_sink(task_id, text, skill, model_id, outcome, cost_usd, latency_ms):
            run_id = CURRENT_RUN_ID.get()
            if not run_id:
                return
            # The experience corpus persists task text (a durable payload sink). A zero-retention
            # org contributes no rows — routing for its traffic falls back to the payload-free
            # paths (benchmark/label), exactly as when the corpus is empty.
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
    from ..driver.knn import build_experience_knn

    knn = build_experience_knn(settings, runs, embedder, gateway.catalog)

    # Label routing (default ON). Any incoherence between bindings and the running catalog —
    # a bound id this catalog doesn't carry (normal on reduced/dev catalogs), a fake-lane
    # binding, or a missing classifier model — soft-disables the feature with a loud log and
    # routing falls back to classify(). Boot never blocks: the ladder below is always correct
    # (the Benchmarks.load missing-file semantics).
    # Shared with the gateway's smart route: one soft-disable path, one loud log. ERROR, not
    # warning — on the full catalog a typo'd labels.yaml silently reverts the fleet to benchmark
    # routing. The shipped file is CI-guarded (test_shipped_bindings_are_valid_against_openrouter_
    # catalog); the log is the tripwire for custom TOTO_GW_LABEL_BINDINGS files and reduced catalogs.
    labels = _build_labels(settings, gateway.catalog)

    # Subagent runners: flag on → live pi/claude_code adapters register before the gateway
    # catch-all; pi's callback provider is THIS gateway's /v1 (loopback), authing with the
    # operator token when auth is on. Flag off → None → default gateway-only registry.
    adapters = None
    if settings.subagent_runners:
        from ..driver.adapters import AdapterRegistry

        adapters = AdapterRegistry.with_subagents(
            complete_fn, gateway_base_url=f"http://127.0.0.1:{settings.port}/v1",
            gateway_api_key=settings.auth_token, timeout=settings.subagent_timeout)

    return Driver(
        catalog=gateway.catalog, complete_fn=complete_fn,
        driver_model=settings.driver_model, triage_model=settings.triage_model,
        toto=toto, observe=observe, adapters=adapters,
        # Share the gateway's Benchmarks object (not a second load) so the boot overlay + a
        # POST /v1/admin/benchmarks/refresh hot-swap are seen by BOTH planes at once.
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
