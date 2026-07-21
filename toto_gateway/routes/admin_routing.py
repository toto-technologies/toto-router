"""GET/PUT routing-policy admin API — org-default + per-team tag->model routing (C6).

Makes NVIDIA's task-type routing (routing/labels.yaml — the GLOBAL tag->model map) editable PER
TEAM as an overlay. Gated on require_role("admin") and org-scoped: an admin may only read/set a
policy for a team in THEIR org (the operator super-credential is above org scope). Mirrors
admin_catalog.py: Depends(require_role), the {"error": {...}} shape, GET reads the EFFECTIVE view a
UI renders, PUT full-replaces the overlay + writes an admin:* audit row.

The org-default endpoint stores the policy under the org_id key. That policy applies to teamless
owner/API-token traffic, which is the common pi `toto/smart` path. Team endpoints still win for
callers attached to a team.

GET returns the full label table with clear auto-selection: every label with its desc (from
labels.yaml), the currently-bound model (team override or global default), whether it's overridden,
plus the team's optimize preset (or the global default when unset). PUT validates fail-closed —
every bound model id must exist in the running catalog (a team can't route to a phantom model), the
optimize preset must be valid, and privacy-governed `redact` can't be bound. `other` is bindable as
the explicit catch-all fallback.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..auth import ROUTING_OPTIMIZE
from ..routing.labels import LabelBindings
from .admin_catalog_adoptions import scope_effective_catalog as _scope_catalog
from .admin_usage import _scope_org
from .deps import Identity, require_read_role, require_role

router = APIRouter()

# Custom task-type (CT) name = a slug: lowercase alnum + underscore, starting with a letter. Kept
# tight so a custom label reads like a builtin (invoice_parsing), never a free-text sentence that
# would bloat the classifier prompt or collide oddly with parsing.
CUSTOM_LABEL_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")

# The global default optimize preset (classify() falls back to "balanced" when none is set) — the
# display value the UI shows for a team that hasn't overridden it.
GLOBAL_OPTIMIZE = "balanced"

# Labels that stay unbound by DOCTRINE: `redact` is governed by data_policy/guard residency, never a
# cloud binding. `other` IS bindable — it's the designated catch-all/fallback knob (a bound `other`
# is what smart.fallback_model uses for unclassified + classify-failed requests), so an operator can
# say "route everything else HERE" instead of falling to benchmark-best. A PUT binding `redact` is
# rejected; the GET view marks it bindable:false so the UI disables that one row.
NON_BINDABLE = frozenset({"redact"})


def _error(status: int, message: str, err_type: str, code: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": err_type, "code": code}})


async def _team_in_scope(request: Request, identity: Identity, team_id: str):
    """(team_row, error_response) — exactly one is non-None. Fail-closed: an unknown team OR a team
    in another org both return 404 (not 403), so a scoped admin can't probe another org's team ids
    (no cross-org existence leak). The operator is above org scope."""
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        return None, _error(503, "auth store unavailable", "config_error")
    team = await auth.get_team(team_id)
    if team is None or (not identity.is_operator and team["org_id"] != identity.org_id):
        return None, _error(404, "team not found", "invalid_request_error", "team_not_found")
    return team, None


def _global_bindings(request: Request) -> LabelBindings:
    """The shipped global tag->model map (routing/labels.yaml, or the TOTO_GW_LABEL_BINDINGS
    override). Loaded fresh — cheap YAML — so the admin table renders even when label routing is
    runtime-disabled (reduced catalog); the descs + defaults are what the UI needs regardless."""
    settings = request.app.state.settings
    return LabelBindings(getattr(settings, "label_bindings", "") or None)


def _catalog(request: Request):
    gw = getattr(request.app.state, "gateway", None)
    return getattr(gw, "catalog", None)  # base-catalog-ok: fallback for reduced test apps only — handlers pass _scope_catalog


def _benchmarks(request: Request):
    """The gateway's benchmark table (or None on a reduced test app) — used to compute the per-label
    advisor pick (`benchmark_pick`) the console shows next to a bound row."""
    gw = getattr(request.app.state, "gateway", None)
    return getattr(gw, "_benchmarks", None)


# A description under this many words is rejected outright — the classifier can't reliably match
# one or two words against a request. The console warns (softly) below ~5 words; the hard floor
# here is deliberately lower so a terse-but-real description still saves.
MIN_DESC_WORDS = 3

# The writing rules, verbatim in the 400 body: the error message is where an agent creating task
# types programmatically actually learns the shape.
DESC_GUIDANCE = (
    "Describe the REQUEST behaviorally in one focused sentence — what the user asks for, concrete "
    "enough to be distinguishable from the built-in task types (a description that overlaps "
    "summarization will steal its traffic). Good: 'writing or explaining SQL queries against a "
    "relational database'. Bad: 'database stuff'.")


def _validate_custom_labels(raw, vocab: set[str], catalog) -> tuple[list[dict], JSONResponse | None]:
    """Validate a PUT's custom_labels (CT), fail-closed. Returns (clean_list, None) or ([], error).
    Each entry is {name, desc, model}: name is a slug, lowercase-unique, NOT a builtin labels.yaml
    label (no shadowing the closed vocab); model must exist in the running catalog (a team can't
    route to a phantom model).

    `desc` IS the routing behavior: the classifier reads every prompt and matches it against these
    descriptions, so it must describe the request behaviorally in one focused sentence,
    distinguishable from the built-in types — e.g. "writing or explaining SQL queries against a
    relational database", never "database stuff". Required; fewer than MIN_DESC_WORDS words is
    rejected with the guidance in the error body."""
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], _error(400, "custom_labels must be a list of {name, desc, model}",
                          "invalid_request_error", "invalid_custom_labels")
    seen: set[str] = set()
    clean: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            return [], _error(400, "each custom label must be an object {name, desc, model}",
                              "invalid_request_error", "invalid_custom_labels")
        name = c.get("name")
        if not isinstance(name, str) or not CUSTOM_LABEL_SLUG.match(name):
            return [], _error(400, f"custom label name {name!r} must be a lowercase slug "
                              "(letters, digits, underscore, starting with a letter)",
                              "invalid_request_error", "invalid_custom_label_name")
        if name in vocab:
            return [], _error(400, f"custom label {name!r} collides with a builtin label",
                              "invalid_request_error", "custom_label_collision")
        if name in seen:
            return [], _error(400, f"duplicate custom label {name!r}",
                              "invalid_request_error", "duplicate_custom_label")
        desc = c.get("desc")
        if not isinstance(desc, str) or not desc.strip():
            return [], _error(400, f"custom label {name!r} needs a desc — the classifier matches "
                              f"every prompt against it, so the description IS the routing "
                              f"behavior. {DESC_GUIDANCE}",
                              "invalid_request_error", "invalid_custom_label_desc")
        if len(desc.split()) < MIN_DESC_WORDS:
            return [], _error(400, f"custom label {name!r} desc {desc.strip()!r} is too thin for "
                              f"the classifier to match reliably. {DESC_GUIDANCE}",
                              "invalid_request_error", "invalid_custom_label_desc")
        model = c.get("model")
        if not isinstance(model, str) or catalog is None or catalog.get(model) is None:
            return [], _error(400, f"unknown catalog model {model!r} for custom label {name!r}",
                              "invalid_request_error", "unknown_model")
        seen.add(name)
        clean.append({"name": name, "desc": desc.strip(), "model": model})
    return clean, None


def _routing_view(request: Request, policy: dict | None, catalog=None) -> tuple[list[dict], list[dict]]:
    """(label rows, custom labels) for a routing-policy GET — the merged view of the global vocab
    plus this scope's overlay + invented task types. Shared by the team and org endpoints.

    The view reports DISPATCH TRUTH, resolved through the catalog exactly like smart._binding_entry does:
    a stored binding is displayed as the entry it actually resolves to (so a legacy-alias binding
    shows the real model name), and a binding whose model has been RETIRED from the catalog is
    flagged `stale: true` with `model` falling back to the default — because that is what routing
    actually does with it. The stored key stays visible as `bound_model` so an admin can see what
    to clean up. Without this, the UI showed retired bindings as if they were still routing."""
    overlay = (policy or {}).get("bindings") or {}
    labels = _global_bindings(request)
    if catalog is None:  # callers pass the scope's effective catalog (_scope_catalog); base fallback
        catalog = _catalog(request)
    benchmarks = _benchmarks(request)

    def _advisor(label, routed_model):
        """The optimizer's benchmark pick for `label`, shown only when it DIFFERS from the model
        the scope actually routes to (the advisor demoted below an explicit binding). None → the
        binding IS the benchmark best, or no benchmark category / no data."""
        if catalog is None or benchmarks is None:
            return None
        from ..routing.smart import benchmark_pick_for
        pick = benchmark_pick_for(label, catalog, labels, benchmarks, None)
        return pick if pick and pick != routed_model else None

    def _resolved(model_id):
        """(display_id, stale): the catalog-resolved id for a stored key, or (None, True) when the
        model is gone. Pre-rename stored keys repair through normalize_legacy_id first (same
        read-boundary rule as dispatch). No catalog handle (reduced test app) → echo the key,
        never flag stale."""
        from ..catalog import normalize_legacy_id

        if not model_id:
            return None, False
        model_id = normalize_legacy_id(model_id)
        if catalog is None:
            return model_id, False
        entry = catalog.get(model_id)
        return (entry.id, False) if entry is not None else (None, True)

    rows = []
    for label in labels.vocab():
        desc = (labels.labels.get(label) or {}).get("desc")
        default_model = labels.model_for(label)       # global labels.yaml default (may be None)
        bindable = label not in NON_BINDABLE
        override = overlay.get(label) if bindable else None
        if override:  # pre-rename stored keys repair at read — retired ids never surface
            from ..catalog import normalize_legacy_id
            override = normalize_legacy_id(override)
        resolved, stale = _resolved(override)
        routed = resolved or default_model
        rows.append({
            "label": label,
            "desc": desc,
            "model": routed,                           # what routing actually uses for this scope
            "default_model": default_model,            # the global auto-selection
            "overridden": override is not None and not stale,
            "bound_model": override,                   # the stored key, normalized (None if unset)
            "benchmark_pick": _advisor(label, routed) if bindable else None,  # optimizer advice when it differs
            "stale": stale,
            "bindable": bindable,
            "custom": False,
        })
    custom = (policy or {}).get("custom_labels") or []
    for c in custom:
        resolved, stale = _resolved(c.get("model"))
        rows.append({"label": c.get("name"), "desc": c.get("desc"), "model": resolved,
                     "default_model": None, "overridden": True, "bound_model": c.get("model"),
                     "stale": stale, "bindable": True, "custom": True})
    return rows, custom


# Per-task-type stickiness hold cap: a memo hold longer than a day is almost certainly a mistake
# (a conversation that idle for a day is a new task), so reject it fail-closed at write time.
MAX_STICK_TTL = 86400.0


def _validate_stick_ttls(raw, vocab: set, custom_labels: list[dict]):
    """(stick_ttls, error). {label -> positive seconds ≤ MAX_STICK_TTL} for a KNOWN label (builtin or
    a custom task type from THIS body). Absent/empty → {} (flat holds). Fail-closed on a bad shape."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, _error(400, "stick_ttls must be an object of label -> seconds",
                            "invalid_request_error", "invalid_stick_ttls")
    known = vocab | {c.get("name") for c in custom_labels}
    out = {}
    for label, secs in raw.items():
        if label not in known:
            return None, _error(400, f"unknown label {label!r} in stick_ttls", "invalid_request_error",
                                "unknown_label")
        if not isinstance(secs, (int, float)) or isinstance(secs, bool) or not (0 < secs <= MAX_STICK_TTL):
            return None, _error(400, f"stick_ttls[{label!r}] must be a number in (0, {int(MAX_STICK_TTL)}]",
                                "invalid_request_error", "invalid_stick_ttls")
        out[label] = float(secs)
    return out, None


