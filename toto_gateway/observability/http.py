"""One retrying GET-JSON helper both admin connectors share.

Provider admin APIs are polled, not latency-critical: a 429/5xx gets a few backoff
retries (honoring Retry-After via the shared resilience primitives); any 4xx surfaces
immediately as AdminAPIError so a bad/wrong-type key fails loud at the route layer.
"""

from __future__ import annotations

import asyncio

import httpx

from ..resilience import backoff, retry_after_seconds
from .schema import AdminAPIError

MAX_ATTEMPTS = 4
BACKOFF_BASE = 1.0


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    provider: str = "",
) -> dict:
    """GET url and return parsed JSON. Retries 429/5xx/transport errors with capped
    backoff; raises AdminAPIError on non-retryable status or when retries run out.
    Auth headers live on the client, not here."""
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:  # DNS/timeout/connection — transient
            last_exc = exc
            await asyncio.sleep(backoff(attempt, BACKOFF_BASE))
            continue
        if resp.status_code < 400:
            return resp.json()
        if resp.status_code == 429 or resp.status_code >= 500:
            last_exc = AdminAPIError(resp.status_code, _detail(resp), provider=provider)
            wait = retry_after_seconds(_carrier(resp))
            await asyncio.sleep(backoff(attempt, BACKOFF_BASE, retry_after=wait))
            continue
        raise AdminAPIError(resp.status_code, _detail(resp), provider=provider)
    if isinstance(last_exc, AdminAPIError):
        raise last_exc
    raise AdminAPIError(0, f"{provider}: {last_exc}", provider=provider)


def _detail(resp: httpx.Response) -> str:
    """Provider error message without ever echoing the request (which carries the key)."""
    try:
        body = resp.json()
        return str(body.get("error", {}).get("message") or body.get("error") or body)[:500]
    except Exception:  # noqa: BLE001 — non-JSON error body
        return resp.text[:500]


def _carrier(resp: httpx.Response):
    """Adapt an httpx Response to the .response.headers duck-type retry_after_seconds reads."""

    class _E:
        response = resp

    return _E()
