"""Prompts + parsing for the driver graph — pure strings and stdlib JSON, no LLM calls.

The driver's LLM nodes (triage/decompose/synthesize/answer) are wired in core.py/graph.py;
this module owns only what is deterministic and unit-testable: the prompt templates, the
OpenAI-style message builders, and the parsers that recover JSON from messy model output.

Parser contract (fail SAFE, never raise): triage falls back to "multistep" (decomposing is
the conservative default — worse to answer a hard query one-shot than to over-decompose a
trivial one); parse_tasks drops malformed tasks and keeps valid siblings, [] on total loss.
The DECOMPOSE task schema mirrors classify.py's routing inputs (complexity / requires.tools /
requires.data_policy / scope / keywords / intent) so decomposed tasks route without remapping.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from pathlib import Path

# --- Persona un-weld -----------------------------------------------------------
# The identity + voice (TOTO_IDENTITY / TOTO_VOICE / TOTO_SYSTEM) and the two user-facing prompts
# that compose on them (SYNTHESIZE_PROMPT / DIRECT_ANSWER_PROMPT / REVISE_PROMPT) + their builders
# now live in toto_gateway/persona.py — the ONE brand-carrying, config-swappable surface. This
# ENGINE module carries zero brand and imports nothing from persona; persona imports the engine
# helpers below. The dev-dashboard override registry still reaches those surfaces through the
# by-module-string seam (PROMPT_SURFACES), exactly as it reaches routes.lists / companion.


# --- Prompt templates -------------------------------------------------------

TRIAGE_PROMPT = """You are a triage classifier for a task-routing system.

Classify the user request as exactly one of:
  - "trivial": a single-shot question answerable directly, right now, with no
    decomposition, tool use, or multi-step work (e.g. a definition, a fact, a
    short rewrite, a greeting).
  - "multistep": needs to be broken into 2 or more distinct tasks — research,
    analysis, comparison, planning, anything requiring sequential or parallel
    sub-work before a final answer.

Questions ABOUT YOU — the assistant, "Toto" — are ALWAYS "trivial". This covers
anything the user directs at "you"/"your" or at Toto: what you are, what you can
do, how you work, how YOU decide/route/price/choose things, who built you, "tell
me about yourself". You answer these directly from what you already know about
yourself — they are never research and never need decomposition. The ONLY
exception: a request to research, benchmark, or compare Toto AGAINST some other
product is real work and stays "multistep".

When in doubt, prefer "multistep": under-decomposing a hard request is worse
than over-decomposing an easy one.

