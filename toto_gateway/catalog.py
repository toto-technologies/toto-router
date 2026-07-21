"""The data-driven model/cartridge catalog (context doc §4, §9 guardrail #4).

Guardrail #4: never hard-code "the frontier model." Even the Phase-0 passthrough resolves
the incoming `model` field against this catalog to pick a lane + upstream + price. This file
is the seed of the Phase-1 routing catalog — it just grows capability/latency/exemplar fields.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .benchmarking.domain import CredentialScopeRef

Lane = Literal["economy", "frontier", "fake", "provider"]
Residency = Literal["in_perimeter", "cloud"]


class Price(BaseModel):
    prompt: float = 0.0  # USD per 1k prompt tokens
    completion: float = 0.0  # USD per 1k completion tokens
    # Cached prompt tokens (Usage.tokens_cached, a subset of prompt_tokens) bill at this fraction of
    # the prompt rate — a provider prefix-cache read is ~0.1x input, not full price (verified
    # per provider against published pricing). Optional; absent → 0.1, the
    # near-universal read discount. Set 1.0 to disable the discount for an entry.
    cache_read_multiplier: float = 0.1
    # Cached prompt tokens the provider WROTE this turn (Usage.tokens_cache_write) cost the base
    # prompt rate PLUS this multiplier's premium: total = base + write_tokens*prompt*(mult-1). A
    # write is more expensive than a plain input token (Anthropic 1.25x/5-min TTL). Optional; absent
    # → 1.0 (no premium — byte-identical cost for everyone whose provider doesn't charge one). Set
    # 1.25 on Anthropic-family entries.
    cache_write_multiplier: float = 1.0


# HISTORICAL RECORD, not configuration: the tier-word ids retired by the 2026-07-09 catalog
# rename and their canonical replacements. Exists ONLY so one-time data migrations (trace-row
# backfill, stored-policy-blob normalization at read) can repair references written before the
# rename. Nothing may resolve these at request time; the words themselves are banned from catalog
# ids by tests/test_catalog.py's naming guard. Do not add entries — this list is closed history.
LEGACY_MODEL_IDS: dict[str, str] = {
    "or-economy": "or-qwen3-coder-flash",
    "or-economy-general": "or-sonnet-4.6",  # llama retired 2026-07-12; the generalist is sonnet — repairs must land on a live id
    "or-flash": "or-gemini-2.5-flash",
    "or-haiku": "or-haiku-4.5",
    "or-frontier": "or-sonnet-4.6",
    "claude-frontier": "claude-sonnet-4.6",
    "gpt-frontier": "gpt-4o",
    "fw-economy": "fw-glm-5.2",
    "fw-economy-general": "fw-deepseek-v4-pro",
}


def normalize_legacy_id(model_id):
    """Canonical id for a possibly-pre-rename stored reference. Data-repair boundary only."""
    return LEGACY_MODEL_IDS.get(model_id, model_id)


class CatalogEntry(BaseModel):
    id: str
    # Extra client-facing ids that resolve to this same entry (e.g. a short name). Carried through
    # from the YAML so the console can show them; resolution against them is a caller concern.
    aliases: list[str] = Field(default_factory=list)
    lane: Lane
    # For local/fake: an OpenAI-compatible base URL (or the literal "fake").
    # For frontier: a provider keyword the runner registry understands (e.g. "anthropic").
    endpoint: str
    residency_class: Residency
    price_usd_per_1k: Price = Field(default_factory=Price)
    context_window: int = 8192
    # Provider prompt-cache TTL (seconds): how long a warm prefix stays cheap upstream. The SOURCE
    # OF TRUTH for the warmth-aware re-routing window (routing.smart.cache_ttl_s); absent → a
    # per-family heuristic default.
    cache_ttl_s: int | None = None
    # frontier alias → concrete upstream model id (never hard-coded as "the smart one").
    upstream_model: str | None = None
    # OpenAI-compatible provider knobs (decision: don't get pinned to one provider). Any
    # OpenAI-compatible host — OpenRouter, Together, Fireworks, Groq, a direct lab — is just a
    # catalog entry: endpoint=openai + a base_url + which env var holds the key. None base_url
    # = OpenAI default.
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    # Capability: this entry speaks native OpenAI tool calling (tools / tool_calls / role:"tool").
    # False → the wire coerces tool traffic to labeled text (runners/openai.py) and smart routing
    # never resolves a tools-bearing request here. Default True: coercion is the exception
    # (gemini-2.5-flash via OpenRouter aborts on a tool role, verified 2026-07-08).
    tools: bool = True
    # Provider-inventory provenance. Static YAML entries leave these unset; dynamic entries are
    # materialized from one immutable RoutingCandidate without mutating Catalog.models.
    identity_id: str | None = None
    offer_id: str | None = None
    provider: str | None = None
    credential_scope: CredentialScopeRef | None = None
    modalities: tuple[str, ...] = ()
    supported_parameters: tuple[str, ...] = ()
    max_output_tokens: int | None = None
    snapshot_completed_at: float | None = None
    snapshot_expires_at: float | None = None
    capability_residency: tuple[str, ...] = ()
    # Basename of the catalog fragment this entry last came from (set by Catalog.load) — provenance
    # for the console so a reader knows which file to edit. None when built directly (not via load).
    source: str | None = None
    # Where the PRICE came from (orthogonal to `source`): yaml = hand-maintained fragment fact,
    # discovered = provider-reported via inventory snapshot, manual = a stored operator/org
    # override applied at the effective_catalog seam. The console badges this so a reader knows
    # whether a number was verified against a provider or typed by a human.
    price_source: Literal["yaml", "discovered", "manual"] = "yaml"

    @property
    def effective_upstream_model(self) -> str:
        return self.upstream_model or self.id

    @property
    def resolved_base_url(self) -> str | None:
        """base_url with ${ENV} references expanded from the environment — the seam for providers
        whose URL embeds a non-secret account/region id (Cloudflare: .../accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1)
        alongside the secret in api_key_env. A base_url with no $ is returned unchanged, so every
        existing provider (OpenRouter, Fireworks, OpenAI) is byte-for-byte untouched. Use this
        wherever base_url becomes an outbound HTTP target; host-only uses (egress allowlist, provider
        grouping) can read the raw base_url since the host is never templated."""
        return os.path.expandvars(self.base_url) if self.base_url else None

    @property
    def credential_scope_label(self) -> str | None:
        scope = self.credential_scope
        return f"{scope.kind}:{scope.scope_id}" if scope is not None else None


class Catalog(BaseModel):
    models: list[CatalogEntry]

    def get(self, model_id: str) -> CatalogEntry | None:
        for entry in self.models:
            if entry.id == model_id:
                return entry
        return None

    def frontier_reference(self) -> CatalogEntry | None:
        """The entry whose price defines the frontier baseline (savings denominator, §13).

        First a real frontier-lane entry; else the first cloud-residency entry (covers the
        priced fake-frontier used in the offline demo). None if the catalog has no frontier.
        """
        for entry in self.models:
            if entry.lane == "frontier":
                return entry
        for entry in self.models:
            if entry.residency_class == "cloud":
                return entry
        return None

    def require(self, model_id: str) -> CatalogEntry:
        entry = self.get(model_id)
        if entry is None:
            known = ", ".join(e.id for e in self.models)
            raise UnknownModelError(model_id, known)
        return entry

    @classmethod
    def load(cls, path: str | Path) -> "Catalog":
        """Load one catalog file, or several composed left-to-right. `path` may be a comma-separated
        list — each file's `models` are merged, and a later file extends or overrides an earlier one
        by `id`. This lets each provider own its own fragment (e.g. catalog.fireworks.yaml) instead
        of being stuffed into another provider's file: TOTO_GW_CATALOG="base.yaml,catalog.fireworks.yaml"."""
        paths = [p.strip() for p in str(path).split(",") if p.strip()]
        if not paths:
            raise ValueError("empty catalog path")
        merged: dict[str, dict] = {}
        for p in paths:
            raw = yaml.safe_load(Path(p).read_text())
            if not raw or "models" not in raw:
                raise ValueError(f"catalog {p} has no 'models' key")
            for m in raw["models"]:
                merged[m["id"]] = {**m, "source": Path(p).name}  # later files override by id (source too); dict keeps first-seen order
        return cls.model_validate({"models": list(merged.values())})