# Cache-policy (A8) knobs. Every field is optional and, when absent, inherits the global env default
# at request time — so the console can override ONE knob for an org without pinning the others.
CACHE_KEYS = frozenset({"preset", "auto_inject", "auto_inject_min_messages", "warmth_routing"})
# auto_inject_min_messages sanity band: below 1 caches nothing; above ~50 a "continuing" gate is
# meaningless. Reject outside fail-closed (matches the stick-ttl cap discipline).
MIN_MESSAGES_MAX = 50


def _validate_cache(raw):
    """(cache, error). Per-org cache-behavior overrides {preset, auto_inject, auto_inject_min_messages,
    warmth_routing}, each optional (absent = inherit global env). Fail-closed: unknown keys rejected,
    booleans coerced, min_messages an int in [1, MIN_MESSAGES_MAX], preset a free-text slug ≤32 chars.
    Absent/empty → {} (pure inherit). Only the keys actually SET are stored (full-replace, like the
    sibling overlays)."""
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, _error(400, "cache must be an object", "invalid_request_error", "invalid_cache")
    unknown = set(raw) - CACHE_KEYS
    if unknown:
        return None, _error(400, f"unknown cache keys: {sorted(unknown)}", "invalid_request_error",
                            "invalid_cache")
    out: dict = {}
    for k in ("auto_inject", "warmth_routing"):  # tri-state: absent inherits, present coerces to bool
        if raw.get(k) is not None:
            out[k] = bool(raw[k])
    mm = raw.get("auto_inject_min_messages")
    if mm is not None:
        if isinstance(mm, bool) or not isinstance(mm, int) or not (1 <= mm <= MIN_MESSAGES_MAX):
            return None, _error(400, f"auto_inject_min_messages must be an int in [1, {MIN_MESSAGES_MAX}]",
                                "invalid_request_error", "invalid_cache")
        out["auto_inject_min_messages"] = mm
    preset = raw.get("preset")
    if preset is not None:
        if not isinstance(preset, str) or len(preset) > 32:
            return None, _error(400, "preset must be a string of ≤32 chars", "invalid_request_error",
                                "invalid_cache")
        out["preset"] = preset
    return out, None


