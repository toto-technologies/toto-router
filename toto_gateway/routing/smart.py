"""Smart auto-routing for the passthrough plane (chunk SR1).

The sentinel model `smart` makes /v1/chat/completions classify the request and pick the model
per the team's routing policy — the NVIDIA-style task->model routing that otherwise lives only
on the driver plane (/v1/route, /v1/sessions), unreachable by OpenAI clients. So `pi -m
toto/smart` gets the right model for the work, per the console-configured policy.

Reuses the driver's classifier prompt/parser (driver.prompts), the label bindings
(routing.labels), and the benchmark ladder (driver.classify) — no new routing logic.

Resolve ladder for a classified label (mirrors Driver._decide_one, minus the driver-only user
pin / kNN, which have no equivalent on a raw chat request):

  team binding OR custom-label model (effective_policy C6/CT)
    -> global labels.yaml binding (LabelBindings.model_for)
      -> unbound label ('other'/'redact') OR classifier down: the benchmark best
         (classify() with empty metadata — the exact fallback the driver's ladder lands on)

Guard downgrade + catalog allow/deny still apply: they run in Gateway._plan on the RESOLVED
model, unchanged, after this module rewrites req.model.

This module stays pure — the gateway hands it an async `classify_fn(messages, model_id) -> text`
that dispatches straight on the runner (no user-facing trace turn); the classifier call never
pollutes the response.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Protocol

from ..driver import prompts
from ..driver.classify import classify

# Case-insensitive sentinels. `toto-smart` is accepted so a provider-prefixed id (toto/smart ->
# smart, or an explicit toto-smart) both land here.
SMART_MODELS = frozenset({"smart", "toto-smart"})

# Classifier input cap: the label only needs the gist of the request, and agentic harnesses can
# carry very large user messages — an uncapped classify call adds latency + tokens for nothing.
CLASSIFY_TEXT_MAX = 4000

# Label memo (SR2 agentic stickiness): one conversation classifies ONCE. Keyed on the
# CONVERSATION anchor (gateway conversation_key: sha256 of system + first user message) + the
# label vocab (a policy/label change re-classifies immediately; tenant vocabs never collide), so every
# turn of a chat — a pi tool loop OR a genuine multi-turn conversation whose newest user message
# differs each turn — reuses the first turn's label with zero added latency AND resolves to the
# same model. No mid-conversation model flips that break tool-call continuity and forfeit upstream
# prompt caches. Classification INPUT is still the current last-user message; only the memo KEY is
# the anchor. Falls back to the (text, vocab) key when no anchor exists (degenerate non-chat).
# Sliding TTL; label-only (bindings still resolve fresh each request, so console policy changes
# take effect immediately).
_LABEL_TTL_S = 900.0
_LABEL_CACHE_MAX = 1024
# (expiry, label, metadata, ttl, data_label) — metadata is the totoshape classify block (or None),
# data_label is the org taxonomy data-classification (W2-C7, or None), both cached with the label so a
# sticky repeat turn keeps ALL THREE — the data constraint must hold on every turn, not just the one
# that classified. ttl is the hold a StickinessPolicy chose for THIS entry (or _LABEL_TTL_S by
# default); it rides in the row so the sliding re-stamp preserves the policy's hold instead of
# snapping back to the constant. In-memory, fresh per process → format change needs no migration.
_label_cache: dict[str, tuple[float, str, dict | None, float, str | None]] = {}


def _cache_key(text: str, vocab: dict) -> str:
    return hashlib.sha1(repr((text, sorted(vocab))).encode()).hexdigest()


def _cache_get(key: str) -> tuple[str, dict | None, str | None] | None:
    row = _label_cache.get(key)
    if row is None:
        return None
    expiry, label, meta, ttl, data_label = row
    if expiry < time.monotonic():
        _label_cache.pop(key, None)
        return None
    _label_cache[key] = (time.monotonic() + ttl, label, meta, ttl, data_label)  # slide preserves the hold
    return label, meta, data_label


def _cache_put(key: str, label: str, meta: dict | None, ttl: float = _LABEL_TTL_S,
               data_label: str | None = None) -> None:
    if len(_label_cache) >= _LABEL_CACHE_MAX:
        _label_cache.pop(next(iter(_label_cache)))  # ponytail: FIFO eviction; LRU if it matters
    _label_cache[key] = (time.monotonic() + ttl, label, meta, ttl, data_label)


# --- Stickiness seam (S1) -------------------------------------------------------------------
# The memo above pins a conversation's label for a flat _LABEL_TTL_S. A StickinessPolicy makes that
# hold an explicit, per-entry decision: assess(ctx) chooses how long THIS conversation stays pinned.
# `stick=None` (the default everywhere but app.py) → the constant, byte-for-byte as before. The seam
# lives in smart_route so streaming inherits it, and assess sees only cheap in-memory context (never
# an engine/session) so it stays free on the hot path. The decision rides label_metadata under
# "stick" — the one field that survives fallbacks — so it lands on the trace without touching
# route_reason's label:<l> grammar.


@dataclass
class StickCtx:
    conversation_key: str | None
    label: str
    vocab: dict
    require_tools: bool
    policy: object


@dataclass
class StickDecision:
    hold_ttl: float          # replaces _LABEL_TTL_S for this entry's memo hold
    strength: float = 1.0    # 0..1 — reserved for the future warmth-scaled switch margin
    reason: str = ""         # observability: rides label_metadata["stick"]


class StickinessPolicy(Protocol):
    def assess(self, ctx: StickCtx) -> StickDecision: ...


class SlidingTTL:
    """Policy #1 (the default): today's behavior — one flat sliding hold for every conversation."""

    def assess(self, ctx: StickCtx) -> StickDecision:
        return StickDecision(hold_ttl=_LABEL_TTL_S, reason="sliding_ttl")


