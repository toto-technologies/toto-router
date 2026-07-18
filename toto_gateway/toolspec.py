"""Toto Tool Contract (TTC v1) — validate / substitute / execute a declarative custom tool.

A custom tool is ONE JSON document composing canonical tools (docs/plans/2026-07-06-tool-contract.md
§1). There is no code-execution sandbox in v1: this module is pure validation, `{{params.x}}`
substitution, and a linear walk of steps through a caller-provided async dispatch fn. Every rule
is enforced at CREATE and at RUN — an imported spec gets no bypass (§1). Template expansion
(§4) lives here too so the REST route and the companion tool share ONE code path.

Deliberately dependency-light: it takes `runs`/`dispatch`/`scope` as arguments rather than
importing the store or the companion, so the same functions back the route and the agent.
"""

from __future__ import annotations

import json
import re

from .tool_scopes import COMPOSABLE, CORE, GATED, _MCP_EXTRA, composable

# --- caps + shapes (the AX levers live here, per §6) ------------------------------------------
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
PARAM_TYPES = {"str", "num", "bool"}
MAX_PARAMS = 20
MAX_STEPS = 10
MAX_SPEC_BYTES = 8 * 1024
MAX_TOOLS_PER_USER = 50
MAX_TEMPLATE_OBJECTS = 20
MAX_PAYLOAD_BYTES = 32 * 1024  # mirrors routes/objects.MAX_PAYLOAD_BYTES (kept local, not imported)

# The data-only canvas kind registry. Lives HERE (core — template expansion §4 validates against
# it) rather than routes/objects, which is an app-plane module the OSS export deletes; objects.py
# imports it back from this module. Grows one line at a time.
OBJECT_KINDS = {"note", "clip", "metric", "container", "card", "board", "chart", "calendar",
                "timeline", "template", "htmlview"}

# Reserved words a custom tool name may never take — the contract's own JSON keys plus the two
# spec-level shells. Builtin tool names are checked separately (see _builtin_names).
RESERVED = frozenset({"tool", "version", "description", "params", "steps", "call", "args", "spec"})

# {{params.<name>}} — only inside string values (§1). name grammar matches a param key.
_SUB_RE = re.compile(r"\{\{params\.([a-z][a-z0-9_]*)\}\}")


class SpecError(ValueError):
    """A validation failure — the message is model- and human-facing (surfaced verbatim)."""


def _builtin_names() -> frozenset[str]:
    """Every canonical tool name across the three registries (companion/MCP/pi) — a custom tool
    name must not collide with any (§1). Derived from tool_scopes (no toto_mcp/tools.ts read):
    CORE is the shared floor, _MCP_EXTRA + GATED are MCP's additions, pi == CORE, and CUSTOM's own
    four names are builtins too. TOOL_NAMES comes from the core leaf registry (tool_names.py)."""
    from .tool_names import TOOL_NAMES
    gated = frozenset().union(*GATED.values()) if GATED else frozenset()
    return frozenset(TOOL_NAMES) | CORE | _MCP_EXTRA | gated


# --- validation -------------------------------------------------------------------------------

def validate_spec(spec: object) -> dict:
    """Return the spec unchanged if it satisfies every §1 rule, else raise SpecError. Enforced at
    create AND at run. Does NOT check per-user count (needs the store) or run-time scope (the ∩
    with the caller's effective scope happens in execute_steps)."""
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object")
    if len(json.dumps(spec, default=str).encode()) > MAX_SPEC_BYTES:
        raise SpecError(f"spec exceeds {MAX_SPEC_BYTES} bytes")

    name = spec.get("tool")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise SpecError("tool name must match ^[a-z][a-z0-9_]{2,40}$")
    if name in RESERVED or name in _builtin_names():
        raise SpecError(f"tool name {name!r} collides with a builtin/reserved name")

    version = spec.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise SpecError("version must be an int >= 1")

    desc = spec.get("description")
    if not isinstance(desc, str) or not (1 <= len(desc) <= 500):
        raise SpecError("description must be 1-500 chars")

    params = spec.get("params", {})
    if not isinstance(params, dict) or len(params) > MAX_PARAMS:
        raise SpecError(f"params must be an object with <= {MAX_PARAMS} entries")
    for pname, pdef in params.items():
        if not isinstance(pname, str) or not NAME_RE.match(pname):
            raise SpecError(f"param name {pname!r} must match ^[a-z][a-z0-9_]{{2,40}}$")
        if not isinstance(pdef, dict) or pdef.get("type") not in PARAM_TYPES:
            raise SpecError(f"param {pname!r} needs a type in {sorted(PARAM_TYPES)}")
        if not isinstance(pdef.get("description"), str) or not pdef["description"].strip():
            raise SpecError(f"param {pname!r} needs a description")

    steps = spec.get("steps")
    if not isinstance(steps, list) or not (1 <= len(steps) <= MAX_STEPS):
        raise SpecError(f"steps must be a list of 1-{MAX_STEPS} entries")
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise SpecError(f"step {i} must be an object")
        call = step.get("call")
        allowed = composable()  # call-time: includes EXTERNAL reads when that surface is enabled
        if call not in allowed:
            raise SpecError(f"step {i}: {call!r} is not composable "
                            f"(allowed: {sorted(allowed)})")
        if not isinstance(step.get("args", {}), dict):
            raise SpecError(f"step {i}: args must be an object")

    _check_substitutions(spec, set(params))
    return spec


