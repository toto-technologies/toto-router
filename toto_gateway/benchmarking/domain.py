"""Immutable provider-inventory records shared by discovery, storage, and routing snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProviderName = Literal["openrouter", "fireworks"]
PersistedInventoryStatus = Literal["success", "partial", "failed"]
InventoryStatus = Literal["success", "partial", "failed", "stale"]
CredentialScopeKind = Literal["platform", "organization", "user"]

_SAFE_METADATA_KEYS = {"key_id"}
_SENSITIVE_KEY_WORDS = {
    "auth",
    "authorization",
    "bearer",
    "credential",
    "key",
    "password",
    "secret",
    "token",
}
_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "cipher",
    "encrypt",
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\bsk[-_][a-z0-9][a-z0-9_-]{5,}"),
    re.compile(r"\bgAAAA[A-Za-z0-9_-]{12,}"),
)
_FIREWORKS_VALUE_CANDIDATE_RE = re.compile(r"\bfw_[A-Za-z0-9_]{16,}\b")
_ERROR_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|access[_-]?token|api[_-]?key|credential(?:[_-]?[a-z0-9]+)?|"
    r"encrypted[_-]?key|cipher(?:text)?|password|secret|token)\s*[:=]\s*[^\s;,]+"
)
MAX_ERROR_SUMMARY_LENGTH = 512


def _normalized_metadata_key(key: object) -> str:
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key).strip())
    return re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")


def _is_sensitive_metadata_key(key: object) -> bool:
    normalized = _normalized_metadata_key(key)
    if normalized in _SAFE_METADATA_KEYS:
        return False
    words = tuple(part for part in normalized.split("_") if part)
    compact = "".join(words)
    return (
        any(word in _SENSITIVE_KEY_WORDS for word in words)
        or any(fragment in compact for fragment in _SENSITIVE_KEY_FRAGMENTS)
        or compact.endswith(("key", "token", "auth"))
    )


def _is_fireworks_credential(value: str) -> bool:
    suffix = value[3:] if value.startswith("fw_") else ""
    return (
        len(suffix) >= 16
        and suffix.isalnum()
        and any(character.islower() for character in suffix)
        and any(character.isupper() for character in suffix)
        and any(character.isdigit() for character in suffix)
    )


def _contains_secret_value(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS) or any(
        _is_fireworks_credential(match.group())
        for match in _FIREWORKS_VALUE_CANDIDATE_RE.finditer(value)
    )


def _assert_safe_metadata(value: object, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_sensitive_metadata_key(key):
                raise ValueError(f"secret-bearing metadata key at {path}.{key}")
            _assert_safe_metadata(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_metadata(child, f"{path}[{index}]")
    elif isinstance(value, str) and _contains_secret_value(value):
        raise ValueError(f"secret-bearing metadata value at {path}")


def sanitize_error_summary(value: str | None) -> str | None:
    """Redact common credential shapes before retaining a bounded provider error summary."""
    if value is None:
        return None
    sanitized = str(value)
    for pattern in _SECRET_VALUE_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    sanitized = _FIREWORKS_VALUE_CANDIDATE_RE.sub(
        lambda match: "[REDACTED]" if _is_fireworks_credential(match.group()) else match.group(),
        sanitized,
    )
    sanitized = _ERROR_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", sanitized)
    return sanitized[:MAX_ERROR_SUMMARY_LENGTH]


def _stable_id(prefix: str, *parts: str) -> str:
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def stable_offer_id(provider: ProviderName, upstream_model_id: str) -> str:
    """Stable across snapshots: one provider/upstream callable realization."""
    return _stable_id("offer", provider, upstream_model_id)


def stable_snapshot_offer_id(snapshot_id: str, offer_id: str) -> str:
    return _stable_id("snapshot_offer", snapshot_id, offer_id)


def stable_route_id(provider: ProviderName, upstream_model_id: str) -> str:
    """The public, provider-qualified dynamic route ID required by chunk 1."""
    if not upstream_model_id or upstream_model_id != upstream_model_id.strip():
        raise ValueError("upstream_model_id must be non-empty without surrounding whitespace")
    return f"{provider}/{upstream_model_id}"


class FrozenRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class CredentialScopeRef(FrozenRecord):
    kind: CredentialScopeKind
    scope_id: str = Field(min_length=1)


class Actor(FrozenRecord):
    actor_id: str = Field(min_length=1)
    org_id: str | None = None
    kind: Literal["user", "agent", "operator", "system"] = "user"


class OfferCapabilities(FrozenRecord):
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    modalities: tuple[str, ...] = ()
    supported_parameters: tuple[str, ...] = ()
    residency: tuple[str, ...] = ()


class OfferPricing(FrozenRecord):
    prompt_usd_per_1k: float | None = Field(default=None, ge=0)
    completion_usd_per_1k: float | None = Field(default=None, ge=0)
    request_usd: float | None = Field(default=None, ge=0)
    image_usd: float | None = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"


class ModelIdentity(FrozenRecord):
    identity_id: str = Field(min_length=1)
    vendor: str = Field(min_length=1)
    family: str = Field(min_length=1)
    release: str | None = None
    reasoning_variant: str | None = None
    quantization: str | None = None
    fine_tune: str | None = None
    context_variant: str | None = None
    display_name: str = Field(min_length=1)
    provisional: bool = False
    created_at: float


class IdentityAliasDecision(FrozenRecord):
    alias_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    identity_id: str = Field(min_length=1)
    method: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    evidence: dict = Field(default_factory=dict)
    reviewer: str | None = None
    decided_at: float
    superseded_at: float | None = None

    @field_validator("evidence")
    @classmethod
    def evidence_cannot_carry_secrets(cls, value: dict) -> dict:
        _assert_safe_metadata(value, "evidence")
        return value

    @model_validator(mode="after")
    def supersession_follows_decision(self) -> "IdentityAliasDecision":
        if self.superseded_at is not None and self.superseded_at < self.decided_at:
            raise ValueError("superseded_at cannot precede decided_at")
        return self


class ProviderOffer(FrozenRecord):
    snapshot_offer_id: str
    offer_id: str
    identity_id: str
    route_id: str
    provider: ProviderName
    upstream_model_id: str
    base_url: str
    credential_scope: CredentialScopeRef
    capabilities: OfferCapabilities
    pricing: OfferPricing
    adapter_revision: str
    raw_metadata: dict

    @field_validator("base_url")
    @classmethod
    def base_url_cannot_embed_credentials(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("raw_metadata")
    @classmethod
    def raw_metadata_cannot_carry_secrets(cls, value: dict) -> dict:
        _assert_safe_metadata(value, "raw_metadata")
        return value

    @model_validator(mode="after")
    def ids_match_provider_route(self) -> "ProviderOffer":
        expected_offer = stable_offer_id(self.provider, self.upstream_model_id)
        if self.offer_id != expected_offer:
            raise ValueError(f"offer_id must equal {expected_offer}")
        expected_route = stable_route_id(self.provider, self.upstream_model_id)
        if self.route_id != expected_route:
            raise ValueError(f"route_id must equal {expected_route}")
        return self


class InventorySnapshot(FrozenRecord):
    snapshot_id: str = Field(min_length=1)
    provider: ProviderName
    credential_scope: CredentialScopeRef
    status: InventoryStatus
    started_at: float
    completed_at: float
    expires_at: float
    pagination_complete: bool
    adapter_revision: str = Field(min_length=1)
    source_metadata: dict = Field(default_factory=dict)
    error_summary: str | None = Field(default=None, max_length=MAX_ERROR_SUMMARY_LENGTH)
    offers: tuple[ProviderOffer, ...] = ()

    @field_validator("source_metadata")
    @classmethod
    def source_metadata_cannot_carry_secrets(cls, value: dict) -> dict:
        _assert_safe_metadata(value, "source_metadata")
        return value

    @field_validator("error_summary", mode="before")
    @classmethod
    def sanitize_retained_error_summary(cls, value: str | None) -> str | None:
        return sanitize_error_summary(value)

    @model_validator(mode="after")
    def timestamps_and_status_are_consistent(self) -> "InventorySnapshot":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.expires_at < self.completed_at:
            raise ValueError("expires_at cannot precede completed_at")
        if self.status == "success" and not self.pagination_complete:
            raise ValueError("a successful snapshot must complete pagination")
        return self


class RoutingCandidate(FrozenRecord):
    snapshot_id: str
    snapshot_completed_at: float
    snapshot_expires_at: float
    snapshot_offer_id: str
    offer_id: str
    identity_id: str
    route_id: str
    provider: ProviderName
    upstream_model_id: str
    base_url: str
    credential_scope: CredentialScopeRef
    capabilities: OfferCapabilities
    pricing: OfferPricing
    adapter_revision: str
    raw_metadata: dict