class LabelAwareTTL:
    """Policy #2: the memo hold varies by TASK TYPE. A code_generation thread stays pinned longer
    than a one-shot classification. TTL for a label resolves in precedence:

      1. the caller's org/team routing policy `stick_ttls` (ctx.policy.stick_ttls) — the console
         table, the per-tenant AX lever;
      2. the global default map (TOTO_GW_STICK_TTLS, passed at construction);
      3. `_LABEL_TTL_S` (the flat SlidingTTL default).

    With no maps configured it returns `_LABEL_TTL_S` for every label, so it behaves identically to
    SlidingTTL — the app wires it unconditionally.

    Seeding future defaults from empirical priors (turns-per-conversation per label, on testing
    traces — do NOT run at request time, this is an offline recipe):

        SELECT label,
               COUNT(*)                                     AS turns,
               COUNT(DISTINCT conversation_key)             AS convs,
               COUNT(*) * 1.0 / COUNT(DISTINCT conversation_key) AS turns_per_conv
        FROM   traces
        WHERE  conversation_key IS NOT NULL AND label IS NOT NULL
        GROUP  BY label
        ORDER  BY turns_per_conv DESC;

    Labels with high turns_per_conv (code_generation, chatbot) want the longer holds; closed_qa /
    classification / extraction want short ones.
    """

    def __init__(self, default_ttls: dict | None = None) -> None:
        self._defaults = {str(k): float(v) for k, v in (default_ttls or {}).items()}

    def assess(self, ctx: StickCtx) -> StickDecision:
        org = getattr(ctx.policy, "stick_ttls", None) or {}
        ttl = org.get(ctx.label) or self._defaults.get(ctx.label) or _LABEL_TTL_S
        return StickDecision(hold_ttl=float(ttl), reason="label_ttl")


# Declared sessions (S3): a client that names its own session identity (x-session-id header, body
# session_id, or prompt_cache_key) is telling us "these turns are ONE task" — the Claude Code /
# OpenRouter lesson (users file idle-timeout release of a declared session as a *bug*). The gateway
# stamps such a request's conversation_key as `declared:<hash>` (overriding the message fingerprint),
# so the memo anchors on the declared identity and every turn shares the label. The hold is long and
# identity-based, committed eagerly on the first turn.
DECLARED_TTL_S = 4 * 3600.0


def is_declared(conversation_key: str | None) -> bool:
    return (conversation_key or "").startswith("declared:")


class DeclaredSession:
    """Policy #3: a declared session commits eagerly to a long, identity-based hold. Any other
    conversation delegates to the inner policy (LabelAwareTTL in production)."""

    def __init__(self, inner: "StickinessPolicy") -> None:
        self._inner = inner

    def assess(self, ctx: StickCtx) -> StickDecision:
        if is_declared(ctx.conversation_key):
            return StickDecision(hold_ttl=DECLARED_TTL_S, reason="declared_session")
        return self._inner.assess(ctx)


