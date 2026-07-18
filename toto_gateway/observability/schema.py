"""Normalized shapes both provider connectors emit — the contract insights.py consumes.

Anthropic and OpenAI report the same three things in different envelopes (token usage
buckets, cost buckets, org members). Each connector flattens its provider's envelope
into these frozen dataclasses at the fetch boundary; nothing downstream ever sees a raw
provider payload. Money is always float USD dollars here — Anthropic reports decimal-
string cents, OpenAI reports float dollars; connectors normalize, insights never converts.
"""

from __future__ import annotations

from dataclasses import dataclass


class AdminAPIError(Exception):
    """A provider admin-API failure the route layer can translate honestly (401 bad key,
    403 wrong key type/plan, 429 rate limited, 5xx provider down)."""

    def __init__(self, status_code: int, message: str, *, provider: str = ""):
        self.status_code = status_code
        self.provider = provider
        super().__init__(message)


@dataclass(frozen=True)
class UsageBucket:
    """One time-bucket of token usage, at whatever grouping the connector requested."""

    provider: str                 # "anthropic" | "openai"
    starting_at: str              # ISO-8601 UTC bucket start
    ending_at: str                # ISO-8601 UTC bucket end
    model: str | None             # None when the provider didn't group by model
    scope_id: str | None          # anthropic workspace_id / openai project_id
    scope_name: str | None        # resolved display name when the connector knows it
    actor_id: str | None          # anthropic api_key_id|account_id / openai user_id|api_key_id
    actor_name: str | None        # resolved key name / user email when known
    input_tokens: int             # uncached input tokens
    cached_input_tokens: int      # cache-read input tokens
    cache_creation_tokens: int    # cache-write tokens (anthropic; 0 for openai)
    output_tokens: int
    requests: int | None          # openai num_model_requests; None where not reported


@dataclass(frozen=True)
class CostBucket:
    """One time-bucket of billed cost. amount_usd is DOLLARS (already normalized)."""

    provider: str
    starting_at: str
    ending_at: str
    model: str | None             # parsed from description/line_item when present
    line_item: str | None         # provider's own description string, verbatim
    scope_id: str | None          # anthropic workspace_id / openai project_id
    amount_usd: float


@dataclass(frozen=True)
class OrgMember:
    """One human in the provider org."""

    provider: str
    id: str
    email: str | None
    name: str | None
    role: str | None
    added_at: str | None          # ISO-8601 when the provider reports it
