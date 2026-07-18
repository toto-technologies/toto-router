"""Least-privilege tool scopes per agent surface — the single source of truth for which tools
each surface may run, with a hot-editable operator override layer.

Generalizes the two-seam gate `recall` already uses (omit from the prompt + refuse at dispatch)
to every tool on every surface: each surface declares a frozenset of allowed tool names, read at
prompt-build AND at dispatch, fail-closed. CORE 19 lives HERE now (it was a literal in
tests/test_tool_parity.py) so the parity tests and the runtime import ONE definition.

Overrides mirror driver/prompts.apply_overrides: an in-memory per-surface {add, remove} delta,
persisted to a JSON sibling file (TOTO_GW_SCOPES_FILE) so a dashboard edit survives a restart.
effective_scope(surface) = code default + adds - removes. A dashboard edit is operator-authed,
audited (routes/dev.scope_audit), and moves scope_hash(surface) — so a tool-set change is a
first-class, filterable LangSmith variable next to a prompt commit
(docs/plans/2026-07-05-tool-segmentation.md).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# The 19 canonical tools every user-facing surface exposes (docs/handoff-admin-tool-dashboard.md
# §2). Was the CORE literal in tests/test_tool_parity.py; moved here so scope + parity share one
# source of truth. `recall` is a member but only *registered* when the memory plane is on — the
# clamp is registry-level (registered_tools), the same dynamic membership recall already had.
CORE: frozenset[str] = frozenset({
    "spawn_session", "check_status", "read_canvas", "place_on_canvas", "get_list",
    "create_list", "add_item", "set_item_status", "delete_item", "delete_object",
    "get_session_result", "memory_read", "memory_write", "memory_delete", "recall",
    "put_object", "edit_item", "continue_session", "enrich",
})

# MCP is a superset by design: CORE + low-level reads the terminal/agents use directly.
_MCP_EXTRA: frozenset[str] = frozenset({"get_session", "list_sessions", "wait_session", "list_lists"})

# Model advisor: benchmark-backed "which model for X" recommendation. NEVER in CORE (the parity set
# stays 19) and companion-ONLY — a chat-shaped advisory DIALOGUE tool, not a low-level verb, so it
# never joins mcp/pi. Always-on (no flag: read-only public-benchmark advice, not routing), so it
# lives statically in the two companion scopes rather than as a flag-gated union like CUSTOM/EXTERNAL.
ADVISOR: frozenset[str] = frozenset({"recommend_model"})

# Documents: save markdown to the user's Documents page. Same posture as ADVISOR — a chat-shaped
# verb, companion-ONLY (never mcp/pi), always on (writes only the caller's own store prefix).
DOCUMENTS: frozenset[str] = frozenset({"save_document"})

SCOPES: dict[str, frozenset[str]] = {
    "companion.text": CORE | ADVISOR | DOCUMENTS,
    "companion.voice": CORE | ADVISOR | DOCUMENTS,  # decision 1: alias of text; the delta is model + typed
                                      # delete-confirm, NOT the tool set (kept so the seam exists)
    "extract": frozenset({"memory_write"}),
    "dream": frozenset(),             # content-plane docs only — zero canvas/list/memory tools
    "driver": frozenset(),            # tools_required is a routing hint, not action tools
    "mcp": CORE | _MCP_EXTRA,
    "pi": CORE,
}

# spawn_local_swarm: env-gated (TOTO_MCP_LOCAL_SWARM=1), never in a default scope (decision 5).
# Declared here so parity treats its absence from CORE as correct, not drift.
GATED: dict[str, frozenset[str]] = {"mcp": frozenset({"spawn_local_swarm"})}

# Custom-tools contract (docs/plans/2026-07-06-tool-contract.md §6). NEVER in CORE — flag-gated
# (TOTO_GW_CUSTOM_TOOLS): the four authoring/dispatch verbs join every USER surface's scope when
# the flag is on, and no job surface (extract/dream/driver) ever gets them. Union happens in
# effective_scope so a flag flip moves scope_hash exactly like a prompt/scope commit.
CUSTOM: frozenset[str] = frozenset({
    "create_tool", "delete_tool", "run_custom_tool", "instantiate_template"})
_CUSTOM_SURFACES = ("companion.text", "companion.voice", "mcp", "pi")

# External tools (docs/plans/2026-07-06-pipedream-assessment.md) — the FIRST external tool surface,
# read-only reads of a user's connected third-party accounts via the Pipedream Connect pilot. NEVER
# in CORE; joins every USER surface (never a job surface) when the pilot is enabled AND configured.
# Gated on the pilot's OWN condition (pipedream.enabled = TOTO_GW_PIPEDREAM + creds), not a second
# flag — the surface and its data source flip together. Union in effective_scope so enabling it moves
# scope_hash exactly like any scope commit; when it's in scope it's also composable (below).
EXTERNAL: frozenset[str] = frozenset({"calendar_events"})
_EXTERNAL_SURFACES = ("companion.text", "companion.voice", "mcp", "pi")

# The composition allow-list (contract §1/§6): what a custom tool's steps may call. Destructive
# verbs stay OUT (their two-step confirm can't nest) and spend-y verbs stay under the human's hand;
# instantiate_template (server-side expansion) is IN. Code-only in v1 — widening it moves scope_hash.
# EXTERNAL reads join it dynamically (composable()) when the external surface is enabled.
COMPOSABLE: frozenset[str] = (
    CORE - {"delete_item", "delete_object", "memory_delete", "spawn_session", "continue_session"}
) | {"instantiate_template"}


def composable() -> frozenset[str]:
    """COMPOSABLE at CALL time: the static base plus EXTERNAL reads when the external surface is on,
    so a custom tool's steps may include a calendar read (contract §1). Flag-off → the base only —
    a spec with an EXTERNAL step then fails validation, exactly as if the tool didn't exist."""
    return (COMPOSABLE | EXTERNAL) if _external_tools_enabled() else COMPOSABLE


