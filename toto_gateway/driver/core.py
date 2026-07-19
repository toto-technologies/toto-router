"""Driver core — the Sonnet-class agent loop, as plain async nodes.

The driver decomposes a user request into Toto tasks and routes each task by its metadata:

    triage ─┬─ trivial   → answer_trivial ───────────────┐
            └─ multistep → decompose → dispatch → synthesize
                                                          └─→ answer

Each node is an ordinary async method taking the graph `state` and returning the keys it
updates — so every node is unit-testable with a fake `complete_fn`, no LangGraph required.
`graph.py` only *wires* these into a StateGraph (framework at the edge). `run()` drives them.

Module layout (split by concern; this module re-exports the full historical surface):
  - contracts.py — Exec + the fn seams (CompleteFn/StreamFn/Observer), RouteState,
    DriverResult, RouteDecision, and the pure helpers.
  - dispatch.py — per-task decide (pure) + execute; `/v1/routing/decide` shares decide_one.
  - streaming.py — delta batching + the mid-stream tool-call guard.
  - graph.py — the LangGraph wiring of the nodes below (the topology in one screen).

LangGraph contract (verified against 1.2.7): a node must RETURN its channel updates — in-place
mutation of the state dict does not reliably persist. So spans are accumulated via a reducer
(`Annotated[list, operator.add]`): helpers return the spans they emit, nodes aggregate and
return them under "spans". The durable sink (`observe`) fires immediately regardless.

Boundaries kept rigorously:
  - PRIVACY: only task metadata + execution provenance reach Toto (via TotoClient's allowlist).
    Prompts, answers, and content never leave through this layer.
  - GUARDS: every dispatched task passes the fail-closed RuleGuard first; MNPI egress → blocked,
    sensitive handling → forced local.
  - OBSERVABILITY: every node emits a span to the injected observer (local JSONL always;
    LangSmith is env-driven by LangGraph itself, zero code here).
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from typing import Any, Callable

from ..artifacts import make_artifact
from ..benchmarks import Benchmarks
from ..catalog import Catalog
from ..pipeline import BLOCK, DOWNGRADE_LOCAL, Signal
from ..resilience import backoff as _backoff_fn
from ..resilience import err_label as _err_label
from ..resilience import fallbacks as _fallbacks_fn
from ..resilience import is_retryable as _is_retryable
from ..resilience import retry_after_seconds
from ..schemas import ChatCompletionRequest, Message
from ..signals.guards import RuleGuard
from .. import persona
from . import prompts
from .adapters import AdapterRegistry
from .contracts import (  # re-exported: the historical driver.core surface
    _DELTA_CHARS,
    _DELTA_SECS,
    MAX_DECOMPOSE_TASKS,
    CompleteFn,
    DriverResult,
    Exec,
    Observer,
    RouteDecision,
    RouteState,
    StreamFn,
    _first_in_perimeter_model,
    _list_name,
    _privacy_pinned,
    _safe_corpus,
)
from .dispatch import classify_label, decide_one, dispatch_one, effective_catalog_for
from .streaming import _TOOL_OBJ_RE, stream_run  # re-exported
from .toto_client import TotoClient

__all__ = [
    "Driver", "DriverResult", "Exec", "RouteDecision", "RouteState",
    "CompleteFn", "StreamFn", "Observer", "MAX_DECOMPOSE_TASKS",
]


def _ls_enabled() -> bool:
    """True iff LangSmith tracing is on (LANGSMITH_TRACING=true + a key). Checked per call so
    the driver has zero LangSmith coupling unless the customer opts in — BYO tracing."""
    try:
        from langsmith.utils import tracing_is_enabled

        return bool(tracing_is_enabled())
    except Exception:
        return False


class Driver:
    def __init__(
        self,
        *,
        catalog: Catalog,
        complete_fn: CompleteFn,
        driver_model: str,
        triage_model: str,
        toto: TotoClient | None = None,
        guard: RuleGuard | None = None,
        observe: Observer | None = None,
        adapters: AdapterRegistry | None = None,
        benchmarks: Benchmarks | None = None,
        preferences: Callable[..., dict] | None = None,
        max_tokens: dict[str, int] | None = None,
        stream_fn: StreamFn | None = None,
        emit_delta: Callable[[str, str], None] | None = None,
        provider_retries: int = 2,
        provider_backoff_base: float = 0.5,
        provider_backoff_cap: float = 30.0,
        embedder=None,
        embed_routing: bool = False,
        corpus_sink=None,
        knn=None,
        labels=None,
        label_model: str = "",
        label_timeout_ms: int = 10000,
        delta_flush_chars: int = _DELTA_CHARS,
        delta_flush_ms: int = int(_DELTA_SECS * 1000),
    ) -> None:
        self.catalog = catalog
        self.benchmarks = benchmarks or Benchmarks()
        # User routing preferences ({"optimize": ..., "pins": {skill: model_id}}), read fresh
        # per dispatch so Settings changes apply to the very next run. Never raises.
        self._preferences = preferences or (lambda _uid=None: {})
        self._complete = complete_fn  # the driver's OWN reasoning calls (triage/decompose/synth)
        self.driver_model = driver_model
        self.triage_model = triage_model
        # Per-role output caps (role -> max_tokens). Empty = uncapped (backward-compatible for
        # tests that build a Driver directly); the app wires the Settings defaults in prod.
        self._max_tokens = max_tokens or {}
        # Streaming: when a stream_fn is wired, user-facing answers (answer_trivial/synthesize)
        # stream batched deltas to the run event plane via emit_delta. Absent → plain completion.
        self._stream = stream_fn
        self._emit_delta = emit_delta or (lambda node, text: None)
        self._delta_chars = delta_flush_chars
        self._delta_secs = delta_flush_ms / 1000.0
        self._retries = provider_retries
        self._backoff_base = provider_backoff_base
        self._backoff_cap = provider_backoff_cap
        # Embedding routing (skill via nearest-centroid, keyword fallback) + experience corpus.
        # Both optional/None → today's keyword-only path. embed_routing gates skill inference;
        # corpus_sink (best-effort, fire-and-forget) logs the dispatched task for future kNN.
        self._embedder = embedder
        self._embed_routing = embed_routing
        self._corpus_sink = corpus_sink
        # Experience-kNN proposer (dark; None = flag off → dispatch seam skipped, byte-identical).
        self._knn = knn
        # Label routing (LabelBindings; None = off → dispatch seam skipped, byte-identical).
        # The classifier call runs on label_model, a catalog entry id validated at app build.
        self._labels = labels
        self._label_model = label_model
        self._label_timeout_ms = label_timeout_ms
        self.toto = toto
        self.guard = guard or RuleGuard()
        self._observe = observe or (lambda span: None)
        # Task execution goes through the adapter seam (gateway by default; claude_code/pi stubs
        # selectable per task via metadata.requires.runner). The classifier picks the model; the
        # adapter picks the harness.
        self._adapters = adapters or AdapterRegistry.default_gateway(complete_fn)
        self._graph = None  # lazily compiled LangGraph (graph.py), cached

    # --- LLM calls: resilience, streaming, tracing ---------------------------

    async def _llm(self, model_id: str, messages: list[dict], *, name: str = "llm",
                   max_tokens: int | None = None, temperature: float | None = None) -> Exec:
        req = ChatCompletionRequest(model=model_id, messages=[Message(**m) for m in messages],
                                    max_tokens=max_tokens, temperature=temperature)
        ex, _model, _note = await self._call(req, self._complete, name=name)
        return ex

    async def _answer(self, model_id: str, messages: list[dict], *, name: str, node: str,
                      max_tokens: int | None = None) -> Exec:
        """A user-facing answer: streams batched deltas to the run event plane when a stream_fn
        is wired, else a plain completion. Returns the full Exec either way (graph contract
        unchanged). Retryable stream errors restart the answer cleanly via _call."""
        if self._stream is None:
            return await self._llm(model_id, messages, name=name, max_tokens=max_tokens)
        req = ChatCompletionRequest(model=model_id, messages=[Message(**m) for m in messages],
                                    max_tokens=max_tokens)
        ex, _model, _note = await self._call(req, lambda r: stream_run(self, r, node), name=name)
        return ex

    async def _answer_gated(self, model_id: str, messages: list[dict], *, name: str, node: str,
                            gate, max_tokens: int | None = None) -> tuple[Exec, bool]:
        """Like _answer, but for a caller that can't yet tell a tool call from an answer (the
        companion agent): `gate` decides per-prelude whether to stream. Returns (Exec, streamed) —
        streamed True iff any answer_delta was published. Falls back to a plain _llm (streamed
        False) when no stream_fn is wired, so eval/offline paths behave exactly as before."""
        if self._stream is None:
            return await self._llm(model_id, messages, name=name, max_tokens=max_tokens), False
        req = ChatCompletionRequest(model=model_id, messages=[Message(**m) for m in messages],
                                    max_tokens=max_tokens)
        streamed = [False]

        def g(prelude: str):
            v = gate(prelude)
            if v is True:
                streamed[0] = True
            return v

        ex, _model, _note = await self._call(
            req, lambda r: stream_run(self, r, node, gate=g), name=name)
        return ex, streamed[0]

    async def _call(self, req: ChatCompletionRequest, run, *, name: str,
                    route_meta: dict | None = None, privacy: bool = False):
        """Run an LLM call with resilience: bounded same-model retries (backoff + jitter), then
        fall back across catalog entries within the same residency boundary. Returns
        (Exec, final_model_id, note) where note is None or a 'fallback: A 429 → B' string.
        Non-retryable errors raise immediately; when every candidate exhausts, the ORIGINAL
        error is raised (not an obscure last-fallback error). Emits a model_fallback span on
        each entry switch so the theater + governance show what actually ran."""
        candidates = [req.model, *self._fallbacks(req.model, privacy=privacy)]
        first_exc: BaseException | None = None
        for i, model_id in enumerate(candidates):
            r = req if model_id == req.model else req.model_copy(update={"model": model_id})
            for attempt in range(self._retries + 1):
                try:
                    ex = await self._run_traced(name=name, req=r, run=run, route_meta=route_meta)
                    note = None if i == 0 else \
                        f"fallback: {candidates[0]} {_err_label(first_exc)} → {model_id}"
                    return ex, model_id, note
                except Exception as exc:
                    if not _is_retryable(exc):
                        raise
                    first_exc = first_exc or exc
                    if attempt < self._retries:
                        await asyncio.sleep(self._backoff(attempt, exc))
            nxt = candidates[i + 1] if i + 1 < len(candidates) else None
            if nxt is not None:
                await self._emit("model_fallback", **{"from": model_id, "to": nxt,
                           "reason": _err_label(first_exc), "attempt": i + 1})
        raise first_exc  # every candidate exhausted — surface the original failure

    def _backoff(self, attempt: int, exc: BaseException | None = None) -> float:
        """Sleep before the next same-model retry — honors the upstream Retry-After when the
        exception carries one, else exp backoff + jitter (shared resilience.backoff)."""
        return _backoff_fn(
            attempt, self._backoff_base, cap=self._backoff_cap,
            retry_after=retry_after_seconds(exc) if exc is not None else None)

    def _fallbacks(self, model_id: str, *, privacy: bool) -> list[str]:
        # Delegates to the shared, provider-agnostic policy (resilience.fallbacks). Kept as a thin
        # method so existing call sites + tests (d._fallbacks(...)) are unchanged.
        return _fallbacks_fn(self.catalog, model_id, privacy=privacy)

    async def _run_traced(self, *, name: str, req: ChatCompletionRequest, run,
                          route_meta: dict | None = None) -> Exec:
        """Execute run(req)->Exec, wrapped as a LangSmith child run (run_type=llm) when tracing
        is on — it nests under the current LangGraph node automatically. No-op when off.
        route_meta carries the routing DECISION (route_reason/skill/etc.) so it's visible on the
        span's Attributes tab, not just in the JSONL observer + Toto execution record."""
        if not _ls_enabled():
            ex = await run(req)
        else:
            from langsmith import trace as ls_trace

            inputs = {"model": req.model,
                      "messages": [{"role": m.role, "content": m.text()} for m in req.messages]}
            with ls_trace(name=name, run_type="llm", inputs=inputs) as rt:
                ex = await run(req)
                # Prefer the served upstream model for the first-class chip; provider from the
                # upstream when we have it, else derive it from the model string / adapter.
                ls_model_name = ex.upstream_model or ex.model
                ls_provider = ex.provider or (ex.model.split("/")[0]
                                              if "/" in (ex.model or "") else ex.adapter)
                # openrouter_model/provider/generation_id: only surface what the upstream returned.
                upstream_meta = {k: v for k, v in (
                    ("openrouter_model", ex.upstream_model),
                    ("provider", ex.provider),
                    ("generation_id", ex.generation_id)) if v}
                rt.end(
                    # usage_metadata in OUTPUTS is what LangSmith aggregates into the trace-level
                    # token/cost columns — metadata alone never rolls up.
                    outputs={"content": ex.text,
                             "usage_metadata": {
                                 "input_tokens": ex.tokens_prompt or 0,
                                 "output_tokens": ex.tokens_completion or 0,
                                 "total_tokens": (ex.tokens_prompt or 0)
                                                 + (ex.tokens_completion or 0)}},
                    # ls_model_name/ls_provider are LangSmith's conventional keys — set them so the
                    # RESOLVED model shows in the first-class Model chip/column + per-model cost in
                    # Monitoring, not just buried in the Metadata tab.
                    metadata={"model": ex.model, "lane": ex.lane, "adapter": ex.adapter,
                              "ls_model_name": ls_model_name,
                              "ls_provider": ls_provider,
                              **upstream_meta,
                              "tokens_prompt": ex.tokens_prompt, "tokens_completion": ex.tokens_completion,
                              "tokens_cached": ex.tokens_cached,
                              "cost_usd": ex.cost_usd, "latency_ms": ex.latency_ms,
                              **(route_meta or {})},
                )
        # Per-LLM-call span for the live eval stream. No-op when _observe is the default
        # (lambda span: None) — cheap + guarded, so this shared prod path can't break on it.
        await self._emit("llm_call", model=ex.model, upstream_model=ex.upstream_model,
                         provider=ex.provider, generation_id=ex.generation_id,
                         tokens_prompt=ex.tokens_prompt,
                         tokens_completion=ex.tokens_completion, tokens_cached=ex.tokens_cached,
                         cost_usd=ex.cost_usd, latency_ms=ex.latency_ms, name=name)
        return ex

    # --- spans + Toto plumbing -----------------------------------------------

    async def _emit(self, node: str, **data: Any) -> dict:
        span = {"node": node, "ts": time.time(), **data}
        try:
            r = self._observe(span)  # durable sink fires now, independent of graph state
            if inspect.isawaitable(r):  # async store observer (prod) vs sync list.append (tests)
                await r
        except Exception:
            pass  # observability must never break the run
        return span

    async def _toto(self, coro_factory, op: str) -> tuple[Any, list[dict]]:
        """Run a Toto call; return (result, spans). Failures degrade to a span, never raise —
        persistence is best-effort, the driver still routes + answers if Toto is down."""
        if not self.toto:
            return None, []
        try:
            return await coro_factory(), []
        except Exception as exc:  # network / auth / shape
            return None, [await self._emit("toto_error", op=op, error=f"{type(exc).__name__}: {exc}")]

    def _reasoning_model(self, state: RouteState, configured: str) -> str:
        """The model a driver reasoning node (triage/decompose/answer/synthesize) uses. When the
        run is pinned local — the RAW-query guard returned DOWNGRADE_LOCAL — force a local-lane
        model so the query/results never reach frontier; else the configured model unchanged.
        run() has already blocked the no-local-lane case, so the fallback here is defensive."""
        if not state.get("local_pinned"):
            return configured
        entry = self.catalog.get(configured)
        if entry is not None and entry.residency_class == "in_perimeter":
            return configured  # already in-perimeter — no egress, keep it
        return _first_in_perimeter_model(self.catalog) or configured

    # --- per-task routing (dispatch.py owns the bodies) ----------------------

    def _effective_catalog(self) -> Catalog:
        return effective_catalog_for(self)

    async def _classify_label(self, text: str,
                              custom: list[dict] | None = None) -> tuple[str | None, dict | None]:
        """(label, metadata) or (None, None) on ANY failure — see dispatch.classify_label."""
        return await classify_label(self, text, custom)

    async def _decide_one(self, t: dict, *, optimize: str | None = None,
                          pins: dict[str, str] | None = None, run_pinned: bool = False,
                          label_pins: dict[str, str] | None = None,
                          team_bindings: dict[str, str] | None = None,
                          team_custom: list[dict] | None = None) -> RouteDecision:
        """Pure routing decision for one task — `/v1/routing/decide` calls this directly."""
        return await decide_one(self, t, optimize=optimize, pins=pins, run_pinned=run_pinned,
                                label_pins=label_pins, team_bindings=team_bindings,
                                team_custom=team_custom)

    async def _dispatch_one(self, t: dict, optimize: str | None = None,
                            pins: dict[str, str] | None = None, idx: int = 0,
                            run_pinned: bool = False,
                            label_pins: dict[str, str] | None = None,
                            team_bindings: dict[str, str] | None = None,
                            team_custom: list[dict] | None = None) -> list[dict]:
        """Decide (pure) then execute one task — see dispatch.dispatch_one."""
        return await dispatch_one(self, t, optimize, pins, idx, run_pinned,
                                  label_pins, team_bindings, team_custom)

    # --- nodes ---------------------------------------------------------------

    async def triage(self, state: RouteState) -> dict:
        if state.get("kind"):  # pre-triaged (companion spawn: the spawn decision IS triage) — no LLM
            span = await self._emit("triage", kind=state["kind"], model="",
                              reason="pre-triaged: spawned by companion")
            return {"kind": state["kind"], "spans": [span]}
        ex = await self._llm(
            self._reasoning_model(state, self.triage_model),
            prompts.build_triage_messages(state["query"], history=state.get("history")),
            name="triage.llm", max_tokens=self._max_tokens.get("triage"))
        parsed = prompts.parse_triage(ex.text)
        # Fail-safe parsing: anything but an explicit "trivial" routes multistep.
        kind = "trivial" if parsed.get("kind") == "trivial" else "multistep"
        span = await self._emit("triage", kind=kind, model=ex.model, reason=parsed.get("reason"),
                          cost=ex.cost_usd, tokens_prompt=ex.tokens_prompt,
                          tokens_cached=ex.tokens_cached)
        return {"kind": kind, "spans": [span]}

    async def answer_trivial(self, state: RouteState) -> dict:
        q = state["query"]
        spans: list[dict] = []
        item = {"task": _list_name(q), "description": "Direct trivial answer by driver model",
                "metadata": {"scope": "qa"}}
        list_id, ids, s = await self._create_tasks(q, [item], kind="trivial")
        spans += s
        item_id = ids[0] if ids else None
        if item_id:
            _, s = await self._toto(lambda: self.toto.set_status(item_id, "in_progress"), "status")
            spans += s

        ex = await self._answer(
            self._reasoning_model(state, self.driver_model),
            persona.build_direct_messages(q, history=state.get("history")),
            name="answer.llm", node="answer_trivial",
            max_tokens=self._max_tokens.get("answer"))
        spans.append(await self._emit("answer_trivial", model=ex.model, cost=ex.cost_usd,
                                tokens_prompt=ex.tokens_prompt, tokens_cached=ex.tokens_cached))

        item.update(
            item_id=item_id, lane=ex.lane, model_id=ex.model, tools_required=[],
            result=None,  # never store the answer on the task — content stays out of Toto
            execution={
                "runner": "gateway", "executor": ex.model, "lane": ex.lane, "model": ex.model,
                "tokens_prompt": ex.tokens_prompt, "tokens_completion": ex.tokens_completion,
                "tokens_cached": ex.tokens_cached,
                "cost_usd": ex.cost_usd, "outcome": "completed", "latency_ms": ex.latency_ms,
                "route_reason": "trivial: direct answer by driver model",
                "artifact": make_artifact("task_result", ex.text, produced_by=ex.model,
                                          evidence=[ex.lane], confidence=None),
            },
        )
        if item_id:
            _, s = await self._toto(lambda: self.toto.set_status(item_id, "done"), "status")
            spans += s
            _, s = await self._toto(lambda: self.toto.write_execution(item_id, item["execution"]), "exec")
            spans += s
        return {"answer": ex.text, "tasks": [item], "list_id": list_id, "spans": spans}

    async def decompose(self, state: RouteState) -> dict:
        q = state["query"]
        pre = state.get("tasks")
        if pre:  # orchestrator-authored (spawn_session, already parse_tasks-validated): pass
            # through — no decompose LLM. Same Toto persistence + clamp as a decomposed run;
            # dispatch consumes the tasks unchanged, so the precedence ladder still rules.
            spans = []
            if len(pre) > MAX_DECOMPOSE_TASKS:  # clamp parity with the LLM path — same span
                spans.append(await self._emit("decompose_clamp", kept=MAX_DECOMPOSE_TASKS,
                                              dropped=len(pre) - MAX_DECOMPOSE_TASKS))
            tasks = [dict(t) for t in pre[:MAX_DECOMPOSE_TASKS]]
            for t in tasks:
                t["authored"] = True  # provenance: dispatch stamps route_reason off this
            list_id, ids, s = await self._create_tasks(q, tasks, kind="multistep")
            spans += s
            for t, iid in zip(tasks, ids or [None] * len(tasks)):
                t["item_id"] = iid
            spans.append(await self._emit("decompose", n_tasks=len(tasks), list_id=list_id,
                                          model="", authored=True))
            return {"tasks": tasks, "list_id": list_id, "spans": spans}
        history = state.get("history")
        spans: list[dict] = []
        dmodel = self._reasoning_model(state, self.driver_model)
        ex = await self._llm(dmodel, prompts.build_decompose_messages(q, history=history),
                             name="decompose.llm", max_tokens=self._max_tokens.get("decompose"))
        # Fail-safe parsing: parse_tasks drops malformed tasks; an empty parse gets one strict
        # retry, then degrades to a single escalated task — never a crashed run.
        tasks = prompts.parse_tasks(ex.text)
        if not tasks:  # parser failed → one strict-JSON retry before degrading
            retry = await self._llm(
                dmodel, prompts.build_decompose_retry_messages(q, ex.text, history=history),
                name="decompose.retry.llm", max_tokens=self._max_tokens.get("decompose"),
            )
            tasks = prompts.parse_tasks(retry.text)
            spans.append(await self._emit("decompose_retry",
                                    outcome="recovered" if tasks else "unparseable"))
        if not tasks:  # still unparseable → single task, but ESCALATE: triage already judged
            # this multistep, so routing the degraded run local would compound the failure.
            tasks = [{"task": _list_name(q), "description": "Escalated single task (decomposition unparseable)",
                      "metadata": {"complexity": "high"}}]
            spans.append(await self._emit("decompose_fallback", reason="unparseable decomposition"))
        if len(tasks) > MAX_DECOMPOSE_TASKS:  # belt-and-suspenders: prompt asks ≤4, clamp anyway
            spans.append(await self._emit("decompose_clamp", kept=MAX_DECOMPOSE_TASKS,
                                    dropped=len(tasks) - MAX_DECOMPOSE_TASKS))
            tasks = tasks[:MAX_DECOMPOSE_TASKS]
        list_id, ids, s = await self._create_tasks(q, tasks, kind="multistep")
        spans += s
        for t, iid in zip(tasks, ids or [None] * len(tasks)):
            t["item_id"] = iid
        spans.append(await self._emit("decompose", n_tasks=len(tasks), list_id=list_id, model=ex.model,
                                cost=ex.cost_usd, tokens_prompt=ex.tokens_prompt,
                                tokens_cached=ex.tokens_cached))
        return {"tasks": tasks, "list_id": list_id, "spans": spans}

    async def dispatch(self, state: RouteState) -> dict:
        # Independent tasks run CONCURRENTLY — decomposition exists precisely to parallelize.
        # Each dispatch_one mutates only its own task dict and returns its own spans, so there
        # are no shared writes; wall-clock ≈ the slowest single task, not the sum.
        # return_exceptions: one task crashing must never take down its siblings — the failed
        # task degrades to outcome=failed and synthesis proceeds with the survivors.
        tasks = state["tasks"]
        try:
            # Per-user Settings: scope the read to the run owner so one user's prefs never drive
            # another's routing (get_preferences(user_id=...) in prod; the test lambda ignores it).
            prefs = self._preferences(state.get("user_id"))  # async store getter or sync lambda
            if inspect.isawaitable(prefs):
                prefs = await prefs
            prefs = prefs or {}
        except Exception:
            prefs = {}
        # Team routing overlay: the caller's team tag->model bindings + optimize preset, resolved
        # server-side and carried on the request identity (effective_policy). Empty/None when the
        # team has no routing policy, the caller is the operator, or this is an internal
        # (no-identity) run → byte-identical global behavior. Read once here, threaded per task below.
        team_bindings: dict[str, str] = {}
        team_optimize: str | None = None
        # Custom task types: the team's invented labels [{name, desc, model}]. Their descs
        # expand the classifier vocab for THIS request (so the classifier can emit them); a match
        # routes to `model` at team-binding tier — folded into team_bindings below so the existing
        # binding-resolution ladder handles it (custom names can't collide with a builtin label —
        # PUT-validated). Empty when the team has none -> byte-identical global routing.
        team_custom: list[dict] = []
        try:
            from ..routes.deps import current_identity
            from ..routing.decision import effective_policy

            pol = effective_policy(current_identity())
            if pol is not None:
                team_bindings = dict(pol.label_bindings or {})
                team_optimize = pol.optimize
                team_custom = pol.custom_labels or []
                for c in team_custom:  # custom label -> its bound model, team-binding tier
                    if c.get("name") and c.get("model"):
                        team_bindings.setdefault(c["name"], c["model"])
        except Exception:  # overlay resolution must never break dispatch — fall to global routing
            pass
        # optimize precedence: explicit run knob > user Settings > team preset > global default.
        optimize = state.get("optimize") or prefs.get("optimize") or team_optimize
        pins = prefs.get("pins") or {}
        label_pins = prefs.get("label_models") or {}  # user's label->model overrides (Settings)
        # Run-level pin (raw-query guard DOWNGRADE_LOCAL) propagates to every sub-task: a
        # decomposed task that neither re-trips the guard nor carries data_policy=local must
        # still stay in-perimeter, or its prompt_text leaks via embed/kNN/corpus/frontier.
        run_pinned = bool(state.get("local_pinned"))
        span_groups = await asyncio.gather(
            *(dispatch_one(self, t, optimize, pins, idx, run_pinned, label_pins, team_bindings,
                           team_custom)
              for idx, t in enumerate(tasks)),
            return_exceptions=True,
        )
        spans: list[dict] = []
        for t, group in zip(tasks, span_groups):
            if isinstance(group, BaseException):
                err = f"{type(group).__name__}: {group}"
                t.update(result=None, execution={"outcome": "failed", "route_reason": f"dispatch crashed: {err}"})
                spans.append(await self._emit("dispatch_error", task=t.get("task"), error=err))
            else:
                spans.extend(group)
        # If ANY sub-task was pinned local (data_policy / per-task guard), raise the run-level
        # residency so synthesize reasons over the aggregated results on the local lane too —
        # never egressing local-only output to frontier. No flagged task → key absent →
        # frontier as before (byte-identical). A flagged task passed the per-task no-local-lane
        # gate, so the raised flag never makes synthesize's _reasoning_model fall open to frontier.
        out: dict = {"tasks": tasks, "spans": spans}
        if any(t.get("local_pinned") for t in tasks):
            out["local_pinned"] = True
        return out

    async def synthesize(self, state: RouteState) -> dict:
        q = state["query"]
        # ponytail: 0/1 results → return it, skip the weave call
        non_empty = [t for t in state["tasks"] if (t.get("result") or "").strip()]
        if len(non_empty) <= 1:
            answer = (non_empty[0].get("result") if non_empty else "") or ""
            span = await self._emit("synthesize", model="", cost=0.0, skipped=True)
            return {"answer": answer, "spans": [span]}
        pairs = [
            {"task": t.get("task", ""), "result": t.get("result") or "(no result / blocked)"}
            for t in state["tasks"]
        ]
        ex = await self._answer(
            self._reasoning_model(state, self.driver_model),
            persona.build_synthesize_messages(q, pairs, history=state.get("history")),
            name="synthesize.llm", node="synthesize",
            max_tokens=self._max_tokens.get("synthesize"))
        span = await self._emit("synthesize", model=ex.model, cost=ex.cost_usd,
                          tokens_prompt=ex.tokens_prompt, tokens_cached=ex.tokens_cached)
        return {"answer": ex.text, "spans": [span]}

    async def revise_card(self, prev_summary: str, new_result: str) -> str:
        """One LLM call per continued turn: merge the prior session card summary with the new
        turn's result into a single revised summary (revise the draft, don't append). Not
        streamed — it's a background artifact, not a user-facing answer."""
        ex = await self._llm(self.driver_model,
                             persona.build_revise_messages(prev_summary, new_result),
                             name="revise.llm", max_tokens=self._max_tokens.get("synthesize"))
        return ex.text

    # --- Toto persistence (metadata only) ------------------------------------

    async def _create_tasks(
        self, query: str, tasks: list[dict], *, kind: str
    ) -> tuple[str | None, list[str] | None, list[dict]]:
        if not self.toto:
            return None, None, []
        spans: list[dict] = []
        list_meta = {
            "purpose": f"Driver session ({kind}): routed decomposition of a user request.",
            "scope": "backend", "component": "toto-gateway/driver",
            "intent": "Each task is routed to an executor by its metadata; provenance written back.",
        }
        list_id, s = await self._toto(lambda: self.toto.create_list(_list_name(query), list_meta), "create_list")
        spans += s
        if not list_id:
            return None, None, spans
        # Metadata only — description is about the work, never the model's answer.
        items = [
            {"task": t.get("task", ""), "description": t.get("description", ""),
             "metadata": t.get("metadata") or {}}
            for t in tasks
        ]
        ids, s = await self._toto(lambda: self.toto.batch_items(list_id, items), "batch_items")
        spans += s
        return list_id, ids, spans

    # --- run -----------------------------------------------------------------

    async def _blocked_result(self, query: str, reasons: list[str]) -> DriverResult:
        """Whole-run refusal: the RAW-query guard blocked before any node could ship it to a
        frontier reasoning/embedding call. Never invokes the graph."""
        span = await self._emit("guard_block", reasons=reasons)
        item = {"task": _list_name(query), "description": "Blocked by egress guard", "metadata": {},
                "blocked": True, "lane": None, "model_id": None, "result": None,
                "execution": {"outcome": "blocked_constraints", "route_reason": "; ".join(reasons)}}
        return DriverResult(query=query, kind="blocked",
                            answer="This request was blocked by the safety guard and not routed.",
                            tasks=[item], list_id=None, spans=[span])

    async def run(self, query: str, optimize: str | None = None,
                  history: list[dict] | None = None, kind: str | None = None,
                  user_id: str | None = None, tasks: list[dict] | None = None) -> DriverResult:
        from .graph import build_graph  # lazy: avoids core<->graph import cycle

        # EGRESS GATE (fail-closed) on the RAW query, BEFORE build_graph/triage — the driver's own
        # reasoning nodes (triage/decompose/answer_trivial/synthesize) call frontier ungated, and
        # the trivial path never reaches the per-task guard at all. BLOCK → refuse the run; a
        # guard downgrade pins every reasoning node local (or blocks if no in-perimeter model exists).
        verdict = self.guard.check(
            ChatCompletionRequest(model=self.driver_model,
                                  messages=[Message(role="user", content=query)]), Signal())
        if verdict.action == BLOCK:
            return await self._blocked_result(query, verdict.reasons)
        local_pinned = verdict.action == DOWNGRADE_LOCAL
        # Authored tasks descend from NOTHING gated (a decomposed task descends from the gated
        # query): their text reaches Toto/classify/executors directly, so gate each one exactly
        # like the raw query BEFORE the graph — BLOCK refuses the whole run, DOWNGRADE_LOCAL pins
        # the run local. Fail-closed, before _create_tasks can egress a byte.
        for t in tasks or []:
            v = self.guard.check(
                ChatCompletionRequest(model=self.driver_model, messages=[Message(
                    role="user",
                    content=f"{t.get('task', '')}\n\n{t.get('description', '')}")]), Signal())
            if v.action == BLOCK:
                return await self._blocked_result(query, v.reasons)
            local_pinned = local_pinned or v.action == DOWNGRADE_LOCAL
        if local_pinned and _first_in_perimeter_model(self.catalog) is None:
            return await self._blocked_result(
                query, ["privacy: in-perimeter handling required, no in-perimeter model available"])

        if self._graph is None:
            self._graph = build_graph(self)
        state: RouteState = {"query": query, "spans": []}
        if local_pinned:
            state["local_pinned"] = True
        if user_id is not None:
            state["user_id"] = user_id
        if optimize:
            state["optimize"] = optimize
        if history:
            state["history"] = history
        if tasks:  # orchestrator-authored (pre-validated): decompose passes them through, and
            # the kind is definitionally multistep — the authored list IS the decomposition.
            state["tasks"] = tasks
            kind = "multistep"
        if kind:  # pre-triaged (companion spawn skips triage — see the triage node)
            state["kind"] = kind
        thread_id = uuid.uuid4().hex
        config = {"configurable": {"thread_id": thread_id}}  # required by the checkpointer
        try:
            final: RouteState = await self._graph.ainvoke(state, config)
        finally:
            # server runs never replay this thread; drop it so a long-lived process doesn't
            # accumulate checkpoints unbounded (Studio/tests use their own graph + persistence).
            try:
                self._graph.checkpointer.delete_thread(thread_id)
            except Exception:
                pass
        return DriverResult(
            query=query,
            kind=final.get("kind", "unknown"),
            answer=final.get("answer", ""),
            tasks=final.get("tasks", []),
            list_id=final.get("list_id"),
            spans=final.get("spans", []),
        )
