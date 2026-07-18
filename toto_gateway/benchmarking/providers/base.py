"""Provider-inventory contracts and credential-safe boundary helpers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator, model_validator

from ..domain import (
    CredentialScopeRef,
    FrozenRecord,
    OfferCapabilities,
    OfferPricing,
    PersistedInventoryStatus,
    ProviderName,
    sanitize_error_summary,
)

_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "ciphertext",
    "credential",
    "credential_value",
    "password",
    "secret",
    "token",
}
_RESOURCE_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+:-]*(?:/[A-Za-z0-9][A-Za-z0-9._+:-]*)+"
)
_NON_SECRET_CREDENTIAL_PARTS = {
    "audit",
    "contract",
    "fireworks",
    "key",
    "openrouter",
    "private",
    "prod",
    "valid",
}


class UnsafeProviderRow(ValueError):
    """A provider row contains unsafe or structurally invalid retained data."""


class DiscoveryConfig(FrozenRecord):
    credential_scope: CredentialScopeRef
    account_id: str = ""
    discover_deployments: bool = False
    # Enumerate the credential's own accounts (GET /v1/accounts) and discover each one's models
    # and deployments. Used for user/org scopes, where the key — not platform settings — is the
    # authority on which accounts exist. Ignored when account_id is set explicitly.
    discover_accounts: bool = False

    @field_validator("account_id")
    @classmethod
    def account_id_is_a_path_segment(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise ValueError("account_id must be a single provider path segment")
        return value


class IdentityHint(FrozenRecord):
    vendor: str = Field(min_length=1)
    family: str = Field(min_length=1)
    release: str | None = None
    reasoning_variant: str | None = None
    quantization: str | None = None
    fine_tune: str | None = None
    context_variant: str | None = None
    display_name: str = Field(min_length=1)
    canonical_id: str | None = None
    hugging_face_id: str | None = None


class DiscoveredOffer(FrozenRecord):
    provider: ProviderName
    upstream_model_id: str = Field(min_length=1)
    base_url: str
    credential_scope: CredentialScopeRef
    identity: IdentityHint
    capabilities: OfferCapabilities = Field(default_factory=OfferCapabilities)
    pricing: OfferPricing = Field(default_factory=OfferPricing)
    raw_metadata: dict = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def base_url_is_safe_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        return value.rstrip("/")


class DiscoveryResult(FrozenRecord):
    provider: ProviderName
    adapter_revision: str = Field(min_length=1)
    status: PersistedInventoryStatus
    pagination_complete: bool
    offers: tuple[DiscoveredOffer, ...] = ()
    source_metadata: dict = Field(default_factory=dict)
    error_summary: str | None = None

    @model_validator(mode="after")
    def status_matches_payload(self) -> "DiscoveryResult":
        if self.status == "success" and not self.pagination_complete:
            raise ValueError("successful discovery must complete pagination")
        if self.status == "failed" and self.offers:
            raise ValueError("failed discovery cannot contain offers")
        return self


class ProviderInventoryAdapter(ABC):
    provider: ProviderName
    adapter_revision: str

    @abstractmethod
    async def discover(self, credential: str, config: DiscoveryConfig) -> DiscoveryResult:
        """Discover the offers visible to exactly one credential scope."""


def sanitize_text(value: object, credential: str) -> str:
    """Remove a credential and useful fragments from provider-controlled error text."""
    text = str(value)
    fragments = {credential}
    fragments.update(
        part
        for part in re.split(r"[^A-Za-z0-9]+", credential)
        if len(part) >= 6 and part.casefold() not in _NON_SECRET_CREDENTIAL_PARTS
    )
    if len(credential) >= 12:
        fragments.update({credential[:8], credential[-8:]})
    for fragment in sorted((part for part in fragments if part), key=len, reverse=True):
        text = re.sub(re.escape(fragment), "[redacted]", text, flags=re.IGNORECASE)
    return (sanitize_error_summary(text) or "")[:500]


def sanitize_metadata(value: Any, credential: str) -> Any:
    """Recursively remove secret-shaped fields and credential fragments."""
    if isinstance(value, dict):
        safe = {}
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS:
                continue
            safe[str(key)] = sanitize_metadata(child, credential)
        return safe
    if isinstance(value, (list, tuple)):
        return [sanitize_metadata(child, credential) for child in value]
    if isinstance(value, str):
        return sanitize_text(value, credential)
    return value


def validate_resource_id(value: object, *, field: str, credential: str) -> str:
    text = _provider_text(value, field=field, credential=credential)
    if not _RESOURCE_ID_RE.fullmatch(text):
        raise UnsafeProviderRow(f"invalid {field}")
    return text


def validate_hugging_face_id(
    value: object,
    *,
    credential: str,
) -> str | None:
    if value is None or value == "":
        return None
    text = _provider_text(value, field="hugging_face_id", credential=credential)
    parsed = urlsplit(text)
    if parsed.scheme:
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != "huggingface.co"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise UnsafeProviderRow("invalid hugging_face_id")
        resource = parsed.path.strip("/")
    else:
        resource = text.strip("/")
    if not _RESOURCE_ID_RE.fullmatch(resource):
        raise UnsafeProviderRow("invalid hugging_face_id")
    return text


def validate_display_name(value: object, *, fallback: str, credential: str) -> str:
    # Display names are cosmetic — surrounding whitespace (e.g. OpenRouter "Gemma 4 A4B ")
    # is provider sloppiness, not an unsafe row. Trim it rather than quarantine the offer.
    text = value.strip() if isinstance(value, str) else value
    if text is None or text == "":
        return fallback
    return _provider_text(text, field="display_name", credential=credential)


def validate_retained_value(value: Any, *, field: str, credential: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            validate_retained_value(child, field=f"{field}.{key}", credential=credential)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            validate_retained_value(child, field=f"{field}[{index}]", credential=credential)
    elif isinstance(value, str):
        # Retained metadata is optional/informational — an empty string means the provider
        # simply omitted the field (e.g. Fireworks importedFrom="", OpenRouter expiration_date="").
        # Only non-empty values carry content worth the safety scan; empty is benign, not unsafe.
        # (validate_resource_id/display_name/hugging_face_id still reject empty on their own.)
        if value:
            _provider_text(value, field=field, credential=credential)


def _provider_text(value: object, *, field: str, credential: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise UnsafeProviderRow(f"invalid {field}")
    if sanitize_text(value, credential) != value:
        raise UnsafeProviderRow(f"unsafe {field}")
    return value


def response_error(
    provider: ProviderName,
    source: str,
    response: httpx.Response,
    credential: str,
) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                detail = error.get("message") or error
            else:
                detail = error or body.get("message") or body
        else:
            detail = body
    except Exception:  # noqa: BLE001 - non-JSON provider error
        detail = response.text
    safe = sanitize_text(detail, credential)
    return f"{provider} {source} failed (HTTP {response.status_code}): {safe}"


def transport_error(
    provider: ProviderName,
    source: str,
    error: Exception,
    credential: str,
) -> str:
    detail = sanitize_text(error, credential)
    return f"{provider} {source} failed ({type(error).__name__}): {detail}"
