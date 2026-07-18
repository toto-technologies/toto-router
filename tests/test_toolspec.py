"""TTC v1 spec math — validation table, substitution, param binding, and step execution.

Pure (no DB): validate_spec / substitute / bind_params / execute_steps. Template expansion and the
store/route/companion wiring are covered in test_custom_tools.py.
"""

from __future__ import annotations

import json

import pytest

from toto_gateway import toolspec
from toto_gateway.toolspec import SpecError


def _good() -> dict:
    return {
        "tool": "morning_dashboard", "version": 1,
        "description": "Stamp my morning dashboard.",
        "params": {"focus": {"type": "str", "required": True, "description": "focus phrase"}},
        "steps": [
            {"call": "create_list", "args": {"title": "Today — {{params.focus}}"}},
            {"call": "put_object", "args": {"kind": "note", "object_id": "m",
                                            "payload": {"text": "Focus: {{params.focus}}"}}},
        ],
    }


# --- validation table -----------------------------------------------------------------------

def test_good_spec_validates():
    spec = _good()
    assert toolspec.validate_spec(spec) is spec


@pytest.mark.parametrize("mutate, needle", [
    (lambda s: s.update(tool="Bad-Name"), "match"),
    (lambda s: s.update(tool="ab"), "match"),                     # too short (< 3)
    (lambda s: s.update(tool="read_canvas"), "collides"),         # builtin collision
    (lambda s: s.update(tool="params"), "collides"),              # reserved word
    (lambda s: s.update(version=0), "version"),
    (lambda s: s.update(version=True), "version"),                # bool is not an int here
    (lambda s: s.update(description=""), "description"),
    (lambda s: s.update(description="x" * 501), "description"),
    (lambda s: s.update(params={f"prm{i}": {"type": "str", "description": "d"} for i in range(21)}),
     "20"),
    (lambda s: s.update(params={"pri": {"type": "widget", "description": "d"}}), "type"),
    (lambda s: s.update(params={"pri": {"type": "str"}}), "description"),
    (lambda s: s.update(steps=[]), "1-10"),
    (lambda s: s.update(steps=[{"call": "create_list", "args": {}}] * 11), "1-10"),
    (lambda s: s.update(steps=[{"call": "delete_object", "args": {}}]), "composable"),
    (lambda s: s.update(steps=[{"call": "spawn_session", "args": {}}]), "composable"),
    (lambda s: s.update(steps=[{"call": "create_list", "args": {"t": "{{params.nope}}"}}]),
     "unknown param"),
])
def test_bad_specs_rejected(mutate, needle):
    spec = _good()
    mutate(spec)
    with pytest.raises(SpecError) as e:
        toolspec.validate_spec(spec)
    assert needle in str(e.value)


def test_oversize_spec_rejected():
    spec = _good()
    spec["steps"][0]["args"]["blob"] = "x" * (toolspec.MAX_SPEC_BYTES + 1)
    with pytest.raises(SpecError) as e:
        toolspec.validate_spec(spec)
    assert "bytes" in str(e.value)


# --- substitution + binding ------------------------------------------------------------------

def test_substitute_recurses_strings_only():
    params = {"focus": "ship it", "n": "3"}
    out = toolspec.substitute(
        {"title": "Today — {{params.focus}}", "count": 5,
         "nested": {"k": "x{{params.n}}y"}, "list": ["a", "{{params.focus}}"]}, params)
    assert out == {"title": "Today — ship it", "count": 5,
                   "nested": {"k": "x3y"}, "list": ["a", "ship it"]}


def test_bind_params_required_and_optional():
    spec = {"params": {"a": {"type": "str", "required": True, "description": "d"},
                       "b": {"type": "bool", "description": "d"}}}
    bound = toolspec.bind_params(spec, {"a": "hi", "b": True})
    assert bound == {"a": "hi", "b": "true"}
    # optional absent → empty string; required absent → refusal (no partial run)
    assert toolspec.bind_params(spec, {"a": "hi"}) == {"a": "hi", "b": ""}
    with pytest.raises(SpecError) as e:
        toolspec.bind_params(spec, {"b": False})
    assert "required" in str(e.value)


def test_substitute_unknown_ref_raises():
    with pytest.raises(SpecError):
        toolspec.substitute("{{params.ghost}}", {"real": "1"})


# --- execution ------------------------------------------------------------------------------

async def test_execute_steps_happy_path_returns_receipt():
    spec = _good()
    calls = []

    async def dispatch(call, args):
        calls.append((call, args))
        return f"{call} ok"

    scope = frozenset(toolspec.COMPOSABLE)  # everything composable is in scope
    receipt = await toolspec.execute_steps(spec, {"focus": "launch"}, scope, dispatch)
    assert calls[0] == ("create_list", {"title": "Today — launch"})
    assert calls[1][1]["payload"] == {"text": "Focus: launch"}   # substitution reached the step
    assert "step 1 (create_list): create_list ok" in receipt
    assert "step 2 (put_object)" in receipt


async def test_execute_steps_out_of_scope_step_refuses_and_stops():
    spec = {"steps": [{"call": "create_list", "args": {}},
                      {"call": "put_object", "args": {}}]}
    ran = []

    async def dispatch(call, args):
        ran.append(call)
        return "ok"

    # scope excludes put_object → step 2 refuses, step 1 already ran, nothing after
    scope = frozenset({"create_list"})
    receipt = await toolspec.execute_steps(spec, {}, scope, dispatch)
    assert ran == ["create_list"]
    assert "step 2 (put_object): refused" in receipt and "stopped here" in receipt


async def test_execute_steps_raising_dispatch_stops():
    spec = {"steps": [{"call": "create_list", "args": {}},
                      {"call": "add_item", "args": {}}]}

    async def dispatch(call, args):
        if call == "create_list":
            raise RuntimeError("boom")
        return "ok"

    receipt = await toolspec.execute_steps(spec, {}, frozenset(toolspec.COMPOSABLE), dispatch)
    assert "failed — RuntimeError: boom" in receipt
    assert "add_item" not in receipt  # stopped before the second step


def test_module_self_check_runs():
    # the __main__ table is the ponytail check; assert its spec is JSON-serializable + bounded
    assert len(json.dumps(_good()).encode()) < toolspec.MAX_SPEC_BYTES
