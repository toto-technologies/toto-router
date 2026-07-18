"""The dispatch core (context doc §7.1, Phase-0 slice).

ingest -> catalog resolve -> dispatch via Runner -> tee response -> account -> trace.

No routing intelligence: the lane is whatever the catalog says for the requested model. The
value Phase 0 proves lives in the accounting + trace, not the decision. Two concerns are kept
rigorously correct here because nothing downstream can fix them later:

  1. Usage/cost accounting — prefer upstream-reported usage; estimate + flag otherwise.
  2. Routing tax — `latency_ms_gateway_overhead` = total wall minus the upstream's own wall,
     so we measure exactly what the gateway adds (guardrail #3).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import time
import uuid
from typing import AsyncIterator

from .breaker import CircuitBreaker, CircuitOpen, provider_key
from .catalog import Catalog, CatalogEntry, UnknownModelError, effective_catalog
from .pipeline import (
    ALLOW,
    BLOCK,
    DOWNGRADE_LOCAL,
    AllowGuard,
    BlockedError,
    CatalogRouter,
    DataPolicyDeniedError,
    Decision,
    Guard,
    GuardVerdict,
    ModelNotPermittedError,
    NoCache,
    NoExtractor,
    ResponseCache,
    Router,
    Signal,
    SignalExtractor,
)
from .pricing import compute_cost_usd
from .resilience import backoff as _backoff
from .routing.decision import effective_policy, resolve_fail_policy
from .routing.candidates import (
    CandidateCatalog,
    CandidateIneligibleError,
    CandidateNotFound,
    EligibilityContext,
    EligibilityEngine,
    request_modalities,
    requested_parameters,
)
from .resilience import err_label as _err_label
from .resilience import fallbacks as _fallbacks
from .resilience import is_retryable as _is_retryable
from .resilience import retry_after_seconds as _retry_after
from .runners.registry import RunnerRegistry
from .schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
)
from .obs import escalated_from_var, request_id_var
from .tokens import estimate_prompt_tokens, estimate_tokens
from .trace import TraceRecord, TraceWriter, sql_engine, write_request_content


class StreamStallError(Exception):
    """A streamed upstream opened then went silent past the inter-chunk deadline — we abandon it
    (close upstream, finalize the trace as error=stream_stall) rather than hold the slot."""


class GatewayDegradedError(Exception):
    """W1-C1: a smart-routing degradation happened (classify_failed | policy_error | breaker_open)
    and the caller's org fail_policy is 'closed' — reject with 503 instead of serving the failure
    floor. `reason` is the degraded_mode string; the route handler renders it in the error body."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BudgetExceededError(Exception):
    """W2-C5: the caller's team/org monthly budget is over 100% and its action is 'reject'. The
    route handler renders a 402 budget_exceeded body; the gateway already wrote the trace row
    (status=error, budget_state=rejected), mirroring the fail-closed degraded trace."""

    def __init__(self, decision) -> None:
        super().__init__("budget_exceeded")
        self.decision = decision  # BudgetDecision (pct/spend/monthly_usd/scope for the error body)


def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex}"


def _conversation_key(messages) -> str | None:
    """A stable fingerprint grouping every turn of one conversation: 16-hex prefix of sha256 over
    (system text) + NUL + (FIRST user text). Turns share a system prompt + opening user message, so
    the key is identical across a multi-turn chat regardless of later messages. None when there is
    no user message to anchor on (degenerate / non-chat)."""
    system = next((m.text() for m in messages if m.role == "system"), "")
    first_user = next((m.text() for m in messages if m.role == "user"), None)
    if first_user is None:
        return None
    return hashlib.sha256(f"{system}\x00{first_user}".encode()).hexdigest()[:16]


def _declared_key(value: str | None) -> str | None:
    """A client-declared session identity (S3) → the `declared:<hash>` conversation_key that
    overrides the message fingerprint, so every turn the client tags with the same session id
    anchors on ONE memo entry (a long, eager hold). None when nothing was declared."""
    if not value:
        return None
    return "declared:" + hashlib.sha256(str(value).encode()).hexdigest()[:16]


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000))


def _label_metadata_json(smart) -> str | None:
    """SmartResult.label_metadata (the totoshape classify block) as a JSON string for the trace's
    label_metadata column, or None. Compact + sorted so identical metadata is byte-identical."""
    md = getattr(smart, "label_metadata", None)
    return json.dumps(md, sort_keys=True, separators=(",", ":")) if md else None


class GatewayResult:
    """A non-streaming dispatch result: the response plus its finalized trace record."""

    def __init__(self, response: ChatCompletionResponse, trace: TraceRecord) -> None:
        self.response = response
        self.trace = trace


