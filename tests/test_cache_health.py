"""Cache-health time series (A8): the per-bucket caching-health rollup + its org-scoped HTTP surface.

`metering.cache_health` buckets gateway_events by day/hour and reports request/token totals, the
warm-hold turn count, and a zero-safe hit rate. `GET /v1/admin/usage/cache-health` serves it behind
the same admin + org-scoping floor as the sibling usage endpoints. The aggregation runs on sqlite
here (mirrors test_cache_savings); the hit-rate math is dialect-independent Python.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from toto_gateway.metering import cache_health
from toto_gateway.trace import TraceRow


@pytest.fixture()
def trace_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


def _write(engine, **kw) -> None:
    row = dict(request_id="r", ts_start="2026-07-08T10:00:00+00:00", lane="frontier",
               runner_id="openrouter", model="or-sonnet-5", residency_class="cloud", status="ok")
    row.update(kw)
    with Session(engine) as s:
        s.add(TraceRow(**row))
        s.commit()


# --- bucket math -----------------------------------------------------------------


def test_hit_rate_and_warm_hold_per_bucket(trace_engine):
    org = "o_" + uuid.uuid4().hex
    # Two turns on 07-08: 1000 prompt / 600 cached, one of them held warm.
    _write(trace_engine, org_id=org, ts_start="2026-07-08T10:00:00+00:00", tokens_prompt=400,
           tokens_cached=200, tokens_cache_write=50, route_reason="label:code_generation")
    _write(trace_engine, org_id=org, ts_start="2026-07-08T12:00:00+00:00", tokens_prompt=600,
           tokens_cached=400, tokens_cache_write=0, route_reason="label:code_generation:warm-hold")
    # A different day (07-09), one cold turn.
    _write(trace_engine, org_id=org, ts_start="2026-07-09T09:00:00+00:00", tokens_prompt=1000,
           tokens_cached=0, route_reason="label:other")

    out = cache_health(trace_engine, org_id=org, granularity="day")
    by = {b["bucket"]: b for b in out}
    assert set(by) == {"2026-07-08", "2026-07-09"}

    d1 = by["2026-07-08"]
    assert d1["requests"] == 2 and d1["tokens_prompt"] == 1000 and d1["tokens_cached"] == 600
    assert d1["tokens_cache_write"] == 50
    assert d1["hit_rate"] == pytest.approx(0.6)      # 600 / 1000
    assert d1["warm_hold_requests"] == 1             # only the :warm-hold turn

    d2 = by["2026-07-09"]
    assert d2["hit_rate"] == 0.0                      # 0 cached / 1000 prompt
    assert d2["warm_hold_requests"] == 0


def test_hit_rate_zero_prompt_no_division_error(trace_engine):
    """An empty-prompt bucket reads 0.0, never a ZeroDivisionError."""
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, tokens_prompt=0, tokens_cached=0)
    [b] = cache_health(trace_engine, org_id=org)
    assert b["hit_rate"] == 0.0 and b["tokens_prompt"] == 0


def test_hour_granularity_and_org_isolation(trace_engine):
    a, b = "o_" + uuid.uuid4().hex, "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=a, ts_start="2026-07-08T10:15:00+00:00", tokens_prompt=100, tokens_cached=50)
    _write(trace_engine, org_id=a, ts_start="2026-07-08T11:45:00+00:00", tokens_prompt=100, tokens_cached=10)
    _write(trace_engine, org_id=b, ts_start="2026-07-08T10:00:00+00:00", tokens_prompt=999999, tokens_cached=999999)

    out = cache_health(trace_engine, org_id=a, granularity="hour")
    assert [x["bucket"] for x in out] == ["2026-07-08T10", "2026-07-08T11"]  # never sees org b
    assert out[0]["hit_rate"] == pytest.approx(0.5)


def test_only_ok_rows_and_window(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, ts_start="2026-07-08T10:00:00+00:00", tokens_prompt=100, tokens_cached=50)
    _write(trace_engine, org_id=org, ts_start="2026-07-08T10:00:00+00:00", tokens_prompt=100,
           tokens_cached=100, status="error")                     # excluded (not ok)
    _write(trace_engine, org_id=org, ts_start="2026-07-20T10:00:00+00:00", tokens_prompt=100, tokens_cached=100)  # out of window
    out = cache_health(trace_engine, org_id=org, start="2026-07-08", end="2026-07-09")
    [b] = out
    assert b["requests"] == 1 and b["hit_rate"] == pytest.approx(0.5)


# --- HTTP surface: admin + org scoping -------------------------------------------


@pytest.fixture()
def usage_app(tmp_path):
    from fastapi.testclient import TestClient

    from toto_gateway.app import create_app
    from toto_gateway.config import Settings
    from toto_gateway.trace import sql_engine

    trace_db = f"sqlite:///{tmp_path}/trace.db"
    settings = Settings(catalog="catalog.yaml", trace_jsonl="", trace_db=trace_db,
                        trace_stdout=False, auth_token="test-operator-token", db=":memory:",
                        fake_exec=True)
    app = create_app(settings=settings)
    with TestClient(app) as client:
        engine = sql_engine(app.state.gateway.writer)
        yield client, app, engine


def _seed(engine, **kw):
    row = dict(request_id="r", ts_start="2026-07-08T10:00:00+00:00", lane="frontier",
               runner_id="openrouter", model="or-sonnet-5", residency_class="cloud", status="ok")
    row.update(kw)
    with Session(engine) as s:
        s.add(TraceRow(**row))
        s.commit()


def _override_identity(app, **kw):
    from toto_gateway.routes.deps import Identity, require_auth
    app.dependency_overrides[require_auth] = lambda: Identity(user_id="u1", authenticated=True, **kw)


def test_endpoint_shape(usage_app):
    client, app, engine = usage_app
    _override_identity(app, org_id="o_a", role="admin")
    _seed(engine, org_id="o_a", tokens_prompt=1000, tokens_cached=600,
          route_reason="label:code_generation:warm-hold")
    r = client.get("/v1/admin/usage/cache-health", params={"granularity": "day"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"buckets", "from", "to", "granularity"}
    assert body["granularity"] == "day"
    [b] = body["buckets"]
    assert set(b) == {"bucket", "requests", "tokens_prompt", "tokens_cached",
                      "tokens_cache_write", "warm_hold_requests", "hit_rate"}
    assert b["hit_rate"] == pytest.approx(0.6) and b["warm_hold_requests"] == 1
    app.dependency_overrides.clear()


def test_endpoint_cross_org_denied(usage_app):
    client, app, _engine = usage_app
    _override_identity(app, org_id="o_a", role="admin")
    assert client.get("/v1/admin/usage/cache-health", params={"org_id": "o_b"}).status_code == 403
    assert client.get("/v1/admin/usage/cache-health", params={"org_id": "o_a"}).status_code == 200
    app.dependency_overrides.clear()


def test_endpoint_admin_required(usage_app):
    client, app, _engine = usage_app
    _override_identity(app, org_id="o_a", role="member")
    assert client.get("/v1/admin/usage/cache-health").status_code == 403
    app.dependency_overrides.clear()


def test_endpoint_operator_defaults_to_local_org_in_oss(usage_app):
    client, _app, _engine = usage_app  # default bearer = operator; oss edition
    # OSS: the operator is the single `local` tenant, so an org-less call resolves there (no 400).
    assert client.get("/v1/admin/usage/cache-health").status_code == 200
    assert client.get("/v1/admin/usage/cache-health", params={"org_id": "o_x"}).status_code == 200


def test_endpoint_bad_granularity_rejected(usage_app):
    client, app, _engine = usage_app
    _override_identity(app, org_id="o_a", role="admin")
    assert client.get("/v1/admin/usage/cache-health", params={"granularity": "week"}).status_code == 422
    app.dependency_overrides.clear()