async def _validate_routing_body(request: Request, body: dict, catalog=None):
    """Validate a routing-policy PUT body → (bindings, optimize, custom_labels, stick_ttls, cache, error).
    Fail-closed: known+bindable label, a model that EXISTS in the scope's effective catalog (shipped
    base + the scope's adoptions — what dispatch actually resolves), a valid optimize preset, clean
    custom labels, sane per-task-type stickiness holds, valid cache overrides. Shared by team + org
    endpoints, which pass _scope_catalog(...) so adopted models are bindable."""
    bindings = body.get("bindings")
    if bindings is None:
        bindings = {}
    if not isinstance(bindings, dict):
        return None, None, None, None, None, _error(400, "bindings must be an object of label -> catalog model id",
                                                    "invalid_request_error", "invalid_bindings")
    optimize = body.get("optimize")
    if optimize is not None and optimize not in ROUTING_OPTIMIZE:
        return None, None, None, None, None, _error(400, f"optimize must be one of {ROUTING_OPTIMIZE}",
                                                    "invalid_request_error", "invalid_optimize")
    labels = _global_bindings(request)
    vocab = set(labels.vocab())
    if catalog is None:
        catalog = _catalog(request)
    for label, model_id in bindings.items():
        if label not in vocab:
            return None, None, None, None, None, _error(400, f"unknown label {label!r}", "invalid_request_error",
                                                        "unknown_label")
        if label in NON_BINDABLE:
            return None, None, None, None, None, _error(400, f"label {label!r} is not bindable (governed by "
                                                        "privacy/fallback)", "invalid_request_error", "label_not_bindable")
        if not isinstance(model_id, str) or catalog is None or catalog.get(model_id) is None:
            return None, None, None, None, None, _error(400, f"unknown catalog model {model_id!r} for label "
                                                        f"{label!r}", "invalid_request_error", "unknown_model")
    custom_labels, cerr = _validate_custom_labels(body.get("custom_labels"), vocab, catalog)
    if cerr is not None:
        return None, None, None, None, None, cerr
    stick_ttls, serr = _validate_stick_ttls(body.get("stick_ttls"), vocab, custom_labels)
    if serr is not None:
        return None, None, None, None, None, serr
    cache, kerr = _validate_cache(body.get("cache"))
    if kerr is not None:
        return None, None, None, None, None, kerr
    return bindings, optimize, custom_labels, stick_ttls, cache, None