class Gateway:
    def __init__(
        self,
        catalog: Catalog,
        registry: RunnerRegistry,
        writer: TraceWriter,
        *,
        candidates: CandidateCatalog | None = None,
        extractor: SignalExtractor | None = None,
        guard: Guard | None = None,
        router: Router | None = None,
        cache: ResponseCache | None = None,
        max_concurrent_llm: int = 0,
        retries: int = 2,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        passthrough_fallback: bool = True,
        breaker_fail_threshold: int = 5,
        breaker_reset_seconds: float = 30.0,
        stream_stall_timeout: float = 30.0,
        observe=None,
        breaker_redis=None,
        labels=None,
        benchmarks=None,
        classifier_model: str = "",
        label_timeout_ms: int = 10000,
        log_content: bool = False,
        stick=None,
        memo_redis=None,
        warmth_routing: bool = False,
    ) -> None:
        self.catalog = catalog
        self.candidates = candidates or CandidateCatalog()
        # W2-C5: team/org monthly budget enforcer. None (tests, driver-internal, no auth store) →
        # no budget check, today's behavior. app.py sets it after the AuthStore exists.
        self.budget = None
        self._eligibility = EligibilityEngine()
        self.registry = registry
        self.writer = writer
        # Observability content-capture (TOTO_GW_LOG_CONTENT): when on, capture prompt+response
        # into the request_content sibling table at finalize. Off (default here; app wires the
        # settings default of True) → no content written, exactly as before.
        self._log_content = log_content
        # Global valve on concurrent outbound LLM calls (0 = unlimited). asyncio.Semaphore is
        # loop-agnostic until first await (3.10+), so building it here (no loop yet) is safe.
        self._llm_sem = asyncio.Semaphore(max_concurrent_llm) if max_concurrent_llm > 0 else None
        self._max_llm = max_concurrent_llm  # cap kept so gw_llm_semaphore_inflight = cap - available
        # Passthrough-plane resilience (P3): same-model retry + residency-bounded fallback, shared
        # with the driver via resilience.py. Only engaged when a caller passes resilient=True
        # (routes/chat.py) — the driver plane owns its own retry in Driver._call, so its
        # gateway.complete calls stay resilient=False (no double stack).
        self._retries = retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._passthrough_fallback = passthrough_fallback
        # Stream stall / first-token deadline (P5): abandon a stream whose next chunk doesn't
        # arrive within this budget, instead of holding the slot for the full read timeout.
        self._stall_timeout = stream_stall_timeout
        # Per-provider circuit breaker (P4): keyed by base_url host, in-process per replica. Only
        # consulted on the resilient (passthrough) path — the driver plane owns its own resilience.
        # breaker_redis (Wave 2 R1): optional redis.asyncio client → cross-replica shared OPEN state
        # (peers fast-fail together). None → in-process per-replica, exactly as before.
        self._breaker = CircuitBreaker(fail_threshold=breaker_fail_threshold,
                                       reset_seconds=breaker_reset_seconds, redis=breaker_redis)
        # Optional span sink for breaker transitions (circuit_open/circuit_close). No-op by default.
        self._observe = observe or (lambda span: None)
        # Phase 1 decision pipeline. Defaults reproduce exact Phase-0 behaviour.
        self.extractor = extractor or NoExtractor()
        self.guard = guard or AllowGuard()
        self.router = router or CatalogRouter()
        self.cache = cache or NoCache()
        # Smart auto-routing (SR1): the `smart` sentinel model classifies the request and picks a
        # model per the team's label bindings. Requires the classifier model (or-haiku-4.5 on the
        # deploy) in the catalog; does NOT require the driver — the gateway makes the classify call
        # itself, straight on the runner. labels=None (label routing off / soft-disabled) → smart
        # still answers, degrading to the benchmark default. See routing/smart.py.
        self._labels = labels
        self._benchmarks = benchmarks
        self._classifier_model = classifier_model
        self._label_timeout_ms = label_timeout_ms
        # Stickiness policy (S1): governs the label memo's per-conversation hold. None → the flat
        # sliding TTL, byte-for-byte as before; app.py wires SlidingTTL() as the explicit default.
        self._stick = stick
        # Optional Redis L2 for the label memo (S4): cross-replica sharing of the classification.
        # Same client as the breaker; None → per-replica L1 only. Fail-open on any Redis error.
        self._memo_redis = memo_redis
        # TTL-aware incumbent hold (chunk B): when True, smart_route keeps a conversation's warm
        # model over a fresh pick while its provider prefix cache is live. False → fresh every turn.
        self._warmth_routing = warmth_routing

    @property
    def benchmarks(self):
        """The live Benchmarks. Shared with the Driver (one object) so a hot-swap of its `.models`
        (POST /v1/admin/benchmarks/refresh) is seen by both planes at once."""
        return self._benchmarks

    def catalog_for(self, identity=None) -> Catalog:
        """The caller's EFFECTIVE catalog: base + their server-side adoptions (catalog-adoption).
        No adoptions (operator, driver-internal, the common single-user case) → `self.catalog`
        unchanged, at zero cost. The one seam every per-request catalog read goes through so an
        adopted model is routable by explicit name on BOTH planes."""
        return effective_catalog(self.catalog, identity)

    def resolve(self, model_id: str, identity=None) -> CatalogEntry:
        catalog = self.catalog_for(identity)
        resolved = self.candidates.resolve(catalog, model_id, identity)
        if not isinstance(resolved, CandidateNotFound):
            return resolved
        known = ", ".join([
            *(entry.id for entry in catalog.models),
            *(entry.id for entry in self.candidates.platform_entries(catalog)),
        ])
        raise UnknownModelError(model_id, known)

    @property
    def smart_enabled(self) -> bool:
        """Smart routing can actually classify on this deploy (labels loaded + classifier in the
        catalog). When False the `smart` model still answers — it degrades to the benchmark
        default — but never classifies."""
        return self._labels is not None and self.catalog.get(self._classifier_model) is not None

    async def _classify_text(self, messages: list[dict], model_id: str, max_tokens: int = 200,
                             *, identity=None) -> str:
        """One classifier call, straight on the runner — NOT through complete(), so it writes no
        trace and never appears as a user-facing turn. Caller guarantees model_id is in the catalog.
        `max_tokens` defaults to the classifier's 200; analytics insights pass more for prose.

        W3-C1: resolves against the CALLER's effective catalog (base + their adoptions) so an
        org-adopted model can BE the classifier. `identity=None` (operator/analytics) → the base
        catalog, unchanged. The caller (smart_route) already gates on the effective catalog before
        dispatching here, so an absent classifier degrades to classify_failed rather than raising."""
        from .schemas import Message

        entry = self.catalog_for(identity).require(model_id)  # caller's effective catalog (adoptions)
        runner = self.registry.for_entry(entry)
        req = ChatCompletionRequest(
            model=model_id, messages=[Message(**m) for m in messages],
            temperature=0.0, max_tokens=max_tokens,
        )
        async with (self._llm_sem or contextlib.nullcontext()):
            resp = await runner.chat(req, entry)
        return (resp.choices[0].message.content or "") if resp.choices else ""

    async def _resolve_smart(self, req: ChatCompletionRequest, identity):
        """If req.model is the `smart` sentinel, classify + resolve to a real model and return the
        rewritten request plus a SmartResult (route_reason/label to stamp). Otherwise a no-op:
        (req, None). Never raises — a classify failure degrades to the benchmark default."""
        from .routing import smart

        if not smart.is_smart(req.model):
            return req, None
        text = next((m.text() for m in reversed(req.messages) if m.role == "user"), "")
        conv_key = getattr(req, "conversation_key", None)
        catalog = self.catalog_for(identity)  # base + caller adoptions — adopted ids are selectable here
        require_tools = bool(getattr(req, "tools", None))  # SR2: tools-bearing → tool-capable model
        # W1-C1: a routing-policy-engine error degrades to the benchmark default (policy=None); the
        # reason rides the SmartResult so complete()/stream() can fail-closed on it if the org asks.
        try:
            policy = effective_policy(identity)
        except Exception:
            result = smart.SmartResult(
                smart.fallback_model(catalog, self._benchmarks, None, require_tools),
                "smart:policy_error", None)
            return req.model_copy(update={"model": result.model_id}), result
        # Warmth routing is a per-request knob (A8): the caller's org/team cache policy overrides the
        # global default when it names `warmth_routing`, else the gateway-wide flag stands.
        pol_wr = (getattr(policy, "cache", None) or {}).get("warmth_routing")
        warmth_routing = self._warmth_routing if pol_wr is None else bool(pol_wr)
        taxonomy = getattr(policy, "taxonomy", None) or None  # W2-C7: rides the one classify call
        # W3-C1: the org's chosen classifier wins over the gateway default. classify_fn carries the
        # identity so _classify_text resolves the id against the caller's effective catalog (adoptions).
        classifier_model = getattr(policy, "classifier_model", None) or self._classifier_model
        classify_fn = lambda msgs, mid: self._classify_text(msgs, mid, identity=identity)  # noqa: E731
        if self._labels is None:  # label routing off/soft-disabled → benchmark default, still answers
            # W2-C7 small-fix 8a: a DISTINCT reason for the labels-off deploy path, so _smart_degraded
            # never conflates it with a genuine classify failure (labels-off is config, not degradation).
            result = smart.SmartResult(
                smart.fallback_model(catalog, self._benchmarks, policy, require_tools),
                "smart:labels_off", None,
            )
        else:
            result = await smart.smart_route(
                text, catalog=catalog, labels=self._labels, benchmarks=self._benchmarks,
                classifier_model=classifier_model, policy=policy,
                classify_fn=classify_fn, timeout_s=self._label_timeout_ms / 1000.0,
                require_tools=require_tools, conversation_key=conv_key, stick=self._stick,
                memo_redis=self._memo_redis, warmth_routing=warmth_routing, taxonomy=taxonomy,
            )
        return req.model_copy(update={"model": result.model_id}), result

    async def _resolve_data_policy(self, req: ChatCompletionRequest, identity, smart):
        """(data_label, constraint) for the org's data-classification taxonomy (W2-C7), or (None,
        None) when the org configured none (zero overhead — the common case). The constraint is
        'local_only' | 'deny' | None.

        On the SMART path the data label rode the smart classify (smart.data_label — no extra call).
        On an EXPLICIT-model request under a taxonomy org we spend ONE classify call (memoized per
        conversation, shared with any smart turns) so the data policy can't be bypassed by naming a
        model directly — this is the point of the feature. When no classifier is available the label
        is None and the taxonomy `default` constraint applies (fail-closed default). Never raises."""
        from .routing import smart as smart_mod

        try:
            policy = effective_policy(identity)
        except Exception:
            return None, None
        taxonomy = getattr(policy, "taxonomy", None) or {}
        if not taxonomy.get("labels"):
            return None, None
        if smart is not None:
            data_label = smart.data_label
        elif self.smart_enabled:  # explicit model + taxonomy org → classify for the data label
            text = next((m.text() for m in reversed(req.messages) if m.role == "user"), "")
            # W3-C1: same org classifier + caller-catalog binding as the smart path.
            classifier_model = getattr(policy, "classifier_model", None) or self._classifier_model
            classify_fn = lambda msgs, mid: self._classify_text(msgs, mid, identity=identity)  # noqa: E731
            try:
                res = await smart_mod.smart_route(
                    text, catalog=self.catalog_for(identity), labels=self._labels,
                    benchmarks=self._benchmarks, classifier_model=classifier_model,
                    policy=policy, classify_fn=classify_fn,
                    timeout_s=self._label_timeout_ms / 1000.0,
                    conversation_key=getattr(req, "conversation_key", None),
                    memo_redis=self._memo_redis, taxonomy=taxonomy)
                data_label = res.data_label
            except Exception:
                data_label = None
        else:  # no classifier on this deploy → the taxonomy default constraint (fail-closed)
            data_label = None
        return data_label, policy.taxonomy_constraint(data_label)

    def _cache_prefs(self, identity) -> dict:
        """The resolved cache auto-inject knobs for this request (A8): the caller's org/team cache
        policy wins per-field over the global env default. Stamped on the request so runners read
        auto-inject prefs off getattr(req, "cache_prefs") without importing identity/settings. A
        field the policy doesn't name inherits the global default; no policy at all → pure globals
        (byte-identical to pre-A8). Warmth routing is resolved separately in _resolve_smart (it's a
        smart-route input, not a runner one)."""
        from .config import get_settings

        cache = getattr(effective_policy(identity), "cache", None) or {}
        settings = get_settings()
        ai, mm = cache.get("auto_inject"), cache.get("auto_inject_min_messages")
        return {
            "auto_inject": settings.anthropic_auto_cache if ai is None else bool(ai),
            "auto_inject_min_messages":
                settings.anthropic_auto_cache_min_messages if mm is None else int(mm),
        }

    def _plan(self, req: ChatCompletionRequest,
              identity=None, *, data_constraint: str | None = None,
              data_label: str | None = None,
              ) -> tuple[CatalogEntry, Signal, Decision, str, ChatCompletionRequest]:
        """Run the decision pipeline: (catalog-policy substitute) -> extract -> guard -> route ->
        resolve -> (catalog-policy permit). `identity` feeds effective_policy so routing honors the
        team's catalog allow/deny overlay (C2); no overlay → effective_policy returns None → the
        router uses its global policy, unchanged. Returns the (possibly substituted) req so both
        the passthrough and driver planes dispatch exactly the model that was permitted.

        This is the single choke point for BOTH planes (passthrough + the driver's complete_fn both
        route through Gateway.complete/stream → here), so the fail-closed 403 is enforced once."""
        policy = effective_policy(identity)
        catalog = self.catalog_for(identity)  # base + caller adoptions (catalog-adoption)
        # default_model substitution: a caller who omits `model` gets the team default (C2). Applied
        # BEFORE planning so the default is what routes, is guarded, and is dispatched.
        if policy is not None and getattr(policy, "default_model", None) and not (req.model or "").strip():
            req = req.model_copy(update={"model": policy.default_model})
        signal = self.extractor.extract(req)
        verdict = self.guard.check(req, signal)
        if verdict.action == BLOCK:
            raise BlockedError(verdict.reasons)
        # W2-C7 data-classification enforcement at the routing FLOOR — holds for smart AND explicit
        # models (the resolved/requested model is what routes through here). `local_only` reuses the
        # guard's DOWNGRADE_LOCAL machinery (residency floor → first in-perimeter model), so an
        # explicit frontier model on restricted data lands local. `deny` is rejected earlier, before
        # the cache read (complete/stream), so a denied request is never served from cache.
        if data_constraint == "local_only" and verdict.action == ALLOW:
            verdict = GuardVerdict(action=DOWNGRADE_LOCAL,
                                   reasons=[f"data_policy:local_only:{data_label}"])
        decision = self.router.decide(req, signal, verdict, catalog, policy=policy)
        entry = self.resolve(decision.model_id, identity)
        # W1-C3 org allowlist gate: deny-by-default at the ORG level, checked on the RESOLVED model
        # BEFORE provider eligibility so an unapproved model surfaces a clean model_not_permitted
        # 403 (ask-your-admin) instead of a generic ineligibility. This is the same predicate
        # Policy.permits folds in, so the smart path already routes candidate selection AROUND
        # unapproved models where an approved one exists — this final check catches the rest, so a
        # smart fallback can never reach the wire with a model outside the org's approved set.
        if policy is not None and getattr(policy, "org_allowlist", None) is not None \
                and entry.id not in policy.org_allowlist:
            raise ModelNotPermittedError(entry.id, allowlist=True)
        # Catalog-scope enforcement: fail-closed 403 model_not_permitted BEFORE any upstream call.
        # Checked on the RESOLVED model (what would actually be dispatched), so a guard/policy
        # downgrade to a permitted model still passes and a denied model never reaches the wire.
        if entry.lane == "provider":
            from .credentials import byok_keys

            max_output = req.max_tokens or getattr(req, "max_completion_tokens", None) or 0
            context = EligibilityContext(
                token_estimate=signal.token_estimate,
                max_output_tokens=max_output,
                has_tools=signal.has_tools,
                modalities=request_modalities(req),
                requested_parameters=requested_parameters(req),
                user_credential_envs=frozenset(byok_keys.get()),
                platform_credential_envs=frozenset(
                    (entry.api_key_env,) if os.environ.get(entry.api_key_env) else ()),
                now=time.time(),
            )
            eligibility = self._eligibility.evaluate(entry, context, policy)
            if not eligibility.eligible:
                raise CandidateIneligibleError(entry, eligibility.reasons)
        elif policy is not None and not policy.permits(entry):
            raise ModelNotPermittedError(entry.id)
        return entry, signal, decision, verdict.action, req

    def _baseline(self, usage: Usage) -> float | None:
        ref = self.catalog.frontier_reference()
        return compute_cost_usd(ref, usage) if ref else None

    def _begin_trace(
        self, entry: CatalogEntry, *, request_id: str, stream: bool, harness, task_id,
        identity=None, conversation_key: str | None = None,
    ) -> TraceRecord:
        runner = self.registry.for_entry(entry)
        # Stamp tenant identity on EVERY trace path (normal, cache-hit, blocked, stream) — this is
        # the single choke point, so it also covers the paths _account never touches. Duck-typed
        # (reads .org_id/.team_id); None for identity-thin callers (operator, driver-internal).
        record = TraceRecord.begin(
            request_id=request_id,
            conversation_key=conversation_key,
            lane=entry.lane,
            runner_id=getattr(runner, "runner_id", entry.lane),
            model=entry.id,
            residency_class=entry.residency_class,
            stream=stream,
            harness=harness,
            task_id=task_id,
            org_id=getattr(identity, "org_id", None),
            team_id=getattr(identity, "team_id", None),
            user_id=getattr(identity, "user_id", None),  # analytics A1 (label derived at finalize)
            identity_id=entry.identity_id,
            offer_id=entry.offer_id,
            provider=entry.provider,
            upstream_model=entry.effective_upstream_model if entry.lane == "provider" else None,
            credential_scope=entry.credential_scope_label,
        )
        # W3-C3: the escalation signal for THIS request, from the x-toto-escalated-from header the
        # middleware validated onto the contextvar. Read here (not passed) so every trace path —
        # normal, cache-hit, blocked, denied, degraded — and every surface records it identically.
        record.escalated_from = escalated_from_var.get()
        return record

    def _account(self, trace: TraceRecord, entry: CatalogEntry, usage: Usage, estimated: bool):
        trace.tokens_prompt = usage.prompt_tokens
        trace.tokens_completion = usage.completion_tokens
        trace.tokens_cached = usage.tokens_cached
        trace.tokens_cache_write = usage.tokens_cache_write
        trace.cost_usd = compute_cost_usd(entry, usage)
        trace.cost_estimated = estimated
        trace.frontier_baseline_usd = self._baseline(usage)
        # Conversation warmth stat (S4): in-memory only, read by WarmthHold at assess on the next
        # turn. This is the single accounting choke point (complete + stream both land here).
        from .routing import smart

        smart.record_warmth(trace.conversation_key, usage.tokens_cached, entry.id)

    def _stamp_decision(self, trace, signal: Signal, decision: Decision, guard_action: str,
                        data_label: str | None = None) -> None:
        trace.route_reason = decision.reason
        trace.guard_action = guard_action
        trace.data_label = data_label  # W2-C7: the org data classification (None when no taxonomy)
        trace.signal_intent = signal.intent if signal.intent != "unknown" else None
        trace.signal_complexity = signal.complexity if signal.complexity != "unknown" else None

    def _blocked_trace(self, req, *, request_id, stream, harness, task_id, t0, exc,
                       identity=None) -> TraceRecord:
        entry = self.resolve(req.model, identity)
        trace = self._begin_trace(
            entry, request_id=request_id, stream=stream, harness=harness, task_id=task_id,
            identity=identity,
        )
        trace.status, trace.error, trace.guard_action = "blocked", str(exc), "block"
        self._finalize(trace, t0, upstream_s=0.0)
        return trace

    def _denied_trace(self, model_id, *, request_id, stream, harness, task_id, t0, exc,
                      identity=None) -> TraceRecord:
        """Provenance for a catalog-policy denial — same shape as a block, so an admin sees the
        refused model in the trace/audit. The denied model resolved (it's in the catalog, just not
        permitted), so we can begin its trace normally."""
        entry = self.resolve(model_id, identity)
        trace = self._begin_trace(
            entry, request_id=request_id, stream=stream, harness=harness, task_id=task_id,
            identity=identity,
        )
        trace.status, trace.error = "denied", str(exc)
        trace.route_reason, trace.guard_action = "policy:model_not_permitted", "deny"
        self._finalize(trace, t0, upstream_s=0.0)
        return trace

    def _ineligible_trace(self, exc: CandidateIneligibleError, *, request_id, stream, harness,
                          task_id, t0, identity=None) -> TraceRecord:
        trace = self._begin_trace(
            exc.entry, request_id=request_id, stream=stream, harness=harness, task_id=task_id,
            identity=identity,
        )
        trace.status, trace.error = "denied", str(exc)
        trace.route_reason, trace.guard_action = f"candidate:{exc.reasons[0]}", "deny"
        self._finalize(trace, t0, upstream_s=0.0)
        return trace

    def _data_denied_trace(self, req, *, request_id, stream, harness, task_id, t0, data_label,
                           identity=None) -> TraceRecord:
        """Provenance for a W2-C7 data-policy 403 (deny constraint): no upstream call is made, but the
        trace row is written carrying the data_label + a data_policy_denied reason, so the denial is
        auditable exactly like a served request."""
        entry = self.resolve(req.model, identity)
        trace = self._begin_trace(
            entry, request_id=request_id, stream=stream, harness=harness, task_id=task_id,
            identity=identity,
        )
        trace.status, trace.error = "denied", "data_policy_denied"
        trace.route_reason, trace.guard_action = "policy:data_policy_denied", "block"
        trace.data_label = data_label
        self._finalize(trace, t0, upstream_s=0.0)
        return trace

    # --- fail-policy (W1-C1) -------------------------------------------------

    def _smart_degraded(self, smart) -> str | None:
        """The degradation reason for a smart-routing SmartResult, or None. A request served by a
        failure floor rather than the intended classification: the classifier ran and failed
        ("classify_failed") or the routing policy engine errored ("policy_error"). classify_failed
        is gated on `smart_enabled` so a deploy with label routing OFF (classifier absent / labels
        unloaded) is NOT treated as a per-request degradation — that's config, not a failure.
        A classified-but-unbound label ('other'/benchmark_best/fallback) is the classifier WORKING,
        so it is not degraded. Breaker-forced fallback is detected in the dispatch loop instead."""
        if smart is None:
            return None
        if smart.route_reason == "smart:policy_error":
            return "policy_error"
        if smart.route_reason == "smart:classify_failed" and self.smart_enabled:
            return "classify_failed"
        return None

    def _fail_policy(self, identity):
        """The caller's raw fail policy — a scalar 'open'/'closed' OR a per-reason matrix dict (W2-C7),
        team->org via the routing overlay. Resolve it against a specific degradation reason with
        resolve_fail_policy(fp, reason). Fail-open on any policy-read error: a broken policy can't tell
        us to reject (that would 503 the request on a config error), so the switch itself degrades open."""
        try:
            return getattr(effective_policy(identity), "fail_policy", "open") or "open"
        except Exception:
            return "open"

    def _degraded_trace(self, req, *, request_id, stream, harness, task_id, t0, reason,
                        route_reason=None, data_label=None, identity=None) -> TraceRecord:
        """Provenance for a fail-closed 503 (W1-C1): the request degraded and the org rejects on
        degradation, so no upstream call is made — but the trace row is still written, carrying
        degraded_mode, so the degradation is auditable exactly like a served one."""
        entry = self.resolve(req.model, identity)
        trace = self._begin_trace(
            entry, request_id=request_id, stream=stream, harness=harness, task_id=task_id,
            identity=identity,
        )
        trace.status, trace.error = "error", f"gateway_degraded:{reason}"
        trace.degraded_mode = reason
        trace.route_reason = route_reason or f"smart:{reason}"
        trace.data_label = data_label  # W2-C7: stamp the classification even on a degradation reject
        self._finalize(trace, t0, upstream_s=0.0)
        return trace

    # --- budgets (W2-C5) -----------------------------------------------------

    async def _apply_budget(self, req: ChatCompletionRequest, identity, *, request_id: str,
                            stream: bool, harness, task_id, t0: float):
        """Consult the team/org monthly budget once, at the top of complete()/stream(). Returns
        (possibly model-rewritten req, budget_state | None). Over budget: 'reject' writes a trace
        row and raises BudgetExceededError (402 at the route); 'downgrade' rewrites req.model to the
        cheapest eligible model; 'observe' (and any request under budget) just stamps. No enforcer /
        no budget / under budget → (req, None), byte-identical to before. Never enforces on a failure
        (the enforcer fails open)."""
        if self.budget is None:
            return req, None
        decision = await self.budget.decide(getattr(identity, "org_id", None),
                                            getattr(identity, "team_id", None),
                                            getattr(identity, "user_id", None))
        if decision is None or not decision.over:
            return req, None
        if decision.action == "reject":
            entry = self.resolve(req.model, identity)
            trace = self._begin_trace(entry, request_id=request_id, stream=stream, harness=harness,
                                      task_id=task_id, identity=identity)
            trace.status, trace.error = "error", "budget_exceeded"
            trace.budget_state = "rejected"
            trace.route_reason = "budget:rejected"
            self._finalize(trace, t0, upstream_s=0.0)
            raise BudgetExceededError(decision)
        if decision.action == "downgrade":
            cheap = self._cheapest_permitted(identity)
            if cheap is not None and cheap.id != req.model:
                return req.model_copy(update={"model": cheap.id}), "downgraded"
            return req, "downgraded" if cheap is not None else "over"  # no cheaper option → serve, stamp
        return req, "over"  # observe

    def _cheapest_permitted(self, identity):
        """The cheapest catalog model the caller's policy permits (price = prompt+completion per 1k),
        for the over-budget downgrade. permits() is permissive with no policy, so this is just the
        catalog's cheapest entry in the common case. None if the catalog is empty."""
        policy = effective_policy(identity)
        catalog = self.catalog_for(identity)

        def _price(e):
            p = e.price_usd_per_1k
            return p.prompt + p.completion

        eligible = [e for e in catalog.models if policy is None or policy.permits(e)]
        return min(eligible, key=_price) if eligible else None

    # --- non-streaming -------------------------------------------------------

    async def complete(
        self,
        req: ChatCompletionRequest,
        *,
        harness: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        resilient: bool = False,
        allow_fallback: bool | None = None,
        identity=None,
        declared_session: str | None = None,
    ) -> GatewayResult:
        t0 = time.perf_counter()
        # Prefer the middleware's request_id (echoed as X-Request-ID) so the trace record, the
        # X-Request-ID header, the x_toto block, and the LangSmith run all share ONE id and JOIN.
        # Captured at entry (not read later) so streaming finalize inside the response generator
        # can't miss it. Falls back to a self-minted id for direct callers (tests, driver-internal).
        request_id = request_id or request_id_var.get() or _new_request_id()
        # A client-declared session (S3) overrides the message fingerprint as the anchor; else the
        # stable system+first-user fingerprint. Client-sent session_id/prompt_cache_key still win
        # upstream in the runner (they stay in the body — declared_session is a separate arg).
        conv_key = _declared_key(declared_session) or _conversation_key(req.messages)
        # Stamp the conversation anchor + resolved cache prefs (A8) on the request so both survive
        # the model_copy chain down to the runner (cache-affinity hints; auto-inject knobs) and the
        # anchor drives the smart-route label memo. Internal fields, stripped before upstream
        # forwarding (schemas.passthrough_params).
        req = req.model_copy(update={"conversation_key": conv_key,
                                     "cache_prefs": self._cache_prefs(identity)})

        # Smart auto-routing (SR1): resolve the `smart` sentinel to a real model BEFORE cache +
        # plan, so cache keying, guard, catalog policy, timeouts, breaker, fallback, cost + trace
        # all run unchanged on the resolved model. No-op (smart=None) for a normally-named model.
        req, smart = await self._resolve_smart(req, identity)
        # W1-C2: the classify wall for THIS request, or None when no classifier call ran (non-smart
        # request, sticky-session memo hit, or labels-off). Stamped on every trace below so the
        # fast path is visible and analytics can aggregate it.
        classify_ms = smart.classify_ms if smart is not None else None

        # W2-C7 data-classification: resolve the org taxonomy's data label + constraint (rides the
        # smart classify, or one dedicated classify on an explicit-model request under a taxonomy org).
        # (None, None) with zero overhead when the org configured no taxonomy.
        data_label, data_constraint = await self._resolve_data_policy(req, identity, smart)

        # Fail policy (W1-C1): a smart-routing degradation (classifier failed / policy engine
        # errored) under an org set fail-closed is a 503, not a silent fall-through to the floor.
        # Resolved once here (raw scalar-or-matrix) — reused by the breaker-fallback check below.
        # Fail-open is the default, so degraded_mode is only STAMPED (below); the request serves the floor.
        #
        # W2-C7 composition (documented 2x2 in docs/qa/QA-taxonomy.md): fail_policy decides serve-vs-503 for a
        # ROUTING degradation and is checked FIRST — a closed org 503s a classify failure BEFORE the
        # taxonomy default is consulted. Only a SERVED request reaches the taxonomy constraint below
        # (the `deny` check + the local_only floor in _plan); on a classify failure that serves (open),
        # the taxonomy `default` constraint applies via _resolve_data_policy.
        fail_policy = self._fail_policy(identity)
        degraded_reason = self._smart_degraded(smart)
        if degraded_reason and resolve_fail_policy(fail_policy, degraded_reason) == "closed":
            self._degraded_trace(
                req, request_id=request_id, stream=False, harness=harness, task_id=task_id,
                t0=t0, reason=degraded_reason, route_reason=smart.route_reason,
                data_label=data_label, identity=identity)
            raise GatewayDegradedError(degraded_reason)

        # `deny` rejects the request — BEFORE the cache read, so a denied classification is never
        # served from cache. (A served request reaching _plan gets local_only enforced there.)
        if data_constraint == "deny":
            self._data_denied_trace(
                req, request_id=request_id, stream=False, harness=harness, task_id=task_id,
                t0=t0, data_label=data_label, identity=identity)
            raise DataPolicyDeniedError(data_label)

        # Budget (W2-C5): over 100% → reject (402, raises here) / downgrade (rewrite req.model to the
        # cheapest eligible model, so cache + plan + dispatch all run on it) / observe (stamp only).
        # Under budget or no budget → (req, None), unchanged. Runs before cache so a downgrade caches
        # under the cheap model's key.
        req, budget_state = await self._apply_budget(
            req, identity, request_id=request_id, stream=False, harness=harness, task_id=task_id,
            t0=t0)

        # BYOK requests bypass the shared exact-match cache entirely: its key has no user/BYOK
        # identity (cache/exact.py _normalize), so a hit would serve one user's BYOK-funded (and
        # possibly private-model) completion to another. Skip both read and write when a per-user
        # key is active. ponytail: cheapest correct fix; fold user_id into the key if BYOK+cache
        # ever need to coexist.
        from .credentials import byok_keys

        byok_active = bool(byok_keys.get())
        # Zero-retention (W1-C4): the caller's org opted out of ALL durable payload persistence.
        # Resolved once here and threaded to every sink below — content capture, the shared exact
        # cache (neither served-from nor seeded, exactly like BYOK), and the LangSmith mirror.
        zr = getattr(identity, "zero_retention", False)

        # Effective catalog for this caller: base + their adoptions (catalog-adoption). An adopted
        # model is a materialized static entry here, so it caches + falls back like any base model.
        catalog = self.catalog_for(identity)

        # Exact-match cache (non-stream only — YAGNI on streamed caching for v1).
        dynamic_requested = catalog.get(req.model) is None
        cached = None if byok_active or zr or dynamic_requested else self.cache.get(req)
        if cached is not None:
            entry = self.resolve(cached.model if catalog.get(cached.model) else req.model, identity)
            trace = self._begin_trace(
                entry, request_id=request_id, stream=False, harness=harness, task_id=task_id,
                identity=identity, conversation_key=conv_key,
            )
            trace.cache_hit, trace.route_reason, trace.status = True, "cache", "ok"
            trace.budget_state = budget_state  # W2-C5: over-budget observe/downgrade stamp
            trace.classify_ms = classify_ms  # W1-C2: the classify (if any) ran before the cache read
            trace.data_label = data_label  # W2-C7: stamp for audit (a cache hit has zero egress, so
            # local_only is satisfied — deny already rejected above, before the cache read)
            trace.tokens_prompt = cached.usage.prompt_tokens
            trace.tokens_completion = cached.usage.completion_tokens
            trace.cost_usd = 0.0  # a cache hit costs nothing
            trace.frontier_baseline_usd = self._baseline(cached.usage)
            self._finalize(trace, t0, upstream_s=0.0)
            cached_text = cached.choices[0].message.content if cached.choices else ""
            self._capture_content(request_id, req, cached_text, zr)
            self._trace_smart_ls(smart, req, trace, cached_text, zr)
            return GatewayResult(cached, trace)

        t_plan = time.perf_counter()  # W1-C2: decision-pipeline (plan) wall — the "route" stage
        try:
            entry, signal, decision, guard_action, req = self._plan(
                req, identity, data_constraint=data_constraint, data_label=data_label)
        except BlockedError as exc:
            self._blocked_trace(
                req, request_id=request_id, stream=False, harness=harness, task_id=task_id,
                t0=t0, exc=exc, identity=identity,
            )
            raise
        except ModelNotPermittedError as exc:
            self._denied_trace(
                exc.model_id, request_id=request_id, stream=False, harness=harness,
                task_id=task_id, t0=t0, exc=exc, identity=identity,
            )
            raise
        except CandidateIneligibleError as exc:
            self._ineligible_trace(
                exc, request_id=request_id, stream=False, harness=harness, task_id=task_id,
                t0=t0, identity=identity,
            )
            raise
        plan_ms = _ms(time.perf_counter() - t_plan)  # reached only when planning succeeded

        # Resilience (P3): same-model retry (bounded) then residency-bounded fallback across
        # catalog entries — shared policy with the driver (resilience.py). Only engaged when the
        # caller opts in (resilient=True, the passthrough route); the driver plane leaves it False
        # so Driver._call stays the single retry authority (no double stack). Fallback is on by
        # default (Alex ruling) with a per-request opt-out; it never crosses the residency
        # boundary (fallbacks() is residency-bounded), and the SERVED model is on the returned
        # trace (surfaced in x_toto.model). retries=0 + no fallback ⇒ byte-identical to Phase 0.
        retries = self._retries if resilient else 0
        fb_on = self._passthrough_fallback if allow_fallback is None else allow_fallback
        candidate_entries = [entry]
        if resilient and fb_on and entry.lane != "provider":
            candidate_entries += [
                catalog.require(model_id)
                for model_id in _fallbacks(catalog, entry.id, privacy=False)
            ]
        candidates = [candidate.id for candidate in candidate_entries]

        first_exc: BaseException | None = None
        breaker_forced = False  # W1-C1: a prior candidate's OPEN breaker forced this fallback
        for i, cand_entry in enumerate(candidate_entries):
            model_id = cand_entry.id
            key = provider_key(cand_entry.base_url)
            cand_req = req if model_id == req.model else req.model_copy(update={"model": model_id})
            runner = self.registry.for_entry(cand_entry)
            trace = self._begin_trace(
                cand_entry, request_id=request_id, stream=False, harness=harness, task_id=task_id,
                identity=identity, conversation_key=conv_key,
            )
            self._stamp_decision(trace, signal, decision, guard_action, data_label)
            trace.classify_ms, trace.plan_ms = classify_ms, plan_ms  # W1-C2: per-stage timings
            trace.budget_state = budget_state  # W2-C5: over-budget observe/downgrade stamp
            # W1-C1: a fallback served because the primary's breaker was open is a "breaker_open"
            # degradation; otherwise carry the smart-routing degraded_mode (classify/policy) through.
            trace.degraded_mode = "breaker_open" if breaker_forced else degraded_reason
            if smart is not None and i == 0:  # smart chose this model — stamp label:… over passthrough
                trace.route_reason = smart.route_reason
                trace.label_metadata = _label_metadata_json(smart)
            if i > 0:  # a fallback served this — record which model + why, honestly.
                trace.route_reason = \
                    f"fallback: {candidates[0]} {_err_label(first_exc)} → {model_id}"

            # Breaker: skip a provider whose breaker is OPEN — no wire, straight to the next
            # candidate (or a fast CircuitOpen when none remain). Passthrough plane only.
            if resilient and (not self._breaker.allow(key) or await self._breaker.peer_open(key)):
                first_exc = first_exc or CircuitOpen(key)
                trace.status, trace.error = "error", f"circuit_open:{key}"
                # Fail-closed (W1-C1): don't fall through to a fallback provider — a breaker-forced
                # fallback IS the degradation this org opted out of. 503 with the trace row written.
                if resolve_fail_policy(fail_policy, "breaker_open") == "closed":
                    trace.degraded_mode = "breaker_open"
                    self._finalize(trace, t0, upstream_s=0.0)
                    raise GatewayDegradedError("breaker_open")
                breaker_forced = True  # a later candidate that serves is breaker-degraded
                self._finalize(trace, t0, upstream_s=0.0)
                continue

            candidate_exc: BaseException | None = None
            for attempt in range(retries + 1):
                try:
                    t_up0 = time.perf_counter()
                    async with (self._llm_sem or contextlib.nullcontext()):
                        response = await runner.chat(cand_req, cand_entry)
                    upstream_s = time.perf_counter() - t_up0

                    usage = response.usage
                    estimated = usage.total_tokens == 0
                    if estimated:
                        # Upstream gave us nothing usable — estimate from request + response text.
                        content = response.choices[0].message.content if response.choices else ""
                        usage = Usage.of(
                            estimate_prompt_tokens(req.messages), estimate_tokens(content or "")
                        )
                        response.usage = usage
                    self._account(trace, cand_entry, usage, estimated)
                    trace.status = "ok"
                    if resilient and self._breaker.on_success(key):
                        await self._emit_span("circuit_close", provider=key, model=model_id)
                        await self._breaker.clear_open(key)  # tell peers this provider recovered
                    if not byok_active and not zr and entry.lane != "provider":
                        self.cache.put(req, response)
                    self._finalize(trace, t0, upstream_s=upstream_s)
                    served_text = response.choices[0].message.content if response.choices else ""
                    self._capture_content(request_id, req, served_text, zr)
                    self._trace_smart_ls(smart, req, trace, served_text, zr)
                    return GatewayResult(response, trace)
                except UnknownModelError:
                    raise
                except Exception as exc:
                    candidate_exc = exc
                    retryable = _is_retryable(exc)
                    # Record only transient failures on the breaker (never a 4xx) — a burst of
                    # client errors must not trip it. on_failure returns True on the trip edge.
                    if resilient and retryable and self._breaker.on_failure(key):
                        await self._emit_span("circuit_open", provider=key, model=model_id,
                                              threshold=self._breaker._threshold)
                        await self._breaker.record_open(key)  # peers fast-fail this provider too
                    # Same-model retry only while transient, opted in, budget left, breaker not
                    # (just) open. Otherwise stop attempting this candidate.
                    if (resilient and retryable and attempt < retries
                            and self._breaker.allow(key)):
                        first_exc = first_exc or exc
                        await asyncio.sleep(_backoff(
                            attempt, self._backoff_base, cap=self._backoff_cap,
                            retry_after=_retry_after(exc)))
                        continue
                    break

            # Candidate exhausted (retries spent, non-retryable, or breaker opened mid-way).
            trace.status = "error"
            trace.error = f"{type(candidate_exc).__name__}: {candidate_exc}"
            self._finalize(trace, t0, upstream_s=0.0)
            first_exc = first_exc or candidate_exc
            # Fall back to the next residency-bounded candidate on a transient failure; a
            # non-retryable 4xx raises at once (never burns a fallback on it).
            if resilient and _is_retryable(candidate_exc) and i + 1 < len(candidates):
                continue
            raise candidate_exc
        raise first_exc  # every candidate exhausted / all breakers open — surface the failure

    @staticmethod
    async def _aclose(aiter) -> None:
        """Close an async iterator (the upstream stream) so its socket/slot is released promptly
        on a stall. Best-effort — a runner whose iterator has no aclose is simply dropped."""
        aclose = getattr(aiter, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass

    async def _emit_span(self, node: str, **data) -> None:
        """Emit a gateway observability span (breaker transitions). Never raises — a broken
        observer must not break a request. Supports sync (list.append) and async sinks."""
        span = {"node": node, "ts": time.time(), **data}
        try:
            r = self._observe(span)
            if inspect.isawaitable(r):
                await r
        except Exception:
            pass

    # --- streaming -----------------------------------------------------------

    async def stream(
        self,
        req: ChatCompletionRequest,
        *,
        harness: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
        on_trace=None,
        identity=None,
        declared_session: str | None = None,
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Yield the chunks to forward to the client; write the trace when the stream closes.
        on_trace (if given) receives the finished TraceRecord — lets a caller recover
        tokens/cost/latency for a streamed call the way complete() returns them."""
        t0 = time.perf_counter()
        # Capture the middleware request_id at entry (see complete()): streaming finalize runs
        # inside the response generator, so we must grab it now, not read the contextvar later.
        request_id = request_id or request_id_var.get() or _new_request_id()
        conv_key = _declared_key(declared_session) or _conversation_key(req.messages)  # S3 override
        req = req.model_copy(update={"conversation_key": conv_key,  # see complete(); anchor + affinity
                                     "cache_prefs": self._cache_prefs(identity)})  # A8 auto-inject knobs
        # Smart auto-routing (SR1): classify + resolve BEFORE the stream opens (the classify call
        # is its own bounded request; the user stream sees only the resolved model).
        req, smart = await self._resolve_smart(req, identity)
        classify_ms = smart.classify_ms if smart is not None else None  # W1-C2 (see complete())
        # W2-C7 data-classification: resolve the org taxonomy's label + constraint (see complete()).
        # `local_only` threads into _plan to force the in-perimeter floor; `deny` rejects (SSE surfaces
        # it as an error event, like a block). fail_policy is checked FIRST — see the complete() note.
        data_label, data_constraint = await self._resolve_data_policy(req, identity, smart)
        # Fail policy (W1-C1): reject a smart-routing degradation up front when the org fails closed;
        # else stamp the reason on the trace below (fail-open serves the floor). The breaker/fallback
        # loop is complete()-only, so streaming degradations are classify/policy, not breaker_open.
        degraded_reason = self._smart_degraded(smart)
        if degraded_reason and resolve_fail_policy(self._fail_policy(identity), degraded_reason) == "closed":
            self._degraded_trace(
                req, request_id=request_id, stream=True, harness=harness, task_id=task_id,
                t0=t0, reason=degraded_reason, route_reason=smart.route_reason,
                data_label=data_label, identity=identity)
            raise GatewayDegradedError(degraded_reason)
        if data_constraint == "deny":
            self._data_denied_trace(
                req, request_id=request_id, stream=True, harness=harness, task_id=task_id,
                t0=t0, data_label=data_label, identity=identity)
            raise DataPolicyDeniedError(data_label)

        # Budget (W2-C5): same check as complete() — reject raises here (402), downgrade rewrites
        # req.model before _plan runs, observe just stamps. See _apply_budget.
        req, budget_state = await self._apply_budget(
            req, identity, request_id=request_id, stream=True, harness=harness, task_id=task_id,
            t0=t0)
        zr = getattr(identity, "zero_retention", False)  # W1-C4: gate the stream's content capture
        t_plan = time.perf_counter()  # W1-C2: decision-pipeline (plan) wall — the "route" stage
        try:
            entry, signal, decision, guard_action, req = self._plan(
                req, identity, data_constraint=data_constraint, data_label=data_label)
        except BlockedError as exc:
            self._blocked_trace(
                req, request_id=request_id, stream=True, harness=harness, task_id=task_id,
                t0=t0, exc=exc, identity=identity,
            )
            raise
        except ModelNotPermittedError as exc:
            self._denied_trace(
                exc.model_id, request_id=request_id, stream=True, harness=harness,
                task_id=task_id, t0=t0, exc=exc, identity=identity,
            )
            raise
        except CandidateIneligibleError as exc:
            self._ineligible_trace(
                exc, request_id=request_id, stream=True, harness=harness, task_id=task_id,
                t0=t0, identity=identity,
            )
            raise
        plan_ms = _ms(time.perf_counter() - t_plan)  # reached only when planning succeeded
        runner = self.registry.for_entry(entry)
        trace = self._begin_trace(
            entry, request_id=request_id, stream=True, harness=harness, task_id=task_id,
            identity=identity, conversation_key=conv_key,
        )
        self._stamp_decision(trace, signal, decision, guard_action, data_label)
        trace.classify_ms, trace.plan_ms = classify_ms, plan_ms  # W1-C2: per-stage timings
        trace.budget_state = budget_state  # W2-C5: over-budget observe/downgrade stamp
        trace.degraded_mode = degraded_reason  # W1-C1: fail-open served the floor; record it
        if smart is not None:  # stamp label:… / smart:classify_failed over the passthrough reason
            trace.route_reason = smart.route_reason
            trace.label_metadata = _label_metadata_json(smart)
        want_usage = bool(req.stream_options and req.stream_options.include_usage)

        acc_text: list[str] = []
        upstream_usage: Usage | None = None
        upstream_s = 0.0
        finalized = False
        t_up0 = time.perf_counter()
        slot_held = False

        async def acquire_slot() -> None:
            nonlocal slot_held
            if self._llm_sem is not None and not slot_held:
                await self._llm_sem.acquire()
                slot_held = True

        def release_slot() -> None:
            nonlocal slot_held
            if self._llm_sem is not None and slot_held:
                self._llm_sem.release()
                slot_held = False

        await acquire_slot()
        # Drive the upstream iterator by hand so every inter-chunk gap (including first-token) is
        # bounded by _stall_timeout. A silent-after-open provider is abandoned at the deadline, not
        # held for the full read timeout. ponytail: one budget for first-token + inter-chunk; split
        # if first-token ever needs a looser bound than steady-state.
        aiter = runner.stream(req, entry).__aiter__()
        stream_attempt = 0
        upstream_started = False

        async def retry_before_first_chunk(exc: BaseException) -> bool:
            nonlocal aiter, stream_attempt
            if (entry.lane != "provider" or upstream_started or stream_attempt >= self._retries
                    or not _is_retryable(exc)):
                return False
            await self._aclose(aiter)
            release_slot()
            await asyncio.sleep(_backoff(
                stream_attempt, self._backoff_base, cap=self._backoff_cap,
                retry_after=_retry_after(exc)))
            stream_attempt += 1
            await acquire_slot()
            aiter = runner.stream(req, entry).__aiter__()
            return True

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=self._stall_timeout)
                except StopAsyncIteration:
                    break
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    if await retry_before_first_chunk(exc):
                        continue
                    await self._aclose(aiter)  # close upstream so its socket/slot is released
                    trace.status, trace.error = "error", "stream_stall"
                    raise StreamStallError() from None
                except Exception as exc:
                    # A dynamic direct route may retry its SAME transport when opening the stream
                    # fails transiently. Once any upstream chunk exists, replay would duplicate
                    # client-visible bytes, so mid-stream failures still terminate honestly.
                    if await retry_before_first_chunk(exc):
                        continue
                    raise
                upstream_started = True
                # Capture usage out-of-band; forward to client only if requested.
                if chunk.usage is not None:
                    upstream_usage = chunk.usage
                    if not want_usage:
                        continue
                else:
                    for ch in chunk.choices:
                        if ch.delta.content:
                            acc_text.append(ch.delta.content)
                yield chunk
            upstream_s = time.perf_counter() - t_up0
            trace.status = "ok"
        except StreamStallError:
            # trace.status/error already set at the deadline; finalize the PARTIAL trace below.
            upstream_s = time.perf_counter() - t_up0
            raise
        except GeneratorExit:
            # Client disconnected mid-stream: still record what we served (partial, but real).
            upstream_s = time.perf_counter() - t_up0
            trace.status = "error"
            trace.error = "client_disconnected"
            raise
        except Exception as exc:
            upstream_s = time.perf_counter() - t_up0
            trace.status = "error"
            trace.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if not finalized:
                finalized = True
                if upstream_usage is not None and upstream_usage.total_tokens > 0:
                    self._account(trace, entry, upstream_usage, estimated=False)
                else:
                    est = Usage.of(
                        estimate_prompt_tokens(req.messages), estimate_tokens("".join(acc_text))
                    )
                    self._account(trace, entry, est, estimated=True)
                self._finalize(trace, t0, upstream_s=upstream_s)
                stream_text = "".join(acc_text)
                self._capture_content(request_id, req, stream_text, zr)
                self._trace_smart_ls(smart, req, trace, stream_text, zr)
                if on_trace is not None:
                    on_trace(trace)
            release_slot()

    # --- finalize ------------------------------------------------------------

    def _finalize(self, trace: TraceRecord, t0: float, *, upstream_s: float) -> None:
        total_s = time.perf_counter() - t0
        trace.latency_ms_total = _ms(total_s)
        trace.upstream_ms = _ms(upstream_s)  # W1-C2: the upstream wall — overhead decomposes off it
        trace.latency_ms_gateway_overhead = max(0, _ms(total_s - upstream_s))
        trace.finish()
        self.writer.write(trace)

    def _capture_content(self, request_id: str, req: ChatCompletionRequest, response_text: str,
                         zero_retention: bool = False) -> None:
        """Observability content-capture: store the resolved prompt (request messages) + the served
        response text, keyed by request_id, when TOTO_GW_LOG_CONTENT is on. Fail-open — content is
        for observability, never a reason to fail a served request (mirrors MultiTraceWriter).

        W1-C4: a zero-retention org's payload never lands here regardless of the env flag — the org
        opt-out always wins over TOTO_GW_LOG_CONTENT."""
        if zero_retention or not self._log_content:
            return
        engine = sql_engine(self.writer)
        if engine is None:  # no SQL trace sink → nowhere to keep content
            return
        try:
            import json

            prompt = json.dumps([m.model_dump(exclude_none=True) for m in req.messages])
            write_request_content(engine, request_id, prompt, response_text or "")
        except Exception as exc:  # never break the request on a content-capture failure
            import sys

            print(json.dumps({"event": "gateway.content_capture_error", "error": str(exc)}),
                  file=sys.stderr, flush=True)

    def _trace_smart_ls(self, smart, req: ChatCompletionRequest, trace, content: str,
                        zero_retention: bool = False) -> None:
        """Emit a LangSmith run for a smart-routed request (BYO tracing, env-gated). No-op unless
        this request was smart-routed AND LangSmith is on — so the smart passthrough is visible in
        the same project as the driver's StateGraph. Never raises (see routing.smart_trace).

        W1-C4: LangSmith is an external durable telemetry store carrying prompt+response — a
        zero-retention org's payload is never mirrored there, regardless of the tracing flag."""
        from .routing import smart_trace

        if zero_retention or smart is None or not smart_trace.tracing_enabled():
            return
        smart_trace.emit(
            messages=[{"role": m.role, "content": m.text()} for m in req.messages],
            classifier_model=self._classifier_model,
            label=smart.label, route_reason=smart.route_reason, resolved_model=smart.model_id,
            served_model=trace.model, content=content or "",
            tokens_prompt=trace.tokens_prompt, tokens_completion=trace.tokens_completion,
            cost_usd=trace.cost_usd, latency_ms=trace.latency_ms_total,
            classify_ms=smart.classify_ms,
            request_id=trace.request_id, conversation_key=trace.conversation_key,
        )
