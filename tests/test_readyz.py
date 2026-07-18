"""O4 — deepened /readyz (observability.md chunk 4).

On a Postgres deploy /readyz gates fail-closed on the two planes a replica needs to serve
correctly: the SSE wake-bus listener must be armed, and the content plane must resolve. SQLite /
single-replica (runs._pg False) skips the gate — a memory-off replica still passes.
"""

from __future__ import annotations

import types

import pytest

from harness.appharness import in_process_app

pytestmark = pytest.mark.asyncio


def _stub_runs(*, pg=True, armed=True):
    return types.SimpleNamespace(_pg=pg, wake_armed=lambda: armed)


def _stub_content(*, ok=True):
    async def ping():
        if not ok:
            raise RuntimeError("content plane unreachable")

    return types.SimpleNamespace(ping=ping)


async def test_readyz_healthy_sqlite_passes():
    """Default replica (SQLite, in-proc bus, memory off) is ready — the PG gate is skipped."""
    async with in_process_app() as (client, _app):
        r = await client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"


async def _readyz_with_stubs(app, client, *, runs, content):
    """Swap in PG-mode stubs for one request, then restore so lifespan teardown stays clean."""
    orig_runs, orig_content = app.state.runs, app.state.content
    app.state.runs, app.state.content = runs, content
    try:
        return await client.get("/readyz")
    finally:
        app.state.runs, app.state.content = orig_runs, orig_content


async def test_readyz_dead_wake_bus_fails_closed():
    async with in_process_app() as (client, app):
        r = await _readyz_with_stubs(app, client,
                                     runs=_stub_runs(armed=False),  # LISTEN task died
                                     content=_stub_content(ok=True))
        assert r.status_code == 503
        assert r.json()["reason"] == "fanout"


async def test_readyz_content_plane_unreachable_fails_closed():
    async with in_process_app() as (client, app):
        r = await _readyz_with_stubs(app, client,
                                     runs=_stub_runs(armed=True),
                                     content=_stub_content(ok=False))  # resolve raises
        assert r.status_code == 503
        assert r.json()["reason"] == "content"


async def test_readyz_pg_replica_all_planes_up_passes():
    async with in_process_app() as (client, app):
        r = await _readyz_with_stubs(app, client,
                                     runs=_stub_runs(armed=True),
                                     content=_stub_content(ok=True))
        assert r.status_code == 200
        assert r.json()["status"] == "ready"