# Fail policy (W1-C1): open (degrade to the failure floor, today's behavior) | closed (503 when
# smart routing intelligence degrades). Validated fail-closed at write; anything else is a 422.
FAIL_POLICIES = frozenset({"open", "closed"})
# W2-C7 per-reason fail matrix: the degradation reasons a dict fail_policy may key on. Console stays
# scalar-only this chunk (API-only widening) — a dict is accepted, never emitted by the UI.
FAIL_REASONS = frozenset({"classify_failed", "breaker_open", "policy_error"})


def _validate_fail_policy(body: dict) -> tuple[object, JSONResponse | None]:
    """(fail_policy, error). Absent → 'open' (full-replace default, like the sibling overlays). A
    present value is EITHER the scalar 'open'/'closed' (applies to every reason — the console default)
    OR a per-reason matrix object {reason: 'open'|'closed'} (W2-C7); anything else is a 422."""
    raw = body.get("fail_policy")
    if raw is None:
        return "open", None
    if isinstance(raw, str):
        if raw not in FAIL_POLICIES:
            return "open", _error(422, f"fail_policy must be one of {sorted(FAIL_POLICIES)}",
                                  "invalid_request_error", "invalid_fail_policy")
        return raw, None
    if isinstance(raw, dict):
        unknown = set(raw) - FAIL_REASONS
        if unknown:
            return "open", _error(422, f"fail_policy reasons must be a subset of {sorted(FAIL_REASONS)}; "
                                  f"unknown {sorted(unknown)}", "invalid_request_error", "invalid_fail_policy")
        for reason, v in raw.items():
            if v not in FAIL_POLICIES:
                return "open", _error(422, f"fail_policy[{reason!r}] must be one of {sorted(FAIL_POLICIES)}",
                                      "invalid_request_error", "invalid_fail_policy")
        return raw, None
    return "open", _error(422, "fail_policy must be 'open'/'closed' or a per-reason object",
                          "invalid_request_error", "invalid_fail_policy")


