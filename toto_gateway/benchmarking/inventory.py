"""Out-of-band provider discovery, reconciliation, and immutable snapshot persistence."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .domain import (
    Actor,
    IdentityAliasDecision,
    InventorySnapshot,
    ModelIdentity,
    ProviderOffer,
    stable_offer_id,
    stable_route_id,
    stable_snapshot_offer_id,
)
from .identity import IdentityReconciler, ReconciledOffer
from .platform_store import BenchmarkPlatformStore, InventoryExecutionFence
from .providers.base import (
    DiscoveryConfig,
    DiscoveryResult,
    ProviderInventoryAdapter,
    sanitize_metadata,
    sanitize_text,
    transport_error,
)


@dataclass(frozen=True)
class InventoryRefreshRequest:
    adapter: ProviderInventoryAdapter
    credential: str = field(repr=False)
    config: DiscoveryConfig


@dataclass(frozen=True)
class InventoryRefreshOutcome:
    snapshots: tuple[InventorySnapshot, ...]
    identities: tuple[ModelIdentity, ...]


class InventoryRefreshService:
    """The only layer that turns network discovery into persisted inventory records."""

    def __init__(
        self,
        store: BenchmarkPlatformStore,
        *,
        reconciler: IdentityReconciler | None = None,
        clock: Callable[[], float] = time.time,
        max_staleness_hours: float = 24.0,
    ) -> None:
        if max_staleness_hours < 0:
            raise ValueError("max_staleness_hours must be non-negative")
        self._store = store
        self._reconciler = reconciler or IdentityReconciler(clock=clock)
        self._clock = clock
        self._max_staleness_s = max_staleness_hours * 3600
        self._known_identities: dict[str, ModelIdentity] = {}
        self._audit_actor = Actor(actor_id="benchmark-inventory", kind="system")

    async def refresh(
        self,
        requests: Sequence[InventoryRefreshRequest],
        *,
        known_identities: Sequence[ModelIdentity] = (),
        approved_aliases: Sequence[IdentityAliasDecision] | None = None,
        execution_fence: Callable[[], InventoryExecutionFence] | None = None,
    ) -> InventoryRefreshOutcome:
        requested = tuple(requests)
        providers = [request.adapter.provider for request in requested]
        if len(providers) != len(set(providers)):
            raise ValueError("refresh accepts at most one credential scope per provider")
        started_at = self._clock()
        results = await asyncio.gather(*(self._discover(request) for request in requested))
        completed_at = self._clock()
        known = dict(self._known_identities)
        known.update((identity.identity_id, identity) for identity in known_identities)
        reconciliation = self._reconciler.reconcile(
            [offer for result in results for offer in result.offers],
            tuple(known.values()),
            approved_aliases=approved_aliases or (),
        )
        await self._store.ensure_identities(reconciliation.identities)
        self._known_identities.update(
            (identity.identity_id, identity) for identity in reconciliation.identities
        )
        identity_ids = {
            (offer.provider, offer.upstream_model_id): offer.identity_id
            for offer in reconciliation.offers
        }
        reconciled = {
            (offer.provider, offer.upstream_model_id): offer
            for offer in reconciliation.offers
        }
        if approved_aliases is None:
            latest_aliases = await self._store.latest_alias_decisions(tuple(reconciled))
        else:
            latest_aliases = {}
            for alias in approved_aliases:
                key = (alias.provider, alias.source_id)
                current = latest_aliases.get(key)
                if current is None or alias.decided_at > current.decided_at:
                    latest_aliases[key] = alias
        snapshots = []
        for request, result in zip(requested, results, strict=True):
            snapshot = self._snapshot(
                request,
                result,
                identity_ids,
                started_at=started_at,
                completed_at=completed_at,
            )
            offers = snapshot.offers
            decisions = self._alias_decisions(
                snapshot, reconciled, latest_aliases, completed_at,
            )
            await self._store.commit_reconciled_inventory(
                snapshot.model_copy(update={"offers": ()}),
                offers,
                decisions,
                self._audit_actor,
                execution_fence=execution_fence() if execution_fence is not None else None,
            )
            snapshots.append(snapshot)
        return InventoryRefreshOutcome(tuple(snapshots), reconciliation.identities)

    async def _discover(self, request: InventoryRefreshRequest) -> DiscoveryResult:
        try:
            result = await request.adapter.discover(request.credential, request.config)
            if result.provider != request.adapter.provider:
                raise ValueError("adapter returned a result for a different provider")
            if result.adapter_revision != request.adapter.adapter_revision:
                raise ValueError("adapter revision changed during discovery")
            return result
        except Exception as error:  # noqa: BLE001 - isolate provider adapter failures
            return DiscoveryResult(
                provider=request.adapter.provider,
                adapter_revision=request.adapter.adapter_revision,
                status="failed",
                pagination_complete=False,
                source_metadata={"unexpected_adapter_failure": True},
                error_summary=transport_error(
                    request.adapter.provider,
                    "discovery",
                    error,
                    request.credential,
                ),
            )

    def _snapshot(
        self,
        request: InventoryRefreshRequest,
        result: DiscoveryResult,
        identity_ids: dict[tuple[str, str], str],
        *,
        started_at: float,
        completed_at: float,
    ) -> InventorySnapshot:
        snapshot_id = f"snapshot_{uuid.uuid4().hex}"
        offers = []
        for discovered in result.offers:
            offer_id = stable_offer_id(discovered.provider, discovered.upstream_model_id)
            offers.append(
                ProviderOffer(
                    snapshot_offer_id=stable_snapshot_offer_id(snapshot_id, offer_id),
                    offer_id=offer_id,
                    identity_id=identity_ids[(discovered.provider, discovered.upstream_model_id)],
                    route_id=stable_route_id(discovered.provider, discovered.upstream_model_id),
                    provider=discovered.provider,
                    upstream_model_id=discovered.upstream_model_id,
                    base_url=discovered.base_url,
                    credential_scope=discovered.credential_scope,
                    capabilities=discovered.capabilities,
                    pricing=discovered.pricing,
                    adapter_revision=result.adapter_revision,
                    raw_metadata=sanitize_metadata(discovered.raw_metadata, request.credential),
                )
            )
        # Sanitize before domain construction; InventorySnapshot also validates fail closed.
        error_summary = (
            sanitize_text(result.error_summary, request.credential)
            if result.error_summary is not None
            else None
        )
        return InventorySnapshot(
            snapshot_id=snapshot_id,
            provider=result.provider,
            credential_scope=request.config.credential_scope,
            status=result.status,
            started_at=started_at,
            completed_at=completed_at,
            expires_at=completed_at + self._max_staleness_s,
            pagination_complete=result.pagination_complete,
            adapter_revision=result.adapter_revision,
            source_metadata=sanitize_metadata(result.source_metadata, request.credential),
            error_summary=error_summary,
            offers=tuple(offers),
        )

    def _alias_decisions(
        self,
        snapshot: InventorySnapshot,
        reconciled: dict[tuple[str, str], ReconciledOffer],
        latest_aliases: dict[tuple[str, str], IdentityAliasDecision],
        decided_at: float,
    ) -> tuple[IdentityAliasDecision, ...]:
        decisions = []
        seen: set[tuple[str, str]] = set()
        for offer in snapshot.offers:
            source_key = (offer.provider, offer.upstream_model_id)
            if source_key in seen:
                continue
            seen.add(source_key)
            outcome = reconciled[(offer.provider, offer.upstream_model_id)]
            decision_time = decided_at
            latest = latest_aliases.get(source_key)
            if latest is not None:
                decision_time = max(decision_time, latest.decided_at + 0.000001)
            hint = outcome.discovered.identity
            alias_uuid = uuid.uuid5(
                uuid.NAMESPACE_URL, snapshot.snapshot_id + offer.offer_id
            ).hex
            decisions.append(IdentityAliasDecision(
                alias_id=f"alias_{alias_uuid}",
                provider=offer.provider,
                source_id=offer.upstream_model_id,
                identity_id=offer.identity_id,
                method=outcome.method,
                confidence=outcome.confidence,
                evidence={
                    "snapshot_id": snapshot.snapshot_id,
                    "adapter_revision": offer.adapter_revision,
                    "canonical_id": hint.canonical_id,
                    "hugging_face_id": hint.hugging_face_id,
                    "fingerprint": {
                        "vendor": hint.vendor,
                        "family": hint.family,
                        "release": hint.release,
                        "reasoning_variant": hint.reasoning_variant,
                        "quantization": hint.quantization,
                        "fine_tune": hint.fine_tune,
                        "context_variant": hint.context_variant,
                    },
                },
                reviewer=None,
                decided_at=decision_time,
            ))
        return tuple(decisions)
