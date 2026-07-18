"""Per-stage latency instrumentation (W1-C2).

Traces persist a stage breakdown — classify_ms / plan_ms / upstream_ms — so gateway overhead
(= total − upstream) is decomposable in SQL rather than one opaque number. A request that ran no
classifier (explicit model, sticky-session memo hit, labels off) is the FAST PATH: its classify_ms
is NULL/0. `latency_summary` rolls this up (p50/p95 overhead, per-stage avg/p95, fast-path share);
`GET /v1/admin/latency/summary` serves it org-scoped like the other admin_usage reads.
"""

from __future__ import annotations

import sqlite3
from typing import AsyncIterator

import pytest
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.gateway import Gateway
from toto_gateway.metering import latency_summary
from toto_gateway.routing import smart
from toto_gateway.routing.labels import LabelBindings
from toto_gateway.runners.fake import FakeRunner
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.schemas import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse, Message
from toto_gateway.trace import MemoryTraceWriter, SqlModelTraceWriter, TraceRow

# --- schema: stage fields persist + migrate onto a pre-existing table ---------------------------


def test_stage_fields_round_trip(tmp_path):
    """A trace carrying the stage fields writes + reads back unchanged (they are real columns)."""
    w = SqlModelTraceWriter(f"sqlite:///{tmp_path}/t.db")
    with Session(w.engine) as s:
        s.add(TraceRow(request_id="r", ts_start="2026-07-12T10:00:00+00:00", lane="frontier",
                       runner_id="x", model="m", residency_class="cloud", status="ok",
                       classify_ms=42.5, plan_ms=3, upstream_ms=1800))
        s.commit()
    with Session(w.engine) as s:
        row = s.exec(select(TraceRow)).first()
    assert (row.classify_ms, row.plan_ms, row.upstream_ms) == (42.5, 3, 1800)


