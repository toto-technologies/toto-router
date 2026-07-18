"""Shared provider-I/O resilience policy (Wave 1).

ONE home for the retry/fallback primitives BOTH planes use — the passthrough Gateway
(gateway.complete) and the Driver (driver.core.Driver._call). Extracted from the driver's
tested logic (it proved these in prod) so there is a single implementation, not a copy:

  - is_retryable(exc)              — 429 / 5xx / timeout / connection are transient; 4xx never is.
  - fallbacks(catalog, id, ...)    — same-residency catalog entries to try when a model keeps failing.
  - retry_after_seconds(exc)       — honor an upstream Retry-After header (delta-seconds or HTTP-date).
  - backoff(attempt, base, ...)    — Retry-After if present, else exp backoff + jitter, capped.

All catalog-driven and provider-agnostic: no hardcoded "openai"/"anthropic", keyed off
entry.residency_class / entry.lane (the residency bound is also the privacy bound — never leak
a request across residency, even under fallback).
"""

from __future__ import annotations

import random

from .catalog import Catalog


def is_retryable(exc: BaseException) -> bool:
    """Retry transient provider failures (429 / 5xx / timeout / connection); NEVER client errors
    (4xx auth/validation). Classify on the SDK exception TYPE + status_code, not the message —
    OpenRouter nests the upstream code in error.metadata.raw, but the SDK still raises the right
    top-level type."""
    try:
        import openai

        if isinstance(exc, (openai.RateLimitError, openai.APITimeoutError,
                            openai.APIConnectionError, openai.InternalServerError)):
            return True
        if isinstance(exc, openai.APIStatusError):
            return exc.status_code >= 500  # 4xx (auth/validation/not-found) is not retryable
    except Exception:
        pass
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def err_label(exc: BaseException | None) -> str:
    """Short, honest tag for a fallback note / span reason, e.g. '429' or 'APIConnectionError'."""
    if exc is None:
        return "unknown"
    status = getattr(exc, "status_code", None)
    return str(status) if isinstance(status, int) else type(exc).__name__


def fallbacks(catalog: Catalog, model_id: str, *, privacy: bool = False) -> list[str]:
    """Catalog entries we may retry on when model_id keeps failing. Same residency_class ALWAYS
    (never leak across the privacy boundary); same lane too when the route was privacy/guard
    pinned. Fake lane excluded. Catalog order — simple beats re-scoring."""
    entry = catalog.get(model_id)
    if entry is None:
        return []
    out = []
    for e in catalog.models:
        if e.id == model_id or e.endpoint == "fake":
            continue
        if e.residency_class != entry.residency_class:
            continue
        if privacy and e.lane != entry.lane:
            continue
        out.append(e.id)
    return out


def retry_after_seconds(exc: BaseException) -> float | None:
    """Seconds to wait per the upstream's `Retry-After` header, if the exception carries an HTTP
    response with one. Handles both the delta-seconds form (`Retry-After: 3`) and the HTTP-date
    form. None when absent/unparseable → the caller falls back to exponential backoff."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        val = headers.get("retry-after")
    except Exception:
        return None
    if not val:
        return None
    try:
        return max(0.0, float(val))  # delta-seconds
    except (TypeError, ValueError):
        pass
    try:  # HTTP-date form → seconds from now
        import time
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(val)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:
        return None


def backoff(attempt: int, base: float, *, retry_after: float | None = None,
            cap: float = 30.0) -> float:
    """Sleep (seconds) before the next attempt. Honor an upstream Retry-After when present (so we
    never re-hit a 429 before its advertised cooldown), else exponential backoff + jitter. Capped
    both ways so a bogus `Retry-After: 9999` can't wedge a worker for hours."""
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, cap)
    return min(base * (2 ** attempt) + random.uniform(0, base), cap)


def _demo() -> None:
    """Self-check (no network): the four primitives on realistic inputs."""
    from types import SimpleNamespace

    cat = Catalog.model_validate({"models": [
        {"id": "a", "lane": "frontier", "endpoint": "openai", "residency_class": "cloud"},
        {"id": "b", "lane": "frontier", "endpoint": "openai", "residency_class": "cloud"},
        {"id": "local", "lane": "economy", "endpoint": "openai", "residency_class": "in_perimeter"},
        {"id": "f", "lane": "fake", "endpoint": "fake", "residency_class": "in_perimeter"},
    ]})
    # fallbacks stay within residency and drop self + fake.
    assert fallbacks(cat, "a") == ["b"], fallbacks(cat, "a")
    assert fallbacks(cat, "local") == [], fallbacks(cat, "local")  # never crosses to cloud
    # retryable classification (status_code duck-typing path).
    assert is_retryable(SimpleNamespace(status_code=429))
    assert is_retryable(SimpleNamespace(status_code=503))
    assert not is_retryable(SimpleNamespace(status_code=400))
    assert is_retryable(ConnectionError()) and not is_retryable(ValueError())
    # Retry-After honored + capped; absent → exp backoff within cap.
    exc = SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "7"}))
    assert retry_after_seconds(exc) == 7.0
    assert backoff(0, 0.5, retry_after=7.0, cap=30) == 7.0
    assert backoff(3, 0.5, retry_after=9999, cap=30) == 30.0
    assert 4.0 <= backoff(3, 0.5) <= 4.5  # 0.5*2^3 + jitter[0,0.5)
    print("resilience self-check OK")


if __name__ == "__main__":
    _demo()
