"""Parser + builder tests for the driver prompts module. Pure, no LLM, no I/O."""

from __future__ import annotations

import json

from toto_gateway.driver.prompts import (
    DECOMPOSE_PROMPT,
    TRIAGE_PROMPT,
    build_decompose_messages,
    build_triage_messages,
    parse_tasks,
    parse_triage,
)
# The user-facing (persona-composed) builders moved to toto_gateway/persona.py (brand un-weld).
from toto_gateway.persona import build_direct_messages, build_synthesize_messages

# --- fixtures ---------------------------------------------------------------

TRIAGE_OBJ = {"kind": "trivial", "reason": "single definition lookup"}

VALID_TASK = {
    "task": "Survey local model options",
    "description": "Enumerate the candidate on-prem models and their license terms. "
    "Capture context window, quantization, and hardware footprint. Done when a "
    "comparison table exists.",
    "metadata": {
        "component": "research",
        "scope": "research",
        "complexity": "medium",
        "keywords": ["local", "models", "license"],
        "intent": "A ranked shortlist of local models with license + footprint columns.",
        "requires": {"tools": ["web_search"], "data_policy": "default"},
    },
}
MALFORMED_TASK = {"task": "", "description": "missing name", "metadata": {"x": 1}}
TASKS_OBJ = {"tasks": [VALID_TASK, MALFORMED_TASK]}


def _fenced(obj) -> str:
    return f"```json\n{json.dumps(obj)}\n```"


def _prosed(obj) -> str:
    return f"Sure! Here's what I came up with:\n\n{json.dumps(obj)}\n\nHope that helps!"


# --- parse_triage -----------------------------------------------------------

def test_triage_clean_json():
    assert parse_triage(json.dumps(TRIAGE_OBJ)) == {
        "kind": "trivial",
        "reason": "single definition lookup",
    }


def test_triage_fenced_json():
    assert parse_triage(_fenced(TRIAGE_OBJ))["kind"] == "trivial"


def test_triage_prose_wrapped():
    out = parse_triage(_prosed({"kind": "multistep", "reason": "needs steps"}))
    assert out == {"kind": "multistep", "reason": "needs steps"}


def test_triage_garbage_safe_default():
    assert parse_triage("total nonsense, no json here") == {
        "kind": "multistep",
        "reason": "unparseable; safe default",
    }


def test_triage_empty_safe_default():
    assert parse_triage("") == {"kind": "multistep", "reason": "unparseable; safe default"}


def test_triage_bad_kind_safe_default():
    # Valid JSON but nonsense "kind" -> safe default.
    assert parse_triage(json.dumps({"kind": "maybe", "reason": "x"})) == {
        "kind": "multistep",
        "reason": "unparseable; safe default",
    }


# --- parse_tasks ------------------------------------------------------------

def test_tasks_clean_json_drops_malformed_keeps_valid():
    out = parse_tasks(json.dumps(TASKS_OBJ))
    assert len(out) == 1
    assert out[0]["task"] == "Survey local model options"


def test_tasks_fenced_json():
    out = parse_tasks(_fenced(TASKS_OBJ))
    assert len(out) == 1 and out[0]["metadata"]["complexity"] == "medium"


def test_tasks_prose_wrapped():
    out = parse_tasks(_prosed({"tasks": [VALID_TASK]}))
    assert len(out) == 1 and out[0]["task"] == "Survey local model options"


def test_tasks_garbage_returns_empty():
    assert parse_tasks("no json, just vibes") == []


def test_tasks_empty_returns_empty():
    assert parse_tasks("") == []


def test_tasks_multiple_valid_kept():
    second = dict(VALID_TASK, task="Draft the comparison memo")
    out = parse_tasks(json.dumps({"tasks": [VALID_TASK, second, MALFORMED_TASK]}))
    assert [t["task"] for t in out] == [
        "Survey local model options",
        "Draft the comparison memo",
    ]


def test_tasks_bare_array_tolerated():
    out = parse_tasks(json.dumps([VALID_TASK, MALFORMED_TASK]))
    assert len(out) == 1


def test_tasks_missing_metadata_dropped():
    bad = {"task": "no meta", "description": "has words", "metadata": {}}
    assert parse_tasks(json.dumps({"tasks": [bad]})) == []


# --- builders ---------------------------------------------------------------

def test_build_triage_messages_shape():
    msgs = build_triage_messages("what is a moat?")
    assert msgs[0] == {"role": "system", "content": TRIAGE_PROMPT}
    assert msgs[1] == {"role": "user", "content": "what is a moat?"}


def test_build_decompose_messages_shape():
    msgs = build_decompose_messages("plan a rollout")
    # system block is a cache-marked parts list (context-caching P0), text byte-identical
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"][0]["text"] == DECOMPOSE_PROMPT
    assert msgs[1] == {"role": "user", "content": "plan a rollout"}


def test_build_synthesize_messages_embeds_results():
    msgs = build_synthesize_messages(
        "compare A and B",
        [{"task": "research A", "result": "A is fast"},
         {"task": "research B", "result": "B is cheap"}],
    )
    user = msgs[1]["content"]
    assert "compare A and B" in user
    assert "research A" in user and "A is fast" in user
    assert "research B" in user and "B is cheap" in user


def test_build_synthesize_handles_empty_results():
    msgs = build_synthesize_messages("q", [])
    assert "no sub-task results" in msgs[1]["content"]


def test_build_direct_messages_shape():
    msgs = build_direct_messages("hi")
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert len(msgs) == 2


def test_parse_tasks_survives_braces_inside_strings():
    # A description containing { } must not break the brace-balancer.
    t = dict(VALID_TASK, description="Handle the {weird} case with } braces { inside.")
    out = parse_tasks(_prosed({"tasks": [t]}))
    assert len(out) == 1
