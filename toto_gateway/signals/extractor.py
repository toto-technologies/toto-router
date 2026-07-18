"""HeuristicExtractor — keyword/regex signal extraction for Phase 1 routing brain.

Ponytail: stdlib only, no ML, no training. Pure functions + a keyword dict.
The intent set is small and stable — add new labels only when the router needs to
branch on them. complexity uses a two-signal formula: raw token count + a handful
of "cognitive load" trigger words.
"""

from __future__ import annotations

import re

from ..pipeline import Signal
from ..schemas import ChatCompletionRequest
from ..tokens import estimate_prompt_tokens

# ---------------------------------------------------------------------------
# Intent keyword map — each entry is (label, [keywords]).
# Evaluated top-to-bottom; first match wins. Keep the list short and readable.
# ---------------------------------------------------------------------------
_INTENT_RULES: list[tuple[str, list[str]]] = [
    # High-specificity verbs first — these must win before generic code/shell keywords.
    ("redact",     ["redact", "anonymize", "remove pii", "mask", "scrub"]),
    ("classify",   ["classify", "categorize", "sentiment", "what type", "detect", "is this a"]),
    ("translate",  ["translate", "in french", "in spanish", "in german", "in japanese", "in chinese", "language"]),
    ("summarize",  ["summarize", "summary", "tldr", "brief", "overview", "recap", "condense"]),
    ("extract",    ["extract", "parse", "pull out", "retrieve", "read from"]),
    ("calc",       ["calculate", "compute", "math", "equation", "formula", "how much", "how many", "percentage", "average"]),
    # search before shell so "grep the codebase" → search, not shell.
    ("search",     ["search the codebase", "search for", "find in", "where is", "locate", "look for", "grep the", "list all"]),
    ("web_search", ["search the web", "look up online", "google", "browse", "website", "url", "http"]),
    ("sql",        ["select ", "insert ", "update ", "delete ", "create table", "sql", "query", "database", "postgres", "sqlite", "mysql"]),
    # shell comes after search/sql — grep/find in a codebase context already matched above.
    ("shell",      ["bash", "shell", "terminal", "chmod", "curl", "grep", "awk", "sed", "pipe", "stdin", "stdout"]),
    # code_edit uses explicit "write/fix/refactor" phrases; avoid naked tokens like "function"
    # that appear naturally in classify/plan prompts too.
    ("code_edit",  ["fix the", "fix this", "refactor", "implement", "write a function", "write code", "add a method", "edit the code", "change this code", "update the code", "def ", "return "]),
    ("plan",       ["plan", "roadmap", "strategy", "design", "architect", "outline", "steps to", "how to"]),
    ("chat",       []),  # fallback
]

# Words that signal high cognitive load — presence → +1 on the complexity scale.
_HIGH_COMPLEXITY_WORDS = re.compile(
    r"\b(analyze|analyse|derive|multi[- ]?step|compare|evaluate|audit|review|reason|infer|"
    r"synthesize|synthesise|trade[- ]?off|architecture|recursive|optimiz|refactor|debug|"
    r"investigate|explain why|explain how)\b",
    re.IGNORECASE,
)

_LOW_COMPLEXITY_WORDS = re.compile(
    r"\b(list|grep|find|ping|echo|count|rename|move|copy|delete|print|show|display|"
    r"what is|what's|who is|define|hi|hello|thanks|help)\b",
    re.IGNORECASE,
)

# Token thresholds for complexity (prompt token estimate).
_LOW_THRESHOLD = 60
_HIGH_THRESHOLD = 300


def _classify_intent(text: str) -> str:
    low = text.lower()
    for label, keywords in _INTENT_RULES:
        if not keywords:
            return label  # fallback (chat)
        if any(kw in low for kw in keywords):
            return label
    return "chat"


def _classify_complexity(text: str, token_estimate: int) -> str:
    high_hits = len(_HIGH_COMPLEXITY_WORDS.findall(text))
    low_hits = len(_LOW_COMPLEXITY_WORDS.findall(text))

    # score: 0=low, 1=medium, 2=high
    score = 1  # medium baseline

    if token_estimate >= _HIGH_THRESHOLD:
        score += 1
    elif token_estimate <= _LOW_THRESHOLD:
        score -= 1

    if high_hits >= 2:
        score += 2
    elif high_hits >= 1:
        score += 1
    if low_hits >= 1 and high_hits == 0:
        score -= 1

    if score <= 0:
        return "low"
    if score >= 2:
        return "high"
    return "medium"


class HeuristicExtractor:
    """Signal extractor using keyword heuristics — intent + complexity for the trace and the
    guard/policy floor. No ML, no network, stdlib only. (The embedding field is gone: the
    exemplar/cosine router that consumed it is retired; the driver classifier routes on task
    metadata instead.)"""

    def extract(self, req: ChatCompletionRequest) -> Signal:
        has_tools = bool(req.model_dump(exclude_none=True).get("tools"))
        token_estimate = estimate_prompt_tokens(req.messages)

        # Operate on the last user turn; fall back to the whole conversation text.
        last_user_text = ""
        for msg in reversed(req.messages):
            if msg.role == "user":
                last_user_text = msg.text()
                break
        if not last_user_text:
            # No user turn — use whatever is there (system-only prompts, etc.)
            last_user_text = " ".join(m.text() for m in req.messages)

        intent = _classify_intent(last_user_text)
        complexity = _classify_complexity(last_user_text, token_estimate)

        return Signal(
            intent=intent,
            complexity=complexity,
            token_estimate=token_estimate,
            has_tools=has_tools,
        )
