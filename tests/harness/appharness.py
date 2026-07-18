"""In-process async app harness (Chunk H1).

`in_process_app` boots the REAL `create_app` and binds an `httpx.AsyncClient` to it via
`httpx.ASGITransport` (no socket) with `asgi_lifespan.LifespanManager` driving startup + drain.
Unlike the sync Starlette `TestClient`, this runs true concurrency: `asyncio.gather` N requests
through ONE app instance. Both deps are already installed — ponytail: reuse, add nothing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from asgi_lifespan import LifespanManager

from toto_gateway.app import create_app
from toto_gateway.config import Settings

# The operator bearer the harness client sends. Mirrors tests/conftest.py:OP_TOKEN — the operator
# identity bypasses the per-run rate limiter, so N concurrent creates all resolve (not 12→429).
OP_TOKEN = "test-operator-token"


def default_settings(**overrides) -> Settings:
    """Offline app settings: driver ON, fake-lane execution, in-memory SQLite, operator auth.
    A short drain so teardown never blocks on in-flight fake runs. Override any field."""
    base = dict(
        catalog="catalog.yaml",
        trace_jsonl="", trace_db="", trace_stdout=False,
        db=":memory:", toto_token="",
        auth_token=OP_TOKEN,
        driver=True, fake_exec=True,
        driver_model="echo-cloud", triage_model="echo-local",  # fake-lane catalog entries
        drain_seconds=1,
        # Unbounded by default: the harness measures OUR throughput/overhead, not the shipped
        # admission valve. Tests that exercise the 429/pool shed set these explicitly.
        max_concurrent_runs=0, max_concurrent_llm_calls=0,
    )
    base.update(overrides)
    return Settings(**base)


@asynccontextmanager
async def in_process_app(gateway=None, *, settings: Settings | None = None, **overrides):
    """Yield (AsyncClient, app) bound to a real create_app. gateway=None → create_app builds the
    fake-lane gateway itself (fake_exec). Pass a gateway (e.g. faults.gateway) to fault the wire."""
    settings = settings or default_settings(**overrides)
    app = create_app(settings=settings, gateway=gateway)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://app.local",
            headers={"authorization": f"Bearer {OP_TOKEN}"},
            timeout=30.0,
        ) as client:
            yield client, app


async def drive_to_terminal(app, run_id: str, *, tries: int = 200, delay: float = 0.02) -> dict:
    """Poll the run store until the run reaches a terminal status (done/failed/cancelled) or the
    budget runs out. Returns the final session row. In-process store, so this is a cheap await
    loop — no HTTP round-trip needed to read the outcome the background task wrote."""
    import asyncio

    store = app.state.runs
    for _ in range(tries):
        row = await store.get_session(run_id)
        if row and row.get("status") in ("done", "failed", "cancelled"):
            return row
        await asyncio.sleep(delay)
    return await store.get_session(run_id) or {}
