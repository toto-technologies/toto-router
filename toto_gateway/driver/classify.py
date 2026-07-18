"""Fast, deterministic metadata classifier — the driver's routing brain.

A dict and a few rules: read a task's structured metadata (produced by the decomposing
agent) and pick a lane + concrete catalog model + the tools the task needs. Supersedes the
retired exemplar/cosine query router — structured metadata is a cleaner routing signal than
raw query text. No models, no I/O, no deps: same input -> same decision, logged to trace.

Model choice WITHIN the lane is benchmark-informed: the task's skill (code / reasoning /
general) is inferred from the same metadata words, and the lane's entries are ranked by
their offline leaderboard scores (benchmarks.yaml — see toto_gateway/benchmarks.py). Still
pure lookups: no network, no embeddings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..benchmarks import OPTIMIZE, SKILLS, Benchmarks
from ..catalog import Catalog, CatalogEntry

KNOWN_TOOLS = ("web_search", "retrieval", "code_exec", "filesystem")
FRONTIER_WORDS = frozenset(
    {"analyze", "compare", "thesis", "decompose", "research", "synthesize",
     "market", "valuation", "forecast", "moat", "evaluate", "assess",
     "dcf", "memo", "diligence", "strategy", "recommendation", "projection", "outlook"}
)
ECONOMY_WORDS = frozenset(
    {"grep", "redact", "classify", "extract", "lookup", "format", "tag",
     "dedupe", "mask", "scrub", "parse", "convert", "sort", "count", "filter"}
)
CODE_WORDS = frozenset(
    {"code", "sql", "regex", "script", "refactor", "debug", "python", "function",
     "parse", "query", "grep", "test", "compile"}
)

_EMPTY_BENCHMARKS = Benchmarks()


@dataclass
class TaskDecision:
    lane: str                  # TIER: "economy" | "frontier"
    tools_required: list[str]  # subset of KNOWN_TOOLS
    model_id: str              # a catalog entry id to dispatch to
    reason: str                # human-readable, logged to trace
    skill: str = "general"     # inferred benchmark dimension (code/reasoning/general)
    # Routing-rejection receipts: in-lane alternatives that lost, each {"model_id","reason"}.
    rejected: list[dict] = field(default_factory=list)


def _words(metadata: dict) -> set[str]:
    """Lowercased tokens from intent/scope/keywords, split on non-alnum."""
    import re

    blob = " ".join(
        [str(metadata.get("intent", "")), str(metadata.get("scope", ""))]
        + [str(k) for k in metadata.get("keywords", []) or []]
    )
    return set(re.findall(r"[a-z0-9]+", blob.lower()))


def infer_skill(words: set[str], tools_required: list[str]) -> str:
    """The benchmark dimension this task exercises. Coarse on purpose — three buckets
    that map onto what leaderboards actually publish (coding / reasoning / general)."""
    if "code_exec" in tools_required or words & CODE_WORDS:
        return "code"
    if words & FRONTIER_WORDS:
        return "reasoning"
    return "general"


def _resolve_model(catalog: Catalog, lane: str, skill: str, benchmarks: Benchmarks,
                   optimize: str) -> tuple[str, float]:
    """Benchmark-best real entry of the lane; falls back to the old first-match chain when
    the lane has no real entries. Returns (model_id, score) for the trace."""
    real = [e for e in catalog.models if e.lane == lane and e.endpoint != "fake"]
    pick = benchmarks.best(real, skill, optimize)
    if pick:
        return pick.id, benchmarks.score(pick.effective_upstream_model, skill)
    for e in catalog.models:
        if e.lane == lane:
            return e.id, 0.0
    for e in catalog.models:
        if e.endpoint != "fake":
            return e.id, 0.0
    return catalog.models[0].id, 0.0


def _resolve_in_perimeter(catalog: Catalog, skill: str, benchmarks: Benchmarks,
                          optimize: str) -> tuple[CatalogEntry, float] | None:
    """Benchmark-best entry that physically stays IN-PERIMETER (residency, not tier). Privacy
    work keys off residency: sensitive data must never land on a cheap CLOUD economy model, so
    we select by `residency_class == "in_perimeter"`, not by lane. Falls back to the first
    in-perimeter entry (incl. a fake box for offline tests). None if the catalog has none."""
    real = [e for e in catalog.models
            if e.residency_class == "in_perimeter" and e.endpoint != "fake"]
    pick = benchmarks.best(real, skill, optimize)
    if pick:
        return pick, benchmarks.score(pick.effective_upstream_model, skill)
    for e in catalog.models:
        if e.residency_class == "in_perimeter":
            return e, 0.0
    return None


def _bench_losers(catalog: Catalog, lane: str, skill: str, benchmarks: Benchmarks,
                  optimize: str, winner_id: str, winner_score: float) -> list[dict]:
    """Top-3 in-lane REAL entries that lost the benchmark pick — routing-rejection receipts.
    Pure lookups over the same entries _resolve_model ranked (no I/O). Empty with no benchmarks."""
    if not benchmarks.models:
        return []
    losers = []
    for e in catalog.models:
        if e.lane != lane or e.endpoint == "fake" or e.id == winner_id:
            continue
        s = benchmarks.score(e.effective_upstream_model, skill)
        losers.append((s, e.id))
    losers.sort(key=lambda t: -t[0])  # highest-scoring near-misses first
    return [
        {"model_id": mid,
         "reason": f"lost on benchmark score ({skill}={s:.2f} < winner {winner_score:.2f}) "
                   f"under optimize={optimize}"}
        for s, mid in losers[:3]
    ]


def classify(metadata: dict, catalog: Catalog, benchmarks: Benchmarks | None = None,
             optimize: str | None = None, skill: str | None = None) -> TaskDecision:
    """Pure decision (no I/O). `skill`, when given, overrides keyword inference — the embedding
    router computes it out-of-band and passes it in; None → today's keyword path unchanged."""
    benchmarks = benchmarks if benchmarks is not None else _EMPTY_BENCHMARKS
    requires = metadata.get("requires")
    if not isinstance(requires, dict):  # LLM-produced metadata: tolerate junk shapes
        requires = {}
    # The user's quality/cost knob: per-task metadata beats the request-level value.
    optimize = requires.get("optimize") or optimize
    if optimize not in OPTIMIZE:
        optimize = "balanced"
    tools_required = [t for t in (requires.get("tools") or []) if t in KNOWN_TOOLS]
    complexity = metadata.get("complexity") or "medium"
    words = _words(metadata)

    # TIER precedence — first match wins. Explicit signals (data policy, tool needs,
    # complexity) outrank the word lists: words only break the complexity=medium tie,
    # so a frontier noun leaking in from a filename can't flip a low-complexity task.
    #
    # Privacy is the exception: it keys off RESIDENCY, not tier. `data_policy` sensitivity
    # forces an in-perimeter model below (never a cheap CLOUD economy one) — the `privacy`
    # flag defers actual model selection to _resolve_in_perimeter.
    data_policy = requires.get("data_policy")
    privacy = data_policy in {"local_only", "local"}
    if privacy:
        lane, reason = "economy", f"privacy: data_policy={data_policy} pins work in-perimeter"
    elif complexity == "high":
        lane, reason = "frontier", "frontier: complexity=high"
    elif "web_search" in tools_required or "retrieval" in tools_required:
        lane, reason = "frontier", "frontier: needs web_search/retrieval"
    elif complexity == "low":
        lane, reason = "economy", "economy: complexity=low"
    elif words & FRONTIER_WORDS:
        lane, reason = "frontier", f"frontier: reasoning/research signal {sorted(words & FRONTIER_WORDS)}"
    elif words & ECONOMY_WORDS:
        lane, reason = "economy", f"economy: mechanical signal {sorted(words & ECONOMY_WORDS)}"
    else:
        lane, reason = "economy", "economy: default (cheap-first, no escalation signal)"

    skill = skill if skill in SKILLS else infer_skill(words, tools_required)
    if privacy:
        perim = _resolve_in_perimeter(catalog, skill, benchmarks, optimize)
        if perim is not None:
            entry, score = perim
            lane, model_id = entry.lane, entry.id  # report the in-perimeter box's real tier
            rejected = []  # residency-selected: in-lane benchmark losers don't apply
        else:
            # No in-perimeter model exists. classify can't block (the guard layer does that),
            # so do NOT silently route secrets to a cloud economy model — surface the broken
            # guarantee loudly in the reason for the trace and any policy floor downstream.
            model_id, score = _resolve_model(catalog, lane, skill, benchmarks, optimize)
            reason += "; WARNING: no in-perimeter model available — privacy guarantee cannot be met"
            rejected = _bench_losers(catalog, lane, skill, benchmarks, optimize, model_id, score)
    else:
        model_id, score = _resolve_model(catalog, lane, skill, benchmarks, optimize)
        rejected = _bench_losers(catalog, lane, skill, benchmarks, optimize, model_id, score)
    if benchmarks.models:
        reason += f"; bench: skill={skill} optimize={optimize} {model_id}={score:.2f}"

    return TaskDecision(
        lane=lane,
        tools_required=tools_required,
        model_id=model_id,
        reason=reason,
        skill=skill,
        rejected=rejected,
    )