def _custom_tools_enabled() -> bool:
    """The TOTO_GW_CUSTOM_TOOLS flag, read live (lru_cached settings). Lazy import dodges any
    import-order coupling; off (default) → CUSTOM never enters a scope."""
    from .config import get_settings
    return bool(get_settings().custom_tools)


def _external_tools_enabled() -> bool:
    """The Pipedream pilot's own enabled() condition, read live. Lazy import dodges import-order
    coupling; off/unconfigured (default) → EXTERNAL never enters a scope and is never composable."""
    from . import pipedream
    from .config import get_settings
    return pipedream.enabled(get_settings())

_OVERRIDES: dict[str, dict[str, list[str]]] = {}  # surface -> {"add": [...], "remove": [...]}


def effective_scope(surface: str) -> frozenset[str]:
    """The surface's allowed tools: code default + operator adds - operator removes. KeyError on
    an unknown surface IS the fail-closed answer (dispatch treats 'no scope' as 'refuse all')."""
    base = SCOPES[surface]
    if surface in _CUSTOM_SURFACES and _custom_tools_enabled():
        base = base | CUSTOM  # flag-gated union — user surfaces only, never a job surface
    if surface in _EXTERNAL_SURFACES and _external_tools_enabled():
        base = base | EXTERNAL  # pilot-gated union — user surfaces only, never a job surface
    ov = _OVERRIDES.get(surface) or {}
    return frozenset((base | set(ov.get("add", []))) - set(ov.get("remove", [])))


def scope_hash(surface: str) -> str:
    """Short content hash over the EFFECTIVE scope — a hot-edit moves it, so a tool-set change is
    filterable in LangSmith exactly like a prompt commit (sorted set, sha256, first 12 hex)."""
    blob = ",".join(sorted(effective_scope(surface)))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def default_scope(surface: str) -> frozenset[str]:
    """Pristine (code-defined) scope, regardless of active overrides."""
    return SCOPES[surface]


def active_scope_overrides() -> dict[str, dict[str, list[str]]]:
    """The currently-applied override deltas (for persistence + the dashboard diff view)."""
    return {s: {"add": list(d.get("add", [])), "remove": list(d.get("remove", []))}
            for s, d in _OVERRIDES.items()}


def _diff(surface: str, desired: set[str]) -> dict[str, list[str]] | None:
    """Minimal add/remove delta of `desired` from the code default; None when they match."""
    base = set(SCOPES[surface])
    add, remove = sorted(desired - base), sorted(base - desired)
    return {"add": add, "remove": remove} if (add or remove) else None


def set_membership(surface: str, tool: str, present: bool) -> None:
    """Add (present=True) or remove (present=False) one tool from the surface's effective scope,
    stored as the minimal diff from the code default. Toggling back to the default clears the
    override row so scope_hash returns to its pristine value."""
    if surface not in SCOPES:
        raise ValueError(f"unknown surface {surface!r} — known: {sorted(SCOPES)}")
    desired = set(effective_scope(surface))
    desired.add(tool) if present else desired.discard(tool)
    d = _diff(surface, desired)
    if d is None:
        _OVERRIDES.pop(surface, None)
    else:
        _OVERRIDES[surface] = d