def _check_substitutions(spec: dict, declared: set[str]) -> None:
    """Every {{params.x}} anywhere in the steps args must reference a DECLARED param (§1 —
    unknown ref is a validation error, caught at create so a broken spec never reaches run)."""
    for step in spec["steps"]:
        for ref in _refs(step.get("args", {})):
            if ref not in declared:
                raise SpecError(f"unknown param reference {{{{params.{ref}}}}} "
                                f"(declared: {sorted(declared)})")


def _refs(value: object) -> set[str]:
    """Every param name referenced by {{params.x}} in the string values reachable from `value`."""
    out: set[str] = set()
    if isinstance(value, str):
        out.update(_SUB_RE.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            out |= _refs(v)
    elif isinstance(value, list):
        for v in value:
            out |= _refs(v)
    return out


# --- param binding + substitution -------------------------------------------------------------

def bind_params(spec: dict, provided: object) -> dict:
    """The concrete param values for a run: every REQUIRED param must be present (else refusal, no
    partial execution — §1/§2); absent optional params render as an empty string. Values are
    coerced to their declared type for substitution. Raises SpecError on a missing required param."""
    provided = provided if isinstance(provided, dict) else {}
    out: dict[str, str] = {}
    for pname, pdef in (spec.get("params") or {}).items():
        if pname in provided and provided[pname] is not None:
            out[pname] = _as_str(provided[pname])
        elif pdef.get("required"):
            raise SpecError(f"missing required param {pname!r}")
        else:
            out[pname] = ""
    return out


def _as_str(v: object) -> str:
    """Render a param value for string interpolation — bools as true/false, everything else str()."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def substitute(value: object, params: dict[str, str]) -> object:
    """Recursively interpolate {{params.x}} in every string reachable from `value` (§1: string
    values only). `params` is already bound (bind_params), so every declared ref resolves; a
    reference to an undeclared param is impossible post-validation but guarded here too."""
    if isinstance(value, str):
        def _repl(m: re.Match) -> str:
            key = m.group(1)
            if key not in params:
                raise SpecError(f"unknown param reference {{{{params.{key}}}}}")
            return params[key]
        return _SUB_RE.sub(_repl, value)
    if isinstance(value, dict):
        return {k: substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, params) for v in value]
    return value


# --- execution --------------------------------------------------------------------------------

async def execute_steps(spec: dict, params: dict[str, str], scope, dispatch) -> str:
    """Run a validated spec's steps linearly through `dispatch(call, args) -> str`, returning a
    receipt of per-step results. Each step's call must be in COMPOSABLE ∩ `scope` at run time
    (§2) — an out-of-scope or non-composable call REFUSES and stops the sequence. A dispatch that
    raises also stops it. No rollback: the completed steps stand (the same upserts the model could
    have issued directly). The whole receipt returns to the model as one tool result."""
    allowed = composable() & frozenset(scope)
    lines: list[str] = []
    for i, step in enumerate(spec["steps"], 1):
        call = step["call"]
        if call not in allowed:
            lines.append(f"step {i} ({call}): refused — not available in this scope; stopped here.")
            break
        args = substitute(step.get("args", {}), params)
        try:
            result = await dispatch(call, args)
        except Exception as exc:  # a raising dispatch stops the run, narratably
            lines.append(f"step {i} ({call}): failed — {type(exc).__name__}: {exc}; stopped here.")
            break
        lines.append(f"step {i} ({call}): {result}")
    return "\n".join(lines)


# --- template expansion (§4) ------------------------------------------------------------------

def validate_template(payload: object) -> dict:
    """A canvas template payload (§4): name/description, optional params, optional container, and
    1-20 object entries whose kinds are canvas object kinds ∪ {list}. Substitution refs must be
    declared. Raises SpecError. `object_kinds` is checked at expand time (needs the live set)."""
    if not isinstance(payload, dict):
        raise SpecError("template payload must be a JSON object")
    objects = payload.get("objects")
    if not isinstance(objects, list) or not (1 <= len(objects) <= MAX_TEMPLATE_OBJECTS):
        raise SpecError(f"template needs 1-{MAX_TEMPLATE_OBJECTS} objects")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise SpecError("template params must be an object")
    declared = set(params)
    for i, obj in enumerate(objects):
        if not isinstance(obj, dict) or not isinstance(obj.get("kind"), str):
            raise SpecError(f"template object {i} needs a string kind")
        for ref in _refs({k: v for k, v in obj.items() if k in ("title", "payload")}):
            if ref not in declared:
                raise SpecError(f"template object {i}: unknown param reference "
                                f"{{{{params.{ref}}}}}")
    return payload


async def expand_template(runs, user_id, template_id, params, *, object_kinds,
                          list_kind_ok=True, x=None, y=None, actor=None):
    """Instantiate a template into REAL objects (§4), the ONE path shared by the REST route and the
    companion tool. Reads the template owner-scoped (cross-user = miss), validates, substitutes
    params, then for each entry creates a fresh object of its own kind stamped with `user_id`,
    positioned offset from an anchor (auto right-edge when x/y absent — the _place_on_canvas
    behavior). An optional `container` wraps the group via the existing parent mechanism. Returns
    {"container": id|None, "objects": [{kind, object_id}], "count"} or {"error": msg}."""
    import uuid

    tmpl = next((o for o in await runs.get_objects("template", user_id=user_id)
                 if o["object_id"] == template_id), None)
    if tmpl is None:
        return {"error": f"No template {template_id} visible to this user."}
    try:
        payload = validate_template(tmpl["payload"])
        bound = bind_params({"params": payload.get("params", {})}, params)
    except SpecError as e:
        return {"error": str(e)}

    entries = substitute(payload["objects"], bound)
    allowed_kinds = set(object_kinds) | ({"list"} if list_kind_ok else set())
    for entry in entries:
        if entry["kind"] not in allowed_kinds:
            return {"error": f"template kind {entry['kind']!r} must be one of "
                             f"{sorted(allowed_kinds)}"}
        pl = entry.get("payload")
        if pl is not None and len(json.dumps(pl, default=str).encode()) > MAX_PAYLOAD_BYTES:
            return {"error": f"template object payload exceeds {MAX_PAYLOAD_BYTES} bytes"}

    # Anchor: explicit coords, else right-edge of the world surface (mirrors _place_on_canvas).
    if isinstance(x, (int, float)) and isinstance(y, (int, float)):
        ax, ay = float(x), float(y)
    else:
        rows = await runs.get_positions(user_id=user_id, parent="")
        ax = (max(r["x"] for r in rows) + 420.0) if rows else 0.0
        ay = min((r["y"] for r in rows), default=0.0)

    # Optional container wrap: the group hangs off a fresh container placed at the anchor; children
    # then live on the container's own surface (parent=container_id) at their dx/dy origin.
    container_id = None
    if isinstance(payload.get("container"), dict):
        cfg = substitute(payload["container"], bound)
        container_id = uuid.uuid4().hex[:12]
        await runs.put_object("container", container_id,
                              {"name": cfg.get("name", ""), "mode": cfg.get("mode", "shelf")},
                              user_id=user_id, actor=actor)
        await runs.set_positions([{"kind": "container", "object_id": container_id,
                                   "x": ax, "y": ay, "z": 0, "parent": ""}],
                                 user_id=user_id, actor=actor)
        base_x = base_y = 0.0
        parent = container_id
    else:
        base_x, base_y, parent = ax, ay, ""

    created: list[dict] = []
    for entry in entries:
        kind, oid = entry["kind"], uuid.uuid4().hex[:12]
        if kind == "list":
            await runs.create_list(oid, str(entry.get("title") or "Untitled"), user_id=user_id)
        else:
            await runs.put_object(kind, oid, entry.get("payload") or {}, user_id=user_id, actor=actor)
        pos = {"kind": kind, "object_id": oid,
               "x": base_x + float(entry.get("dx", 0)), "y": base_y + float(entry.get("dy", 0)),
               "z": 0, "parent": parent}
        for dim in ("w", "h"):
            if isinstance(entry.get(dim), (int, float)):
                pos[dim] = float(entry[dim])
        await runs.set_positions([pos], user_id=user_id, actor=actor)
        created.append({"kind": kind, "object_id": oid})
    return {"container": container_id, "objects": created, "count": len(created)}


if __name__ == "__main__":  # ponytail: one runnable self-check of the pure spec math (no I/O)
    good = {"tool": "morning_dashboard", "version": 1, "description": "d",
            "params": {"focus": {"type": "str", "required": True, "description": "f"}},
            "steps": [{"call": "create_list", "args": {"title": "Today — {{params.focus}}"}}]}
    assert validate_spec(good) is good
    for bad, why in [
        ({**good, "tool": "Bad"}, "regex"),
        ({**good, "tool": "read_canvas"}, "collision"),
        ({**good, "version": 0}, "version"),
        ({**good, "description": ""}, "desc"),
        ({**good, "steps": [{"call": "delete_object", "args": {}}]}, "non-composable"),
        ({**good, "steps": [{"call": "x", "args": {}}] * 11}, "too many steps"),
        ({**good, "steps": [{"call": "create_list", "args": {"t": "{{params.nope}}"}}]}, "unknown ref"),
    ]:
        try:
            validate_spec(bad)
        except SpecError:
            pass
        else:
            raise AssertionError(f"expected SpecError: {why}")
    # substitution + bind
    p = bind_params(good, {"focus": "ship"})
    assert substitute({"t": "Today — {{params.focus}}"}, p) == {"t": "Today — ship"}
    try:
        bind_params(good, {})
    except SpecError:
        pass
    else:
        raise AssertionError("missing required must raise")
    print("toolspec self-check ok")