def test_ensure_columns_adds_stage_fields_to_preexisting_table(tmp_path):
    """create_all never ALTERs an existing gateway_events; _ensure_columns must ADD the W1-C2 stage
    columns onto an old table, else a select over them 500s on a deployed DB (the 2026-07-08 class)."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE gateway_events (id INTEGER PRIMARY KEY, ts_start TEXT, model TEXT, lane TEXT)")
    con.commit(); con.close()
    w = SqlModelTraceWriter(f"sqlite:///{db}")
    cols = {c["name"] for c in inspect(w.engine).get_columns("gateway_events")}
    assert {"classify_ms", "plan_ms", "upstream_ms"}.issubset(cols), sorted(cols)
    SqlModelTraceWriter(f"sqlite:///{db}")  # idempotent, no raise


# --- gateway: classify_ms on a real classify; fast path shows none ------------------------------

_RAW = {"labels": {
    "code_generation": {"model": "or-qwen3-coder-flash", "desc": "write or debug code"},
    "other": {"model": None, "desc": "none of the above"},
}}


def _catalog() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id="or-haiku-4.5", lane="economy", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="or-qwen3-coder-flash", lane="economy", endpoint="fake", residency_class="cloud"),
        CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="fake", residency_class="cloud"),
    ])


class _LabelRunner:
    """Fixed JSON label for the classifier call (system prompt = LABEL_PROMPT); echoes otherwise."""

    def __init__(self, entry: CatalogEntry, reply: str) -> None:
        self.entry, self.runner_id, self._fake, self._reply = entry, f"lr-{entry.id}", FakeRunner(entry), reply

    async def chat(self, req, entry) -> ChatCompletionResponse:
        sys = req.messages[0].text() if req.messages and req.messages[0].role == "system" else ""
        if "label one piece of work" in sys:
            return ChatCompletionResponse.simple(model=entry.id, content=self._reply,
                                                 usage=self._fake._usage(req, ""))
        return await self._fake.chat(req, entry)

    def stream(self, req, entry) -> AsyncIterator[ChatCompletionChunk]:
        return self._fake.stream(req, entry)


def _gw(*, labels=_RAW, classifier="or-haiku-4.5"):
    writer = MemoryTraceWriter()
    gw = Gateway(catalog=_catalog(), registry=RunnerRegistry(
        factory=lambda e: _LabelRunner(e, '{"label": "code_generation", "reason": "r"}')),
        writer=writer, labels=LabelBindings(_raw=labels) if labels else None,
        benchmarks=None, classifier_model=classifier, label_timeout_ms=500)
    return gw, writer


def _req(content="write a python function", *, model="smart", stream=False):
    return ChatCompletionRequest(model=model,
                                 messages=[Message(role="user", content=content)], stream=stream)


@pytest.mark.asyncio
async def test_classify_ms_recorded_on_a_real_classify():
    smart._label_cache.clear()
    gw, _ = _gw()
    res = await gw.complete(_req())
    assert res.trace.route_reason == "label:code_generation"
    assert res.trace.classify_ms is not None and res.trace.classify_ms > 0  # the classifier ran
    assert res.trace.plan_ms is not None      # the decision pipeline was timed
    assert res.trace.upstream_ms is not None   # the upstream wall was captured
    # overhead decomposes off upstream: total − upstream == overhead (the guardrail-#3 identity)
    assert res.trace.latency_ms_gateway_overhead == max(
        0, res.trace.latency_ms_total - res.trace.upstream_ms)


@pytest.mark.asyncio
async def test_explicit_model_is_fast_path_zero_classify():
    """An explicit (non-smart) model never classifies — classify_ms is None, the fast-path signal."""
    smart._label_cache.clear()
    gw, _ = _gw()
    res = await gw.complete(_req(model="or-sonnet-4.6"))
    assert res.trace.classify_ms is None      # no classifier call → fast path
    assert res.trace.upstream_ms is not None   # upstream still measured


@pytest.mark.asyncio
async def test_sticky_memo_hit_is_fast_path_zero_classify():
    """A second turn of the same conversation reuses the memo → no classify call → classify_ms None,
    while the first turn (which classified) has classify_ms > 0."""
    smart._label_cache.clear()
    gw, _ = _gw()
    first = await gw.complete(_req())
    assert first.trace.classify_ms > 0
    second = await gw.complete(_req())        # same text/conversation → memo hit
    assert second.trace.route_reason == "label:code_generation"
    assert second.trace.classify_ms is None   # skipped classification, fast path


# --- metering: latency_summary rollup -----------------------------------------------------------


@pytest.fixture()
def engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(e)  # ponytail: sqlite-only — latency_summary has no dialect SQL
    return e                          # (percentiles are Python; only basic select/where hits the DB)


def _seed(engine, **kw):
    row = dict(request_id="r", ts_start="2026-07-12T10:00:00+00:00", lane="frontier",
               runner_id="x", model="m", residency_class="cloud", status="ok")
    row.update(kw)
    with Session(engine) as s:
        s.add(TraceRow(**row)); s.commit()


def test_latency_summary_percentiles_and_fast_path(engine):
    org = "o_acme"
    # 4 overhead values 10/20/30/40; two rows classified (classify_ms>0), two fast-path (None/0).
    _seed(engine, org_id=org, latency_ms_gateway_overhead=10, classify_ms=200.0, upstream_ms=1000, plan_ms=2)
    _seed(engine, org_id=org, latency_ms_gateway_overhead=20, classify_ms=400.0, upstream_ms=2000, plan_ms=4)
    _seed(engine, org_id=org, latency_ms_gateway_overhead=30, classify_ms=None, upstream_ms=1500, plan_ms=3)
    _seed(engine, org_id=org, latency_ms_gateway_overhead=40, classify_ms=0, upstream_ms=500, plan_ms=1)
    _seed(engine, org_id="o_other", latency_ms_gateway_overhead=999, classify_ms=999.0)  # isolation noise
    _seed(engine, org_id=org, status="error", latency_ms_gateway_overhead=888)  # excluded (not ok)

    s = latency_summary(engine, org_id=org)
    assert s["requests"] == 4                              # the error row is excluded
    assert s["overhead_ms"] == {"p50": 20.0, "p95": 40.0}  # nearest-rank over 10/20/30/40
    assert s["stages"]["classify_ms"]["avg"] == 300.0      # (200+400)/2; NULL/0 skipped
    assert s["stages"]["classify_ms"]["p95"] == 400.0
    assert s["stages"]["upstream_ms"]["avg"] == 1250.0     # (1000+2000+1500+500)/4
    assert s["fast_path"] == {"requests": 2, "share": 0.5}  # classify_ms None + 0


def test_latency_summary_empty_window(engine):
    s = latency_summary(engine, org_id="o_nobody")
    assert s["requests"] == 0 and s["fast_path"]["share"] == 0.0
    assert s["overhead_ms"]["p95"] is None and s["stages"]["classify_ms"]["avg"] is None


# --- HTTP: shape + org scoping (require_role admin, cross-org denied) ----------------------------


@pytest.fixture()
def latency_app(tmp_path):
    from fastapi.testclient import TestClient

    from toto_gateway.app import create_app
    from toto_gateway.config import Settings
    from toto_gateway.trace import sql_engine

    settings = Settings(catalog="catalog.yaml", trace_jsonl="", trace_db=f"sqlite:///{tmp_path}/t.db",
                        trace_stdout=False, auth_token="test-operator-token", db=":memory:", fake_exec=True)
    app = create_app(settings=settings)
    with TestClient(app) as client:
        yield client, app, sql_engine(app.state.gateway.writer)


def _override_identity(app, **kw):
    from toto_gateway.routes.deps import Identity, require_auth
    app.dependency_overrides[require_auth] = lambda: Identity(user_id="u1", authenticated=True, **kw)


def test_endpoint_shape(latency_app):
    client, app, engine = latency_app
    _override_identity(app, org_id="o_a", role="admin")
    from datetime import date
    today = date.today().isoformat()
    _seed(engine, org_id="o_a", ts_start=f"{today}T10:00:00+00:00",
          latency_ms_gateway_overhead=12, classify_ms=250.0, upstream_ms=900, plan_ms=2)
    _seed(engine, org_id="o_a", ts_start=f"{today}T11:00:00+00:00",
          latency_ms_gateway_overhead=8, classify_ms=None, upstream_ms=700, plan_ms=1)
    r = client.get("/v1/admin/latency/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["org_id"] == "o_a" and body["days"] == 7 and body["requests"] == 2
    assert set(body["overhead_ms"]) == {"p50", "p95"}
    assert set(body["stages"]) == {"classify_ms", "plan_ms", "upstream_ms"}
    assert body["fast_path"]["requests"] == 1 and body["fast_path"]["share"] == 0.5
    app.dependency_overrides.clear()


def test_cross_org_denied(latency_app):
    client, app, _ = latency_app
    _override_identity(app, org_id="o_a", role="admin")
    assert client.get("/v1/admin/latency/summary", params={"org_id": "o_b"}).status_code == 403
    assert client.get("/v1/admin/latency/summary", params={"org_id": "o_a"}).status_code == 200
    app.dependency_overrides.clear()


def test_admin_role_required(latency_app):
    client, app, _ = latency_app
    _override_identity(app, org_id="o_a", role="member")
    assert client.get("/v1/admin/latency/summary", params={"org_id": "o_a"}).status_code == 403
    app.dependency_overrides.clear()
