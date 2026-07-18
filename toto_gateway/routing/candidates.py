"""Synchronous immutable provider-candidate resolution and request eligibility."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

from ..benchmarking.domain import CredentialScopeRef, RoutingCandidate
from ..catalog import Catalog, CatalogEntry, Price
from ..credentials import PROVIDERS

CandidatePartitionKey = tuple[str, str, str]


@dataclass(frozen=True)
class CandidateNotFound:
    model_id: str


@dataclass(frozen=True)
class EligibilityContext:
    token_estimate: int = 0
    max_output_tokens: int = 0
    has_tools: bool = False
    modalities: tuple[str, ...] = ("text",)
    requested_parameters: tuple[str, ...] = ()
    user_credential_envs: frozenset[str] = frozenset()
    platform_credential_envs: frozenset[str] = frozenset()
    now: float = field(default_factory=time.time)


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reasons: tuple[str, ...] = ()


class CandidateIneligibleError(Exception):
    """A discovered route exists but cannot serve this request without violating its contract."""

    def __init__(self, entry: CatalogEntry, reasons: tuple[str, ...]) -> None:
        self.entry = entry
        self.model_id = entry.id
        self.reasons = reasons
        super().__init__(f"model {entry.id!r} is ineligible: {', '.join(reasons)}")


def _scope_key(scope: CredentialScopeRef) -> tuple[str, str]:
    return scope.kind, scope.scope_id


def candidate_partition_key(candidate: RoutingCandidate) -> CandidatePartitionKey:
    return (
        candidate.provider,
        candidate.credential_scope.kind,
        candidate.credential_scope.scope_id,
    )


def _coerce_scope(value) -> CredentialScopeRef | None:
    if isinstance(value, CredentialScopeRef):
        return value
    if isinstance(value, Mapping):
        try:
            return CredentialScopeRef.model_validate(value)
        except ValueError:
            return None
    return None


@dataclass(frozen=True, init=False)
class CandidateCatalog:
    """An immutable request-path index built from the last-good inventory snapshots.

    Replacing inventory means constructing and atomically swapping this object. Resolution never
    queries storage or a provider. The authenticated identity supplies the authoritative scope for
    each provider so two credentials are never inferred or blended from whichever rows exist.
    """

    _candidates: tuple[RoutingCandidate, ...]
    _by_route: Mapping[str, tuple[RoutingCandidate, ...]]

    def __init__(self, candidates: Iterable[RoutingCandidate] = ()) -> None:
        frozen = tuple(candidates)
        by_route: dict[str, tuple[RoutingCandidate, ...]] = {}
        for candidate in frozen:
            by_route[candidate.route_id] = (*by_route.get(candidate.route_id, ()), candidate)
        object.__setattr__(self, "_candidates", frozen)
        object.__setattr__(self, "_by_route", MappingProxyType(by_route))

    @property
    def candidates(self) -> tuple[RoutingCandidate, ...]:
        return self._candidates

    def resolve(self, base: Catalog, model_id: str, identity=None) -> CatalogEntry | CandidateNotFound:
        static = base.get(model_id)
        if static is not None:
            return static
        route_candidates = self._by_route.get(model_id, ())
        if not route_candidates:
            return CandidateNotFound(model_id)
        provider = route_candidates[0].provider
        scope = self._effective_scope(provider, identity)
        if scope is None:
            return CandidateNotFound(model_id)
        matches = [c for c in route_candidates if _scope_key(c.credential_scope) == _scope_key(scope)]
        if not matches:
            return CandidateNotFound(model_id)
        return _materialize(max(matches, key=lambda c: c.snapshot_completed_at))

    def platform_entries(self, base: Catalog) -> tuple[CatalogEntry, ...]:
        return self.entries_for(base, None)

    def entries_for(self, base: Catalog, identity) -> tuple[CatalogEntry, ...]:
        """The dynamic entries this caller can actually route to. For a provider where the caller's
        authoritative scope is BYOK (user/org), that scope's rows replace the platform rows — a
        BYOK caller resolves fail-closed against their own partition, so listing platform rows to
        them would advertise unroutable models. All other providers list platform scope."""
        overrides: dict[str, tuple[str, str]] = {}
        mapping = getattr(identity, "provider_credential_scopes", None)
        if isinstance(mapping, Mapping):
            for provider, raw in mapping.items():
                scope = _coerce_scope(raw)
                if scope is not None and scope.kind != "platform":
                    overrides[provider] = _scope_key(scope)
        platform_key = ("platform", "platform")
        latest: dict[str, RoutingCandidate] = {}
        now = time.time()
        for candidate in self._candidates:
            wanted = overrides.get(candidate.provider, platform_key)
            if (_scope_key(candidate.credential_scope) != wanted
                    or candidate.snapshot_expires_at < now
                    or base.get(candidate.route_id) is not None):
                continue
            current = latest.get(candidate.route_id)
            if current is None or candidate.snapshot_completed_at > current.snapshot_completed_at:
                latest[candidate.route_id] = candidate
        return tuple(_materialize(candidate) for candidate in latest.values())

    def replace_scope_partitions(
        self,
        replacements: Mapping[CandidatePartitionKey, Iterable[RoutingCandidate]],
    ) -> "CandidateCatalog":
        """Return one immutable index with only the named provider/scope partitions replaced."""
        if not replacements:
            return self
        replaced = set(replacements)
        candidates = [
            candidate
            for candidate in self._candidates
            if candidate_partition_key(candidate) not in replaced
        ]
        for replacement in replacements.values():
            candidates.extend(replacement)
        return CandidateCatalog(sorted(
            candidates,
            key=lambda candidate: (*candidate_partition_key(candidate), candidate.route_id),
        ))

    def _effective_scope(self, provider: str, identity) -> CredentialScopeRef | None:
        # This mapping is the authority chosen by credential resolution + inventory refresh. Never
        # infer it from available rows: a BYOK inventory may legitimately contain zero offers, and
        # falling through to a platform row would silently execute under the wrong credential.
        mapping = getattr(identity, "provider_credential_scopes", None)
        if not isinstance(mapping, Mapping):
            return None
        return _coerce_scope(mapping.get(provider))


class EligibilityEngine:
    """Pure final eligibility over one materialized dynamic route and one request context."""

    def evaluate(self, candidate: CatalogEntry, context: EligibilityContext,
                 policy=None) -> EligibilityDecision:
        if candidate.lane != "provider":
            return EligibilityDecision(True)

        reasons: list[str] = []
        if candidate.snapshot_expires_at is not None and context.now > candidate.snapshot_expires_at:
            reasons.append("snapshot_expired")
        requested_context = context.token_estimate + context.max_output_tokens
        if candidate.context_window <= 0 or requested_context > candidate.context_window:
            reasons.append("context_window_exceeded")
        if (candidate.max_output_tokens is not None and context.max_output_tokens
                and context.max_output_tokens > candidate.max_output_tokens):
            reasons.append("max_output_tokens_exceeded")
        if context.has_tools and not candidate.tools:
            reasons.append("tools_not_supported")
        offered_modalities = set(candidate.modalities or ("text",))
        for modality in context.modalities:
            if modality not in offered_modalities:
                reasons.append(f"modality_not_supported:{modality}")
        supported = set(candidate.supported_parameters)
        for parameter in context.requested_parameters:
            if parameter != "tools" and parameter not in supported:
                reasons.append(f"parameter_not_supported:{parameter}")

        scope = candidate.credential_scope
        available = (context.user_credential_envs
                     if scope is not None and scope.kind in ("user", "organization")
                     else context.platform_credential_envs)
        if candidate.api_key_env not in available:
            reasons.append("credential_unavailable")

        residency_allow = getattr(policy, "residency_allow", None)
        if residency_allow is not None and candidate.residency_class not in residency_allow:
            reasons.append("residency_policy")
        elif policy is not None and not policy.permits(candidate):
            reasons.append("catalog_policy")
        return EligibilityDecision(not reasons, tuple(reasons))


def request_modalities(req) -> tuple[str, ...]:
    modalities = {"text"}
    for message in req.messages:
        if not isinstance(message.content, list):
            continue
        for part in message.content:
            kind = part.get("type", "") if isinstance(part, dict) else ""
            if kind in {"image", "image_url", "input_image"}:
                modalities.add("image")
            elif kind in {"audio", "input_audio"}:
                modalities.add("audio")
    return tuple(sorted(modalities))


def requested_parameters(req) -> tuple[str, ...]:
    data = req.model_dump(exclude_none=True)
    # conversation_key + cache_prefs are gateway-internal (stripped before upstream), never client
    # parameters — excluding them keeps them out of the provider capability eligibility check.
    ignored = {"model", "messages", "stream", "stream_options", "user", "conversation_key",
               "cache_prefs"}
    return tuple(sorted(key for key in data if key not in ignored))


def _materialize(candidate: RoutingCandidate) -> CatalogEntry:
    provider = PROVIDERS[candidate.provider]
    caps = candidate.capabilities
    price = candidate.pricing
    supported = tuple(caps.supported_parameters)
    return CatalogEntry(
        id=candidate.route_id,
        lane="provider",
        endpoint="openai",
        residency_class="cloud",
        price_usd_per_1k=Price(
            prompt=price.prompt_usd_per_1k or 0.0,
            completion=price.completion_usd_per_1k or 0.0,
        ),
        context_window=caps.context_window or 0,
        upstream_model=candidate.upstream_model_id,
        base_url=candidate.base_url,
        api_key_env=provider.api_key_env,
        tools="tools" in supported,
        identity_id=candidate.identity_id,
        offer_id=candidate.offer_id,
        provider=candidate.provider,
        credential_scope=candidate.credential_scope,
        modalities=tuple(caps.modalities),
        supported_parameters=supported,
        max_output_tokens=caps.max_output_tokens,
        snapshot_completed_at=candidate.snapshot_completed_at,
        snapshot_expires_at=candidate.snapshot_expires_at,
        capability_residency=tuple(caps.residency),
        price_source="discovered",  # provider-reported via inventory snapshot, not hand-typed
    )