# --- Conversation warmth stats (S4) ---------------------------------------------------------
# A bounded in-process table of per-conversation warmth, written where the gateway finalizes usage
# (Gateway._account) and read by WarmthHold at assess. In-memory only, fresh per process, NEVER the
# sync trace engine — this must stay off the durable hot path. Same FIFO-cap discipline as the label
# memo. A conversation that shows a live upstream prefix cache (last_tokens_cached>0) or several
# turns is "hot" and earns a longer hold.
_WARMTH_MAX = 4096
# conversation_key -> {turns, last_tokens_cached, last_seen, last_model}. last_model is the model
# actually SERVED on the most recent turn — the incumbent the TTL-aware hold protects (chunk B).
_warmth: dict[str, dict] = {}


def record_warmth(conversation_key: str | None, tokens_cached: int, model: str | None = None) -> None:
    """Bump the warmth stat for a conversation on each finalized turn. No key (degenerate) → no-op.
    `model` records the served model id so the incumbent-hold can prefer it while the prefix is warm."""
    if not conversation_key:
        return
    st = _warmth.get(conversation_key)
    if st is None:
        if len(_warmth) >= _WARMTH_MAX:
            _warmth.pop(next(iter(_warmth)))  # ponytail: FIFO eviction, matches the label memo
        st = _warmth[conversation_key] = {
            "turns": 0, "last_tokens_cached": 0, "last_seen": 0.0, "last_model": None}
    st["turns"] += 1
    st["last_tokens_cached"] = int(tokens_cached or 0)
    st["last_seen"] = time.monotonic()
    if model:
        st["last_model"] = model


# Provider prompt-cache TTLs (seconds) — the re-routing window. The catalog entry's `cache_ttl_s`
# is the source of truth when set; absent, we heuristically match the model-id family (same string
# heuristic the runners use in _is_anthropic_family). Numbers from provider docs only:
# Anthropic 5min, OpenAI 30min (GPT-5.6+ guaranteed),
# DeepSeek best-effort ~24h, everything else 5min.
_CACHE_TTL_DEFAULT = 300


def cache_ttl_s(entry) -> int:
    """The warm-window length (seconds) for `entry`. Catalog `cache_ttl_s` wins; else a per-family
    default off the upstream model id. None entry → the conservative 5-min default."""
    if entry is None:
        return _CACHE_TTL_DEFAULT
    explicit = getattr(entry, "cache_ttl_s", None)
    if explicit:
        return int(explicit)
    m = (getattr(entry, "effective_upstream_model", None) or getattr(entry, "id", "") or "").lower()
    if "claude" in m or "anthropic" in m:
        return 300
    if "deepseek" in m:
        return 86400
    if "gpt" in m or "openai" in m or m.startswith(("o1", "o3", "o4")):
        return 1800
    return _CACHE_TTL_DEFAULT


def warmth_of(conversation_key: str | None) -> dict | None:
    return _warmth.get(conversation_key) if conversation_key else None


class WarmthHold:
    """Policy #4: the hold scales with conversation WARMTH. A hot conversation — a live upstream
    prefix cache (last_tokens_cached > 0) or several turns deep — is stickier, mirroring the Linux
    CFS `task_hot()` rule (a cache-hot task resists migration). Base 900s; a warm conversation
    extends to `warm_ttl` (1h). `strength` is the warm fraction (0..1), reserved for the future
    warmth-scaled switch margin."""

    def __init__(self, base: float = _LABEL_TTL_S, warm_ttl: float = 3600.0, warm_turns: int = 3) -> None:
        self._base = base
        self._warm = warm_ttl
        self._warm_turns = warm_turns

    def assess(self, ctx: StickCtx) -> StickDecision:
        st = warmth_of(ctx.conversation_key)
        turns = st["turns"] if st else 0
        cached = st["last_tokens_cached"] if st else 0
        if cached > 0 or turns >= self._warm_turns:  # warm: extend + flag so the composite floors on it
            return StickDecision(hold_ttl=self._warm, strength=1.0, reason="warmth_hold")
        # Cold: the base hold, and reason "warmth_cold" so the composite knows NOT to floor on it
        # (a deliberately-short label hold must survive a not-yet-warm conversation).
        return StickDecision(hold_ttl=self._base, strength=min(1.0, turns / self._warm_turns),
                             reason="warmth_cold")


