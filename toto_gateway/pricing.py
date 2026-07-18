"""Cost computation from the data-driven catalog (never a hard-coded number; guardrail #4)."""

from __future__ import annotations

from .catalog import CatalogEntry
from .schemas import Usage


def compute_cost_usd(entry: CatalogEntry, usage: Usage) -> float:
    """USD cost of a call given catalog pricing and token usage. Rounded to 6 decimals.

    Cached-read tokens (usage.tokens_cached) and cache-WRITE tokens (usage.tokens_cache_write) are
    two DISJOINT subsets of prompt_tokens per the runner usage contract. Reads bill at
    price.cache_read_multiplier of the prompt rate (a prefix-cache read is ~0.1x input). Writes sit
    in the full-price slice already (they are prompt tokens at the base rate) PLUS a write premium:
    write_tokens * prompt * (cache_write_multiplier - 1) — 0 when the multiplier defaults to 1.0.
    When both cache counts are 0 (fakes, un-cached calls) this is byte-identical to full pricing."""
    price = entry.price_usd_per_1k
    cached = min(max(usage.tokens_cached, 0), usage.prompt_tokens)  # read slice; guard bad data
    uncached = usage.prompt_tokens - cached
    write = min(max(usage.tokens_cache_write, 0), uncached)  # write slice, disjoint from reads
    cost = (uncached / 1000.0) * price.prompt \
        + (cached / 1000.0) * price.prompt * price.cache_read_multiplier \
        + (write / 1000.0) * price.prompt * (price.cache_write_multiplier - 1.0) \
        + (usage.completion_tokens / 1000.0) * price.completion
    return round(cost, 6)


def frontier_baseline_cost_usd(frontier_entry: CatalogEntry, usage: Usage) -> float:
    """What this call *would* have cost on the frontier — the savings denominator (§13)."""
    return compute_cost_usd(frontier_entry, usage)