# W2-C7 data-classification taxonomy: org labels bound to residency constraints. Slug names (reuse
# CUSTOM_LABEL_SLUG), a constraint enum, an optional plain-language desc, a nullable default label.
TAXONOMY_CONSTRAINTS = frozenset({"local_only", "deny", "allow"})
MAX_TAXONOMY_LABELS = 16
_TAXONOMY_DESC_MAX = 200


def _validate_taxonomy(body: dict) -> tuple[dict, JSONResponse | None]:
    """(taxonomy, error). The org's data-classification config {labels: {<slug>: {constraint, desc}},
    default: <slug>|null}. Absent/empty → {} (no taxonomy → no data-policy constraint). Fail-closed:
    ≤16 labels, slug names, a valid constraint enum, a string desc, and a default that names one of
    the labels. Any junk is a 422 (unprocessable)."""
    raw = body.get("taxonomy")
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return {}, _error(422, "taxonomy must be an object {labels, default}",
                          "invalid_request_error", "invalid_taxonomy")
    labels_raw = raw.get("labels") or {}
    if not isinstance(labels_raw, dict):
        return {}, _error(422, "taxonomy.labels must be an object of label -> {constraint, desc}",
                          "invalid_request_error", "invalid_taxonomy")
    if len(labels_raw) > MAX_TAXONOMY_LABELS:
        return {}, _error(422, f"taxonomy supports at most {MAX_TAXONOMY_LABELS} labels",
                          "invalid_request_error", "invalid_taxonomy")
    clean: dict = {}
    for name, row in labels_raw.items():
        if not isinstance(name, str) or not CUSTOM_LABEL_SLUG.match(name):
            return {}, _error(422, f"taxonomy label {name!r} must be a lowercase slug (letters, "
                              "digits, underscore, starting with a letter)",
                              "invalid_request_error", "invalid_taxonomy_label")
        if not isinstance(row, dict):
            return {}, _error(422, f"taxonomy label {name!r} must be an object {{constraint, desc}}",
                              "invalid_request_error", "invalid_taxonomy")
        constraint = row.get("constraint")
        if constraint not in TAXONOMY_CONSTRAINTS:
            return {}, _error(422, f"taxonomy label {name!r} constraint must be one of "
                              f"{sorted(TAXONOMY_CONSTRAINTS)}", "invalid_request_error",
                              "invalid_taxonomy_constraint")
        desc = row.get("desc")
        if desc is not None and not isinstance(desc, str):
            return {}, _error(422, f"taxonomy label {name!r} desc must be a string",
                              "invalid_request_error", "invalid_taxonomy")
        clean[name] = {"constraint": constraint, "desc": (desc or "").strip()[:_TAXONOMY_DESC_MAX]}
    default = raw.get("default")
    if default is not None and (not isinstance(default, str) or default not in clean):
        return {}, _error(422, f"taxonomy default {default!r} must name one of the taxonomy labels",
                          "invalid_request_error", "invalid_taxonomy_default")
    if not clean:
        return {}, None  # empty → stored as {} (no data-policy constraint)
    return {"labels": clean, "default": default}, None