class TotoStickiness:
    """The production stickiness ladder (S4) — ONE class, rungs in precedence order (no
    chain-of-responsibility framework):

        1. DeclaredSession — a `declared:` conversation_key (client-declared session) wins outright
           with a long, eager identity hold.
        2. LabelAwareTTL — the per-task-type hold (org/team `stick_ttls` > global default > flat).
        3. WarmthHold FLOOR — a hot conversation extends the hold; taken as `max` with the label
           hold so warmth only ever lengthens, never shortens, the pin.
    """

    def __init__(self, label_defaults: dict | None = None) -> None:
        self._label = LabelAwareTTL(label_defaults)
        self._warmth = WarmthHold()

    def assess(self, ctx: StickCtx) -> StickDecision:
        if is_declared(ctx.conversation_key):
            return StickDecision(hold_ttl=DECLARED_TTL_S, strength=1.0, reason="declared_session")
        label = self._label.assess(ctx)
        warm = self._warmth.assess(ctx)
        if warm.reason == "warmth_hold" and warm.hold_ttl > label.hold_ttl:  # warm floor, never shortens
            return StickDecision(hold_ttl=warm.hold_ttl, strength=warm.strength, reason="warmth_hold")
        return StickDecision(hold_ttl=label.hold_ttl, strength=warm.strength, reason=label.reason)


# --- Optional Redis L2 for the label memo (S4) ----------------------------------------------
# The label memo is per-replica in-process (uvicorn workers=1, N replicas), so a conversation that
# lands on a different replica re-classifies. An optional Redis L2 (same client as the breaker)
# shares the memo cross-replica: on an L1 miss, try L2; on a put, best-effort SET with the entry's
# ttl. Follows the breaker template exactly — ANY Redis error fails OPEN to per-replica behaviour,
# never blocks the request. L1 dict stays the source of truth.
_MEMO_L2_PREFIX = "stick:memo:"


async def _l2_get(redis, key: str) -> tuple[str, dict | None, str | None] | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(_MEMO_L2_PREFIX + key)
        if raw is None:
            return None
        d = json.loads(raw)
        return d["label"], d.get("meta"), d.get("data_label")
    except Exception:
        return None  # ponytail: Redis outage → per-replica behaviour, never blocks the request


async def _l2_put(redis, key: str, label: str, meta: dict | None, ttl: float,
                  data_label: str | None = None) -> None:
    if redis is None:
        return
    try:
        await redis.set(_MEMO_L2_PREFIX + key,
                        json.dumps({"label": label, "meta": meta, "data_label": data_label}),
                        ex=max(1, int(ttl)))
    except Exception:
        pass


def is_smart(model: str | None) -> bool:
    return (model or "").strip().lower() in SMART_MODELS


@dataclass
class SmartResult:
    model_id: str        # the resolved real catalog id req.model is rewritten to
    route_reason: str    # "label:<l>" | "label:<l>:team" | "label:<l>:fallback" | "smart:classify_failed"
    label: str | None    # the classification (surfaced as x_toto.classified_as); None when it failed
    classify_ms: float | None = None  # wall time of the classify call (for the LangSmith classify span)
    label_metadata: dict | None = None  # totoshape metadata block (captured for the work-map); None otherwise
    data_label: str | None = None  # W2-C7 org data-classification (rode the same classify call); None if no taxonomy


def _tools_ok(entry, require_tools: bool) -> bool:
    """A tools-bearing request (agentic harness) must never resolve to an entry that can't speak
    native tool calling — the loop would silently degrade to textified tools."""
    return entry is not None and (not require_tools or getattr(entry, "tools", True))


def fallback_model(catalog, benchmarks, policy, require_tools: bool = False,
                   labels=None) -> str:
    """The pick when there is no bound label — classifier down/absent, unparseable, or an
    unbound label like 'other'. Precedence:

      1. the policy's explicit `other` binding — the team/org's DESIGNATED catch-all (the
         org's GENERALIST), so 'route everything unclassified here' is one configurable knob;
      2. else the global generalist — labels.yaml's `other` binding (shipped default);
      3. else the driver's classify() with empty metadata (the same benchmark-best decision its
         ladder falls to, honoring the optimize band);
      4. else any catalog entry (empty/edge catalog).

    Every rung skips a candidate that can't satisfy require_tools. Never raises, never 500s."""
    if policy is not None:
        other = (getattr(policy, "label_bindings", None) or {}).get("other")
        if other and _tools_ok(catalog.get(other), require_tools):
            return other
    if labels is not None:
        g = labels.model_for("other")
        if g and _tools_ok(catalog.get(g), require_tools):
            return g
    optimize = getattr(policy, "optimize", None)
    try:
        model_id = classify({}, catalog, benchmarks, optimize).model_id
        if _tools_ok(catalog.get(model_id), require_tools):
            return model_id
    except Exception:
        pass
    ref = catalog.frontier_reference()
    if _tools_ok(ref, require_tools):
        return ref.id
    entry = next((e for e in catalog.models if _tools_ok(e, require_tools)),
                 catalog.models[0] if catalog.models else None)
    return entry.id if entry is not None else ""


