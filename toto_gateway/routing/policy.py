"""Routing policy: hard constraints that beat exemplar similarity.

A plain YAML load + a dict + a loop. No rules engine, no DSL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..pipeline import Signal

_DEFAULT_PATH = Path(__file__).parent / "policy.yaml"


class Policy:
    """Load and apply hard routing constraints.

    Rules shape (from YAML):
      - {intent: redact, residency: in_perimeter}  — force in-perimeter when signal.intent matches
      - {when: mnpi, residency: in_perimeter}       — same, when signal.intent contains keyword
      - {intent: x, lane: frontier}                 — force a TIER (rare; residency is the usual key)
    Global: max_local_context (int) — token threshold above which we force the frontier tier.
    """

    def __init__(self, path: str | Path | None = None, *, _raw: dict[str, Any] | None = None) -> None:
        if _raw is not None:
            data = _raw
        else:
            p = Path(path) if path else _DEFAULT_PATH
            data = yaml.safe_load(p.read_text()) or {}
        self.rules: list[dict[str, str]] = data.get("rules", [])
        self.max_local_context: int = data.get("max_local_context", 32768)
        # Catalog-scope overlay (control-plane C2). Empty by default → permits() allows everything,
        # so an ordinary routing Policy is UNCHANGED. effective_policy() populates these from the
        # team's catalog_policies row (via from_catalog_scope) on top of the global routing rules.
        self.catalog_mode: str | None = None          # 'allow' | 'deny' | None (no catalog scope)
        self.catalog_models: set[str] = set()
        self.residency_allow: set[str] | None = None  # None = every residency class allowed
        # Org deny-by-default gate (control-plane C3). None = allow_all (permissive, today's
        # behavior). A frozenset = the org is in allowlist mode and ONLY these ids (its approved
        # set = allow list + org catalog adoptions) may resolve. effective_policy() populates it
        # from identity.org_allowlist; permits() folds it in so BOTH the passthrough final-entry
        # check and the smart path's candidate eligibility route around unapproved models.
        self.org_allowlist: frozenset[str] | None = None
        self.default_model: str | None = None
        # Routing overlay (control-plane C6). Empty/None by default -> the global routing/labels.yaml
        # stands. effective_policy() populates these from the team's routing_policies row; the driver
        # reads label_bindings to override the shipped tag->model map (per-label) and optimize as the
        # fallback-path preset. Absence -> byte-identical global behavior.
        self.label_bindings: dict[str, str] = {}      # label -> catalog id (only overridden labels)
        self.optimize: str | None = None              # 'quality' | 'balanced' | 'cost' | None
        # Custom task types (CT): the team's INVENTED labels [{name, desc, model}] — NOT in
        # labels.yaml. effective_policy() populates these from the team's routing_policies row; the
        # driver appends {name: desc} to the classifier vocab FOR THAT REQUEST and routes a match to
        # `model` (team-binding tier). Empty by default -> classifier vocab is the global set only.
        self.custom_labels: list[dict] = []
        # Per-task-type stickiness holds (S2): {label -> seconds} the LabelAwareTTL policy reads to
        # set the label-memo hold per task type. effective_policy() populates it from the team's
        # routing_policies row; empty -> LabelAwareTTL falls to its global/default hold.
        self.stick_ttls: dict[str, float] = {}
        # Per-org cache-behavior overrides (A8): the raw {preset, auto_inject, auto_inject_min_messages,
        # warmth_routing} dict, each key optional. effective_policy() populates it from the team's
        # routing_policies row; a present key overrides the global env default per-request (gateway
        # resolves cache_prefs + warmth_routing from here), an absent key inherits. Empty -> pure
        # global behavior (byte-identical to pre-A8 caching).
        self.cache: dict = {}
        # Fail policy (W1-C1): 'open' (default, today's behavior — degrade to the failure floor) or
        # 'closed' (reject with 503 when smart routing intelligence degrades). effective_policy()
        # populates it from the team/org routing overlay; absent/invalid -> 'open'. W2-C7 widens it:
        # a dict {reason: 'open'|'closed'} carries a PER-REASON matrix (resolve_fail_policy reads it);
        # the scalar stays the storage/console default.
        self.fail_policy: str | dict = "open"
        # Data-classification taxonomy (W2-C7): the org's Public/Internal/Confidential/Restricted
        # regime projected onto the gateway. Shape: {labels: {<label>: {constraint: local_only|deny|
        # allow, desc}}, default: <label>|None}. effective_policy() populates it from the org/team
        # routing overlay; empty -> no data-policy constraint (byte-identical routing). The SAME
        # classifier call that assigns the task type assigns the data label (routing/smart.py); the
        # constraint is enforced at the routing floor (Gateway._plan) for smart AND explicit models.
        self.taxonomy: dict = {}
        # Pluggable in-perimeter classifier (W3-C1): the catalog id the smart/data-policy classify
        # call runs on FOR THIS ORG, overriding the global TOTO_GW_LABEL_CLASSIFIER_MODEL. None -> the
        # gateway default. When the org taxonomy carries a local_only/deny constraint the write path
        # (admin_routing) refuses a classifier whose residency_class != in_perimeter — the classify
        # runs BEFORE the residency guard, so the in-perimeter guarantee has to hold at config time.
        self.classifier_model: str | None = None
        # Binding precedence escape hatch. False (default) → an explicit label binding governs ALL
        # traffic for that label; a tools request whose bound model can't speak tools takes the
        # tools guard, never a silent benchmark override. True → restore the pre-precedence behavior:
        # the optimizer may steer such tool traffic to the benchmark best. effective_policy()
        # populates it from the team/org routing overlay; absent → False (byte-identical to bindings-
        # govern). Read in routing/smart.py at the bound-but-non-tool branch.
        self.optimizer_steers_tools: bool = False

    @classmethod
    def from_catalog_scope(cls, blob: dict, *, base: "Policy | None" = None) -> "Policy":
        """Build a Policy carrying a team's catalog allow/deny overlay. Copies the global routing
        rules off `base` (so MNPI/context-overflow constraints still apply for a tenanted caller)
        and layers the catalog scope on top. Never mutates `base` — the caller may pass a shared
        default."""
        p = cls(_raw={})
        if base is not None:
            p.rules = base.rules                      # shared read-only reference (rules aren't mutated)
            p.max_local_context = base.max_local_context
        from ..catalog import normalize_legacy_id

        mode = blob.get("mode")
        if mode in ("allow", "deny"):
            p.catalog_mode = mode
            # pre-2026-07-09 blobs may name retired tier-word ids — repair at the read boundary
            p.catalog_models = {normalize_legacy_id(m) for m in (blob.get("models") or [])}
        res = blob.get("residency")
        p.residency_allow = set(res) if res else None
        p.default_model = normalize_legacy_id(blob.get("default_model")) if blob.get("default_model") else None
        return p

    def permits(self, entry) -> bool:
        """Fail-closed catalog-scope check for one resolved CatalogEntry — True = the team may use
        this model. No overlay (catalog_mode None + residency_allow None) → True (permissive).
        Stored lists written before the 2026-07-09 id rename are normalized to canonical ids at
        blob-load (from_catalog_scope), so gating never depends on retired tier-word ids — a
        pre-rename deny of `or-economy` still denies the renamed entry, by repair not by alias."""
        if self.residency_allow is not None and entry.residency_class not in self.residency_allow:
            return False
        if self.org_allowlist is not None and entry.id not in self.org_allowlist:
            return False  # C3 org deny-by-default: model outside the org's approved set
        if self.catalog_mode == "allow":
            return entry.id in self.catalog_models
        if self.catalog_mode == "deny":
            return entry.id not in self.catalog_models
        return True

    def taxonomy_constraint(self, data_label: str | None) -> str | None:
        """The residency constraint ('local_only' | 'deny') the org taxonomy binds to a request's
        data classification, or None (no taxonomy / 'allow' / no constraint). W2-C7.

        Fail-closed default: when `data_label` is None or not a configured taxonomy label
        (classification failed or returned nothing) AND the taxonomy names a `default`, the default
        label's constraint applies. A data_label that IS configured wins outright — an explicit
        'allow' classification does NOT fall through to the default."""
        tax = self.taxonomy or {}
        labels = tax.get("labels") or {}
        row = labels.get(data_label) if data_label else None
        if row is None:  # unclassified / unknown label → the fail-closed default (if any)
            default = tax.get("default")
            row = labels.get(default) if default else None
        c = (row or {}).get("constraint")
        return c if c in ("local_only", "deny") else None  # 'allow'/unknown → no constraint

    def constrained(self, signal: Signal) -> tuple[str, str] | None:
        """Return a forced constraint ("residency", value) or ("tier", lane), or None.

        Privacy rules (redact/mnpi) force an in-perimeter RESIDENCY — sensitive data must stay
        inside the perimeter, never on a cheap CLOUD economy model. Context overflow forces the
        frontier TIER (economy models can't hold very large prompts). Residency and tier are
        orthogonal, so the two constraint kinds are reported separately."""
        for rule in self.rules:
            if "intent" in rule and signal.intent == rule["intent"]:
                return _target(rule)
            if "when" in rule and rule["when"] in (signal.intent or ""):
                return _target(rule)
        if signal.token_estimate > self.max_local_context:
            return ("tier", "frontier")
        return None

    def conflicts(self) -> list[str]:
        """Return descriptions of rules that force contradictory targets for the same key."""
        seen: dict[str, str] = {}  # key -> forced target value
        found: list[str] = []
        for rule in self.rules:
            target = _target(rule)[1]
            for field in ("intent", "when"):
                if field in rule:
                    key = f"{field}:{rule[field]}"
                    if key in seen and seen[key] != target:
                        found.append(f"conflict on {key}: {seen[key]!r} vs {target!r}")
                    else:
                        seen[key] = target
        return found


def _target(rule: dict[str, str]) -> tuple[str, str]:
    """A rule forces either a residency (privacy) or a tier/lane. Residency wins if both given."""
    if "residency" in rule:
        return ("residency", rule["residency"])
    return ("tier", rule["lane"])