def _taxonomy_requires_local(taxonomy: dict) -> bool:
    """True when the org taxonomy binds ANY label to a residency constraint (local_only|deny) — i.e.
    at least one data class must never egress to a cloud model. That is exactly the condition under
    which the classifier itself (which reads the prompt BEFORE the residency guard can act) must be
    in-perimeter."""
    labels = (taxonomy or {}).get("labels") or {}
    return any((row or {}).get("constraint") in ("local_only", "deny") for row in labels.values())


def _validate_classifier_model(catalog, body: dict, taxonomy: dict) -> tuple[str | None, JSONResponse | None]:
    """(classifier_model, error). The org's chosen in-perimeter classifier id (W3-C1). Absent/empty →
    None (the gateway default classifier). Fail-closed: the id must exist in the scope's EFFECTIVE
    catalog (base + adoptions, same convention as _validate_routing_body since #136 — an org-adopted
    model can be the classifier, matching the runtime catalog_for(identity) lookup); and when the org
    taxonomy carries a local_only/deny constraint the classifier's residency_class MUST be
    'in_perimeter' — the classify call reads the prompt before any residency guard runs, so a cloud
    classifier would leak Restricted text no matter what the routing floor does afterward."""
    raw = body.get("classifier_model")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None
    if not isinstance(raw, str):
        return None, _error(422, "classifier_model must be a catalog model id",
                            "invalid_request_error", "invalid_classifier_model")
    entry = catalog.get(raw) if catalog is not None else None
    if entry is None:
        return None, _error(422, f"unknown catalog model {raw!r} for classifier_model",
                            "invalid_request_error", "unknown_model")
    if _taxonomy_requires_local(taxonomy) and getattr(entry, "residency_class", None) != "in_perimeter":
        return None, _error(422, f"classifier {raw!r} is not in-perimeter (residency "
                            f"{getattr(entry, 'residency_class', None)!r}); this org's data taxonomy "
                            "requires the classifier to run inside the perimeter",
                            "invalid_request_error", "classifier_not_in_perimeter")
    return raw, None