def _vocab(labels, policy) -> dict:
    """Global labels.yaml vocab merged with the caller team's custom labels (CT), so the
    classifier can emit a team-invented label for this request only."""
    v = dict(labels.labels)
    for c in getattr(policy, "custom_labels", None) or []:
        if c.get("name"):
            v[c["name"]] = {"desc": c.get("desc", "")}
    return v


def _binding_entry(label, catalog, labels, policy):
    """(entry, origin) for the model a HUMAN bound to `label` — a team/org override or custom task
    type (control-plane C6/CT) first, else the shipped labels.yaml default — resolved through the
    catalog, IGNORING tools capability. (None, '') when the label is unbound anywhere ('other'/
    'redact' with model: null, or a binding whose model left the catalog). The tools guard runs in
    the caller AFTER this, so an EXPLICIT binding governs ALL traffic for the label, tools or not —
    the optimizer never silently overrides it."""
    if policy is not None:
        team_id = (policy.label_bindings or {}).get(label)
        if not team_id:
            team_id = next((c.get("model") for c in (policy.custom_labels or [])
                            if c.get("name") == label and c.get("model")), None)
        team = catalog.get(team_id or "")
        if team is not None:
            return team, ":team"
    entry = catalog.get(labels.model_for(label) or "")
    return (entry, "") if entry is not None else (None, "")


def benchmark_pick_for(label, catalog, labels, benchmarks, policy, require_tools: bool = False):
    """What benchmark_best WOULD pick for `label`'s category — the optimizer's advice. Computed even
    when an explicit binding governs, so the trace/console can surface 'benchmark pick: <model>'.
    None when the label declares no benchmark category or the data can't decide. require_tools
    filters the candidate pool so the advice never names a model the request couldn't use."""
    cat = labels.category_for(label)
    if not cat or benchmarks is None:
        return None
    optimize = getattr(policy, "optimize", None) or "balanced"
    real = [e for e in catalog.models if e.endpoint != "fake" and _tools_ok(e, require_tools)]
    pick = benchmarks.best(real, cat, optimize)
    return pick.id if pick is not None else None


def _with_bench(meta, chosen_id, bench_pick):
    """Record the optimizer's advice in label_metadata['benchmark_pick'] when it differs from the
    chosen (bound) model — the advisor hint the trace/console reads. Same model → unchanged."""
    if bench_pick and bench_pick != chosen_id:
        return {**(meta or {}), "benchmark_pick": bench_pick}
    return meta


