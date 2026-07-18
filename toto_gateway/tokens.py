"""Token accounting helpers.

Phase 0 policy (Gary fold G2): prefer the upstream's *reported* usage. Only when the upstream
gives us nothing (some streaming endpoints omit a usage block) do we estimate, and we flag the
resulting cost `cost_estimated=True` so the north-star cost metric never silently lies.

The estimator is a deterministic heuristic (~4 chars/token), good enough to keep a running
count and to compare lanes. A real per-model tokenizer can replace it without touching callers.
"""

from __future__ import annotations

from .schemas import Message

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Deterministic, model-agnostic token estimate. Never returns < 0; empty text -> 0."""
    if not text:
        return 0
    return max(1, round(len(text) / _CHARS_PER_TOKEN))


def estimate_prompt_tokens(messages: list[Message]) -> int:
    """Estimate prompt tokens across a message list (role + content), with light per-message overhead."""
    total = 0
    for m in messages:
        total += estimate_tokens(m.text()) + estimate_tokens(m.role) + 3  # ~per-message framing
    return total
