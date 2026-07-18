"""Tests for toto_gateway.pricing — cost math correctness."""

from __future__ import annotations

import pytest

from toto_gateway.catalog import CatalogEntry, Price
from toto_gateway.pricing import compute_cost_usd, frontier_baseline_cost_usd
from toto_gateway.schemas import Usage


def _entry(prompt_per_1k: float, completion_per_1k: float) -> CatalogEntry:
    """Helper to build a minimal CatalogEntry with specific pricing."""
    return CatalogEntry(
        id="test",
        lane="fake",
        endpoint="fake",
        residency_class="in_perimeter",
        price_usd_per_1k=Price(prompt=prompt_per_1k, completion=completion_per_1k),
    )


# --- compute_cost_usd ---


def test_known_cost_1000_each():
    """1000 prompt @ $3/1k + 1000 completion @ $15/1k = $18.00.

    price_usd_per_1k=3.0 means $3.00 per thousand tokens.
    (1000/1000)*3.0 + (1000/1000)*15.0 = 3.0 + 15.0 = $18.00.
    """
    entry = _entry(prompt_per_1k=3.0, completion_per_1k=15.0)
    usage = Usage.of(prompt=1000, completion=1000)
    cost = compute_cost_usd(entry, usage)
    assert cost == pytest.approx(18.0, abs=1e-9)


def test_known_cost_zero_completion():
    """1000 prompt @ $3/1k + 0 completion = $3.00."""
    entry = _entry(3.0, 15.0)
    usage = Usage.of(1000, 0)
    assert compute_cost_usd(entry, usage) == pytest.approx(3.0, abs=1e-9)


def test_known_cost_zero_prompt():
    """0 prompt + 500 completion @ $15/1k = $7.50."""
    entry = _entry(3.0, 15.0)
    usage = Usage.of(0, 500)
    assert compute_cost_usd(entry, usage) == pytest.approx(7.5, abs=1e-9)


def test_zero_price_local_lane():
    """Local lane with $0/$0 pricing always costs $0.0."""
    entry = _entry(0.0, 0.0)
    usage = Usage.of(10000, 5000)
    assert compute_cost_usd(entry, usage) == 0.0


def test_zero_usage_is_zero_cost():
    """Zero tokens = zero cost regardless of price."""
    entry = _entry(3.0, 15.0)
    usage = Usage.of(0, 0)
    assert compute_cost_usd(entry, usage) == 0.0


def test_rounded_to_6_decimals():
    """Result is rounded to 6 decimal places (not an unbounded float)."""
    entry = _entry(1.0, 1.0)
    usage = Usage.of(1, 1)  # 0.001/1000 + 0.001/1000 = 0.000002
    cost = compute_cost_usd(entry, usage)
    # round-trip through 6 decimal repr
    assert cost == round(cost, 6)


def test_large_token_count():
    """200k tokens: math doesn't overflow or lose precision for large counts."""
    entry = _entry(3.0, 15.0)
    usage = Usage.of(100_000, 100_000)
    # 100 * 3.0 + 100 * 15.0 = 300 + 1500 = 1800
    assert compute_cost_usd(entry, usage) == pytest.approx(1800.0, rel=1e-6)


def test_asymmetric_pricing():
    """Different prompt/completion rates compute independently."""
    entry = _entry(0.5, 2.0)
    usage = Usage.of(2000, 4000)
    # (2000/1000)*0.5 + (4000/1000)*2.0 = 1.0 + 8.0 = 9.0
    assert compute_cost_usd(entry, usage) == pytest.approx(9.0, abs=1e-9)


# --- cached-read discount ---


def test_cached_tokens_discounted():
    """Cached prompt tokens bill at the cache_read_multiplier (default 0.1) of the prompt rate.

    1000 prompt of which 800 cached @ $3/1k: 200*3/1k + 800*3/1k*0.1 = 0.6 + 0.24 = $0.84.
    """
    entry = _entry(3.0, 15.0)
    usage = Usage.of(prompt=1000, completion=0, cached=800)
    assert compute_cost_usd(entry, usage) == pytest.approx(0.84, abs=1e-9)


def test_cached_multiplier_configurable():
    """cache_read_multiplier=1.0 disables the discount → cached bills at full prompt rate."""
    entry = CatalogEntry(
        id="t", lane="fake", endpoint="fake", residency_class="in_perimeter",
        price_usd_per_1k=Price(prompt=3.0, completion=15.0, cache_read_multiplier=1.0),
    )
    usage = Usage.of(prompt=1000, completion=0, cached=800)
    assert compute_cost_usd(entry, usage) == pytest.approx(3.0, abs=1e-9)


def test_cached_never_exceeds_prompt():
    """A provider over-reporting cached > prompt can't drive cost negative (clamped to prompt)."""
    entry = _entry(3.0, 15.0)
    usage = Usage.of(prompt=100, completion=0, cached=999)
    # all 100 prompt tokens treated as cached: 100*3/1k*0.1 = 0.03
    assert compute_cost_usd(entry, usage) == pytest.approx(0.03, abs=1e-9)