When prior conversation is present and the new message is a FOLLOW-UP — a
refinement, correction, or short question answerable from the conversation plus
one direct completion ("shorter", "what about X", "now in French", "explain the
second point") — classify "trivial". A follow-up that itself demands fresh
multi-step work (new research, a new comparison) is still "multistep".

Return STRICT JSON only. No prose, no markdown fences. Exactly this shape:
{"kind": "trivial|multistep", "reason": "one short sentence"}"""

# {label_block} is substituted via .replace() (never .format()) so a TOTO_GW_PROMPTS_FILE
# override containing stray braces can't crash the builder. The vocabulary comes from
# routing/labels.yaml at call time — labels are data, this template never names one.
LABEL_PROMPT = """You label one piece of work for a task-routing system.

Pick exactly ONE label from this closed set — the single best fit for what the
work fundamentally IS:
{label_block}

Never invent a label, never combine labels, never pick more than one. If nothing
fits well, use "other".

Return STRICT JSON only. No prose, no markdown fences. Exactly this shape:
{"label": "<one label, verbatim from the set>", "reason": "one short sentence"}"""

# --- Named LABEL_PROMPT variants (prompt-tuning experiments) ------------------
# For scripts/label_experiments.py's prompt x model matrix. Each variant splices ONE extra block
# into the baseline BEFORE the strict-JSON contract line, so the output contract and the
# {label_block} placeholder stay byte-identical across variants (parse_label works unchanged).
# The blocks target the classifier's confusable pairs — closed_qa/summarization, rewrite/
# text_generation, extraction/classification, chatbot/open_qa — from routing/labels.yaml.

_LABEL_FEWSHOT_BLOCK = """Worked examples (query -> label) for the confusable pairs:
  - "Per the contract text above, what is the notice period?" -> closed_qa
  - "Summarize this article in three sentences." -> summarization
  - "Rewrite this paragraph in a warmer tone." -> rewrite
  - "Write a launch announcement for our new pricing." -> text_generation
  - "Pull every email address and phone number from this text." -> extraction
  - "Is this review positive, negative, or neutral?" -> classification
  - "What year did the Eiffel Tower open?" -> open_qa
  - "Hey, how's your day going?" -> chatbot"""

_LABEL_RULES_BLOCK = """Decision rules for the confusable pairs:
  - If the answer must come from text supplied in the request, it is closed_qa
    even when the output is a summary-like condensation; summarization is a plain
    "shorten this" with no question to answer.
  - rewrite transforms text the user supplied (rephrase, reformat, translate);
    text_generation writes new prose from a brief, with no source text to transform.
  - extraction returns values/fields verbatim from the input; classification
    assigns a category, tag, or sentiment from a fixed set.
  - chatbot is social/companionship exchange (greetings, small talk); open_qa
    seeks a factual answer from general knowledge."""


def _label_variant(block: str) -> str:
    """baseline LABEL_PROMPT with `block` spliced in before the strict-JSON contract line, so the
    output contract and the {label_block} placeholder stay byte-identical across variants."""
    anchor = "Return STRICT JSON only."
    return LABEL_PROMPT.replace(anchor, f"{block}\n\n{anchor}", 1)


# totoshape (EXPERIMENT, Alex 2026-07-10): the classification wears the EXACT shape of a Toto
# task's metadata request (the toto skill definition: component/files/keywords/scope/intent) —
# so a classified request is a ready node for a work knowledge graph and its keywords/intent are
# the text an embedding layer indexes to map work across an org/user. Different OUTPUT contract,
# same fewshot examples block, same parse_label (it only extracts "label" from the JSON).
# Costs measured, not assumed, by scripts/label_experiments.py: fatter completion per request,
# truncation risk under the 200-token triage cap, and how badly the repo-oriented scope enum
# strains on non-software work — all findings, all cheap to observe.
_LABEL_TOTOSHAPE_CONTRACT = '''{"label": "<one label, verbatim from the set>",
 "metadata": {
   "component": "<short slug naming the system or subject being worked on>",
   "files": ["paths or named artifacts EXPLICITLY mentioned in the request, else []"],
   "keywords": ["3 to 6 short, specific terms naming the subject and the operation"],
   "scope": "<one of: backend|frontend|schema|infra|test|docs|design|ux|ui|sync|mcp|desktop|other>",
   "intent": "<ONE falsifiable sentence: the observable outcome the user wants>"
 },
 "reason": "one short sentence"}'''


def _totoshape_variant() -> str:
    baseline_contract = '{"label": "<one label, verbatim from the set>", "reason": "one short sentence"}'
    return _label_variant(_LABEL_FEWSHOT_BLOCK).replace(
        baseline_contract, _LABEL_TOTOSHAPE_CONTRACT, 1)


# baseline IS the LABEL_PROMPT object above (same reference — zero behaviour change). build_label_messages
# reads the LIVE module global for "baseline" so the dashboard override seam still flows through; the
# fewshot/rules templates are frozen at import (experiment variants, not the runtime override surface).
LABEL_PROMPT_VARIANTS: dict[str, str] = {
    "baseline": LABEL_PROMPT,
    "fewshot": _label_variant(_LABEL_FEWSHOT_BLOCK),
    "rules": _label_variant(_LABEL_RULES_BLOCK),
    "totoshape": _totoshape_variant(),
}

# The PRODUCTION default variant — what build_label_messages uses when no variant is named, i.e.
# both live classifier call sites (driver label node, /v1 smart route). Set from
# settings.label_prompt_variant at build_gateway; module default mirrors the config default.
_LABEL_VARIANT_DEFAULT = "fewshot"


def set_label_variant(name: str) -> None:
    """Point the production default at a LABEL_PROMPT_VARIANTS entry (config → prompts seam,
    called once at build). Unknown name raises so a typo'd env var fails the boot, not a
    request."""
    global _LABEL_VARIANT_DEFAULT
    if name not in LABEL_PROMPT_VARIANTS:
        raise ValueError(f"unknown label prompt variant {name!r} — "
                         f"one of {sorted(LABEL_PROMPT_VARIANTS)}")
    _LABEL_VARIANT_DEFAULT = name

DECOMPOSE_PROMPT = """You are a task decomposer for a routing system whose execution plane
is Toto (a task manager). Break the user request into 2 to 4 concrete, independently
executable tasks. Prefer the FEWEST tasks that cover the request — merge closely related
work rather than splitting it; never exceed 4. Parallel variants of ONE operation are ONE
task: "translate this to Spanish and French" is a single translation task covering both
languages, not one task per language. Split only when the parts need genuinely different
work (different tools, different research, different skills).

Return STRICT JSON only. No prose, no markdown fences. Exactly this shape:
{"tasks": [
  {
    "task": "3-8 word imperative name",
    "description": "2-4 sentences: what this task involves, why it exists, and what 'done' looks like. REQUIRED and substantive — never a restatement of the title.",
    "metadata": {
      "component": "subsystem or area this touches",
      "scope": "backend|frontend|research|analysis|design|docs|infra|test|...",
      "complexity": "low|medium|high",
      "keywords": ["specific", "technical", "terms"],
      "intent": "one falsifiable sentence describing the observable outcome when done — REQUIRED",
      "requires": {
        "tools": ["web_search"|"retrieval"|"code_exec"|"filesystem"...],
        "data_policy": "default|local_only"
      }
    }
  }
]}

Rules:
  - 2 to 4 tasks. Each task must stand alone.
  - Task descriptions must be SELF-CONTAINED — resolve any reference to the
    prior conversation ("do the same for Austin", "the approach above") into
    explicit content. An executor sees only the description, never the history.
  - "description" (2-4 sentences) and "intent" (one falsifiable sentence) are
    REQUIRED and must be rich — a downstream classifier routes on this metadata.
  - Use "requires.tools": [] when a task needs no tools. Set
    "requires.data_policy": "local_only" only when the task handles sensitive
    data that must stay in-perimeter; otherwise "default"."""

# SYNTHESIZE_PROMPT / REVISE_PROMPT / DIRECT_ANSWER_PROMPT and their builders live in
# toto_gateway/persona.py (they compose on the swappable persona). See the persona un-weld note.

# Sub-task executors get NO identity and NO personality — they are pure task machines whose
# output is consumed by the synthesizer, never shown to the user. A lean instruction only, to
# stop chatty preamble/sign-offs from leaking into the synthesized answer (Poke's split: one
# voice at the interaction layer, silent workers underneath).
EXECUTOR_PROMPT = """You are an execution worker inside a larger system. You are handed one
self-contained task; produce exactly the work product it asks for and nothing else.

No preamble, no postamble, no greeting, no sign-off, no "here's..." runway, no offers of further
help. Do not address a user — your output is consumed by another component that weaves it into a
final answer. Lead with substance, be dense and factual, and stop when the work is done."""


# --- Message builders (OpenAI-style [{"role","content"}, ...]) ---------------

def _mark_cacheable(msg: dict) -> dict:
    """Rewrite a plain-string message as a one-part content list ending in an ephemeral
    cache_control breakpoint (Anthropic prompt caching via OpenRouter; providers that don't
    support the field ignore it, and below the ~1024-token minimum it's a no-op)."""
    content = msg.get("content")
    if not isinstance(content, str):
        return msg  # already parts (or empty) — never double-wrap
    return {**msg, "content": [
        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]}


def add_cache_breakpoints(msgs: list[dict]) -> list[dict]:
    """Context-caching plan Decision 2: at most TWO breakpoints — (a) the end of the system
    block, (b) the last message of the stable prefix (the final history message before the
    volatile current turn). No history → the system breakpoint alone. Applied by the
    driver-model builders (direct/synthesize/decompose); triage (or-qwen3-coder-flash, tiny prompt)
    deliberately unmarked. Content strings are whatever the surface registry currently says —
    overrides compose, this only wraps the built messages."""
    if not msgs:
        return msgs
    out = list(msgs)
    out[0] = _mark_cacheable(out[0])           # (a) system block
    if len(out) > 2:                           # [system, *history, current] → last history msg
        out[-2] = _mark_cacheable(out[-2])     # (b) end of the stable prefix
    return out


def _history_messages(history: list[dict] | None) -> list[dict]:
    """Prior (query, answer) turns as alternating user/assistant messages — the shape chat
    models are trained on, not a stuffed system prompt. Empty when no history."""
    msgs: list[dict] = []
    for turn in history or []:
        q = str(turn.get("query", "")).strip()
        a = str(turn.get("answer", "")).strip()
        if q:
            msgs.append({"role": "user", "content": q})
        if a:
            msgs.append({"role": "assistant", "content": a})
    return msgs


def build_triage_messages(query: str, history: list[dict] | None = None) -> list[dict]:
    """System = triage instructions, prior turns, then the new message."""
    return [
        {"role": "system", "content": TRIAGE_PROMPT},
        *_history_messages(history),
        {"role": "user", "content": query},
    ]


def _taxonomy_block(taxonomy: dict) -> str:
    """The org data-classification instruction spliced into the label prompt (W2-C7). Enumerates the
    org's sensitivity labels + descriptions and asks for a SECOND top-level field alongside whatever
    shape the active variant already emits — one classifier call, two labels."""
    labels = taxonomy.get("labels") or {}
    lines = "\n".join(
        f'  - "{name}": {(row or {}).get("desc", "")}' for name, row in sorted(labels.items()))
    return ("Separately, classify the DATA SENSITIVITY of this work as exactly ONE of the following "
            "classifications — a DIFFERENT dimension from the task label above:\n"
            f"{lines}\n"
            'Your JSON output MUST also include a top-level "data_classification" field holding that '
            'one classification verbatim (or "none" if none clearly applies).')


def build_label_messages(task_text: str, labels: dict[str, dict],
                         variant: str | None = None,
                         taxonomy: dict | None = None) -> list[dict]:
    """System = a LABEL prompt with the vocab enumerated, then the task text. `labels` is the
    LabelBindings.labels dict — each entry's desc becomes the label's one-line definition.
    `variant` selects a LABEL_PROMPT_VARIANTS template; None (both production call sites) means
    the configured default (settings.label_prompt_variant via set_label_variant). "baseline"
    reads the live LABEL_PROMPT global (byte-identical to the pre-variant builder, override
    seam intact).

    `taxonomy` (W2-C7): the org's data-classification config {labels: {...}, default}. When present
    with labels, its vocabulary is spliced into the SAME prompt (before the strict-JSON contract, the
    _label_variant mechanism) asking for a "data_classification" field — so one classifier call
    returns both the task label and the data sensitivity label. Absent/empty → byte-identical prompt."""
    variant = variant or _LABEL_VARIANT_DEFAULT
    template = LABEL_PROMPT if variant == "baseline" else LABEL_PROMPT_VARIANTS.get(variant)
    if template is None:
        raise ValueError(f"unknown label variant {variant!r} — "
                         f"one of {sorted(LABEL_PROMPT_VARIANTS)}")
    block = "\n".join(
        f'  - "{name}": {(row or {}).get("desc", "")}' for name, row in sorted(labels.items())
    )
    system = template.replace("{label_block}", block)
    if taxonomy and (taxonomy.get("labels")):  # W2-C7: same anchor the variants splice on
        anchor = "Return STRICT JSON only."
        system = system.replace(anchor, f"{_taxonomy_block(taxonomy)}\n\n{anchor}", 1)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": task_text},
    ]


def build_decompose_messages(query: str, history: list[dict] | None = None) -> list[dict]:
    return add_cache_breakpoints([
        {"role": "system", "content": DECOMPOSE_PROMPT},
        *_history_messages(history),
        {"role": "user", "content": query},
    ])


DECOMPOSE_RETRY_NUDGE = (
    "Your previous reply could not be parsed as the required JSON. Reply again with ONLY the "
    "JSON object in the exact schema from the instructions — no prose, no markdown fences."
)


def build_decompose_retry_messages(query: str, bad_output: str,
                                   history: list[dict] | None = None) -> list[dict]:
    """One corrective turn after an unparseable decomposition: original exchange + a nudge."""
    return build_decompose_messages(query, history) + [
        {"role": "assistant", "content": bad_output or "(empty reply)"},
        {"role": "user", "content": DECOMPOSE_RETRY_NUDGE},
    ]


# build_synthesize_messages / build_revise_messages / build_direct_messages moved to
# toto_gateway/persona.py (they compose on the swappable persona; they reuse add_cache_breakpoints
# and _history_messages from this engine module).


# --- Parsers (stdlib json + minimal regex, fail safe) ------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> object | None:
    """Recover a JSON object from messy LLM output. Returns the parsed value or None.

    Strategy, cheapest first: (1) parse the whole string; (2) parse the contents of a
    ```json ... ``` fence; (3) scan for the first brace-balanced {...} object, respecting
    string literals so braces inside strings don't fool the counter.
    """
    if not text or not text.strip():
        return None

    # 1. Whole string is JSON.
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # 2. Fenced block.
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. First brace-balanced object embedded in prose.
    blob = _balanced_object(text)
    if blob is not None:
        try:
            return json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _balanced_object(text: str) -> str | None:
    """Return the first top-level {...} substring with balanced braces, or None.

    String-aware: braces inside "..." (with \\-escapes) don't affect the depth count.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


_TRIAGE_SAFE_DEFAULT = {"kind": "multistep", "reason": "unparseable; safe default"}


def parse_triage(text: str) -> dict:
    """-> {"kind": "trivial"|"multistep", "reason": str}. Safe default on any failure."""
    data = _extract_json(text)
    if not isinstance(data, dict):
        return dict(_TRIAGE_SAFE_DEFAULT)
    kind = data.get("kind")
    if kind not in ("trivial", "multistep"):
        return dict(_TRIAGE_SAFE_DEFAULT)
    reason = data.get("reason")
    return {"kind": kind, "reason": str(reason) if reason else ""}


def parse_label(text: str, vocab: list[str] | tuple[str, ...]) -> str | None:
    """-> a label verbatim from `vocab`, or None on ANY failure (None = fallback routing;
    unlike triage there is no dict default because the ladder below is the safe default)."""
    data = _extract_json(text)
    if not isinstance(data, dict):
        return None
    label = data.get("label")
    return label if label in vocab else None


def parse_data_label(text: str, vocab: list[str] | tuple[str, ...] | set[str]) -> str | None:
    """The org data-classification label from the classifier's `data_classification` field (W2-C7),
    or None on ANY failure (missing / "none" / not in `vocab`). None = fall to the taxonomy default
    constraint (the fail-closed default). Shares _extract_json with parse_label — same one call."""
    data = _extract_json(text)
    if not isinstance(data, dict):
        return None
    v = data.get("data_classification")
    return v if v in vocab else None


# The Toto-shape metadata fields the totoshape classifier emits (skill definition parity).
_META_STR_FIELDS = ("component", "scope", "intent")
_META_LIST_FIELDS = ("files", "keywords")
_META_STR_MAX = 200
_META_LIST_MAX = 10


def parse_label_metadata(text: str) -> dict | None:
    """The totoshape classifier's `metadata` block, sanitized for capture — or None.

    Production sanitizer, NOT a judge: keep only the Toto-shape keys, coerce/truncate to safe
    bounds, drop empties. eval/labels.metadata_report stays the quality gate — this only makes
    a classified request's metadata safe to persist on the trace and stamp onto a Toto task.
    Never raises (any classify text, incl. the baseline/fewshot variants with no metadata → None).
    # ponytail: string-bounded, no schema lib — the shape is five known keys.
    """
    data = _extract_json(text)
    if not isinstance(data, dict):
        return None
    md = data.get("metadata")
    if not isinstance(md, dict):
        return None
    out: dict = {}
    for k in _META_STR_FIELDS:
        v = md.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:_META_STR_MAX]
    for k in _META_LIST_FIELDS:
        v = md.get(k)
        if isinstance(v, list):
            items = [str(x).strip()[:_META_STR_MAX] for x in v if str(x).strip()][:_META_LIST_MAX]
            if items:
                out[k] = items
    return out or None


def _valid_task(t: object) -> bool:
    """A task is valid iff it has non-empty task + description + a dict metadata."""
    if not isinstance(t, dict):
        return False
    if not isinstance(t.get("task"), str) or not t["task"].strip():
        return False
    if not isinstance(t.get("description"), str) or not t["description"].strip():
        return False
    if not isinstance(t.get("metadata"), dict) or not t["metadata"]:
        return False
    return True


# Field bounds for parsed tasks (parse_label_metadata's precedent): both the decompose LLM and
# an orchestrator-authored list are untrusted input into Toto + classify() — truncate, never reject.
_TASK_CAPS = {"task": 300, "description": 2000}
_TASK_META_STR_CAPS = {"complexity": 200, "scope": 200, "intent": 200, "component": 200}

# requires.runner allowlist (C2 subagent runners). Default EMPTY = flag off: a runner pin is
# STRIPPED like any unknown key, so a task can never reach an unregistered subagent adapter.
# set_subagent_runners(True) (config → prompts seam, app.build_gateway — the set_label_variant
# pattern) admits exactly {"pi", "claude_code"}; anything else ("gateway", "rm -rf", …) is
# still stripped — a prompt-injected decomposition can't invent a harness.
# ponytail: module-global like _LABEL_VARIANT_DEFAULT — two gateways built in one process share
# the LAST build's setting (fail direction: a later flag-off build strips pins for both, never
# admits them). Make it a parse_tasks parameter threaded through Driver AND companion.core if a
# multi-app-per-process host ever needs split behavior.
_ALLOWED_RUNNERS: frozenset[str] = frozenset()


def set_subagent_runners(enabled: bool) -> None:
    """Config → prompts seam for TOTO_GW_SUBAGENT_RUNNERS (called once at build): flag on →
    _clean_task lets requires.runner ∈ {pi, claude_code} survive; off → stripped (today's
    exact behavior)."""
    global _ALLOWED_RUNNERS
    _ALLOWED_RUNNERS = frozenset({"pi", "claude_code"}) if enabled else frozenset()


def _clean_task(t: dict) -> dict:
    """Strip a valid task to the DECOMPOSE schema, fields bounded. Unknown keys are DROPPED on
    both paths — a prompt-injected decomposition or an authored spawn list can't smuggle routing/
    provenance fields (authored, blocked, lane, model_id, residency, skill...); the driver sets
    `authored` itself post-parse, so the flag is unspoofable. Strings truncate, never reject."""
    md = t["metadata"]
    out_md: dict = {}
    for k, cap in _TASK_META_STR_CAPS.items():
        v = md.get(k)
        if isinstance(v, str):
            out_md[k] = v[:cap]
    kw = md.get("keywords")
    if isinstance(kw, list):
        out_md["keywords"] = [str(x)[:100] for x in kw[:10]]
    req = md.get("requires")
    if isinstance(req, dict):  # classify() validates tools/data_policy values itself
        out_md["requires"] = {k: req[k] for k in ("tools", "data_policy") if k in req}
        if req.get("runner") in _ALLOWED_RUNNERS:  # empty set (flag off) → always stripped
            out_md["requires"]["runner"] = req["runner"]
    return {"task": t["task"][:_TASK_CAPS["task"]],
            "description": t["description"][:_TASK_CAPS["description"]],
            "metadata": out_md}


def parse_tasks(text: str) -> list[dict]:
    """-> the tasks array, each stripped to the known schema (_clean_task). Drops malformed
    tasks, keeps valid siblings, [] on total failure."""
    data = _extract_json(text)
    if isinstance(data, dict):
        tasks = data.get("tasks")
    elif isinstance(data, list):
        tasks = data  # tolerate a bare array
    else:
        tasks = None
    if not isinstance(tasks, list):
        return []
    return [_clean_task(t) for t in tasks if _valid_task(t)]


# --- Prompt-surface registry + override seam ----------------------------------
# The code-side twin of docs/prompt-map.md (same surfaces, same order) and the enabling
# seam for the dev dashboard (docs/plans/2026-07-04-prompt-dashboard.md P0): a JSON file
# {surface_name: text} pointed at by TOTO_GW_PROMPTS_FILE overrides any surface below
# WITHOUT editing code or restarting — core.py and the message builders read these module
# attributes at call time, so setattr here takes effect on the very next LLM call.
#
# Contract: no file / no key → byte-identical prompts. Malformed file, unknown surface, or
# non-string value → ValueError (loud — a typo'd surface name must never silently no-op).

# The persona surfaces live in toto_gateway/persona.py; the registry reaches them by module string
# (importlib), the same seam that already reaches routes.lists / companion.prompts — so this ENGINE
# module still imports nothing from persona.
_PERSONA_MOD = "toto_gateway.persona"

PROMPT_SURFACES: dict[str, dict] = {
    "TOTO_IDENTITY": {
        "module": _PERSONA_MOD, "eval_flow": "voice",
        "steers": "Who Toto IS: self-knowledge, product framing, terrier temperament."},
    "TOTO_VOICE": {
        "module": _PERSONA_MOD, "eval_flow": "voice",
        "steers": "How Toto talks: lead with the answer, short, dry wit, banned-phrase list."},
    "TOTO_SYSTEM": {
        "module": _PERSONA_MOD, "eval_flow": "voice",
        "derived": "TOTO_IDENTITY + TOTO_VOICE (recomputed on override unless set directly)",
        "steers": "Combined identity+voice block prefixing both user-facing answer prompts."},
    "TRIAGE_PROMPT": {
        "module": __name__, "eval_flow": "triage",
        "steers": "Trivial-vs-multistep decision; the decompose threshold."},
    "LABEL_PROMPT": {
        "module": __name__, "eval_flow": "label",
        "steers": "Closed-set task label; the label->model binding decision (routing/labels.yaml)."},
    "DECOMPOSE_PROMPT": {
        "module": __name__, "eval_flow": "decompose",
        "steers": "Task count (2-4), task/metadata shape, fewest-tasks rule."},
    "DIRECT_ANSWER_PROMPT": {
        "module": _PERSONA_MOD, "eval_flow": "voice",
        "derived": "persona + direct-answer suffix (recomputed unless set directly)",
        "steers": "One-shot answer for trivial queries."},
    "SYNTHESIZE_PROMPT": {
        "module": _PERSONA_MOD, "eval_flow": "synthesize",
        "derived": "persona + synthesize suffix (recomputed unless set directly)",
        "steers": "Final multistep answer: weave results, never leak machinery."},
    "EXECUTOR_PROMPT": {
        "module": __name__, "eval_flow": "synthesize",
        "steers": "Sub-task workers: no personality, dense output for the synthesizer."},
    "DECOMPOSE_RETRY_NUDGE": {
        "module": __name__, "eval_flow": "decompose",
        "steers": "One corrective turn after unparseable decompose JSON."},
    "ENRICH_SYSTEM": {
        "module": "toto_gateway.routes.lists", "eval_flow": None,
        "steers": "List-item enrichment (not in the driver graph): bare item -> routed-work metadata."},
    "COMPANION_ROLE": {
        "module": "toto_gateway.companion.prompts", "eval_flow": "companion",
        "steers": "The partner layer on top of TOTO_SYSTEM: spawn-vs-answer judgment, memory "
                  "write policy, narration while work runs."},
    "TOOLS_BLOCK": {
        "module": "toto_gateway.companion.prompts", "eval_flow": "companion",
        "steers": "Companion tool protocol: the four tools, JSON call shape, iteration limit."},
}

# The derived-prompt suffixes now live on the persona module (persona._SYNTH_SUFFIX / _DIRECT_SUFFIX);
# apply_overrides reads them through _surface_module so overriding the identity/voice rebuilds
# SYNTHESIZE/DIRECT byte-exactly around the new persona block.

_DEFAULTS: dict[str, str] = {}  # pristine text per surface, captured before the first override
_ACTIVE: dict[str, str] = {}    # the overrides currently applied


def _surface_module(name: str):
    return importlib.import_module(PROMPT_SURFACES[name]["module"])


def _capture_defaults() -> None:
    for name in PROMPT_SURFACES:
        _DEFAULTS.setdefault(name, getattr(_surface_module(name), name))


def get_surface(name: str) -> str:
    """Current effective text of a surface (override applied if any)."""
    return getattr(_surface_module(name), name)


def default_surface(name: str) -> str:
    """Pristine (code-defined) text of a surface, regardless of active overrides."""
    _capture_defaults()
    return _DEFAULTS[name]


def active_overrides() -> dict[str, str]:
    return dict(_ACTIVE)


def surfaces_hash() -> str:
    """Short content hash over every effective surface — stamps eval runs to a prompt set."""
    blob = json.dumps({n: get_surface(n) for n in PROMPT_SURFACES}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def apply_overrides(overrides: dict[str, str]) -> None:
    """Apply {surface_name: text} over the code defaults, in place, effective immediately.

    Full-replace semantics: surfaces absent from `overrides` reset to their defaults, so
    apply_overrides({}) restores the pristine (byte-identical) state. Derived surfaces
    (TOTO_SYSTEM, SYNTHESIZE_PROMPT, DIRECT_ANSWER_PROMPT) are recomputed from their
    components unless overridden directly. Raises ValueError loudly on anything malformed."""
    unknown = set(overrides) - set(PROMPT_SURFACES)
    if unknown:
        raise ValueError(f"unknown prompt surface(s) {sorted(unknown)} — "
                         f"known: {sorted(PROMPT_SURFACES)}")
    bad = [k for k, v in overrides.items() if not isinstance(v, str) or not v.strip()]
    if bad:
        raise ValueError(f"prompt override(s) {sorted(bad)} must be non-empty strings")
    _capture_defaults()
    _ACTIVE.clear()
    _ACTIVE.update(overrides)
    for name in PROMPT_SURFACES:
        setattr(_surface_module(name), name, overrides.get(name, _DEFAULTS[name]))
    # Recompute the derived persona surfaces on the persona module (resolved by string — no static
    # import). TOTO_SYSTEM = identity+voice; SYNTHESIZE/DIRECT = the ACTIVE persona + their suffix.
    pm = _surface_module("TOTO_SYSTEM")
    if "TOTO_SYSTEM" not in overrides:
        pm.TOTO_SYSTEM = f"{pm.TOTO_IDENTITY}\n\n{pm.TOTO_VOICE}"
    active = pm.get_persona()
    if "SYNTHESIZE_PROMPT" not in overrides:
        pm.SYNTHESIZE_PROMPT = active + pm._SYNTH_SUFFIX
    if "DIRECT_ANSWER_PROMPT" not in overrides:
        pm.DIRECT_ANSWER_PROMPT = active + pm._DIRECT_SUFFIX


def load_overrides_file(path: str) -> dict[str, str]:
    """Load + apply TOTO_GW_PROMPTS_FILE. Missing file → no-op (byte-identical prompts, the
    documented default); anything else wrong → ValueError, never a silent fallback."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"prompt overrides file {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"prompt overrides file {path} must be a JSON object "
                         f"{{surface_name: text}}, got {type(data).__name__}")
    apply_overrides(data)
    return data
