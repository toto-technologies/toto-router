"""Driver core — the Sonnet-class agent loop, as plain async nodes.

The driver decomposes a user request into Toto tasks and routes each task by its metadata:

    triage ─┬─ trivial   → answer_trivial ───────────────┐
            └─ multistep → decompose → dispatch → synthesize
                                                          └─→ answer

Each node is an ordinary async method taking the graph `state` and returning the keys it
updates — so every node is unit-testable with a fake `complete_fn`, no LangGraph required.
`graph.py` only *wires* these into a StateGraph (framework at the edge). `run()` drives them.

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
import operator
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from ..artifacts import make_artifact
from ..benchmarks import Benchmarks
from ..catalog import Catalog, effective_catalog
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
from .classify import TaskDecision, classify
from .toto_client import TotoClient

# Cap decomposition fan-out: more tasks = more parallel frontier calls + a bigger synthesis
# prompt, and over-decomposition was the main latency multiplier. The prompt also asks for ≤4.
MAX_DECOMPOSE_TASKS = 4

# A JSON tool-call object opening. In a gated companion stream, a committed plain answer that
# grows a trailing `{"tool" …}` is the model narrating then appending the call it should have
# emitted alone — freeze at the brace so the raw JSON never streams (or gets spoken). Whitespace
# after `{` tolerated; a real answer never contains this literal.
_TOOL_OBJ_RE = re.compile(r'\{\s*"tool"')


@dataclass
class Exec:
    """Normalized result of one executor completion — text plus the provenance we account."""

    text: str
    model: str = ""
    lane: str = ""
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_cached: int = 0  # prompt tokens the provider served from cache
    cost_usd: float | None = None
    latency_ms: int = 0
    adapter: str = ""  # which HarnessAdapter ran it (provenance)
    # What the UPSTREAM actually served (vs `model`, the internal catalog alias). Empty on
    # fakes/providers that don't return them → the trace just omits them.
    upstream_model: str = ""  # served model string, e.g. "anthropic/claude-sonnet-5"
    provider: str = ""        # provider that answered (OpenRouter body field)
    generation_id: str = ""   # upstream generation id


# Given a request, produce an Exec. Wraps gateway.complete() so the driver stays decoupled
# from Gateway internals and is trivially fakeable in tests.
CompleteFn = Callable[[ChatCompletionRequest], Awaitable[Exec]]

# Like CompleteFn but streams: awaits on_delta(chunk_text) as text arrives, returns the full Exec.
# on_delta is a coroutine (it publishes each batch to the async run store), so callers must await it.
StreamFn = Callable[[ChatCompletionRequest, Callable[[str], Awaitable[None]]], Awaitable[Exec]]

# Observer sink for spans (local JSONL writer in prod; a list.append in tests). Never raises.
Observer = Callable[[dict], None]

# Batch streamed deltas before publishing (each publish = a SQLite row + fan-out): flush when
# the buffer reaches this many chars OR this many seconds elapse, whichever first.
_DELTA_CHARS, _DELTA_SECS = 120, 0.2


def _privacy_pinned(reason: str) -> bool:
    """True when routing forced a residency/guard boundary a fallback must not cross."""
    return reason.startswith("privacy") or "downgrade_local" in reason


async def _safe_corpus(sink, *args) -> None:
    """Run the fire-and-forget corpus write; swallow everything (groundwork must never break a run)."""
    try:
        await sink(*args)
    except Exception:
        pass


class RouteState(TypedDict, total=False):
    query: str
    user_id: str                           # run owner — scopes the per-user Settings read in dispatch
    history: list                          # prior [{query, answer}] turns (multi-turn context)
    optimize: str                          # user knob: "quality" | "balanced" | "cost"
    kind: str                              # "trivial" | "multistep"
    answer: str
    tasks: list[dict]                      # grows metadata + lane/model_id/result/execution/item_id
    list_id: str | None
    local_pinned: bool                     # guard pinned the whole run local on the RAW query
    spans: Annotated[list, operator.add]   # reducer: nodes contribute; the channel accumulates


@dataclass
class DriverResult:
    query: str
    kind: str
    answer: str
    tasks: list[dict] = field(default_factory=list)
    list_id: str | None = None
    spans: list[dict] = field(default_factory=list)

    def provenance(self) -> dict:
        """Roll-up for the API response: per-task routing + total economics."""
        routed = [t for t in self.tasks if t.get("execution")]
        cost = sum((t["execution"].get("cost_usd") or 0.0) for t in routed)
        return {
            "kind": self.kind,
            "list_id": self.list_id,
            "n_tasks": len(self.tasks),
            "cost_usd": round(cost, 6),
            "tasks": [
                {
                    "task": t.get("task"),
                    "lane": t.get("lane"),
                    "model": t.get("model_id"),
                    "tools_required": t.get("tools_required") or [],
                    "route_reason": (t.get("execution") or {}).get("route_reason"),
                    "outcome": (t.get("execution") or {}).get("outcome"),
                    "item_id": t.get("item_id"),
                }
                for t in self.tasks
            ],
        }


def _first_in_perimeter_model(catalog: Catalog) -> str | None:
    """First in-perimeter model (residency, not tier) — the privacy-guard downgrade target.
    Real box preferred; a fake in-perimeter entry is an acceptable offline fallback."""
    for e in catalog.models:
        if e.residency_class == "in_perimeter" and e.endpoint != "fake":
            return e.id
    for e in catalog.models:
        if e.residency_class == "in_perimeter":
            return e.id
    return None


@dataclass
class RouteDecision:
    """Pure routing outcome for one task — what `_dispatch_one` decides BEFORE it executes.
    `_decide_one` produces it; `_dispatch_one` executes it; `/v1/routing/decide` serializes it.
    Same function on both paths, so a decision preview can never diverge from what dispatch does."""
    dec: TaskDecision | None            # None only when blocked
    rejected: list[dict]                # in-lane/overridden alternatives, each {"model_id","reason"}
    label: str | None                   # NVIDIA-style label (None = off / no-label / fallback)
    label_metadata: dict | None = None  # totoshape classify metadata → merged onto the Toto task
    local_pinned: bool = False          # residency pin propagated to synthesize + corpus skip
    blocked: bool = False               # guard BLOCK or privacy-with-no-in-perimeter-model
    block_reasons: list[str] = field(default_factory=list)
    spans: list[dict] = field(default_factory=list)  # observability (e.g. the label span)


def _list_name(query: str) -> str:
    q = " ".join(query.split())
    return (q[:57] + "…") if len(q) > 58 else (q or "toto session")


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
        # tests that build a Driver directly); app.py wires the Settings defaults in prod.
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
        # The classifier call runs on label_model, a catalog entry id validated in app.py.
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

    # --- helpers -------------------------------------------------------------

    async def _llm(self, model_id: str, messages: list[dict], *, name: str = "llm",
                   max_tokens: int | None = None, temperature: float | None = None) -> Exec:
        req = ChatCompletionRequest(model=model_id, messages=[Message(**m) for m in messages],
                                    max_tokens=max_tokens, temperature=temperature)
        ex, _model, _note = await self._call(req, self._complete, name=name)
        return ex

    async def _classify_label(self, text: str,
                              custom: list[dict] | None = None) -> tuple[str | None, dict | None]:
        """One haiku-class call → (verbatim vocab label, totoshape metadata), or (None, None) on ANY
        failure. None label means the fallback ladder decides — the classifier being down is never
        routing being down. Metadata is the totoshape classify block (None unless that variant is
        live); it enriches the Toto task, never the routing decision.
        Hard wall-clock cap: a HUNG provider must degrade to the fallback too, not stall the
        sub-task for the SDK's default timeout while holding an _llm slot.

        `custom` (CT) is the caller's team's invented task types [{name, desc, model}]. Their
        {name: desc} entries are appended to the classifier vocabulary FOR THIS REQUEST — both the
        LABEL_PROMPT enumeration and parse_label's accepted set — so the classifier can emit a
        custom label. No custom labels -> byte-identical to the global-vocab call."""
        labels = self._labels.labels
        if custom:  # append team-invented {name: desc} onto the global vocab for this request only
            labels = {**labels,
                      **{c["name"]: {"desc": c.get("desc", "")} for c in custom if c.get("name")}}
        try:
            ex = await asyncio.wait_for(
                self._llm(self._label_model,
                          prompts.build_label_messages(text, labels),
                          name="label.classify", temperature=0.0,
                          max_tokens=self._max_tokens.get("triage")),
                timeout=self._label_timeout_ms / 1000.0)
            return prompts.parse_label(ex.text, sorted(labels)), prompts.parse_label_metadata(ex.text)
        except Exception:
            return None, None

    async def _answer(self, model_id: str, messages: list[dict], *, name: str, node: str,
                      max_tokens: int | None = None) -> Exec:
        """A user-facing answer: streams batched deltas to the run event plane when a stream_fn
        is wired, else a plain completion. Returns the full Exec either way (graph contract
        unchanged). Retryable stream errors restart the answer cleanly via _call."""
        if self._stream is None:
            return await self._llm(model_id, messages, name=name, max_tokens=max_tokens)
        req = ChatCompletionRequest(model=model_id, messages=[Message(**m) for m in messages],
                                    max_tokens=max_tokens)
        ex, _model, _note = await self._call(req, lambda r: self._stream_run(r, node), name=name)
        return ex

    async def _stream_run(self, req: ChatCompletionRequest, node: str, gate=None) -> Exec:
        """One streamed attempt with a FRESH batch buffer, so a retry/fallback restarts cleanly.
        Stale deltas from a failed attempt stay in the event log but are superseded by the
        terminal snapshot — the client swaps to authoritative text at run_done.

        gate (companion agent only): a callable(prelude)->True/False/None that inspects the leading
        text before anything is published — True to start streaming (plain answer), False to
        suppress the whole reply (it's a tool call, parsed by the caller from the returned Exec),
        None to keep buffering while ambiguous. Absent (driver answer nodes, which already know
        the reply is an answer) → emit from the first delta as before."""
        buf: list[str] = []
        whole: list[str] = []   # full stream so far, for the mid-stream tool-call guard
        last = [time.monotonic()]
        emit = [gate is None]   # may we publish yet? (True immediately when there's no gate)
        suppress = [False]      # gate ruled it a tool call → publish nothing, ever
        frozen = [False]        # committed answer grew a trailing {"tool" object → stop emitting
        seen_brace = [False]    # cheap gate: only scan for the tool object once a '{' appears

        async def flush() -> None:
            if not emit[0] or suppress[0]:
                return
            if buf:
                r = self._emit_delta(node, "".join(buf))  # async publish (prod) or sync (tests)
                if inspect.isawaitable(r):
                    await r
                buf.clear()
                last[0] = time.monotonic()

        async def on_delta(chunk: str) -> None:
            if suppress[0] or frozen[0]:
                return
            whole.append(chunk)
            buf.append(chunk)
            if not emit[0]:
                verdict = gate("".join(buf))
                if verdict is False:
                    suppress[0] = True
                    buf.clear()
                    return
                if verdict is not True:
                    return       # still ambiguous — hold the buffer, emit nothing
                emit[0] = True   # decided: plain answer — the held buffer flushes below
            # Mid-stream tool-call guard (gated companion path only): a committed answer that
            # sprouts a trailing `{"tool" …}` object — the model narrated then appended the call.
            # Emit only the clean prose up to the brace, then freeze so the JSON never streams.
            if gate is not None and ("{" in chunk or seen_brace[0]):
                seen_brace[0] = True
                s = "".join(whole)
                m = _TOOL_OBJ_RE.search(s)
                if m is not None:
                    already = len(s) - sum(len(x) for x in buf)  # chars already published
                    buf[:] = [s[already:m.start()]] if already < m.start() else []
                    await flush()
                    frozen[0] = True
                    return
            if sum(len(x) for x in buf) >= self._delta_chars or \
                    time.monotonic() - last[0] >= self._delta_secs:
                await flush()

        ex = await self._stream(req, on_delta)
        await flush()  # emit the tail
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
            req, lambda r: self._stream_run(r, node, gate=g), name=name)
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
                    # token/cost columns (Decision 6.3) — metadata alone never rolls up.
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
                t["authored"] = True  # provenance: _dispatch_one stamps route_reason off this
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
        # Each _dispatch_one mutates only its own task dict and returns its own spans, so there
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
        # Team routing overlay (control-plane C6): the caller's team tag->model bindings + optimize
        # preset, resolved server-side and carried on the request identity (effective_policy). Empty/
        # None when the team has no routing policy, the caller is the operator, or this is an internal
        # (no-identity) run → byte-identical global behavior. Read once here, threaded per task below.
        team_bindings: dict[str, str] = {}
        team_optimize: str | None = None
        # Custom task types (CT): the team's invented labels [{name, desc, model}]. Their descs
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
        # still stay in-perimeter, or its prompt_text leaks via embed/kNN/corpus/frontier (#29).
        run_pinned = bool(state.get("local_pinned"))
        span_groups = await asyncio.gather(
            *(self._dispatch_one(t, optimize, pins, idx, run_pinned, label_pins, team_bindings,
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
        # never egressing local-only output to frontier (#29). No flagged task → key absent →
        # frontier as before (byte-identical). A flagged task passed the per-task no-local-lane
        # gate, so the raised flag never makes synthesize's _reasoning_model fall open to frontier.
        out: dict = {"tasks": tasks, "spans": spans}
        if any(t.get("local_pinned") for t in tasks):
            out["local_pinned"] = True
        return out

    async def _emit_task_block(self, t: dict, reasons: list[str]) -> list[dict]:
        """Finalize a task the residency gate refused (MNPI egress, or local-required-but-no-local-lane):
        mark it blocked, emit the guard_block span, and write Toto status/exec. Never dispatches."""
        spans: list[dict] = []
        t.update(
            blocked=True, lane=None, model_id=None, result=None,
            execution={"outcome": "blocked_constraints", "route_reason": "; ".join(reasons)},
        )
        spans.append(await self._emit("guard_block", task=t.get("task"), reasons=reasons))
        if t.get("item_id"):
            _, s = await self._toto(lambda: self.toto.set_status(t["item_id"], "done"), "status")
            spans += s
            _, s = await self._toto(lambda: self.toto.write_execution(t["item_id"], t["execution"]), "exec")
            spans += s
        return spans

    def _effective_catalog(self) -> Catalog:
        """Base catalog + the current caller's adoptions (catalog-adoption). The driver runs inside
        the request context, so current_identity() carries the caller resolved at auth (None for
        internal/test runs → base unchanged). This lets the driver's OWN selection — classify, label
        bindings, user pins — pick an adopted model, matching the gateway dispatch choke point.
        Cheap: no adoptions → returns `self.catalog` itself."""
        from ..routes.deps import current_identity
        return effective_catalog(self.catalog, current_identity())

    async def _decide_one(self, t: dict, *, optimize: str | None = None,
                          pins: dict[str, str] | None = None, run_pinned: bool = False,
                          label_pins: dict[str, str] | None = None,
                          team_bindings: dict[str, str] | None = None,
                          team_custom: list[dict] | None = None) -> RouteDecision:
        """The pure routing decision for one task — guard → residency → classify → label → kNN →
        pin → residency re-check. No dispatch, no Toto writes: `_dispatch_one` executes the result,
        `/v1/routing/decide` serializes it. May emit the label span (observability only)."""
        spans: list[dict] = []
        md = t.get("metadata") or {}
        catalog = self._effective_catalog()  # selection resolves adopted models too (catalog-adoption)
        prompt_text = f"{t.get('task', '')}\n\n{t.get('description', '')}".strip()
        probe = ChatCompletionRequest(
            model=self.driver_model, messages=[Message(role="user", content=prompt_text)]
        )

        # GUARD (fail-closed) — per task, before any executor OR embedding sees it.
        verdict = self.guard.check(probe, Signal())
        if verdict.action == BLOCK:
            return RouteDecision(None, [], None, blocked=True,
                                 block_reasons=verdict.reasons, spans=spans)

        # RESIDENCY decided on the RAW task BEFORE any external call: data_policy (cheap,
        # side-effect-free) or a guard downgrade both pin the work local. Nothing sensitive may
        # leave the perimeter as a side effect of deciding how to route it (#29).
        data_policy = (md.get("requires") or {}).get("data_policy")
        local_pinned = (data_policy in {"local_only", "local"}
                        or verdict.action == DOWNGRADE_LOCAL or run_pinned)
        # FAIL CLOSED: pinned in-perimeter but no in-perimeter model exists → block, never fall to frontier (#20).
        if local_pinned and _first_in_perimeter_model(self.catalog) is None:
            return RouteDecision(
                None, [], None, local_pinned=True, blocked=True,
                block_reasons=["privacy: in-perimeter handling required, no in-perimeter model available"],
                spans=spans)
        # Surface residency so the dispatch node can raise the run-level pin: otherwise synthesize
        # aggregates this local-only OUTPUT and egresses it to the frontier driver_model (#29
        # residual). Set AFTER the gate above → a flagged task provably has a local lane, so the
        # raised state flag can never make _reasoning_model fall open to frontier.
        t["local_pinned"] = local_pinned

        # SKILL: embedding nearest-centroid when enabled AND egress is permitted, else keyword.
        # Local-pinned text is never POSTed to the external embedder — degrade to the keyword
        # classifier (the exact embedder-is-None path).
        skill_override = None
        if self._embed_routing and self._embedder is not None and not local_pinned:
            skill_override = await self._embedder.infer_skill(prompt_text)
        # CLASSIFY metadata → executor; guard downgrade_local overrides toward local.
        dec = classify(md, catalog, self.benchmarks, optimize, skill=skill_override)
        rejected = list(dec.rejected)  # benchmark losers; override paths append the displaced pick
        if self._embed_routing:  # only annotate when the flag is on → flag-off is byte-identical
            dec = TaskDecision(dec.lane, dec.tools_required, dec.model_id,
                               dec.reason + f"; skill:{'embed' if skill_override else 'keyword'}",
                               dec.skill)
        # LABEL ROUTING: haiku classifier → closed-set label → labels.yaml binding displaces the
        # benchmark pick (lane AND model — the binding sets the tier). Pinned text never egresses
        # (same gate as the embedder above). Any miss — no/unknown/unbound label — leaves the
        # classify() pick standing; kNN, pins, and the residency re-check below still apply on top.
        label = None
        label_metadata = None
        user_bound = None
        team_bound = None
        if self._labels is not None and not local_pinned:
            # CT: team_custom expands the classifier vocab for this request (its bound models were
            # folded into team_bindings in dispatch, so a custom classification routes below).
            label, label_metadata = await self._classify_label(prompt_text, team_custom)
            # Binding precedence for a classified label: a user's Settings override wins, then the
            # TEAM overlay (control-plane C6, admin config), then the shipped labels.yaml default.
            # A label the team didn't set falls through to the global default; another team gets the
            # global default. All ids PUT-validated against the catalog; a stale id (since left the
            # catalog) falls through to the next tier — .get() on an unknown id returns None.
            user_bound = catalog.get((label_pins or {}).get(label) or "") if label else None
            team_bound = catalog.get((team_bindings or {}).get(label) or "") if label else None
            bound = (user_bound or team_bound
                     or (catalog.get(self._labels.model_for(label) or "") if label else None))
            if bound is not None:
                rejected.append({"model_id": dec.model_id, "reason": "label binding outbid benchmarks"})
                origin = ":user" if user_bound else (":team" if team_bound else "")
                dec = TaskDecision(bound.lane, dec.tools_required, bound.id,
                                   f"label:{label}{origin}", dec.skill)
            else:
                dec = TaskDecision(dec.lane, dec.tools_required, dec.model_id,
                                   dec.reason + f"; label:{label or 'none'}:fallback", dec.skill)
            spans.append(await self._emit("label", task=t.get("task"), label=label,
                                          model=self._label_model,
                                          bound=bound.id if bound is not None else None))
        # EXPERIENCE-kNN: similar past tasks propose a model, overriding the benchmark pick within
        # the decided lane. Skipped for privacy lanes (privacy > kNN), for an EXPLICIT label binding
        # — a user's OR the team's (control-plane C6) — since that is deliberate intent (same
        # authority as pins), and yields to the pin below (pins > kNN). None when flag off / sparse
        # neighbors → prior stays.
        if self._knn is not None and not local_pinned and user_bound is None and team_bound is None:
            prop = await self._knn.propose(prompt_text, dec.lane)
            if prop is not None:
                rejected.append({"model_id": dec.model_id, "reason": "knn outvoted"})
                dec = TaskDecision(dec.lane, dec.tools_required, prop.model_id,
                                   dec.reason + f"; {prop.reason}", dec.skill)
        # User pin for this skill overrides the benchmark pick — but never a privacy lane
        # (data_policy pins beat user pins), and the guard downgrade below still beats both.
        pin = (pins or {}).get(dec.skill)
        if pin and not local_pinned:
            entry = catalog.get(pin)
            if entry is not None:
                rejected.append({"model_id": dec.model_id, "reason": "pin override"})
                dec = TaskDecision(entry.lane, dec.tools_required, entry.id,
                                   dec.reason + "; pin:user", dec.skill)
        # Egress/privacy guard keys off RESIDENCY, not tier: when the run is pinned local
        # (local_pinned — propagated from the raw-query DOWNGRADE_LOCAL guard, main's egress-residual
        # hardening), keep this sub-task in-perimeter unless it already is, landing on an in-perimeter
        # model (never a cheap CLOUD one).
        cur = catalog.get(dec.model_id)
        if local_pinned and (cur is None or cur.residency_class != "in_perimeter"):
            perim = _first_in_perimeter_model(self.catalog)  # local models only — base catalog
            if perim:
                entry = catalog.get(perim)
                rejected.append({"model_id": dec.model_id, "reason": "privacy guard: downgrade_local"})
                dec = TaskDecision(entry.lane, dec.tools_required, perim,
                                   dec.reason + "; guard: downgrade_local", dec.skill)

        return RouteDecision(dec, rejected, label, local_pinned=local_pinned, spans=spans,
                             label_metadata=label_metadata)

    async def _dispatch_one(self, t: dict, optimize: str | None = None,
                            pins: dict[str, str] | None = None, idx: int = 0,
                            run_pinned: bool = False,
                            label_pins: dict[str, str] | None = None,
                            team_bindings: dict[str, str] | None = None,
                            team_custom: list[dict] | None = None) -> list[dict]:
        # DECIDE (pure) then EXECUTE — the decision is the same function /v1/routing/decide exposes.
        rd = await self._decide_one(t, optimize=optimize, pins=pins, run_pinned=run_pinned,
                                    label_pins=label_pins, team_bindings=team_bindings,
                                    team_custom=team_custom)
        spans: list[dict] = list(rd.spans)
        if rd.blocked:
            return spans + await self._emit_task_block(t, rd.block_reasons)
        dec, rejected, label, local_pinned = rd.dec, rd.rejected, rd.label, rd.local_pinned
        if t.get("authored"):  # orchestrator-authored task: provenance stamp. APPENDED, never
            # prefixed — _privacy_pinned keys off the head of the reason string.
            dec.reason += "; orchestrator:authored"
        classified = rd.label_metadata  # totoshape metadata → stamped onto the Toto item at write-back
        md = t.get("metadata") or {}
        # RESIDENCY × RUNNER (fail-closed, BEFORE any spawn): an in-perimeter decision — local
        # pin, data_policy, guard downgrade — must never execute on claude_code, which egresses
        # to Anthropic directly, outside the gateway's residency enforcement. The decision
        # object knows the pin; the adapter alone never would. pi is exempt: its completions
        # come back THROUGH this gateway, which enforces residency itself.
        if local_pinned and (md.get("requires") or {}).get("runner") == "claude_code":
            return spans + await self._emit_task_block(t, [
                "privacy: in-perimeter handling required; runner claude_code egresses to Anthropic"])
        prompt_text = f"{t.get('task', '')}\n\n{t.get('description', '')}".strip()

        if t.get("item_id"):
            _, s = await self._toto(lambda: self.toto.set_status(t["item_id"], "in_progress"), "status")
            spans += s

        exreq = ChatCompletionRequest(
            model=dec.model_id,
            messages=[Message(role="system", content=prompts.EXECUTOR_PROMPT),
                      Message(role="user", content=prompt_text)],
            max_tokens=self._max_tokens.get("dispatch"),
        )
        # The classifier chose the model; the adapter registry chooses the harness (default:
        # gateway; a task can pin claude_code/pi via metadata.requires.runner). _call adds
        # provider retry + fallback (honoring the residency/privacy boundary) and traces each try.
        try:
            ex, final_model, note = await self._call(
                exreq, lambda r: self._adapters.run(r, md),
                name=f"dispatch:{dec.model_id}",
                route_meta={"route_reason": dec.reason, "skill": dec.skill, "lane": dec.lane,
                            "task": t.get("task"),
                            **({"label": label} if self._labels is not None else {})},
                privacy=_privacy_pinned(dec.reason),
            )
        except Exception as exc:  # executor died after retries+fallback (provider, stub, timeout)
            err = f"{type(exc).__name__}: {exc}"
            t.update(
                lane=dec.lane, model_id=dec.model_id, tools_required=dec.tools_required,
                result=None,
                execution={"outcome": "failed", "lane": dec.lane, "model": dec.model_id,
                           "route_reason": dec.reason, "error": err},
            )
            spans.append(await self._emit("dispatch_error", task=t.get("task"),
                                    model=dec.model_id, error=err))
            if t.get("item_id"):
                _, s = await self._toto(lambda: self.toto.set_status(t["item_id"], "done"), "status")
                spans += s
                _, s = await self._toto(
                    lambda: self.toto.write_execution(t["item_id"], t["execution"], classified), "exec")
                spans += s
            return spans

        # Record the model that ACTUALLY ran (fallback may have switched it), with the reason noted.
        final_entry = self._effective_catalog().get(final_model)  # adopted models carry their lane too
        final_lane = final_entry.lane if final_entry else dec.lane
        reason = dec.reason + (f"; {note}" if note else "")
        execution = {
            "runner": ex.adapter or "gateway", "executor": ex.model, "lane": final_lane,
            "model": final_model, "tokens_prompt": ex.tokens_prompt,
            "tokens_completion": ex.tokens_completion, "tokens_cached": ex.tokens_cached,
            "cost_usd": ex.cost_usd,
            "outcome": "completed", "latency_ms": ex.latency_ms, "route_reason": reason,
            # Typed receipt (hash only, never a copy of the answer) + who lost the routing bid.
            "artifact": make_artifact("task_result", ex.text, produced_by=final_model,
                                      evidence=[final_lane, dec.skill], confidence=None),
            "rejected": rejected,
        }
        t.update(
            lane=final_lane, model_id=final_model, tools_required=dec.tools_required, result=ex.text,
            skill=dec.skill, execution=execution,
        )
        residency = final_entry.residency_class if final_entry else "frontier"
        t["residency"] = residency
        spans.append(await self._emit("dispatch", task=t.get("task"), lane=final_lane, model=final_model,
                                cost=ex.cost_usd, reason=reason, skill=dec.skill,
                                residency=residency, tokens_prompt=ex.tokens_prompt,
                                tokens_cached=ex.tokens_cached,
                                sha256=execution["artifact"]["sha256"], n_rejected=len(rejected)))
        if t.get("item_id"):
            _, s = await self._toto(lambda: self.toto.set_status(t["item_id"], "done"), "status")
            spans += s
            _, s = await self._toto(
                lambda: self.toto.write_execution(t["item_id"], t["execution"], classified), "exec")
            spans += s
        # Experience corpus (groundwork) — fire-and-forget, never blocks or breaks the run.
        # Skipped for local-pinned tasks: their prompt_text must not be embedded externally
        # (app.py's sink POSTs it) nor persisted to task_embeddings.
        if self._corpus_sink is not None and not local_pinned:
            asyncio.create_task(_safe_corpus(
                self._corpus_sink, str(idx), prompt_text, dec.skill, final_model,
                "completed", ex.cost_usd, ex.latency_ms))
        return spans

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
        turn's result into a single revised summary (Dayflow's continuity contract — revise the
        draft, don't append). Not streamed — it's a background artifact, not a user-facing answer."""
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
        # the trivial path never reaches the per-task guard at all (#5/#15). BLOCK → refuse the run;
        # a guard downgrade pins every reasoning node local (or blocks if no in-perimeter model exists, #20).
        verdict = self.guard.check(
            ChatCompletionRequest(model=self.driver_model,
                                  messages=[Message(role="user", content=query)]), Signal())
        if verdict.action == BLOCK:
            return await self._blocked_result(query, verdict.reasons)
        local_pinned = verdict.action == DOWNGRADE_LOCAL
        # Authored tasks descend from NOTHING gated (on main every task descended from the gated
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
