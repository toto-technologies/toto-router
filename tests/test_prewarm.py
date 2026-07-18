"""POST /v1/prewarm — the per-org cache pre-warm toggle.

Contract:
  - toggle OFF (default: no routing policy, or prewarm flag unset) → {"status": "disabled"} and
    ZERO upstream calls.
  - toggle ON → exactly ONE Gateway.complete call with max_tokens=1, a finalized trace, and a
    {"status": "warmed"} body carrying the resolved model + conversation_key.
  - the smart sentinel resolves to a real catalog model on the warm path.

The route isn't wired into app.py (that include_router line ships via INTEGRATION-prewarm.md), so
we register it on the test app instance. Identity is injected via dependency_overrides so the
toggle can be flipped without depending on the store-side field the orchestrator applies.
"""

from __future__ import annotations

from tests.harness.appharness import in_process_app
from toto_gateway.routes import prewarm
from toto_gateway.routes.deps import Identity, require_auth


def _identity(prewarm_on: bool) -> Identity:
    policy = {"prewarm": True} if prewarm_on else None
    return Identity(user_id="u1", email="u@example.com", authenticated=True,
                    org_id="o1", routing_policy=policy)


def _spy_complete(gateway):
    """Wrap gateway.complete to record (call count, req.max_tokens). Returns the record dict."""
    record: dict = {"calls": 0, "max_tokens": None}
    original = gateway.complete

    async def spy(req, **kwargs):
        record["calls"] += 1
        record["max_tokens"] = req.max_tokens
        return await original(req, **kwargs)

    gateway.complete = spy
    return record


async def test_prewarm_disabled_makes_no_upstream_call():
    async with in_process_app() as (client, app):
        app.include_router(prewarm.router)
        app.dependency_overrides[require_auth] = lambda: _identity(False)
        record = _spy_complete(app.state.gateway)

        r = await client.post("/v1/prewarm", json={
            "model": "echo-cloud",
            "messages": [{"role": "user", "content": "hello"}],
        })

        assert r.status_code == 200, r.text
        assert r.json()["status"] == "disabled"
        assert record["calls"] == 0  # OFF must never touch the wire


async def test_prewarm_enabled_warms_with_max_tokens_1():
    async with in_process_app() as (client, app):
        app.include_router(prewarm.router)
        app.dependency_overrides[require_auth] = lambda: _identity(True)
        record = _spy_complete(app.state.gateway)

        r = await client.post("/v1/prewarm", json={
            "model": "echo-cloud",
            "messages": [{"role": "user", "content": "hello"}],
        })

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "warmed"
        assert body["model"] == "echo-cloud"
        assert body["conversation_key"]  # trace was finalized with the conversation anchor
        assert record["calls"] == 1  # exactly one upstream call
        assert record["max_tokens"] == 1  # minimal warm request


async def test_prewarm_smart_sentinel_resolves():
    async with in_process_app() as (client, app):
        app.include_router(prewarm.router)
        app.dependency_overrides[require_auth] = lambda: _identity(True)

        r = await client.post("/v1/prewarm", json={
            "model": "smart",
            "messages": [{"role": "user", "content": "warm me up"}],
        })

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "warmed"
        assert body["model"] != "smart"  # classified + resolved to a real catalog id
