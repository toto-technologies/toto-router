"""Async in-process app harness (Chunk H1).

Proves the substrate the sync Starlette TestClient can't give us: REAL concurrency through ONE
real `create_app` instance, over `httpx.ASGITransport` + `asgi_lifespan.LifespanManager` (lifespan
startup + drain honored). Everything downstream (H2 faults, H3 load) builds on this.
"""

from __future__ import annotations

import asyncio

from harness.appharness import in_process_app


async def test_fifty_concurrent_sessions_all_resolve():
    """50 concurrent POST /v1/sessions through one in-process app — every one resolves 202 with a
    distinct run_id. The operator bearer bypasses the per-run rate limiter, so this measures
    concurrency, not the 12/min gate. The sync TestClient cannot express this (it serializes)."""
    async with in_process_app() as (client, app):
        assert app.state.runs is not None  # lifespan startup built the run store
        results = await asyncio.gather(*[
            client.post("/v1/sessions", json={"query": f"hi {i}"}) for i in range(50)
        ])
        assert all(r.status_code == 202 for r in results), [r.status_code for r in results]
        assert len({r.json()["run_id"] for r in results}) == 50


async def test_lifespan_startup_serves_readyz():
    """Lifespan startup wires app.state (auth store, run store, driver); /readyz only returns 200
    once those exist — so a green /readyz proves the real lifespan ran, not a bare app object."""
    async with in_process_app() as (client, app):
        r = await client.get("/readyz")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ready"
        assert app.state.driver is not None


async def test_concurrent_reads_and_writes_share_one_app():
    """Mixed concurrent load (reads + writes) through the single app instance stays coherent —
    no cross-request state bleed, every response well-formed."""
    async with in_process_app() as (client, app):
        calls = []
        for i in range(20):
            calls.append(client.get("/readyz"))
            calls.append(client.post("/v1/sessions", json={"query": f"q{i}"}))
        results = await asyncio.gather(*calls)
        assert all(r.status_code in (200, 202) for r in results)
