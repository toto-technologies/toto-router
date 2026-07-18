"""Conservative provider-offer identity reconciliation.

Behavior-changing suffixes are explicit fingerprint fields. This module deliberately does not
reuse the legacy benchmark-ingest cleaner, which removes dates, effort, and context variants.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .domain import IdentityAliasDecision, ModelIdentity
from .providers.base import DiscoveredOffer, IdentityHint

Fingerprint = tuple[str, str, str, str, str, str, str]


@dataclass(frozen=True)
class ReconciledOffer:
    discovered: DiscoveredOffer
    identity_id: str
    method: str
    confidence: float

    @property
    def provider(self) -> str:
        return self.discovered.provider

    @property
    def upstream_model_id(self) -> str:
        return self.discovered.upstream_model_id


@dataclass(frozen=True)
class ReconciliationResult:
    identities: tuple[ModelIdentity, ...]
    offers: tuple[ReconciledOffer, ...]


class IdentityReconciler:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    def reconcile(
        self,
        offers: Sequence[DiscoveredOffer],
        known_identities: Sequence[ModelIdentity],
        *,
        approved_aliases: Sequence[IdentityAliasDecision] = (),
    ) -> ReconciliationResult:
        discovered = tuple(offers)
        assigned: dict[int, tuple[str, str, float]] = {}
        created: dict[str, ModelIdentity] = {}

        active_aliases = {
            (alias.provider, alias.source_id): alias
            for alias in approved_aliases
            if alias.superseded_at is None
        }
        for index, offer in enumerate(discovered):
            alias = active_aliases.get((offer.provider, offer.upstream_model_id))
            if alias is not None:
                assigned[index] = (alias.identity_id, "approved_alias", 1.0)

        for component, shared_identifier in _identifier_components(discovered):
            if len(component) < 2:
                continue
            targets = {assigned[index][0] for index in component if index in assigned}
            if len(targets) > 1:
                continue
            if targets:
                identity_id = targets.pop()
            else:
                identity = _identity_from_hint(
                    discovered[min(component)].identity,
                    identity_id=_identity_id(
                        "provider-id",
                        shared_identifier,
                        *_behavior_fingerprint(discovered[min(component)].identity),
                    ),
                    provisional=False,
                    created_at=self._clock(),
                )
                created[identity.identity_id] = identity
                identity_id = identity.identity_id
            for index in component:
                assigned.setdefault(index, (identity_id, "provider_canonical_id", 0.98))

        known_by_fingerprint: dict[Fingerprint, list[ModelIdentity]] = defaultdict(list)
        for identity in known_identities:
            known_by_fingerprint[_identity_fingerprint(identity)].append(identity)

        offers_by_fingerprint: dict[Fingerprint, list[int]] = defaultdict(list)
        for index, offer in enumerate(discovered):
            fingerprint = _hint_fingerprint(offer.identity)
            if _usable_fingerprint(fingerprint):
                offers_by_fingerprint[fingerprint].append(index)

        for fingerprint, indexes in offers_by_fingerprint.items():
            unresolved = [index for index in indexes if index not in assigned]
            if not unresolved:
                continue
            targets = {assigned[index][0] for index in indexes if index in assigned}
            targets.update(identity.identity_id for identity in known_by_fingerprint[fingerprint])
            if len(targets) == 1:
                identity_id = targets.pop()
            elif not targets and len(unresolved) >= 2:
                identity = _identity_from_hint(
                    discovered[unresolved[0]].identity,
                    identity_id=_identity_id("fingerprint", *fingerprint),
                    provisional=False,
                    created_at=self._clock(),
                )
                created[identity.identity_id] = identity
                identity_id = identity.identity_id
            else:
                continue
            for index in unresolved:
                assigned[index] = (identity_id, "exact_fingerprint", 0.95)

        for index, offer in enumerate(discovered):
            if index in assigned:
                continue
            identity = _identity_from_hint(
                offer.identity,
                identity_id=_identity_id("provisional", offer.provider, offer.upstream_model_id),
                provisional=True,
                created_at=self._clock(),
            )
            created[identity.identity_id] = identity
            assigned[index] = (identity.identity_id, "provisional", 0.4)

        reconciled = tuple(
            ReconciledOffer(
                discovered=offer,
                identity_id=assigned[index][0],
                method=assigned[index][1],
                confidence=assigned[index][2],
            )
            for index, offer in enumerate(discovered)
        )
        return ReconciliationResult(tuple(created.values()), reconciled)


def infer_identity_hint(
    *,
    upstream_model_id: str,
    display_name: str,
    canonical_id: str | None = None,
    hugging_face_id: str | None = None,
    fine_tune: str | None = None,
    quantization: str | None = None,
    context_variant: str | None = None,
) -> IdentityHint:
    """Extract only explicit identity dimensions from provider IDs."""
    hf_id = _hugging_face_id(hugging_face_id)
    source = hf_id or canonical_id or upstream_model_id
    identity_sources = tuple(
        value
        for value in (upstream_model_id, canonical_id, hf_id, display_name)
        if value
    )
    vendor, model = _vendor_and_model(source)
    releases = {
        match.group(0).replace("_", "-")
        for value in identity_sources
        for match in re.finditer(r"20\d{2}[-_]\d{2}[-_]\d{2}", value)
    }
    release = _join_variants(releases)
    reasoning = _variant_from_sources(
        identity_sources,
        (
            r"(?:^|[-_])(reasoning[-_](?:x?high|medium|low|minimal|none))(?:$|[-_])",
            r"(?:^|[-_])((?:x?high|medium|low|minimal|none)[-_]reasoning)(?:$|[-_])",
            r"(?:^|[-_])(thinking)(?:$|[-_])",
        ),
    )
    inferred_quantization = _variant_from_sources(
        identity_sources,
        (
            r"(?:^|[-_])((?:int|fp)\d+|bf16|awq|gptq|gguf|bnb[-_]?4bit)(?:$|[-_])",
        ),
    )
    inferred_fine_tune = _variant_from_sources(
        identity_sources,
        (
            r"(?:^|[-_ :])(fine[-_ ]?tune(?:[-_ ][a-z0-9]+)*)(?:$|[-_ :])",
            r"(?:^|[-_ :])(ft[-_][a-z0-9][a-z0-9_-]*)(?:$|[-_ :])",
        ),
    )
    inferred_context = _variant_from_sources(
        identity_sources,
        (r"(?:^|[-_])(\d+(?:k|m)(?:[-_]context)?)(?:$|[-_])",),
    )
    family = model
    for variant in (
        release,
        reasoning,
        inferred_quantization,
        inferred_fine_tune,
        inferred_context,
    ):
        if variant:
            family = re.sub(re.escape(variant), "-", family, flags=re.IGNORECASE)
    family = re.sub(r"[-_]+", "-", family).strip("-") or model
    return IdentityHint(
        vendor=_normalized(vendor) or "unknown",
        family=_normalized(family) or "model",
        release=release,
        reasoning_variant=reasoning,
        quantization=_join_variants((quantization, inferred_quantization)),
        fine_tune=_join_variants((fine_tune, inferred_fine_tune)),
        context_variant=_join_variants((context_variant, inferred_context)),
        display_name=display_name or model,
        canonical_id=canonical_id,
        hugging_face_id=hf_id,
    )


def _identifier_components(
    offers: tuple[DiscoveredOffer, ...],
) -> tuple[tuple[set[int], str], ...]:
    identifiers: dict[str, list[int]] = defaultdict(list)
    for index, offer in enumerate(offers):
        for identifier in (offer.identity.canonical_id, offer.identity.hugging_face_id):
            normalized = _normalized_identifier(identifier)
            if normalized:
                identifiers[normalized].append(index)

    parent = list(range(len(offers)))
    shared: dict[int, set[str]] = defaultdict(set)

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for identifier, indexes in identifiers.items():
        for left_pos, left in enumerate(indexes):
            for right in indexes[left_pos + 1 :]:
                if _behavior_fingerprint(offers[left].identity) == _behavior_fingerprint(
                    offers[right].identity
                ):
                    union(left, right)
                    shared[left].add(identifier)
                    shared[right].add(identifier)

    components: dict[int, set[int]] = defaultdict(set)
    component_ids: dict[int, set[str]] = defaultdict(set)
    for index in range(len(offers)):
        root = find(index)
        components[root].add(index)
        component_ids[root].update(shared[index])
    return tuple(
        (component, sorted(component_ids[root])[0])
        for root, component in components.items()
        if len(component) >= 2 and component_ids[root]
    )


def _hint_fingerprint(hint: IdentityHint) -> Fingerprint:
    return tuple(
        _normalized(value)
        for value in (
            hint.vendor,
            hint.family,
            hint.release,
            hint.reasoning_variant,
            hint.quantization,
            hint.fine_tune,
            hint.context_variant,
        )
    )  # type: ignore[return-value]


def _identity_fingerprint(identity: ModelIdentity) -> Fingerprint:
    return tuple(
        _normalized(value)
        for value in (
            identity.vendor,
            identity.family,
            identity.release,
            identity.reasoning_variant,
            identity.quantization,
            identity.fine_tune,
            identity.context_variant,
        )
    )  # type: ignore[return-value]


def _behavior_fingerprint(hint: IdentityHint) -> tuple[str, str, str, str, str]:
    return tuple(
        _normalized(value)
        for value in (
            hint.release,
            hint.reasoning_variant,
            hint.quantization,
            hint.fine_tune,
            hint.context_variant,
        )
    )  # type: ignore[return-value]


def _usable_fingerprint(fingerprint: Fingerprint) -> bool:
    return bool(fingerprint[0] and fingerprint[0] != "unknown" and fingerprint[1])


def _identity_from_hint(
    hint: IdentityHint,
    *,
    identity_id: str,
    provisional: bool,
    created_at: float,
) -> ModelIdentity:
    return ModelIdentity(
        identity_id=identity_id,
        vendor=hint.vendor,
        family=hint.family,
        release=hint.release,
        reasoning_variant=hint.reasoning_variant,
        quantization=hint.quantization,
        fine_tune=hint.fine_tune,
        context_variant=hint.context_variant,
        display_name=hint.display_name,
        provisional=provisional,
        created_at=created_at,
    )


def _identity_id(kind: str, *parts: str) -> str:
    encoded = json.dumps((kind, *parts), ensure_ascii=True, separators=(",", ":")).encode()
    return f"identity_{hashlib.sha256(encoded).hexdigest()[:24]}"


def _normalized(value: object | None) -> str:
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")


def _normalized_identifier(value: str | None) -> str:
    if not value:
        return ""
    return _hugging_face_id(value).casefold().strip("/")


def _hugging_face_id(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().rstrip("/")
    prefix = "https://huggingface.co/"
    if text.casefold().startswith(prefix):
        text = text[len(prefix) :]
    return text


def _vendor_and_model(source: str) -> tuple[str, str]:
    parts = source.strip("/").split("/")
    if len(parts) >= 4 and parts[-2] in {"models", "deployments"}:
        model = parts[-1]
        vendor = _vendor_from_model(model) or parts[1]
        return vendor, model
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return _vendor_from_model(parts[-1]) or "unknown", parts[-1]


def _vendor_from_model(model: str) -> str:
    lowered = model.casefold()
    for prefix, vendor in (
        ("qwen", "qwen"),
        ("llama", "meta-llama"),
        ("deepseek", "deepseek"),
        ("mistral", "mistral"),
        ("mixtral", "mistral"),
        ("gemma", "google"),
    ):
        if lowered.startswith(prefix):
            return vendor
    return ""


def _first_match(value: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _variant_from_sources(sources: tuple[str, ...], patterns: tuple[str, ...]) -> str | None:
    return _join_variants(_first_match(source, patterns) for source in sources)


def _join_variants(values) -> str | None:
    normalized = sorted({_normalized(value) for value in values if value})
    return "+".join(normalized) or None