# Banned tier words for catalog ids — a catalog id names a REAL model, never a tier (Alex ruling,
# 2026-07-10). This is the canonical copy of the CI naming guard in tests/test_catalog.py; adoptions
# enforce it at WRITE time because a stored adoption row can't join the frozen id↔upstream map the
# shipped YAML entries are pinned by. Keep in sync with test_catalog.BANNED_TIER_WORDS.
BANNED_TIER_WORDS = frozenset({"economy", "frontier", "flagship", "premium", "value", "balanced",
                               "general", "smart", "cheap", "fast", "best"})


def id_tier_words(model_id: str) -> set[str]:
    """The banned tier words appearing as segments of `model_id` (split on - and .). Empty = clean."""
    return set(model_id.replace(".", "-").split("-")) & BANNED_TIER_WORDS


def effective_catalog(base: Catalog, identity=None) -> Catalog:
    """The caller's effective catalog: `base` plus their server-side adoptions (catalog-adoption),
    with any stored price overrides applied last. `identity.catalog_adoptions` is a tuple of
    materialized CatalogEntry dicts (resolved at auth from the adoptions store); base wins on id
    collision and each adopted entry is stamped source='adopted'. `identity.price_overrides` is a
    mapping model_id → {prompt_usd_per_1k, completion_usd_per_1k, cache_read_multiplier|None}
    (deps merges team/org/platform scopes, narrower wins, before stamping) — a matching entry's
    Price is replaced and price_source becomes 'manual'; unknown ids are inert. Neither present
    (operator, driver-internal, or the common single-user case) → returns `base` UNCHANGED
    (zero cost, byte-identical routing). Duck-typed identity so the gateway needs no auth import."""
    adoptions = getattr(identity, "catalog_adoptions", None)
    overrides = getattr(identity, "price_overrides", None)
    models = base.models
    if adoptions:
        base_ids = {e.id for e in models}
        extra: list[CatalogEntry] = []
        for entry in adoptions:
            if not isinstance(entry, Mapping) or entry.get("id") in base_ids:  # base wins on collision
                continue
            extra.append(CatalogEntry.model_validate({**entry, "source": "adopted"}))
        if extra:
            models = [*models, *extra]
    if isinstance(overrides, Mapping) and overrides:
        priced: list[CatalogEntry] = []
        touched = False
        for entry in models:
            row = overrides.get(entry.id)
            if not isinstance(row, Mapping):
                priced.append(entry)
                continue
            mult = row.get("cache_read_multiplier")
            price = Price(
                prompt=float(row["prompt_usd_per_1k"]),
                completion=float(row["completion_usd_per_1k"]),
                cache_read_multiplier=(entry.price_usd_per_1k.cache_read_multiplier
                                       if mult is None else float(mult)),
                cache_write_multiplier=entry.price_usd_per_1k.cache_write_multiplier,
            )
            priced.append(entry.model_copy(update={"price_usd_per_1k": price,
                                                   "price_source": "manual"}))
            touched = True
        if touched:
            models = priced
    return base if models is base.models else Catalog(models=models)


class UnknownModelError(Exception):
    """Requested model id is not in the catalog."""

    def __init__(self, model_id: str, known: str) -> None:
        self.model_id = model_id
        self.known = known
        super().__init__(f"unknown model '{model_id}'. known: {known}")
