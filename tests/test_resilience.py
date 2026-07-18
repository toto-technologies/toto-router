"""Unit tests for the shared resilience primitives (toto_gateway/resilience.py).

The single home for the retry/fallback policy both the Gateway (passthrough) and the Driver use.
These pin the pure logic in isolation — the wire-level behaviors are in test_resilience_wire.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.resilience import (
    backoff,
    err_label,
    fallbacks,
    is_retryable,
    retry_after_seconds,
)


def _cat() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id="a", lane="frontier", endpoint="openai", residency_class="cloud"),
        CatalogEntry(id="b", lane="economy", endpoint="openai", residency_class="cloud"),
        CatalogEntry(id="local", lane="economy", endpoint="openai", residency_class="in_perimeter"),
        CatalogEntry(id="fk", lane="fake", endpoint="fake", residency_class="in_perimeter"),
    ])


# --- is_retryable -------------------------------------------------------------------------

def test_is_retryable_status_and_exception_classes():
    assert is_retryable(SimpleNamespace(status_code=429))
    assert is_retryable(SimpleNamespace(status_code=500))
    assert is_retryable(SimpleNamespace(status_code=503))
    assert not is_retryable(SimpleNamespace(status_code=400))
    assert not is_retryable(SimpleNamespace(status_code=404))
    assert is_retryable(ConnectionError()) and is_retryable(TimeoutError())
    assert not is_retryable(ValueError("nope"))


# --- fallbacks ----------------------------------------------------------------------------

def test_fallbacks_same_residency_excludes_self_and_fake():
    assert fallbacks(_cat(), "a") == ["b"]           # same cloud residency, self + fake dropped


def test_fallbacks_privacy_never_crosses_residency():
    # local is the only in_perimeter openai model → no fallback (never leaks to cloud).
    assert fallbacks(_cat(), "local") == []
    assert fallbacks(_cat(), "local", privacy=True) == []


def test_fallbacks_privacy_bounds_lane_too():
    cat = Catalog(models=[
        CatalogEntry(id="p1", lane="economy", endpoint="openai", residency_class="in_perimeter"),
        CatalogEntry(id="p2", lane="economy", endpoint="openai", residency_class="in_perimeter"),
        CatalogEntry(id="p3", lane="frontier", endpoint="openai", residency_class="in_perimeter"),
    ])
    assert fallbacks(cat, "p1") == ["p2", "p3"]                 # residency only
    assert fallbacks(cat, "p1", privacy=True) == ["p2"]        # + same lane


def test_fallbacks_unknown_model_is_empty():
    assert fallbacks(_cat(), "nonexistent") == []


# --- retry_after_seconds + backoff (P2 semantics) -----------------------------------------

def test_retry_after_delta_seconds():
    exc = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "12"}))
    assert retry_after_seconds(exc) == 12.0


def test_retry_after_http_date_is_seconds_from_now():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    exc = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": format_datetime(when)}))
    got = retry_after_seconds(exc)
    assert got is not None and 25 <= got <= 31  # ~30s, allowing clock skew


def test_retry_after_absent_or_bad_is_none():
    assert retry_after_seconds(ValueError()) is None
    assert retry_after_seconds(SimpleNamespace(response=SimpleNamespace(headers={}))) is None
    assert retry_after_seconds(
        SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "soon"}))) is None


def test_backoff_honors_retry_after_capped():
    assert backoff(0, 0.5, retry_after=7.0, cap=30) == 7.0
    assert backoff(0, 0.5, retry_after=9999, cap=30) == 30.0   # bogus header can't wedge a worker


def test_backoff_exponential_with_jitter_when_no_retry_after():
    # base*2^attempt + jitter[0,base): attempt 3, base 0.5 → [4.0, 4.5)
    for _ in range(20):
        v = backoff(3, 0.5)
        assert 4.0 <= v < 4.5


def test_backoff_zero_base_is_zero():
    assert backoff(5, 0.0) == 0.0   # tests run with backoff_base=0 for speed


# --- err_label ----------------------------------------------------------------------------

def test_err_label():
    assert err_label(SimpleNamespace(status_code=429)) == "429"
    assert err_label(ConnectionError()) == "ConnectionError"
    assert err_label(None) == "unknown"
