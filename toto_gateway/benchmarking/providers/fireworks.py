"""Paginated Fireworks serverless, account-model, and deployment discovery."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..domain import OfferCapabilities, OfferPricing
from ..identity import infer_identity_hint
from .base import (
    DiscoveredOffer,
    DiscoveryConfig,
    DiscoveryResult,
    ProviderInventoryAdapter,
    UnsafeProviderRow,
    response_error,
    sanitize_metadata,
    transport_error,
    validate_display_name,
    validate_hugging_face_id,
    validate_resource_id,
    validate_retained_value,
)

_MANAGEMENT_BASE = "https://api.fireworks.ai"
_INFERENCE_BASE = "https://api.fireworks.ai/inference/v1"
# ponytail: 8 accounts per credential; a BYOK key seeing more than that is an operator problem,
# raise the cap when a real customer hits it.
_MAX_ACCOUNTS = 8
_ACCOUNT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MAX_PAGES = 100
_MAX_ROWS = 50_000
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_DISCOVERY_BYTES = 64 * 1024 * 1024
_DISCOVERY_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class _PageResult:
    rows: tuple[dict, ...]
    pages: int
    complete: bool
    error: str | None = None
    total_rows: int = 0


class FireworksInventoryAdapter(ProviderInventoryAdapter):
    provider = "fireworks"
    adapter_revision = "fireworks-models-v1"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def discover(self, credential: str, config: DiscoveryConfig) -> DiscoveryResult:
        deadline = time.monotonic() + _DISCOVERY_TIMEOUT_S
        specs = [
            (
                "public_serverless",
                "/v1/accounts/fireworks/models",
                "models",
                {"filter": "supports_serverless=true", "pageSize": 200},
            )
        ]
        account_ids = [config.account_id] if config.account_id else []
        accounts_truncated = 0

        pages: dict[str, _PageResult] = {}
        remaining_rows = _MAX_ROWS
        byte_budget = {"remaining": _MAX_DISCOVERY_BYTES}
        async with httpx.AsyncClient(
            base_url=_MANAGEMENT_BASE,
            timeout=self._timeout,
            transport=self._transport,
            headers={"Authorization": f"Bearer {credential}"},
        ) as client:
            if config.discover_accounts and not account_ids:
                # The credential is the authority on which accounts it can reach — enumerate them
                # instead of trusting a configured account_id (which is the platform's, not the
                # scope owner's). Account rows never enter offer normalization (skipped below).
                pages["accounts"] = await self._walk(
                    client,
                    source="accounts",
                    path="/v1/accounts",
                    list_key="accounts",
                    params={"pageSize": 200},
                    credential=credential,
                    deadline=deadline,
                    row_limit=remaining_rows,
                    byte_budget=byte_budget,
                )
                remaining_rows -= pages["accounts"].total_rows
                for row in pages["accounts"].rows:
                    segment = _text(row.get("name")).rsplit("/", 1)[-1]
                    if segment and segment not in account_ids and _ACCOUNT_ID_RE.fullmatch(segment):
                        account_ids.append(segment)
                accounts_truncated = max(0, len(account_ids) - _MAX_ACCOUNTS)
                account_ids = account_ids[:_MAX_ACCOUNTS]
            for account_id in account_ids:
                # Explicit single-account config keeps the historical unsuffixed source names.
                suffix = "" if account_id == config.account_id else f":{account_id}"
                specs.append(
                    (
                        f"account_models{suffix}",
                        f"/v1/accounts/{account_id}/models",
                        "models",
                        {"pageSize": 200},
                    )
                )
                if config.discover_deployments:
                    specs.append(
                        (
                            f"deployments{suffix}",
                            f"/v1/accounts/{account_id}/deployments",
                            "deployments",
                            {"pageSize": 200},
                        )
                    )
            for source, path, list_key, params in specs:
                pages[source] = await self._walk(
                    client,
                    source=source,
                    path=path,
                    list_key=list_key,
                    params=params,
                    credential=credential,
                    deadline=deadline,
                    row_limit=remaining_rows,
                    byte_budget=byte_budget,
                )
                remaining_rows -= pages[source].total_rows

        page_errors = [result.error for result in pages.values() if result.error]
        errors = list(page_errors)
        offers: dict[str, DiscoveredOffer] = {}
        quarantined = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append("fireworks overall discovery deadline exceeded")
        else:
            offers, quarantined, normalization_timed_out = await self._normalize_pages(
                pages, credential, config, deadline
            )
            if normalization_timed_out:
                errors.append("fireworks overall discovery deadline exceeded")
        if accounts_truncated:
            errors.append(
                f"fireworks credential sees {accounts_truncated} account(s) beyond the "
                f"{_MAX_ACCOUNTS}-account discovery cap"
            )
        successful_pages = sum(result.pages for result in pages.values())
        if not errors:
            status = "success"
        elif offers or successful_pages:
            status = "partial"
        else:
            status = "failed"
            offers.clear()
        # Quarantined rows are individually rejected by the safety boundary — the enumeration
        # itself completed. They must not demote status/pagination (the read path only surfaces
        # success snapshots, so one sloppy row would blank the whole provider), but the count
        # stays visible in error_summary + source_metadata.quarantined_rows.
        summary_parts = list(errors)
        if quarantined:
            summary_parts.append(f"fireworks quarantined {quarantined} unsafe provider row(s)")
        return DiscoveryResult(
            provider=self.provider,
            adapter_revision=self.adapter_revision,
            status=status,
            pagination_complete=not errors,
            offers=tuple(sorted(offers.values(), key=lambda offer: offer.upstream_model_id)),
            source_metadata={
                "sources": {
                    source: {"pages": result.pages, "complete": result.complete}
                    for source, result in pages.items()
                },
                "quarantined_rows": quarantined,
            },
            error_summary="; ".join(summary_parts) if summary_parts else None,
        )

    async def _normalize_pages(
        self,
        pages: dict[str, _PageResult],
        credential: str,
        config: DiscoveryConfig,
        deadline: float,
    ) -> tuple[dict[str, DiscoveredOffer], int, bool]:
        offers: dict[str, DiscoveredOffer] = {}
        quarantined = 0
        processed = 0
        for source, result in pages.items():
            if source == "accounts":  # account rows name accounts, not models — never offers
                continue
            for row in result.rows:
                if time.monotonic() >= deadline:
                    return offers, quarantined, True
                try:
                    offer = (
                        self._map_deployment(row, credential, config)
                        if source.startswith("deployments")
                        else self._map_model(row, source, credential, config)
                    )
                except UnsafeProviderRow:
                    quarantined += 1
                else:
                    if offer is not None:
                        offers.setdefault(offer.upstream_model_id, offer)
                processed += 1
                if time.monotonic() >= deadline:
                    return offers, quarantined, True
                if processed % 64 == 0:
                    await asyncio.sleep(0)
        return offers, quarantined, False

    async def _walk(
        self,
        client: httpx.AsyncClient,
        *,
        source: str,
        path: str,
        list_key: str,
        params: dict,
        credential: str,
        deadline: float,
        row_limit: int,
        byte_budget: dict[str, int],
    ) -> _PageResult:
        rows: list[dict] = []
        page_params = dict(params)
        page_count = 0
        total_rows = 0
        seen_tokens: set[str] = set()
        while True:
            if row_limit <= 0:
                return _PageResult(
                    tuple(rows), page_count, False,
                    transport_error(
                        self.provider,
                        source,
                        RuntimeError(f"provider row limit {_MAX_ROWS} exceeded"),
                        credential,
                    ),
                    total_rows,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _PageResult(
                    tuple(rows), page_count, False,
                    transport_error(
                        self.provider,
                        source,
                        TimeoutError("overall discovery deadline exceeded"),
                        credential,
                    ),
                    total_rows,
                )
            try:
                response = await asyncio.wait_for(
                    client.get(path, params=page_params), timeout=remaining,
                )
            except TimeoutError:
                return _PageResult(
                    tuple(rows), page_count, False,
                    transport_error(
                        self.provider,
                        source,
                        TimeoutError("overall discovery deadline exceeded"),
                        credential,
                    ),
                    total_rows,
                )
            except httpx.HTTPError as error:
                return _PageResult(
                    tuple(rows),
                    page_count,
                    False,
                    transport_error(self.provider, source, error, credential),
                    total_rows,
                )
            response_bytes = len(response.content)
            if (
                response_bytes > _MAX_RESPONSE_BYTES
                or response_bytes > byte_budget["remaining"]
            ):
                return _PageResult(
                    tuple(rows),
                    page_count,
                    False,
                    transport_error(
                        self.provider,
                        source,
                        RuntimeError(
                            f"provider response byte limit {_MAX_DISCOVERY_BYTES} exceeded"
                        ),
                        credential,
                    ),
                    total_rows,
                )
            byte_budget["remaining"] -= response_bytes
            if response.status_code >= 400:
                return _PageResult(
                    tuple(rows),
                    page_count,
                    False,
                    response_error(self.provider, source, response, credential),
                    total_rows,
                )
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("overall discovery deadline exceeded")
                body = response.json()
                if time.monotonic() >= deadline:
                    raise TimeoutError("overall discovery deadline exceeded")
                page_rows = body.get(list_key) if isinstance(body, dict) else None
                if not isinstance(page_rows, list):
                    raise ValueError(f"response {list_key} must be a list")
            except TimeoutError:
                return _PageResult(
                    tuple(rows),
                    page_count,
                    False,
                    transport_error(
                        self.provider,
                        source,
                        TimeoutError("overall discovery deadline exceeded"),
                        credential,
                    ),
                    total_rows,
                )
            except (TypeError, ValueError) as error:
                return _PageResult(
                    tuple(rows),
                    page_count,
                    False,
                    transport_error(self.provider, f"{source} contract", error, credential),
                    total_rows,
                )
            page_count += 1
            available = max(0, row_limit - total_rows)
            accepted_rows = page_rows[:available]
            rows.extend(row for row in accepted_rows if isinstance(row, dict))
            total_rows += len(accepted_rows)
            if len(page_rows) > available:
                return _PageResult(
                    tuple(rows), page_count, False,
                    transport_error(
                        self.provider,
                        source,
                        RuntimeError(f"provider row limit {_MAX_ROWS} exceeded"),
                        credential,
                    ),
                    total_rows,
                )
            next_page = body.get("nextPageToken")
            if not next_page:
                return _PageResult(tuple(rows), page_count, True, total_rows=total_rows)
            if total_rows >= row_limit:
                return _PageResult(
                    tuple(rows), page_count, False,
                    transport_error(
                        self.provider,
                        source,
                        RuntimeError(f"provider row limit {_MAX_ROWS} exceeded"),
                        credential,
                    ),
                    total_rows,
                )
            if page_count >= _MAX_PAGES:
                return _PageResult(
                    tuple(rows), page_count, False,
                    transport_error(
                        self.provider,
                        source,
                        RuntimeError(f"provider page limit {_MAX_PAGES} exceeded"),
                        credential,
                    ),
                    total_rows,
                )
            token = str(next_page)
            if token in seen_tokens:
                return _PageResult(
                    tuple(rows), page_count, False,
                    transport_error(
                        self.provider,
                        source,
                        RuntimeError("repeated page token"),
                        credential,
                    ),
                    total_rows,
                )
            seen_tokens.add(token)
            page_params["pageToken"] = token

    def _map_model(
        self,
        row: dict,
        source: str,
        credential: str,
        config: DiscoveryConfig,
    ) -> DiscoveredOffer | None:
        upstream_model_id = _text(row.get("name"))
        state = _text(row.get("state"))
        if (
            not upstream_model_id
            or state not in {"", "READY"}
            or not _supports_serverless(row)
        ):
            return None
        upstream_model_id = validate_resource_id(
            upstream_model_id, field="upstream_model_id", credential=credential
        )
        imported_from = row.get("importedFrom")
        canonical_id = validate_resource_id(
            imported_from or upstream_model_id,
            field="canonical_id",
            credential=credential,
        )
        hugging_face_id = validate_hugging_face_id(
            row.get("huggingFaceUrl"), credential=credential
        )
        display_name = validate_display_name(
            row.get("displayName"), fallback=upstream_model_id, credential=credential
        )
        fine_tune = (
            validate_resource_id(
                row.get("fineTuningJob"), field="fine_tune", credential=credential
            )
            if row.get("fineTuningJob")
            else None
        )
        modalities = ("text", "image") if row.get("supportsImageInput") is True else ("text",)
        supported_parameters = ("tools",) if row.get("supportsTools") is True else ()
        retained = {
            "name": upstream_model_id,
            "state": row.get("state"),
            "kind": row.get("kind"),
            "supportsServerless": _supports_serverless(row),
            "importedFrom": imported_from,
            "fineTuningJob": fine_tune,
            "huggingFaceUrl": hugging_face_id,
        }
        validate_retained_value(retained, field="fireworks_model", credential=credential)
        raw_metadata = sanitize_metadata({"source": source, **retained}, credential)
        return DiscoveredOffer(
            provider=self.provider,
            upstream_model_id=upstream_model_id,
            base_url=_INFERENCE_BASE,
            credential_scope=config.credential_scope,
            identity=infer_identity_hint(
                upstream_model_id=upstream_model_id,
                display_name=display_name,
                canonical_id=canonical_id,
                hugging_face_id=hugging_face_id,
                fine_tune=fine_tune,
            ),
            capabilities=OfferCapabilities(
                context_window=_positive_int(row.get("contextLength")),
                modalities=modalities,
                supported_parameters=supported_parameters,
            ),
            pricing=OfferPricing(),
            raw_metadata=raw_metadata,
        )

    def _map_deployment(
        self,
        row: dict,
        credential: str,
        config: DiscoveryConfig,
    ) -> DiscoveredOffer | None:
        upstream_model_id = _text(row.get("name"))
        if not upstream_model_id or _text(row.get("state")) != "READY":
            return None
        upstream_model_id = validate_resource_id(
            upstream_model_id, field="upstream_model_id", credential=credential
        )
        base_model = validate_resource_id(
            row.get("baseModel"), field="base_model", credential=credential
        )
        display_name = validate_display_name(
            row.get("displayName"), fallback=upstream_model_id, credential=credential
        )
        placement = row.get("placement") if isinstance(row.get("placement"), dict) else {}
        residency = _residency(placement, row.get("region"))
        precision = _text(row.get("precision"))
        if precision.endswith("_UNSPECIFIED"):
            precision = ""
        retained = {
            "name": upstream_model_id,
            "baseModel": base_model,
            "state": row.get("state"),
            "precision": precision or None,
            "placement": {
                "region": placement.get("region"),
                "multiRegion": placement.get("multiRegion"),
                "regions": placement.get("regions"),
            },
        }
        validate_retained_value(retained, field="fireworks_deployment", credential=credential)
        raw_metadata = sanitize_metadata({"source": "deployments", **retained}, credential)
        return DiscoveredOffer(
            provider=self.provider,
            upstream_model_id=upstream_model_id,
            base_url=_INFERENCE_BASE,
            credential_scope=config.credential_scope,
            identity=infer_identity_hint(
                upstream_model_id=upstream_model_id,
                display_name=display_name,
                canonical_id=base_model,
                quantization=precision or None,
            ),
            capabilities=OfferCapabilities(
                context_window=_positive_int(row.get("maxContextLength")),
                modalities=("text",),
                residency=residency,
            ),
            pricing=OfferPricing(),
            raw_metadata=raw_metadata,
        )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _supports_serverless(row: dict) -> bool:
    return row.get("supportsServerless") is True or row.get("supports_serverless") is True


def _residency(placement: dict, fallback_region: Any) -> tuple[str, ...]:
    multi_region = _text(placement.get("multiRegion"))
    if multi_region and not multi_region.endswith("_UNSPECIFIED"):
        return (multi_region,)
    regions = placement.get("regions")
    if isinstance(regions, list):
        usable = tuple(
            region
            for region in regions
            if isinstance(region, str) and region and not region.endswith("_UNSPECIFIED")
        )
        if usable:
            return usable
    region = _text(placement.get("region")) or _text(fallback_region)
    return (region,) if region and not region.endswith("_UNSPECIFIED") else ()