@router.get("/v1/admin/org/routing-policy")
async def get_org_routing_policy(request: Request, org_id: str | None = Query(None),
                                 identity: Identity = Depends(require_read_role("admin"))):
    """The ORG-DEFAULT routing overlay — the policy that applies to a caller with no team (a
    personal-org owner, the pi / API-token common case). Stored under the org_id key. This is the
    knob that makes an owner's OWN smart-routed traffic honor the console's routing config.

    Org resolved via `_scope_org`: an operator names it with ?org_id=; a non-operator admin is
    pinned to their home org (a different ?org_id= is 403)."""
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    policy = await request.app.state.auth.get_routing_policy(org)
    rows, custom = _routing_view(request, policy, await _scope_catalog(request, org))
    return {
        "team_id": None,
        "org_id": org,
        "scope": "org",
        "optimize": (policy or {}).get("optimize") or GLOBAL_OPTIMIZE,
        "optimize_overridden": (policy or {}).get("optimize") is not None,
        "version": (policy or {}).get("version", 0),
        "prewarm": bool((policy or {}).get("prewarm")),
        "stick_ttls": (policy or {}).get("stick_ttls") or {},
        "cache": (policy or {}).get("cache") or {},
        "fail_policy": (policy or {}).get("fail_policy") or "open",
        "taxonomy": (policy or {}).get("taxonomy") or {},
        "classifier_model": (policy or {}).get("classifier_model") or None,
        "optimizer_steers_tools": bool((policy or {}).get("optimizer_steers_tools")),
        "labels": rows,
        "custom_labels": custom,
    }


@router.put("/v1/admin/org/routing-policy")
async def put_org_routing_policy(body: dict, request: Request, org_id: str | None = Query(None),
                                 identity: Identity = Depends(require_role("admin"))):
    """Replace the org-default routing policy. Alongside `bindings`/`optimize`/`stick_ttls`/etc.,
    `custom_labels` defines new task types: `[{name, desc, model}]`.

    Writing a custom task type's `desc`: the classifier reads every incoming prompt and matches it
    against these descriptions, so **the description is the routing behavior**. Describe the
    REQUEST behaviorally in one focused sentence — what the user asks for, concrete enough to be
    distinguishable from the built-in task types (an overlap with e.g. `summarization` steals its
    traffic). Descriptions under 3 words are rejected.

        {"custom_labels": [{"name": "sql_authoring",
                            "desc": "writing or explaining SQL queries against a relational database",
                            "model": "or-qwen3-coder-flash"}]}
    """
    org, err = _scope_org(identity, org_id)
    if err is not None:
        return err
    scope_catalog = await _scope_catalog(request, org)
    bindings, optimize, custom_labels, stick_ttls, cache, err = await _validate_routing_body(
        request, body, scope_catalog)
    if err is not None:
        return err
    fail_policy, ferr = _validate_fail_policy(body)
    if ferr is not None:
        return ferr
    taxonomy, terr = _validate_taxonomy(body)
    if terr is not None:
        return terr
    classifier_model, cmerr = _validate_classifier_model(scope_catalog, body, taxonomy)
    if cmerr is not None:
        return cmerr
    auth = request.app.state.auth
    # Org-default row: keyed by org_id in BOTH team_id (the PK) and org_id columns — the org's own
    # sentinel. get_routing_policy(org_id) reads it back; _resolve_routing_policy falls to it for a
    # teamless caller in this org.
    policy = await auth.set_routing_policy(
        org, org, bindings=bindings, optimize=optimize,
        custom_labels=custom_labels, prewarm=bool(body.get("prewarm")), stick_ttls=stick_ttls,
        cache=cache, fail_policy=fail_policy, taxonomy=taxonomy, classifier_model=classifier_model,
        optimizer_steers_tools=bool(body.get("optimizer_steers_tools")),
        updated_by=identity.user_id,
    )
    try:
        await auth.write_audit("admin:routing_policy", user_id=identity.user_id,
                               org_id=org, target_type="org", target_id=org)
    except Exception:
        pass
    rows, custom = _routing_view(request, policy, scope_catalog)
    return {"team_id": None, "org_id": org, "scope": "org",
            "optimize": (policy or {}).get("optimize") or GLOBAL_OPTIMIZE,
            "optimize_overridden": (policy or {}).get("optimize") is not None,
            "version": (policy or {}).get("version", 0),
            "prewarm": bool((policy or {}).get("prewarm")),
            "stick_ttls": (policy or {}).get("stick_ttls") or {},
            "cache": (policy or {}).get("cache") or {},
            "fail_policy": (policy or {}).get("fail_policy") or "open",
            "taxonomy": (policy or {}).get("taxonomy") or {},
            "classifier_model": (policy or {}).get("classifier_model") or None,
            "optimizer_steers_tools": bool((policy or {}).get("optimizer_steers_tools")),
            "labels": rows, "custom_labels": custom}


