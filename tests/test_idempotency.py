"""Idempotency contract for the completion path.

A client retry after a network blip must NOT double-execute a create (double token spend,
duplicate task lists). Two layers:

  K-A — the store: claim_idempotency / store_idempotency_result / get_idempotency_result on the
        idempotency_keys table. Dual-dialect: the store tests run on SQLite AND, gated on
        TOTO_GW_TEST_DATABASE_URL, real Postgres (same skipif pattern as test_pg_store.py:18).
  K-B — the Idempotency-Key dependency wired into the create endpoints. Opt-in: no header behaves
        exactly as before; same key replays the first response without re-executing.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio

from toto_gateway.runs import RunStore

PG_URL = os.environ.get("TOTO_GW_TEST_DATABASE_URL")


# --- K-A: the store, dual-dialect ---------------------------------------------------------------
# Parametrized over both engines. The "pg" leg is skipped unless a PG URL is set — same gate as
# test_pg_store.py so the dialect branches can't silently drift.
@pytest_asyncio.fixture(params=["sqlite", "pg"])
async def store(request):
    if request.param == "pg":
        if not PG_URL:
            pytest.skip("set TOTO_GW_TEST_DATABASE_URL to run the Postgres dialect leg")
        s = RunStore(database_url=PG_URL)
        s._db.execute("DELETE FROM idempotency_keys")  # sync init conn: clean slate between tests
    else:
        s = RunStore(":memory:")
    yield s
    await s.close_pool()


async def test_claim_won_once_then_replays_the_stored_row(store):
    # First claim wins.
    assert await store.claim_idempotency("u1", "k1", "POST", "/v1/lists") == "won"
    # Nothing stored yet -> the row is in-flight (status_code NULL).
    inflight = await store.get_idempotency_result("u1", "k1")
    assert inflight is not None and inflight["status_code"] is None
    # Seal it, then a second claim replays the stored row instead of winning again.
    await store.store_idempotency_result("u1", "k1", 201, '{"id": "abc"}')
    replay = await store.claim_idempotency("u1", "k1", "POST", "/v1/lists")
    assert replay != "won"
    assert replay["status_code"] == 201
    assert replay["response_json"] == '{"id": "abc"}'


async def test_key_is_scoped_per_user(store):
    # Same key, different owners -> independent claims (no cross-user replay).
    assert await store.claim_idempotency("u1", "shared", "POST", "/v1/lists") == "won"
    assert await store.claim_idempotency("u2", "shared", "POST", "/v1/lists") == "won"


async def test_operator_null_owner_dedups(store):
    # user_id None (operator) is coalesced to '' so the composite PK still dedups on both dialects.
    assert await store.claim_idempotency(None, "opk", "POST", "/v1/sessions") == "won"
    assert await store.claim_idempotency(None, "opk", "POST", "/v1/sessions") != "won"


async def test_concurrent_claims_exactly_one_winner(store):
    # Race the same (user, key) over the pool: exactly one INSERT wins, the rest see the row.
    outcomes = await asyncio.gather(
        *(store.claim_idempotency("u1", "race", "POST", "/v1/sessions") for _ in range(8)))
    assert outcomes.count("won") == 1


# --- K-B: the dependency, through the real app --------------------------------------------------
from harness.appharness import drive_to_terminal, in_process_app  # noqa: E402


async def _make_list(client) -> str:
    # /v1/lists is an app-plane surface — absent in the OSS export tree, so its two tests skip
    # there (edition seam); a no-op wherever the module exists.
    pytest.importorskip("toto_gateway.routes.lists")
    r = await client.post("/v1/lists", json={"name": "groceries"})
    assert r.status_code == 201
    return r.json()["list_id"]


async def test_post_item_twice_same_key_is_one_row(app_client):
    client, app = app_client
    list_id = await _make_list(client)
    headers = {"Idempotency-Key": "item-key-1"}
    r1 = await client.post(f"/v1/lists/{list_id}/items", json={"task": "milk"}, headers=headers)
    r2 = await client.post(f"/v1/lists/{list_id}/items", json={"task": "milk"}, headers=headers)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()                       # replay is byte-identical
    lst = (await client.get(f"/v1/lists/{list_id}")).json()
    assert len(lst["items"]) == 1                        # the retry did NOT insert a second row


async def test_no_key_duplicates_as_today(app_client):
    # Opt-in proof: without the header, two POSTs create two rows exactly as before.
    client, app = app_client
    list_id = await _make_list(client)
    await client.post(f"/v1/lists/{list_id}/items", json={"task": "eggs"})
    await client.post(f"/v1/lists/{list_id}/items", json={"task": "eggs"})
    lst = (await client.get(f"/v1/lists/{list_id}")).json()
    assert len(lst["items"]) == 2


async def test_post_session_twice_same_key_one_run_driver_invoked_once():
    async with in_process_app() as (client, app):
        calls: list[int] = []
        orig = app.state.driver.run

        async def counting(*a, **k):
            calls.append(1)
            return await orig(*a, **k)

        app.state.driver.run = counting
        headers = {"Idempotency-Key": "sess-key-1"}
        r1 = await client.post("/v1/sessions", json={"query": "hello"}, headers=headers)
        assert r1.status_code == 202
        run_id = r1.json()["run_id"]
        await drive_to_terminal(app, run_id)             # let the one real run finish

        r2 = await client.post("/v1/sessions", json={"query": "hello"}, headers=headers)
        assert r2.status_code == 202
        assert r2.json()["run_id"] == run_id             # replay returns the ORIGINAL run_id
        assert len(calls) == 1                            # driver executed exactly once


async def test_session_in_flight_duplicate_gets_409():
    # A duplicate that arrives while the first is still running (claimed, no stored result yet)
    # gets 409 retry — the ponytail ceiling (no distributed lock).
    async with in_process_app() as (client, app):
        store = app.state.runs
        # Simulate the first request having claimed but not yet stored its result.
        assert await store.claim_idempotency(None, "busy", "POST", "/v1/sessions") == "won"
        r = await client.post("/v1/sessions", json={"query": "hi"},
                              headers={"Idempotency-Key": "busy"})
        assert r.status_code == 409
