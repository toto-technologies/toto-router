"""Answer streaming to the SSE plane (fix #2).

Two levels: a driver-unit test that _answer batches deltas and returns the full text, and an
end-to-end test that answer_delta events reach the SSE endpoint in seq order and concatenate
to the authoritative snapshot answer. Fake lane throughout — no network.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from toto_gateway.app import create_app
from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.config import Settings
from toto_gateway.driver.core import Driver, Exec


# --- driver unit: batching + append-only ----------------------------------------

@pytest.mark.asyncio
async def test_answer_batches_deltas_and_returns_full_text():
    cat = Catalog(models=[CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="openai",
                                       residency_class="cloud")])
    words = ["hello "] * 100  # ~600 chars → several flushes, not 1, not 100

    async def fake_stream(req, on_delta):
        acc = []
        for w in words:
            await on_delta(w)  # on_delta is a coroutine now (publishes each batch to the run store)
            acc.append(w)
        return Exec(text="".join(acc), model="or-sonnet-4.6")

    published: list[tuple[str, str]] = []
    d = Driver(catalog=cat, complete_fn=None, driver_model="or-sonnet-4.6", triage_model="or-qwen3-coder-flash",
               toto=None, stream_fn=fake_stream,
               emit_delta=lambda node, text: published.append((node, text)))
    ex = await d._answer("or-sonnet-4.6", [{"role": "user", "content": "x"}],
                         name="t", node="synthesize", max_tokens=100)

    assert "".join(t for _, t in published) == ex.text          # append-only, lossless
    assert all(n == "synthesize" for n, _ in published)         # tagged with source node
    assert 2 <= len(published) < len(words)                     # batched (not per-token, not one)


@pytest.mark.asyncio
async def test_no_stream_fn_falls_back_to_completion():
    cat = Catalog(models=[CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="openai",
                                       residency_class="cloud")])

    async def fake_complete(req):
        return Exec(text="full answer", model="or-sonnet-4.6")

    published: list = []
    d = Driver(catalog=cat, complete_fn=fake_complete, driver_model="or-sonnet-4.6",
               triage_model="or-qwen3-coder-flash", toto=None,
               emit_delta=lambda n, t: published.append((n, t)))  # no stream_fn
    ex = await d._answer("or-sonnet-4.6", [{"role": "user", "content": "x"}],
                         name="t", node="synthesize")
    assert ex.text == "full answer"
    assert published == []  # nothing streamed when no stream_fn is wired


# --- end to end over the SSE endpoint -------------------------------------------

def _settings(**over) -> Settings:
    base = dict(
        catalog="catalog.yaml", trace_jsonl="", trace_db="", trace_stdout=False,
        auth_token="", driver=True, fake_exec=True, db=":memory:",
        driver_model="echo-cloud", triage_model="echo-local",
        driver_spans_jsonl="/dev/null", toto_token="",
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture()
def client():
    with TestClient(create_app(settings=_settings()), raise_server_exceptions=True) as c:
        yield c


def _sse_events(resp):
    out, et, eid, data = [], None, None, None
    for line in resp.iter_lines():
        if line.startswith("event: "):
            et = line[7:]
        elif line.startswith("id: "):
            eid = int(line[4:])
        elif line.startswith("data: "):
            data = json.loads(line[6:])
        elif line == "" and et is not None:
            out.append((et, eid, data))
            et, eid, data = None, None, None
    return out


def test_answer_deltas_over_sse_concat_to_final(client):
    # Force a genuine 2-task decomposition so synthesize WEAVES (≥2 non-empty results) and streams.
    # The echo driver otherwise yields a single task, which P4 now returns verbatim without a weave
    # call — correct behavior, but then there are no synthesize deltas to assert on.
    _tmd = {"scope": "research", "complexity": "low", "intent": "done",
            "keywords": ["x"], "requires": {"tools": [], "data_policy": "default"}}

    async def _two_tasks(state):
        return {"tasks": [
            {"task": "part A", "description": "Do part A.", "metadata": _tmd},
            {"task": "part B", "description": "Do part B.", "metadata": _tmd},
        ], "list_id": None, "spans": []}

    client.app.state.driver._graph = None            # force a rebuild that binds the patched node
    client.app.state.driver.decompose = _two_tasks

    run_id = client.post("/v1/sessions", json={"query": "compare A and B and summarize"}).json()["run_id"]
    with client.stream("GET", f"/v1/sessions/{run_id}/events") as resp:
        events = _sse_events(resp)

    deltas = [(eid, d) for (k, eid, d) in events if k == "answer_delta"]
    assert deltas, "expected answer_delta events on the stream"
    # (a) seq order + append-only text from a user-facing node
    ids = [eid for eid, _ in deltas]
    assert ids == sorted(ids)
    assert all(d["node"] in ("synthesize", "answer_trivial") for _, d in deltas)
    concat = "".join(d["text"] for _, d in deltas)

    # (b) snapshot answer is the complete authoritative text == concatenated deltas
    snap = client.get(f"/v1/sessions/{run_id}").json()
    assert snap["status"] == "done"
    assert snap["answer"] and concat == snap["answer"]

    # (c) terminal event still arrives
    assert events[-1][0] == "run_done"