@router.get("/v1/admin/teams/{team_id}/routing-policy")
async def get_routing_policy(team_id: str, request: Request,
                             identity: Identity = Depends(require_read_role("admin"))):
    team, err = await _team_in_scope(request, identity, team_id)
    if err is not None:
        return err
    policy = await request.app.state.auth.get_routing_policy(team_id)
    rows, custom = _routing_view(request, policy, await _scope_catalog(request, team_id))
    team_optimize = (policy or {}).get("optimize")
    return {
        "team_id": team_id,
        "org_id": team["org_id"],
        "optimize": team_optimize or GLOBAL_OPTIMIZE,
        "optimize_overridden": team_optimize is not None,
        "version": (policy or {}).get("version", 0),
        "prewarm": bool((policy or {}).get("prewarm")),
        "stick_ttls": (policy or {}).get("stick_ttls") or {},
        "cache": (policy or {}).get("cache") or {},
        "fail_policy": (policy or {}).get("fail_policy") or "open",
        "taxonomy": (policy or {}).get("taxonomy") or {},
        "optimizer_steers_tools": bool((policy or {}).get("optimizer_steers_tools")),
        "labels": rows,
        "custom_labels": custom,
    }


@router.put("/v1/admin/teams/{team_id}/routing-policy")
async def put_routing_policy(team_id: str, body: dict, request: Request,
                             identity: Identity = Depends(require_role("admin"))):
    """Replace one team's routing policy. Same body shape and `custom_labels` description rules as
    PUT /v1/admin/org/routing-policy — see that endpoint for how to write a task-type `desc`."""
    team, err = await _team_in_scope(request, identity, team_id)
    if err is not None:
        return err

    # W3-C1: the classifier is an ORG-level setting (it guards data egress for the whole org's
    # taxonomy). A team PUT that names one is rejected rather than silently dropped.
    if body.get("classifier_model") is not None:
        return _error(422, "classifier_model is an org-level setting; set it on "
                      "/v1/admin/org/routing-policy", "invalid_request_error", "classifier_model_org_only")

    bindings, optimize, custom_labels, stick_ttls, cache, verr = await _validate_routing_body(
        request, body, await _scope_catalog(request, team_id))
    if verr is not None:
        return verr
    fail_policy, ferr = _validate_fail_policy(body)
    if ferr is not None:
        return ferr
    taxonomy, terr = _validate_taxonomy(body)
    if terr is not None:
        return terr

    auth = request.app.state.auth
    policy = await auth.set_routing_policy(
        team_id, team["org_id"], bindings=bindings, optimize=optimize,
        custom_labels=custom_labels, prewarm=bool(body.get("prewarm")), stick_ttls=stick_ttls,
        cache=cache, fail_policy=fail_policy, taxonomy=taxonomy,
        optimizer_steers_tools=bool(body.get("optimizer_steers_tools")),
        updated_by=identity.user_id,
    )
    try:  # best-effort audit under the reserved admin:* namespace
        await auth.write_audit("admin:routing_policy", user_id=identity.user_id,
                               org_id=team["org_id"], target_type="team", target_id=team_id)
    except Exception:
        pass
    return policy
