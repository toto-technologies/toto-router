"""TotoClient tests — respx mocks, never touches the network.

Verifies method+path+body+Bearer auth for each endpoint, id extraction from the REAL Toto
response shapes, order-aligned batch ids (incl. the GET fallback), and — load-bearing — that
write_execution strips every non-allowlisted provenance key before it leaves the process.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

# ponytail: driver/__init__ eagerly imports sibling `.core` (built by a parallel agent, not
# yet on this branch). Stub it only if absent so toto_client imports in isolation.
try:
    from toto_gateway.driver.toto_client import TotoClient
except ModuleNotFoundError:
    import sys
    import types

    core = types.ModuleType("toto_gateway.driver.core")
    core.Driver = core.DriverResult = object
    sys.modules["toto_gateway.driver.core"] = core
    from toto_gateway.driver.toto_client import TotoClient

BASE = "https://toto.test"
TOKEN = "toto_secret_xyz"


def _router() -> respx.MockRouter:
    # Explicit-transport (non-patching) respx — the repo convention; global patching is broken
    # under httpx 0.28 in this env. See tests/test_mlx_adapter.py for the same pattern.
    return respx.mock(base_url=BASE, assert_all_called=False)


def client(router: respx.MockRouter) -> TotoClient:
    return TotoClient(BASE, TOKEN, transport=httpx.MockTransport(router.handler))


def _auth_ok(request: httpx.Request) -> bool:
    return request.headers.get("Authorization") == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_create_list_returns_id_from_real_shape():
    mock = _router()
    route = mock.post("/api/lists").mock(
        return_value=httpx.Response(200, json={"id": "Lst123", "task": "", "metadata": {}})
    )
    list_id = await client(mock).create_list("Driver run", {"scope": "backend"})

    assert list_id == "Lst123"
    req = route.calls.last.request
    assert req.method == "POST"
    assert _auth_ok(req)
    assert json.loads(req.content) == {"name": "Driver run", "metadata": {"scope": "backend"}}


@pytest.mark.asyncio
async def test_batch_items_order_aligned_from_succeeded():
    items = [{"task": "alpha"}, {"task": "beta"}, {"task": "gamma"}]
    mock = _router()
    # Real envelope: {"succeeded": [full item dicts], "failed": []}. Returned OUT of input
    # order to prove we map by title, not by position.
    route = mock.post("/api/lists/L1/items/batch").mock(
        return_value=httpx.Response(200, json={
            "succeeded": [
                {"id": "idB", "task": "beta"},
                {"id": "idA", "task": "alpha"},
                {"id": "idG", "task": "gamma"},
            ],
            "failed": [],
        })
    )
    ids = await client(mock).batch_items("L1", items)

    assert ids == ["idA", "idB", "idG"]
    req = route.calls.last.request
    assert req.method == "POST"
    assert _auth_ok(req)
    assert json.loads(req.content) == {"items": items}


@pytest.mark.asyncio
async def test_batch_items_duplicate_titles_consume_in_order():
    items = [{"task": "dup"}, {"task": "dup"}]
    mock = _router()
    mock.post("/api/lists/L1/items/batch").mock(
        return_value=httpx.Response(200, json={
            "succeeded": [{"id": "id1", "task": "dup"}, {"id": "id2", "task": "dup"}],
            "failed": [],
        })
    )
    assert await client(mock).batch_items("L1", items) == ["id1", "id2"]


@pytest.mark.asyncio
async def test_batch_items_falls_back_to_get_when_envelope_incomplete():
    items = [{"task": "alpha"}, {"task": "beta"}]
    mock = _router()
    # Batch response missing 'beta' (partial/garbled envelope) -> forces the GET fallback.
    mock.post("/api/lists/L1/items/batch").mock(
        return_value=httpx.Response(200, json={"succeeded": [{"id": "idA", "task": "alpha"}]})
    )
    get_route = mock.get("/api/lists/L1/items").mock(
        return_value=httpx.Response(200, json={
            "items": [{"id": "idA", "task": "alpha"}, {"id": "idB", "task": "beta"}],
            "total": 2, "limit": 200, "offset": 0,
        })
    )
    ids = await client(mock).batch_items("L1", items)

    assert ids == ["idA", "idB"]
    assert get_route.called
    assert _auth_ok(get_route.calls.last.request)


@pytest.mark.asyncio
async def test_set_status_posts_status_body():
    mock = _router()
    route = mock.post("/api/items/I9/status").mock(
        return_value=httpx.Response(200, json={"id": "I9", "status": "in_progress"})
    )
    await client(mock).set_status("I9", "in_progress")

    req = route.calls.last.request
    assert req.method == "POST"
    assert _auth_ok(req)
    assert json.loads(req.content) == {"status": "in_progress"}


@pytest.mark.asyncio
async def test_write_execution_strips_non_allowlisted_keys():
    mock = _router()
    # GET current item (edit REPLACES metadata, so client read-merge-writes).
    mock.get("/api/items/I5").mock(
        return_value=httpx.Response(200, json={"id": "I5", "metadata": {"scope": "backend"}})
    )
    edit_route = mock.post("/api/items/I5/edit").mock(
        return_value=httpx.Response(200, json={"id": "I5"})
    )
    # 'answer' is model output — MUST NOT cross. cost_usd + model are provenance — MUST cross.
    await client(mock).write_execution("I5", {"answer": "secret content", "cost_usd": 0.1, "model": "x"})

    body = json.loads(edit_route.calls.last.request.content)
    execution = body["metadata"]["execution"]
    assert execution == {"cost_usd": 0.1, "model": "x"}
    assert "answer" not in execution
    # Sibling metadata preserved through the read-merge-write.
    assert body["metadata"]["scope"] == "backend"
    assert _auth_ok(edit_route.calls.last.request)


@pytest.mark.asyncio
async def test_write_execution_full_allowlist_survives():
    mock = _router()
    mock.get("/api/items/I7").mock(
        return_value=httpx.Response(200, json={"id": "I7", "metadata": {}})
    )
    edit_route = mock.post("/api/items/I7/edit").mock(
        return_value=httpx.Response(200, json={"id": "I7"})
    )
    execution = {
        "runner": "desktop", "executor": "gateway", "model": "or-sonnet-4.6", "lane": "frontier",
        "tokens_prompt": 10, "tokens_completion": 20, "cost_usd": 0.02, "outcome": "ok",
        "latency_ms": 1234, "fallback_used": False, "route_reason": "intent:research",
        "prompt": "LEAK", "file_contents": "LEAK",  # <- must be dropped
    }
    await client(mock).write_execution("I7", execution)

    sent = json.loads(edit_route.calls.last.request.content)["metadata"]["execution"]
    assert "prompt" not in sent and "file_contents" not in sent
    assert set(sent) == {
        "runner", "executor", "model", "lane", "tokens_prompt", "tokens_completion",
        "cost_usd", "outcome", "latency_ms", "fallback_used", "route_reason",
    }


@pytest.mark.asyncio
async def test_write_execution_merges_classified_metadata_existing_keys_win():
    """The totoshape classified metadata merges into the item's TOP-LEVEL metadata, but a field the
    driver already set (decompose) is never overwritten — shape parity enriches, doesn't clobber."""
    mock = _router()
    mock.get("/api/items/I9").mock(  # decompose already set component=auth
        return_value=httpx.Response(200, json={
            "id": "I9", "metadata": {"component": "auth", "complexity": "high"}})
    )
    edit_route = mock.post("/api/items/I9/edit").mock(
        return_value=httpx.Response(200, json={"id": "I9"})
    )
    classified = {"component": "billing", "keywords": ["sso", "login"], "scope": "backend"}
    await client(mock).write_execution("I9", {"cost_usd": 0.1, "model": "x"}, classified)

    md = json.loads(edit_route.calls.last.request.content)["metadata"]
    assert md["component"] == "auth"       # driver's field wins — not overwritten by 'billing'
    assert md["complexity"] == "high"      # sibling preserved
    assert md["keywords"] == ["sso", "login"]  # new classified field merged in
    assert md["scope"] == "backend"
    assert md["execution"] == {"cost_usd": 0.1, "model": "x"}


@pytest.mark.asyncio
async def test_workmap_read_and_edit_methods():
    """list_lists / list_items / edit_item — the thin methods materialize writes through."""
    mock = _router()
    mock.get("/api/lists").mock(return_value=httpx.Response(
        200, json={"lists": [{"id": "L1", "name": "Work map"}]}))
    mock.get("/api/lists/L1/items").mock(return_value=httpx.Response(
        200, json={"items": [{"id": "I1", "task": "auth", "metadata": {"component": "auth"}}]}))
    edit = mock.post("/api/items/I1/edit").mock(return_value=httpx.Response(200, json={"id": "I1"}))

    c = client(mock)
    assert [lst["name"] for lst in await c.list_lists()] == ["Work map"]
    assert (await c.list_items("L1"))[0]["metadata"]["component"] == "auth"
    await c.edit_item("I1", description="d", metadata={"component": "auth", "workmap": {"requests": 3}})

    body = json.loads(edit.calls.last.request.content)
    assert body == {"description": "d", "metadata": {"component": "auth", "workmap": {"requests": 3}}}
    assert _auth_ok(edit.calls.last.request)