def _warm_hold(result: SmartResult, *, catalog, conversation_key: str | None,
               require_tools: bool, policy, bound: bool) -> SmartResult:
    """TTL-aware incumbent hold (chunk B). If this conversation was served a DIFFERENT model on a
    recent turn AND that incumbent's upstream prefix cache is still warm (last turn had a cache hit
    and is inside the incumbent's provider cache TTL) AND the incumbent still satisfies every hard
    constraint the fresh pick faced (present in catalog, tools-capable when required, permitted by
    the caller's policy) — keep the incumbent instead of swapping. Swapping mid-window forfeits the
    ~90% prefix-cache discount; once the window is cold (or never established) we take the fresh pick
    freely.

    BINDINGS BEAT WARMTH: `bound` is True when the fresh pick came from an EXPLICIT label binding
    (team/org override or the global labels.yaml default) — an operator's deliberate choice. We
    never hold over that: an admin rebind takes effect on the next turn even while the incumbent is
    warm. Warmth only holds against benchmark/optimize DRIFT — the derived paths (benchmark_best /
    fallback) where the fresh model can wobble turn-to-turn as benchmark data refreshes, which is
    the churn the cache economics actually want damped.

    DELIBERATE: while warm, benchmark/optimize drift does NOT re-resolve this conversation until the
    window cools (bounded by the provider TTL, ≤30 min). Escapes that still swap: an explicit binding
    (above), the tools guard (a tools-introducing turn), a policy/vocab change (re-classifies via the
    memo key), and require_tools re-resolution. The overridden fresh pick is recorded in
    label_metadata["warm_hold"] so the console can show what was held over."""
    if bound:  # an explicit binding is an operator decision — it wins over cache warmth
        return result
    st = warmth_of(conversation_key)
    if not st:
        return result
    incumbent = st.get("last_model")
    if not incumbent or incumbent == result.model_id:  # nothing to hold, or already the incumbent
        return result
    inc_entry = catalog.get(incumbent)
    if inc_entry is None or not _tools_ok(inc_entry, require_tools):  # gone, or a tools escape → fresh
        return result
    if policy is not None and not policy.permits(inc_entry):  # incumbent now denied → fresh
        return result
    if not st.get("last_tokens_cached"):  # no live prefix cache → swap is free
        return result
    window = cache_ttl_s(inc_entry)
    elapsed = time.monotonic() - (st.get("last_seen") or 0.0)
    if elapsed >= window:  # cold: past the provider TTL, swap freely
        return result
    meta = {**(result.label_metadata or {}),
            "warm_hold": {"kept": incumbent, "over": result.model_id,
                          "window_left_s": round(window - elapsed, 1)}}
    # `label:<l>:warm-hold` keeps route_reason's label:<l> grammar — _label_from_reason (trace.py)
    # takes segment[1], so analytics still read the same task-type label.
    return SmartResult(incumbent, f"label:{result.label}:warm-hold", result.label,
                       result.classify_ms, meta, data_label=result.data_label)