def reset_scope(surface: str) -> None:
    """Drop this surface's override — back to the code default."""
    if surface not in SCOPES:
        raise ValueError(f"unknown surface {surface!r} — known: {sorted(SCOPES)}")
    _OVERRIDES.pop(surface, None)


def apply_scope_overrides(overrides: dict) -> None:
    """Full-replace the override layer (validated), effective immediately. {} restores the pristine
    code defaults — same semantics as prompts.apply_overrides. Raises ValueError loudly on an
    unknown surface so a malformed file fails at boot, never a silent wrong scope."""
    unknown = set(overrides) - set(SCOPES)
    if unknown:
        raise ValueError(f"unknown surface(s) {sorted(unknown)} — known: {sorted(SCOPES)}")
    _OVERRIDES.clear()
    for surface, diff in overrides.items():
        add = sorted({str(t) for t in (diff or {}).get("add", [])})
        remove = sorted({str(t) for t in (diff or {}).get("remove", [])})
        if add or remove:
            _OVERRIDES[surface] = {"add": add, "remove": remove}


def load_scope_overrides_file(path: str) -> dict:
    """Load + apply TOTO_GW_SCOPES_FILE. Missing → no-op (code defaults, byte-identical scopes);
    malformed → ValueError, never a silent fallback."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"scope overrides file {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"scope overrides file {path} must be a JSON object "
                         f"{{surface: {{add, remove}}}}, got {type(data).__name__}")
    apply_scope_overrides(data)
    return data


def registered_tools(surface: str) -> frozenset[str]:
    """The tools a surface can ACTUALLY run — its live registry — for the scope ⊆ registered guard
    (a scope must never name a tool the surface can't dispatch) and the dashboard add-list. Lazy
    imports dodge an import cycle (companion.prompts imports this module). Jobs (extract/dream/
    driver) have no tool loop, so their registry IS their declared scope."""
    if surface in ("companion.text", "companion.voice"):
        from .tool_names import TOOL_NAMES  # leaf constant module — no companion import (seam)
        return frozenset(TOOL_NAMES)
    if surface == "mcp":
        import sys
        sp = str(Path(__file__).resolve().parent.parent / "scripts")
        if sp not in sys.path:
            sys.path.insert(0, sp)
        import toto_mcp
        return frozenset(toto_mcp.TOOLS)  # includes spawn_local_swarm iff the flag is set
    if surface == "pi":
        import re
        ts = (Path(__file__).resolve().parent.parent
              / "clients" / "pi-toto" / "src" / "tools.ts").read_text()
        return frozenset(re.findall(r'name:\s*"(\w+)"', ts))
    return SCOPES[surface]


if __name__ == "__main__":  # ponytail: one runnable self-check of the pure scope math (no I/O)
    assert effective_scope("companion.text") == CORE | ADVISOR | DOCUMENTS  # companion-only extras, always on
    assert "recommend_model" not in effective_scope("mcp")       # never on mcp/pi
    assert "save_document" not in effective_scope("mcp") and "save_document" not in effective_scope("pi")
    assert "recommend_model" not in effective_scope("pi")
    assert effective_scope("dream") == frozenset()
    assert effective_scope("extract") == frozenset({"memory_write"})
    h0 = scope_hash("companion.text")
    set_membership("companion.text", "delete_item", False)          # narrow
    assert "delete_item" not in effective_scope("companion.text")
    assert scope_hash("companion.text") != h0                        # hash moved
    assert active_scope_overrides()["companion.text"]["remove"] == ["delete_item"]
    set_membership("companion.text", "delete_item", True)            # widen back to default
    assert effective_scope("companion.text") == CORE | ADVISOR | DOCUMENTS
    assert active_scope_overrides() == {}                            # diff collapsed → override gone
    assert scope_hash("companion.text") == h0                        # hash restored
    apply_scope_overrides({"mcp": {"remove": ["spawn_session"]}})
    assert "spawn_session" not in effective_scope("mcp")
    apply_scope_overrides({})                                        # {} = pristine
    assert effective_scope("mcp") == CORE | _MCP_EXTRA
    assert "calendar_events" not in effective_scope("companion.text")  # pilot off by default
    assert composable() == COMPOSABLE                                  # EXTERNAL absent when off
    try:
        set_membership("nope", "x", True)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown surface must raise")
    print("tool_scopes self-check ok")