# --- cache-write premium ---


def _entry_write(mult: float) -> CatalogEntry:
    return CatalogEntry(
        id="t", lane="fake", endpoint="fake", residency_class="in_perimeter",
        price_usd_per_1k=Price(prompt=3.0, completion=15.0, cache_write_multiplier=mult),
    )


def test_write_multiplier_default_is_no_op():
    """cache_write_multiplier defaults to 1.0 → write tokens bill at plain prompt price, so the
    presence of tokens_cache_write changes nothing vs. a call that never wrote to cache."""
    entry = _entry(3.0, 15.0)  # no write multiplier set → 1.0
    with_write = Usage.of(prompt=1000, completion=0, cache_write=400)
    without = Usage.of(prompt=1000, completion=0)
    assert compute_cost_usd(entry, with_write) == compute_cost_usd(entry, without)
    assert compute_cost_usd(entry, with_write) == pytest.approx(3.0, abs=1e-9)


def test_write_premium_added_on_top_of_base():
    """1000 prompt of which 400 written @ $3/1k, write mult 1.25: all 1000 at base ($3.00) plus the
    premium 400*3/1k*(1.25-1) = 400*3/1k*0.25 = $0.30 → $3.30. Writes are NOT double-charged base."""
    entry = _entry_write(1.25)
    usage = Usage.of(prompt=1000, completion=0, cache_write=400)
    assert compute_cost_usd(entry, usage) == pytest.approx(3.30, abs=1e-9)


def test_read_and_write_disjoint_slices():
    """A turn with both cache reads and writes: reads discounted, writes get the premium, the rest
    at base. 1000 prompt = 500 read + 300 write + 200 uncached, read mult 0.1, write mult 1.25:
    (500 write+uncached... ) — uncached=1000-500=500 at base=$1.50; read 500*3/1k*0.1=$0.15;
    write premium 300*3/1k*0.25=$0.225 → $1.875."""
    entry = CatalogEntry(
        id="t", lane="fake", endpoint="fake", residency_class="in_perimeter",
        price_usd_per_1k=Price(prompt=3.0, completion=15.0,
                               cache_read_multiplier=0.1, cache_write_multiplier=1.25),
    )
    usage = Usage.of(prompt=1000, completion=0, cached=500, cache_write=300)
    assert compute_cost_usd(entry, usage) == pytest.approx(1.875, abs=1e-9)


def test_write_clamped_to_uncached_slice():
    """A provider over-reporting writes can't inflate the premium past the full-price slice: with
    900 cached-reads on a 1000 prompt only 100 tokens remain full-price, so a claimed 999 writes are
    clamped to 100. uncached=100@base=$0.30, read 900*3/1k*0.1=$0.27, premium 100*3/1k*0.25=$0.075."""
    entry = CatalogEntry(
        id="t", lane="fake", endpoint="fake", residency_class="in_perimeter",
        price_usd_per_1k=Price(prompt=3.0, completion=15.0,
                               cache_read_multiplier=0.1, cache_write_multiplier=1.25),
    )
    usage = Usage.of(prompt=1000, completion=0, cached=900, cache_write=999)
    assert compute_cost_usd(entry, usage) == pytest.approx(0.30 + 0.27 + 0.075, abs=1e-9)


# --- frontier_baseline_cost_usd ---


def test_frontier_baseline_matches_frontier_price():
    """frontier_baseline_cost_usd uses the frontier entry's pricing.

    1000 prompt @ $3/1k + 1000 completion @ $15/1k = $18.00.
    """
    frontier = _entry(3.0, 15.0)
    usage = Usage.of(1000, 1000)
    baseline = frontier_baseline_cost_usd(frontier, usage)
    assert baseline == pytest.approx(18.0, abs=1e-9)


def test_frontier_baseline_and_compute_cost_equal_for_same_entry():
    """frontier_baseline_cost_usd and compute_cost_usd are equivalent given same entry."""
    entry = _entry(3.0, 15.0)
    usage = Usage.of(500, 200)
    assert frontier_baseline_cost_usd(entry, usage) == compute_cost_usd(entry, usage)


# --- catalog integration ---


def test_echo_local_zero_cost(catalog):
    """echo-local has $0 pricing so any usage costs $0."""

    entry = catalog.require("echo-local")
    usage = Usage.of(9999, 9999)
    assert compute_cost_usd(entry, usage) == 0.0


def test_echo_frontier_has_positive_cost(catalog):
    """echo-cloud has non-zero pricing so 1000/1000 tokens > $0."""
    entry = catalog.require("echo-cloud")
    usage = Usage.of(1000, 1000)
    assert compute_cost_usd(entry, usage) > 0.0
