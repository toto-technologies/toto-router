"""GuardRouter — the raw /v1/chat/completions routing decision.

Content-based lane *selection* now lives in the driver's metadata classifier
(`driver/classify.py`), which entirely supersedes the retired exemplar/cosine router. What
remains at the raw request layer is the deterministic SAFETY floor: the fail-closed guard can
force local handling, and hard policy constraints (redact/mnpi intent, context overflow) beat
the requested model. Otherwise the requested model passes through unchanged — a direct
OpenAI-compatible caller gets exactly the model it asked for, minus what safety forbids.

Precedence (highest to lowest):
  1. Guard override — DOWNGRADE_LOCAL forces a local/in_perimeter model immediately.
  2. Policy         — hard constraints (intent/keyword/context overflow) force a lane.
  3. Passthrough    — honor req.model.

Ponytail: a couple of catalog lookups and a loop. No embeddings, no similarity, no exemplars.
"""

from __future__ import annotations

from functools import lru_cache

from ..catalog import Catalog, CatalogEntry
from ..pipeline import DOWNGRADE_LOCAL, Decision, GuardVerdict, Signal
from ..schemas import ChatCompletionRequest
from .policy import Policy


@lru_cache(maxsize=1)
def _base_policy() -> Policy:
    """The shipped global routing rules (default policy.yaml), loaded once. A catalog overlay is
    layered onto a COPY of this so a tenanted caller keeps MNPI/context-overflow constraints.
    ponytail: assumes GuardRouter runs the default policy (it always does — app.py builds
    GuardRouter() with no path); a custom router policy would fall back to these defaults here."""
    return Policy()


def effective_policy(identity=None, default: Policy | None = None) -> Policy | None:
    """Resolve the routing Policy for a caller (control-plane C2 + C6). Layers the team's catalog
    allow/deny overlay (`identity.catalog_policy`, C2) AND the team's tag->model routing overlay
    (`identity.routing_policy`, C6) — both resolved SERVER-SIDE at auth time (deps.py) — onto the
    global routing rules. No overlay of EITHER kind (no policy row, operator, driver-internal caller)
    → returns `default` (None) so the router keeps its own global policy — ZERO behavior change,
    unchanged routing tests. Duck-typed identity (reads .catalog_policy/.routing_policy) → no import."""
    catalog_blob = getattr(identity, "catalog_policy", None)
    routing_blob = getattr(identity, "routing_policy", None)
    allowlist = getattr(identity, "org_allowlist", None)  # C3: frozenset of approved ids, or None
    if not catalog_blob and not routing_blob and allowlist is None:
        return default
    pol = Policy.from_catalog_scope(catalog_blob or {}, base=default or _base_policy())
    if allowlist is not None:  # C3 org deny-by-default gate — enforced even with no team overlay
        pol.org_allowlist = allowlist
    if routing_blob:  # C6: team tag->model overlay + optimize preset; CT: custom task types
        from ..catalog import normalize_legacy_id  # repair pre-rename stored ids at read
        pol.label_bindings = {k: normalize_legacy_id(v)
                              for k, v in (routing_blob.get("bindings") or {}).items()}
        pol.optimize = routing_blob.get("optimize") or None
        pol.custom_labels = [{**c, "model": normalize_legacy_id(c["model"])} if c.get("model") else c
                             for c in (routing_blob.get("custom_labels") or [])]
        pol.stick_ttls = {str(k): float(v) for k, v in (routing_blob.get("stick_ttls") or {}).items()}
        pol.cache = dict(routing_blob.get("cache") or {})  # A8: per-org cache-behavior overrides
        # W1-C1: fail-open (today's behavior) unless the overlay explicitly says 'closed'. W2-C7: a
        # dict is a PER-REASON matrix — carried through verbatim (resolve_fail_policy reads it).
        fp = routing_blob.get("fail_policy")
        pol.fail_policy = fp if isinstance(fp, dict) else ("closed" if fp == "closed" else "open")
        # W2-C7 data-classification taxonomy (labels bound to residency constraints).
        pol.taxonomy = dict(routing_blob.get("taxonomy") or {})
        # W3-C1 pluggable classifier: the org's chosen classify model (repair pre-rename ids at read).
        cm = routing_blob.get("classifier_model")
        pol.classifier_model = normalize_legacy_id(cm) if cm else None
        # Binding-precedence escape hatch: absent → False (bindings govern tool traffic).
        pol.optimizer_steers_tools = bool(routing_blob.get("optimizer_steers_tools"))
    return pol


def resolve_fail_policy(fail_policy, reason: str) -> str:
    """The fail decision ('open' | 'closed') for a specific degradation `reason` (W1-C1 + W2-C7).
    `fail_policy` is either a scalar 'open'/'closed' (applies to every reason — the storage/console
    default) or a per-reason matrix dict {reason: 'open'|'closed'} (a reason the dict doesn't name
    inherits 'open'). Anything but an explicit 'closed' resolves 'open' (fail-open is the safe
    default — a config typo can't 503 traffic)."""
    if isinstance(fail_policy, dict):
        return "closed" if fail_policy.get(reason) == "closed" else "open"
    return "closed" if fail_policy == "closed" else "open"


class GuardRouter:
    """Router Protocol impl: safety-and-policy floor over an otherwise passthrough decision."""

    def __init__(self, policy: Policy | None = None) -> None:
        self._policy = policy or Policy()

    def decide(
        self,
        req: ChatCompletionRequest,
        signal: Signal,
        verdict: GuardVerdict,
        catalog: Catalog,
        policy: Policy | None = None,
    ) -> Decision:
        # Effective policy: a per-request override (from effective_policy(identity)) wins; None
        # (the thin-slice default) falls back to this router's global policy → unchanged behavior.
        active_policy = policy or self._policy
        # 1. Guard override: safety beats the caller's preference. Keeps privacy teeth — a
        #    downgrade_local verdict actually lands on an in-perimeter model, not merely advisory.
        if verdict.action == DOWNGRADE_LOCAL:
            entry = self._first_in_perimeter(catalog)
            if entry:
                return Decision(model_id=entry.id, reason="guard:downgrade_local")

        # 2. Policy: hard constraint forces either an in-perimeter RESIDENCY (redact/mnpi privacy)
        #    or a TIER (context-window overflow → frontier). The two axes are separate.
        forced = active_policy.constrained(signal)
        if forced is not None:
            kind, value = forced
            entry = (self._first_in_perimeter(catalog) if kind == "residency"
                     else self._first_in_lane(catalog, value))
            if entry:
                return Decision(model_id=entry.id, reason=f"policy:{value}")

        # 3. Passthrough: honor the requested model.
        return Decision(model_id=req.model, reason="passthrough")

    def _first_in_perimeter(self, catalog: Catalog) -> CatalogEntry | None:
        """First in-perimeter entry (residency, not tier) — real box preferred over a fake one."""
        for entry in catalog.models:
            if entry.residency_class == "in_perimeter" and entry.endpoint != "fake":
                return entry
        for entry in catalog.models:
            if entry.residency_class == "in_perimeter":
                return entry
        return None

    def _first_in_lane(self, catalog: Catalog, lane: str) -> CatalogEntry | None:
        for entry in catalog.models:
            if entry.lane == lane:
                return entry
        return None
