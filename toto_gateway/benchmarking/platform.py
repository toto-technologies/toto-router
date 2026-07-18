"""Inventory slice of the BenchmarkPlatform compile -> submit -> inspect interface."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ..credentials import ProviderCredentialUnavailable, resolve_provider_credentials
from ..routing.candidates import (
    CandidateCatalog,
    CandidatePartitionKey,
    EligibilityContext,
    candidate_partition_key,
)
from .domain import (
    CredentialScopeRef,
    FrozenRecord,
    InventoryStatus,
    ModelIdentity,
    ProviderName,
    ProviderOffer,
)
from .inventory import InventoryRefreshRequest, InventoryRefreshService
from .platform_store import BenchmarkPlatformStore, InventoryExecutionFence
from .providers.base import DiscoveryConfig, ProviderInventoryAdapter
from .providers.fireworks import FireworksInventoryAdapter
from .providers.openrouter import OpenRouterInventoryAdapter

_PROVIDERS: tuple[ProviderName, ...] = ("openrouter", "fireworks")
_ADMIN_ROLES = {"owner", "admin"}
_OPERATION_RETENTION_S = 24 * 60 * 60
_TERMINAL_OPERATION_STATUSES = {"succeeded", "partial", "failed", "cancelled"}
_OPERATION_LEASE_S = 60.0
_OPERATION_MAINTENANCE_INTERVAL_S = 10.0
_OPERATION_MAINTENANCE_BATCH = 100


class PlatformAccessError(Exception):
    def __init__(self, status_code: int, message: str, code: str) -> None:
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(message)


class PlatformActor(FrozenRecord):
    actor_id: str = Field(min_length=1)
    user_id: str | None = None
    org_id: str | None = None
    role: str | None = None
    is_operator: bool = False
    kind: Literal["user", "agent", "operator", "system"] = "user"

    @classmethod
    def from_identity(cls, identity) -> "PlatformActor":
        return cls(
            actor_id=identity.user_id or "operator",
            user_id=identity.user_id,
            org_id=identity.org_id,
            role=identity.role,
            is_operator=identity.is_operator,
            kind="operator" if identity.is_operator else identity.actor,
        )


class InventoryRefreshIntent(FrozenRecord):
    kind: Literal["inventory_refresh"] = "inventory_refresh"
    providers: tuple[ProviderName, ...]
    scope: Literal["effective", "platform", "user", "organization"]
    user_id: str | None = None
    org_id: str | None = None

    @field_validator("providers")
    @classmethod
    def providers_are_nonempty_and_unique(
        cls, value: tuple[ProviderName, ...]
    ) -> tuple[ProviderName, ...]:
        if not value:
            raise ValueError("at least one provider is required")
        if len(value) != len(set(value)):
            raise ValueError("providers must be unique")
        return value

    @model_validator(mode="after")
    def explicit_scope_names_its_subject(self) -> "InventoryRefreshIntent":
        if self.scope == "user" and not self.user_id:
            raise ValueError("user scope requires user_id")
        if self.scope == "organization" and not self.org_id:
            raise ValueError("organization scope requires org_id")
        return self


class ModelInventoryQuery(FrozenRecord):
    kind: Literal["model_inventory"] = "model_inventory"
    scope: Literal["effective", "platform", "user"]
    user_id: str | None = None
    provider: ProviderName | None = None
    availability: Literal["all", "available"] = "available"
    eligibility: EligibilityContext | None = None
    identity_ref: str | None = Field(default=None, min_length=1, max_length=512)
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=200)

    @model_validator(mode="after")
    def explicit_user_scope_names_a_user(self) -> "ModelInventoryQuery":
        if self.scope == "user" and not self.user_id:
            raise ValueError("user scope requires user_id")
        return self


class OperationRef(FrozenRecord):
    operation_id: str
    status: Literal["queued", "running", "succeeded", "partial", "failed", "cancelled"]
    created_at: float
    result: dict = Field(default_factory=dict)
    error: str | None = None


class ProviderInventoryState(FrozenRecord):
    credential_scope: CredentialScopeRef
    snapshot_status: InventoryStatus | None = None
    last_attempt_status: Literal["success", "partial", "failed"] | None = None
    stale: bool = False
    partial: bool = False
    completed_at: float | None = None
    expires_at: float | None = None
    last_attempt_at: float | None = None
    error_summary: str | None = None


class ModelInventoryItem(FrozenRecord):
    identity: ModelIdentity
    offers: tuple[ProviderOffer, ...]


class ModelInventoryResult(FrozenRecord):
    items: tuple[ModelInventoryItem, ...]
    provider_credential_scopes: dict[ProviderName, CredentialScopeRef]
    providers: dict[ProviderName, ProviderInventoryState]
    next_cursor: str | None = None
    revision: str | None = None


@dataclass(frozen=True)
class CompiledInventoryPlan:
    plan_id: str
    intent: InventoryRefreshIntent
    actor_id: str
    target_user_id: str | None
    provider_scopes: Mapping[ProviderName, CredentialScopeRef]
    fingerprint: str
    requests: tuple[InventoryRefreshRequest, ...] = field(repr=False)


class BenchmarkPlatform:
    def __init__(
        self,
        store: BenchmarkPlatformStore,
        auth_store,
        settings,
        *,
        adapters: Mapping[ProviderName, ProviderInventoryAdapter] | None = None,
        publish_candidates: Callable[[CandidateCatalog], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._auth = auth_store
        self._settings = settings
        self._adapters = dict(
            adapters
            or {
                "openrouter": OpenRouterInventoryAdapter(),
                "fireworks": FireworksInventoryAdapter(),
            }
        )
        self._publish_candidates = publish_candidates or (lambda _catalog: None)
        self._clock = clock
        self._refresh = InventoryRefreshService(
            store,
            clock=clock,
            max_staleness_hours=settings.inventory_max_staleness_hours,
        )
        self._operation_tasks: dict[str, asyncio.Task] = {}
        self._worker_id = f"benchmark-worker-{uuid.uuid4().hex}"
        self._maintenance_task: asyncio.Task | None = None
        self._hydration_lock = asyncio.Lock()
        self._hydration_generation = 0
        self._candidate_catalog = CandidateCatalog()
        self._candidate_revisions: dict[CandidatePartitionKey, str] = {}
        self.closed = False

    async def start(self) -> None:
        await self.store.migrate()
        await self.store.prune_inventory_refresh_operations(now=self._clock())
        await self._hydrate_candidates()
        await self._sync_candidate_partitions()
        await self._recover_operations()
        self._maintenance_task = asyncio.create_task(self._maintain_operations())

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None
        tasks = tuple(self._operation_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()

    async def compile(
        self, intent: InventoryRefreshIntent, actor: PlatformActor
    ) -> CompiledInventoryPlan:
        if self.closed:
            raise PlatformAccessError(503, "benchmark platform is closed", "platform_closed")
        target = await self._authorize_scope(
            intent.scope, intent.user_id, actor, intent.org_id
        )
        # For organization scope `target` is the org id — resolve the org key alone (user_id=None),
        # so the org partition warms even when the saver also holds a personal key.
        resolve_user = None if intent.scope == "organization" else target
        resolve_org = target if intent.scope == "organization" else None
        try:
            selected = await resolve_provider_credentials(
                self._settings, self._auth, resolve_user, intent.providers, org_id=resolve_org
            )
        except ProviderCredentialUnavailable as error:
            raise PlatformAccessError(
                409, "provider credential unavailable", "credential_unavailable"
            ) from error
        requests = []
        scopes: dict[ProviderName, CredentialScopeRef] = {}
        for provider in intent.providers:
            chosen = selected.get(provider)
            if chosen is None:
                raise PlatformAccessError(
                    409, f"no {provider} credential configured", "credential_unavailable"
                )
            credential, scope = chosen
            if intent.scope == "platform" and scope.kind != "platform":
                raise PlatformAccessError(
                    409, f"no platform {provider} credential configured", "credential_unavailable"
                )
            if intent.scope == "user" and scope.kind != "user":
                raise PlatformAccessError(
                    409, f"no user {provider} credential configured", "credential_unavailable"
                )
            if intent.scope == "organization" and scope.kind != "organization":
                raise PlatformAccessError(
                    409, f"no organization {provider} credential configured",
                    "credential_unavailable",
                )
            adapter = self._adapters.get(provider)
            if adapter is None:
                raise PlatformAccessError(400, f"unsupported provider {provider}", "bad_provider")
            scopes[provider] = scope
            requests.append(
                InventoryRefreshRequest(
                    adapter=adapter,
                    credential=credential,
                    config=self._discovery_config(provider, scope),
                )
            )
        fingerprint = self._fingerprint(intent, actor, target, scopes)
        return CompiledInventoryPlan(
            plan_id=f"plan_{uuid.uuid4().hex}",
            intent=intent,
            actor_id=actor.actor_id,
            target_user_id=target,
            provider_scopes=scopes,
            fingerprint=fingerprint,
            requests=tuple(requests),
        )

    async def submit(
        self,
        plan: CompiledInventoryPlan,
        actor: PlatformActor,
        *,
        idempotency_key: str,
    ) -> OperationRef:
        if self.closed:
            raise PlatformAccessError(503, "benchmark platform is closed", "platform_closed")
        if plan.actor_id != actor.actor_id:
            raise PlatformAccessError(403, "compiled plan belongs to another actor", "plan_owner")
        key = idempotency_key.strip()
        if not key or len(key) > 256:
            raise PlatformAccessError(400, "invalid Idempotency-Key", "invalid_idempotency_key")
        now = self._clock()
        await self.store.prune_inventory_refresh_operations(now=now)
        try:
            created, stored = await self.store.claim_inventory_refresh_operation(
                operation_id=f"op_{uuid.uuid4().hex}",
                actor_id=actor.actor_id,
                scope=plan.intent.scope,
                target_user_id=plan.target_user_id,
                idempotency_key=key,
                fingerprint=plan.fingerprint,
                now=now,
                expires_at=now + _OPERATION_RETENTION_S,
                plan=self._durable_plan(plan),
            )
        except ValueError as error:
            raise PlatformAccessError(
                409, "Idempotency-Key reused with different intent", "idempotency_conflict"
            ) from error
        operation = self._operation_ref(stored)
        if (created or operation.status not in _TERMINAL_OPERATION_STATUSES) and (
            operation.operation_id not in self._operation_tasks
        ):
            self._schedule_operation(operation.operation_id)
        return operation

    async def operation(self, operation_id: str) -> OperationRef:
        stored = await self.store.get_inventory_refresh_operation(operation_id)
        if stored is None:
            raise PlatformAccessError(404, "operation not found", "operation_not_found")
        return self._operation_ref(stored)

    async def wait(self, operation_id: str) -> OperationRef:
        task = self._operation_tasks.get(operation_id)
        if task is None:
            return await self.operation(operation_id)
        await asyncio.gather(task, return_exceptions=True)
        return await self.operation(operation_id)

    async def wait_all(self) -> None:
        tasks = tuple(self._operation_tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def inspect(
        self, query: ModelInventoryQuery, actor: PlatformActor
    ) -> ModelInventoryResult:
        target = await self._authorize_scope(query.scope, query.user_id, actor)
        providers = (query.provider,) if query.provider else _PROVIDERS
        selection = await self._inspection_selection(query.scope, target, providers)
        inspection = await self.store.inventory_inspection(
            selection,
            max_staleness_s=self._settings.inventory_max_staleness_hours * 3600,
            now=self._clock(),
            availability=query.availability,
            after_identity_id=self._decode_cursor(query.cursor),
            limit=query.limit,
            identity_ref=query.identity_ref,
        )
        snapshots = inspection.snapshots
        snapshots_by_provider = {snapshot.provider: snapshot for snapshot in snapshots}
        attempts = inspection.attempts
        states: dict[ProviderName, ProviderInventoryState] = {}
        for provider, scope in selection.items():
            snapshot = snapshots_by_provider.get(provider)
            attempt = attempts.get(provider)
            attempt_status = attempt["status"] if attempt else None
            states[provider] = ProviderInventoryState(
                credential_scope=scope,
                snapshot_status=snapshot.status if snapshot else None,
                last_attempt_status=attempt_status,
                stale=bool(snapshot and snapshot.status == "stale"),
                partial=attempt_status == "partial",
                completed_at=snapshot.completed_at if snapshot else None,
                expires_at=snapshot.expires_at if snapshot else None,
                last_attempt_at=attempt["completed_at"] if attempt else None,
                error_summary=attempt["error_summary"] if attempt else None,
            )

        page = inspection.page
        offers_by_identity: dict[str, list[ProviderOffer]] = {}
        for offer in page.offers:
            offers_by_identity.setdefault(offer.identity_id, []).append(offer)
        items = tuple(
            ModelInventoryItem(
                identity=identity,
                offers=tuple(
                    sorted(
                        offers_by_identity.get(identity.identity_id, ()),
                        key=lambda offer: offer.route_id,
                    )
                ),
            )
            for identity in page.identities
        )
        revision_payload = {
            "selection": {
                provider: scope.model_dump(mode="json")
                for provider, scope in sorted(selection.items())
            },
            "snapshots": sorted(
                (snapshot.provider, snapshot.snapshot_id) for snapshot in snapshots
            ),
            "attempts": sorted(
                (provider, attempt.get("snapshot_id"))
                for provider, attempt in attempts.items()
            ),
        }
        revision = hashlib.sha256(json.dumps(
            revision_payload, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        return ModelInventoryResult(
            items=items,
            provider_credential_scopes=dict(selection),
            providers=states,
            next_cursor=(
                self._encode_cursor(page.identities[-1].identity_id)
                if page.has_more and page.identities
                else None
            ),
            revision=revision,
        )

    async def _run_refresh(self, operation_id: str) -> None:
        now = self._clock()
        claimed = await self.store.claim_inventory_refresh_execution(
            operation_id,
            owner_id=self._worker_id,
            now=now,
            lease_expires_at=now + _OPERATION_LEASE_S,
        )
        if claimed is None:
            return
        execution_task = asyncio.current_task()
        heartbeat = asyncio.create_task(self._renew_execution_lease(operation_id, execution_task))
        try:
            requests = await self._requests_from_operation(claimed)
            identities, aliases = await self.store.reconciliation_context(
                tuple(request.adapter.provider for request in requests)
            )
            outcome = await self._refresh.refresh(
                requests,
                known_identities=identities,
                approved_aliases=aliases,
                execution_fence=lambda: InventoryExecutionFence(
                    operation_id=operation_id,
                    owner_id=self._worker_id,
                    now=self._clock(),
                ),
            )
            providers = {snapshot.provider: snapshot.status for snapshot in outcome.snapshots}
            statuses = set(providers.values())
            status = (
                "succeeded"
                if statuses == {"success"}
                else "failed"
                if statuses == {"failed"}
                else "partial"
            )
            persisted = await self.store.finish_inventory_refresh_execution(
                operation_id,
                owner_id=self._worker_id,
                status=status,
                result={"providers": providers},
                error=None,
                now=self._clock(),
            )
            if persisted is None:
                return
            if any(snapshot.status == "success" for snapshot in outcome.snapshots):
                try:
                    await self._sync_candidate_partitions()
                except Exception:
                    # ponytail: snapshots remain the durable source; the next refresh/startup
                    # retries hydration without falsifying the completed provider outcome.
                    pass
        except Exception:  # noqa: BLE001 - operation failure is retained without secret-bearing text
            await self.store.finish_inventory_refresh_execution(
                operation_id,
                owner_id=self._worker_id,
                status="failed",
                result={},
                error="inventory refresh failed",
                now=self._clock(),
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _requests_from_operation(
        self, operation: Mapping
    ) -> tuple[InventoryRefreshRequest, ...]:
        plan = operation.get("plan") or {}
        raw_providers = plan.get("providers")
        raw_scopes = plan.get("provider_scopes")
        if not isinstance(raw_providers, list) or not raw_providers:
            raise ValueError("durable inventory operation has no providers")
        if not isinstance(raw_scopes, dict):
            raise ValueError("durable inventory operation has no provider scopes")
        providers = tuple(raw_providers)
        if any(provider not in _PROVIDERS for provider in providers):
            raise ValueError("durable inventory operation names an unsupported provider")
        target_user_id = plan.get("target_user_id")
        expected = {p: CredentialScopeRef.model_validate(raw_scopes.get(p)) for p in providers}
        # Org-scoped plans stored the org id in target_user_id; re-resolve the org partition alone
        # (user_id=None) so reconstruction reproduces the same organization credential.
        org_id = next((s.scope_id for s in expected.values() if s.kind == "organization"), None)
        selected = await resolve_provider_credentials(
            self._settings, self._auth, None if org_id else target_user_id, providers,
            org_id=org_id,
        )
        requests = []
        for provider in providers:
            expected_scope = expected[provider]
            chosen = selected.get(provider)
            if chosen is None or chosen[1] != expected_scope:
                raise ProviderCredentialUnavailable(
                    (provider,), "compiled_credential_scope_unavailable"
                )
            credential, scope = chosen
            adapter = self._adapters.get(provider)
            if adapter is None:
                raise ValueError(f"unsupported provider {provider}")
            requests.append(
                InventoryRefreshRequest(
                    adapter=adapter,
                    credential=credential,
                    config=self._discovery_config(provider, scope),
                )
            )
        return tuple(requests)

    def _discovery_config(self, provider: ProviderName, scope: CredentialScopeRef) -> DiscoveryConfig:
        if provider != "fireworks":
            return DiscoveryConfig(credential_scope=scope)
        if scope.kind == "platform":
            return DiscoveryConfig(
                credential_scope=scope,
                account_id=self._settings.fireworks_account_id,
                discover_deployments=self._settings.fireworks_discover_deployments,
            )
        # BYOK scopes never see the platform's account id: the key itself defines the account
        # universe, and deployments must be walked because on-demand fine-tunes only exist there.
        return DiscoveryConfig(
            credential_scope=scope, discover_accounts=True, discover_deployments=True
        )

    async def _renew_execution_lease(
        self, operation_id: str, execution_task: asyncio.Task | None
    ) -> None:
        try:
            while True:
                await asyncio.sleep(_OPERATION_LEASE_S / 3)
                now = self._clock()
                renewed = await self.store.renew_inventory_refresh_execution(
                    operation_id,
                    owner_id=self._worker_id,
                    now=now,
                    lease_expires_at=now + _OPERATION_LEASE_S,
                )
                if not renewed:
                    if execution_task is not None:
                        execution_task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - uncertain ownership must stop provider work
            if execution_task is not None:
                execution_task.cancel()

    def _schedule_operation(self, operation_id: str) -> None:
        if self.closed or operation_id in self._operation_tasks:
            return
        task = asyncio.create_task(self._run_refresh(operation_id))
        self._operation_tasks[operation_id] = task
        task.add_done_callback(
            lambda completed, operation_id=operation_id: self._forget_task(operation_id, completed)
        )

    async def _recover_operations(self) -> None:
        operations = await self.store.recoverable_inventory_refresh_operations(
            now=self._clock(), limit=_OPERATION_MAINTENANCE_BATCH
        )
        for operation in operations:
            self._schedule_operation(operation["operation_id"])

    async def _maintain_operations(self) -> None:
        try:
            while True:
                await asyncio.sleep(_OPERATION_MAINTENANCE_INTERVAL_S)
                try:
                    await self.store.prune_inventory_refresh_operations(
                        now=self._clock(), limit=_OPERATION_MAINTENANCE_BATCH
                    )
                    await self._recover_operations()
                    await self._sync_candidate_partitions()
                except Exception:  # noqa: BLE001 - retry on the next bounded maintenance tick
                    continue
        except asyncio.CancelledError:
            raise

    def _forget_task(self, operation_id: str, completed: asyncio.Task) -> None:
        if self._operation_tasks.get(operation_id) is completed:
            self._operation_tasks.pop(operation_id, None)

    @staticmethod
    def _durable_plan(plan: CompiledInventoryPlan) -> dict:
        return {
            "providers": list(plan.intent.providers),
            "target_user_id": plan.target_user_id,
            "provider_scopes": {
                provider: scope.model_dump(mode="json")
                for provider, scope in plan.provider_scopes.items()
            },
        }

    @staticmethod
    def _operation_ref(stored: Mapping) -> OperationRef:
        return OperationRef(
            operation_id=stored["operation_id"],
            status=stored["status"],
            created_at=stored["created_at"],
            result=stored["result"],
            error=stored["error"],
        )

    async def _authorize_scope(
        self, scope: str, requested_user_id: str | None, actor: PlatformActor,
        requested_org_id: str | None = None,
    ) -> str | None:
        privileged = actor.is_operator or actor.role in _ADMIN_ROLES
        if scope == "platform":
            if not privileged:
                raise PlatformAccessError(403, "admin role required", "insufficient_role")
            return None
        if scope == "organization":
            if not privileged:  # org-wide credential is an admin/owner concern
                raise PlatformAccessError(403, "admin role required", "insufficient_role")
            org = requested_org_id or actor.org_id
            if org is None:
                raise PlatformAccessError(400, "org_id is required", "org_id_required")
            if not actor.is_operator and org != actor.org_id:
                raise PlatformAccessError(403, "cannot access another org", "cross_org_denied")
            return org
        if scope == "user" and not privileged:
            raise PlatformAccessError(403, "admin role required", "insufficient_role")
        target = requested_user_id or actor.user_id
        if target is None:
            if actor.is_operator and scope == "effective":
                return None
            raise PlatformAccessError(400, "user_id is required", "user_id_required")
        if target != actor.user_id and not actor.is_operator and not privileged:
            raise PlatformAccessError(403, "cannot access another user", "cross_user_denied")
        user = await self._auth.get_user(target)
        if user is None:
            raise PlatformAccessError(404, "user not found", "user_not_found")
        if target != actor.user_id and not actor.is_operator:
            membership = await self._auth.get_membership(target)
            if membership is None or membership["org_id"] != actor.org_id:
                raise PlatformAccessError(404, "user not found", "user_not_found")
        return target

    async def _inspection_selection(
        self,
        scope: str,
        target_user_id: str | None,
        providers: tuple[ProviderName, ...],
    ) -> dict[ProviderName, CredentialScopeRef]:
        if scope == "platform" or (scope == "effective" and target_user_id is None):
            return {
                provider: CredentialScopeRef(kind="platform", scope_id="platform")
                for provider in providers
            }
        if scope == "user":
            return {
                provider: CredentialScopeRef(kind="user", scope_id=target_user_id)
                for provider in providers
            }
        try:
            selected = await resolve_provider_credentials(
                self._settings, self._auth, target_user_id, providers
            )
        except ProviderCredentialUnavailable as error:
            raise PlatformAccessError(
                409, "provider credential unavailable", "credential_unavailable"
            ) from error
        return {provider: chosen[1] for provider, chosen in selected.items()}

    async def _hydrate_candidates(self) -> None:
        self._hydration_generation += 1
        generation = self._hydration_generation
        async with self._hydration_lock:
            candidates = await self.store.all_routing_candidates(now=self._clock())
            if generation != self._hydration_generation:
                return
            catalog = CandidateCatalog(candidates)
            self._candidate_catalog = catalog
            self._candidate_revisions = {
                candidate_partition_key(candidate): candidate.snapshot_id
                for candidate in candidates
            }
            self._publish_candidates(catalog)

    async def _sync_candidate_partitions(self) -> bool:
        """Converge changed provider/scope partitions without reading the unchanged offer fleet."""
        self._hydration_generation += 1
        generation = self._hydration_generation
        async with self._hydration_lock:
            for _attempt in range(2):
                now = self._clock()
                revisions = await self.store.latest_routing_candidate_revisions(now=now)
                if generation != self._hydration_generation:
                    return False
                changed = {
                    key for key, revision in revisions.items()
                    if self._candidate_revisions.get(key) != revision
                }
                removed = set(self._candidate_revisions) - set(revisions)
                if not changed and not removed:
                    return False
                scopes = tuple(
                    (provider, CredentialScopeRef(kind=kind, scope_id=scope_id))
                    for provider, kind, scope_id in sorted(changed)
                )
                fetched = await self.store.routing_candidates_for_scopes(scopes, now=now)
                confirmed = await self.store.latest_routing_candidate_revisions(now=self._clock())
                if confirmed != revisions:
                    continue
                replacements = {key: [] for key in changed | removed}
                for candidate in fetched:
                    replacements[candidate_partition_key(candidate)].append(candidate)
                catalog = self._candidate_catalog.replace_scope_partitions(replacements)
                if generation != self._hydration_generation:
                    return False
                self._candidate_catalog = catalog
                self._candidate_revisions = dict(revisions)
                self._publish_candidates(catalog)
                return True
            return False

    @staticmethod
    def _fingerprint(
        intent: InventoryRefreshIntent,
        actor: PlatformActor,
        target_user_id: str | None,
        scopes: Mapping[ProviderName, CredentialScopeRef],
    ) -> str:
        payload = {
            "intent": intent.model_dump(mode="json"),
            "actor_id": actor.actor_id,
            "target_user_id": target_user_id,
            "provider_scopes": {
                provider: scope.model_dump(mode="json") for provider, scope in scopes.items()
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _encode_cursor(identity_id: str) -> str:
        payload = json.dumps({"after": identity_id}, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> str | None:
        if cursor is None:
            return None
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            identity_id = payload["after"]
            if not isinstance(identity_id, str) or not identity_id:
                raise ValueError("cursor identity must be a non-empty string")
        except (binascii.Error, KeyError, TypeError, ValueError, UnicodeDecodeError) as error:
            raise PlatformAccessError(400, "invalid cursor", "invalid_cursor") from error
        return identity_id
