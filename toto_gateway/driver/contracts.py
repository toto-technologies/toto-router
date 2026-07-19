"""Driver contracts — the shared types and pure helpers of the driver plane.

`Exec` is the executor seam: every completion (gateway, adapter, fake) normalizes to one.
`RouteState` is the LangGraph channel schema; `RouteDecision` the pure routing outcome;
`DriverResult` what `run()` hands the API. Everything here is dependency-light and
side-effect-free so `core` (the graph), `dispatch` (per-task routing), and `streaming`
can all import it without cycles.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Awaitable, Callable, TypedDict

from ..catalog import Catalog
from ..schemas import ChatCompletionRequest
from .classify import TaskDecision

# Cap decomposition fan-out: more tasks = more parallel frontier calls + a bigger synthesis
# prompt, and over-decomposition was the main latency multiplier. The prompt also asks for ≤4.
MAX_DECOMPOSE_TASKS = 4


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
    """Pure routing outcome for one task — what dispatch decides BEFORE it executes.
    `decide_one` produces it; `dispatch_one` executes it; `/v1/routing/decide` serializes it.
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


__all__ = [
    "MAX_DECOMPOSE_TASKS", "Exec", "CompleteFn", "StreamFn", "Observer",
    "RouteState", "DriverResult", "RouteDecision",
]
