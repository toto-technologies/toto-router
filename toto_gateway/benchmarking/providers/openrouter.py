"""Authenticated OpenRouter user-model inventory adapter."""

from __future__ import annotations

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

_MANAGEMENT_BASE = "https://openrouter.ai"
_INFERENCE_BASE = "https://openrouter.ai/api/v1"


class OpenRouterInventoryAdapter(ProviderInventoryAdapter):
    provider = "openrouter"
    adapter_revision = "openrouter-user-models-v1"

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._transport = transport

    async def discover(self, credential: str, config: DiscoveryConfig) -> DiscoveryResult:
        async with httpx.AsyncClient(
            base_url=_MANAGEMENT_BASE,
            timeout=self._timeout,
            transport=self._transport,
            headers={"Authorization": f"Bearer {credential}"},
        ) as client:
            try:
                response = await client.get("/api/v1/models/user")
            except httpx.HTTPError as error:
                summary = transport_error(self.provider, "models/user", error, credential)
                return self._failed(summary)
        if response.status_code >= 400:
            return self._failed(response_error(self.provider, "models/user", response, credential))
        try:
            body = response.json()
            rows = body.get("data") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise ValueError("response data must be a list")
            mapped = []
            quarantined = 0
            for row in rows:
                try:
                    mapped.append(self._map_offer(row, credential, config))
                except UnsafeProviderRow:
                    quarantined += 1
            offers = tuple(sorted(mapped, key=lambda offer: offer.upstream_model_id))
        except (TypeError, ValueError) as error:
            return self._failed(
                transport_error(self.provider, "models/user contract", error, credential)
            )
        return DiscoveryResult(
            provider=self.provider,
            adapter_revision=self.adapter_revision,
            # Quarantined rows are individually rejected by the safety boundary — the enumeration
            # itself completed. Demoting the snapshot to "partial" would hide every clean offer
            # (the read path only surfaces success snapshots), letting one sloppy row blank the
            # whole provider. Keep the count visible via error_summary + quarantined_rows.
            status="success",
            pagination_complete=True,
            offers=offers,
            source_metadata={
                "endpoint": "/api/v1/models/user",
                "pages": 1,
                "quarantined_rows": quarantined,
            },
            error_summary=(
                f"openrouter quarantined {quarantined} unsafe provider row(s)"
                if quarantined
                else None
            ),
        )

    def _failed(self, error_summary: str) -> DiscoveryResult:
        return DiscoveryResult(
            provider=self.provider,
            adapter_revision=self.adapter_revision,
            status="failed",
            pagination_complete=False,
            source_metadata={"endpoint": "/api/v1/models/user", "pages": 0},
            error_summary=error_summary,
        )

    def _map_offer(
        self,
        row: object,
        credential: str,
        config: DiscoveryConfig,
    ) -> DiscoveredOffer:
        if not isinstance(row, dict):
            raise UnsafeProviderRow("provider row must be an object")
        upstream_model_id = validate_resource_id(
            row.get("id"), field="upstream_model_id", credential=credential
        )
        canonical_value = row.get("canonical_slug") or upstream_model_id
        canonical_id = validate_resource_id(
            canonical_value, field="canonical_id", credential=credential
        )
        hugging_face_id = validate_hugging_face_id(
            row.get("hugging_face_id"), credential=credential
        )
        display_name = validate_display_name(
            row.get("name"), fallback=upstream_model_id, credential=credential
        )
        architecture = row.get("architecture") if isinstance(row.get("architecture"), dict) else {}
        top_provider = row.get("top_provider") if isinstance(row.get("top_provider"), dict) else {}
        pricing = row.get("pricing") if isinstance(row.get("pricing"), dict) else {}
        modalities = tuple(dict.fromkeys(
            _string_list(architecture.get("input_modalities"))
            + _string_list(architecture.get("output_modalities"))
        ))
        supported_parameters = tuple(_string_list(row.get("supported_parameters")))
        retained = {
            "canonical_slug": canonical_id,
            "hugging_face_id": hugging_face_id,
            "created": row.get("created"),
            "expiration_date": row.get("expiration_date"),
            "modalities": modalities,
            "supported_parameters": supported_parameters,
        }
        validate_retained_value(retained, field="openrouter", credential=credential)
        raw_metadata = sanitize_metadata({"source": "models/user", **retained}, credential)
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
            ),
            capabilities=OfferCapabilities(
                context_window=_positive_int(
                    top_provider.get("context_length") or row.get("context_length")
                ),
                max_output_tokens=_positive_int(top_provider.get("max_completion_tokens")),
                modalities=modalities,
                supported_parameters=supported_parameters,
            ),
            pricing=OfferPricing(
                prompt_usd_per_1k=_per_token_to_per_1k(pricing.get("prompt")),
                completion_usd_per_1k=_per_token_to_per_1k(pricing.get("completion")),
                request_usd=_nonnegative_float(pricing.get("request")),
                image_usd=_nonnegative_float(pricing.get("image")),
            ),
            raw_metadata=raw_metadata,
        )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _nonnegative_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _per_token_to_per_1k(value: Any) -> float | None:
    parsed = _nonnegative_float(value)
    return parsed * 1000 if parsed is not None else None
