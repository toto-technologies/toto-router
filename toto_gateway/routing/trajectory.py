"""Run-stage trajectory signals for agentic conversations.

Dimension and weight design after NVIDIA NeMo Switchyard's stage router (Apache-2.0):
tool-result history is projected onto normalized [0, 1] dimensions, then a weighted
linear scorer emits a signed score in [-1, +1] (positive = this turn wants the capable
tier, negative = efficient), with confidence = |score|. Shadow-mode only for now: the
gateway stamps the score on the trace and routes on nothing here.

Extraction is harness-agnostic: it reads the OpenAI-shaped wire (assistant tool_calls +
role:"tool" results), not any harness's native tool names.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..schemas import Message

# Saturating normalization half-points: counts map to [0, 1) via 1 - exp(-x/scale).
_ERROR_SCALE = 2.0
_TOOL_OPS_SCALE = 6.0
_RECENT_WINDOW = 6          # tool results considered "recent"
_STUCK_MIN_RESULTS = 5      # this many results with no mutation = exploring in circles
_ERROR_PAT = re.compile(r"traceback|error[:\s]|exception|failed|FAILED|npm ERR", re.I)
_TESTS_PASS_PAT = re.compile(r"(\d+) passed", re.I)
_TESTS_FAIL_PAT = re.compile(r"(\d+) failed|FAILED", re.I)
_MUTATION_PAT = re.compile(r"wrote|written|created|updated|edited|applied|patched", re.I)


@dataclass(frozen=True)
class TrajectorySignal:
    """Raw counts off the conversation's tool-result history."""

    tool_results: int
    error_count: int
    recent_error_count: int
    mutation_count: int
    recent_mutation_count: int
    tests_passed: bool
    tests_failed: bool


@dataclass(frozen=True)
class TrajectoryScore:
    score: float
    confidence: float
    top_contribution: str


def _saturating(x: float, scale: float) -> float:
    return 0.0 if x <= 0 else 1.0 - math.exp(-x / scale)


def extract(messages: list[Message]) -> TrajectorySignal | None:
    """Project the conversation's tool-role messages into raw trajectory counts.

    Returns None when there is no tool history — plain chat has no trajectory to score.
    """
    results = [m for m in messages
               if m.role == "tool" and isinstance(m.content, str) and m.content]
    if not results:
        return None
    texts = [m.content for m in results]
    recent = texts[-_RECENT_WINDOW:]
    # Verdict of the LAST test-shaped output wins: a run that failed then passed is green.
    tests_passed = tests_failed = False
    for text in reversed(texts):
        if _TESTS_FAIL_PAT.search(text) and "0 failed" not in text:
            tests_failed = True
            break
        if _TESTS_PASS_PAT.search(text):
            tests_passed = True
            break
    return TrajectorySignal(
        tool_results=len(texts),
        error_count=sum(1 for t in texts if _ERROR_PAT.search(t)),
        recent_error_count=sum(1 for t in recent if _ERROR_PAT.search(t)),
        mutation_count=sum(1 for t in texts if _MUTATION_PAT.search(t)),
        recent_mutation_count=sum(1 for t in recent if _MUTATION_PAT.search(t)),
        tests_passed=tests_passed,
        tests_failed=tests_failed,
    )


# Positive => capable; negative => efficient. Values after Switchyard's calibration:
# a single saturated high-impact axis clears a 0.5 confidence threshold on its own.
WEIGHTS: dict[str, float] = {
    "error_intensity":        0.80,
    "recent_error_intensity": 0.60,
    "stuck_exploring":        0.70,
    "tests_failed":           0.50,
    "tests_passed":          -0.80,
    "mutation_intensity":    -0.40,
    "recent_mutation_intensity": -0.30,
}


def score(signal: TrajectorySignal) -> TrajectoryScore:
    dims = {
        "error_intensity": _saturating(signal.error_count, _ERROR_SCALE),
        "recent_error_intensity": _saturating(signal.recent_error_count, _ERROR_SCALE),
        "stuck_exploring": 1.0 if (signal.tool_results >= _STUCK_MIN_RESULTS
                                   and signal.mutation_count == 0) else 0.0,
        "tests_failed": 1.0 if signal.tests_failed else 0.0,
        "tests_passed": 1.0 if signal.tests_passed else 0.0,
        "mutation_intensity": _saturating(signal.mutation_count, _TOOL_OPS_SCALE),
        "recent_mutation_intensity": _saturating(signal.recent_mutation_count,
                                                 _TOOL_OPS_SCALE),
    }
    contributions = {name: dims[name] * WEIGHTS[name] for name in WEIGHTS}
    raw = sum(contributions.values())
    clipped = max(-1.0, min(1.0, raw))
    top = max(contributions, key=lambda name: abs(contributions[name]))
    # ponytail: regex heuristics over tool text, not structured tool names — harness-agnostic
    # floor; upgrade path is per-harness extractors keyed on the detected harness if traces
    # show the patterns misfire.
    return TrajectoryScore(score=clipped, confidence=abs(clipped), top_contribution=top)