async def smart_route(text, *, catalog, labels, benchmarks, classifier_model, policy,
                      classify_fn, timeout_s, require_tools: bool = False,
                      conversation_key: str | None = None,
                      stick: "StickinessPolicy | None" = None,
                      memo_redis=None, warmth_routing: bool = False,
                      taxonomy: dict | None = None) -> SmartResult:
    """Classify `text` on `classifier_model`, then resolve label -> model via the ladder above.
    One classify call, hard-capped by `timeout_s`; ANY failure (classifier absent from the
    catalog, timeout, parse miss) degrades to fallback_model, never raises.

    The classification is memoized (sliding TTL) on (`conversation_key`, vocab): every later turn
    of the same conversation — an agentic tool loop OR a multi-turn chat with a fresh last-user
    message — skips the classifier entirely (classify_ms=None) and lands on the same label, keeping
    the model sticky. The vocab stays in the key so a policy/label change re-classifies immediately
    (as before) and identical anchors under different tenant vocabs never share an entry. No anchor
    (degenerate non-chat) → memo keyed on (text, vocab), the prior behavior.

    `stick` (a StickinessPolicy) governs the memo hold: on a classify (put) it chooses this entry's
    TTL and records {reason, hold_ttl, hit} under label_metadata["stick"]; a later memo hit re-flags
    hit=True. None → the flat _LABEL_TTL_S, byte-for-byte as before.

    `warmth_routing` (chunk B): when True, a resolved label pick is passed through _warm_hold, which
    keeps the conversation's warm incumbent model over the fresh pick while its provider prefix cache
    is still warm. False (the default for direct callers) → pure fresh resolution, today's behavior.
    Never applied to classify_failed (no label): a classify failure is never overridden."""
    text = (text or "")[:CLASSIFY_TEXT_MAX]
    vocab = _vocab(labels, policy)
    tax_vocab = list((taxonomy or {}).get("labels") or {})  # W2-C7 data-classification vocab
    key = _cache_key(conversation_key or text, vocab)
    cached = _cache_get(key)
    if cached is None and memo_redis is not None:  # L1 miss → try the cross-replica L2 (S4)
        l2 = await _l2_get(memo_redis, key)
        if l2 is not None:
            _cache_put(key, l2[0], l2[1], data_label=l2[2])  # promote into L1; ttl re-decided on classify
            cached = l2
    if cached is not None:
        label, meta, data_label = cached
        if meta and "stick" in meta:  # memo hit: flag the stick record for this turn's trace
            meta = {**meta, "stick": {**meta["stick"], "hit": True}}
    else:
        label, meta, data_label = None, None, None
    classify_ms = None
    if label is None and catalog.get(classifier_model) is not None:
        t0 = time.perf_counter()
        try:
            raw = await asyncio.wait_for(
                classify_fn(prompts.build_label_messages(text, vocab, taxonomy=taxonomy),
                            classifier_model),
                timeout=timeout_s)
            label = prompts.parse_label(raw, sorted(vocab))
            meta = prompts.parse_label_metadata(raw)  # None unless the totoshape variant is live
            data_label = prompts.parse_data_label(raw, tax_vocab) if tax_vocab else None  # W2-C7
        except Exception:
            label = None
        classify_ms = (time.perf_counter() - t0) * 1000.0
        if label is not None:
            ttl = _LABEL_TTL_S
            if stick is not None:  # let the policy choose this entry's hold + record the decision
                dec = stick.assess(StickCtx(conversation_key, label, vocab, require_tools, policy))
                ttl = dec.hold_ttl
                meta = {**(meta or {}), "stick": {"reason": dec.reason,
                                                  "hold_ttl": dec.hold_ttl, "hit": False}}
            _cache_put(key, label, meta, ttl, data_label=data_label)
            if memo_redis is not None:  # best-effort cross-replica share (S4); fail-open on any error
                await _l2_put(memo_redis, key, label, meta, ttl, data_label=data_label)
    if label is None:  # classifier down/absent or unparseable -> benchmark default
        return SmartResult(fallback_model(catalog, benchmarks, policy, require_tools, labels=labels),
                           "smart:classify_failed", None, classify_ms, data_label=data_label)
    entry, origin = _binding_entry(label, catalog, labels, policy)
    # The optimizer's advice for this label, computed regardless of binding (advisor, not authority).
    bench_pick = benchmark_pick_for(label, catalog, labels, benchmarks, policy, require_tools)
    if entry is not None:
        # BOUND — a human bound this label. The binding governs ALL traffic (tools or not); the
        # optimizer is demoted to an advisor (bench_pick, recorded when it differs). Precedence:
        # bindings beat benchmark_best.
        bound = True
        meta = _with_bench(meta, entry.id, bench_pick)
        if _tools_ok(entry, require_tools):
            result = SmartResult(entry.id, f"label:{label}{origin}", label, classify_ms, meta,
                                 data_label=data_label)
        elif getattr(policy, "optimizer_steers_tools", False) and bench_pick:
            # Escape hatch (default off): the optimizer may steer tool traffic off a non-tool
            # binding to the benchmark best — the pre-precedence behavior, restored per policy.
            bound = False  # a benchmark pick can drift turn-to-turn; let warmth damp it, as before
            result = SmartResult(bench_pick,
                                 f"label:{label}:benchmark_best:{labels.category_for(label)}",
                                 label, classify_ms, meta, data_label=data_label)
        else:
            # Tools guard: the bound model can't speak native tools. Do NOT silently benchmark-route
            # — the binding stands as intent; pick a tools-capable fallback and record the guard so
            # Activity shows the binding was displaced by the guard, not by the optimizer.
            result = SmartResult(fallback_model(catalog, benchmarks, policy, require_tools, labels=labels),
                                 f"label:{label}:tools_guard", label, classify_ms, meta,
                                 data_label=data_label)
    else:
        # UNBOUND — no human binding (a cleared binding, or 'other'/'redact'). This is the ONLY path
        # benchmark_best now governs. Prefer the team's explicit 'other' catch-all; else route
        # benchmark-best on the label's CATEGORY; else the generic fallback. Keep the classification
        # so the client still sees classified_as.
        bound = False
        other = (getattr(policy, "label_bindings", None) or {}).get("other")
        cat = labels.category_for(label)
        if bench_pick and cat and not (other and _tools_ok(catalog.get(other), require_tools)):
            result = SmartResult(bench_pick, f"label:{label}:benchmark_best:{cat}", label,
                                 classify_ms, meta, data_label=data_label)
        else:
            result = SmartResult(fallback_model(catalog, benchmarks, policy, require_tools, labels=labels),
                                 f"label:{label}:fallback", label, classify_ms, meta,
                                 data_label=data_label)
    # TTL-aware incumbent hold: prefer this conversation's warm model over the fresh pick while its
    # provider prefix cache is still warm (chunk B). Only on a resolved label; classify_failed above
    # already returned. Gated by the kill-switch; direct callers default off (fresh resolution).
    if warmth_routing:
        result = _warm_hold(result, catalog=catalog, conversation_key=conversation_key,
                            require_tools=require_tools, policy=policy, bound=bound)
    return result
