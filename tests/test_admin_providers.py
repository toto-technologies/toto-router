"""Live provider-health admin API — GET /v1/admin/providers/health + breaker.snapshot().

Boots a full fake-exec app whose gateway writes traces to a SQL sink (the `health_app` fixture,
same shape as test_admin_requests.activity_app), so the route reads real breaker state + seeded
traffic. Covers: closed-state defaults with no traffic, an OPEN breaker surfaced with retry_in,
windowed stats (count/errors/percentiles) computed from seeded rows, and admin/org scoping.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from toto_gateway.app import create_app
from toto_gateway.config import Settings
from toto_gateway.trace import TraceRow, sql_engine


@pytest.fixture()
def health_app(tmp_path):
    trace_db = f"sqlite:///{tmp_path}/trace.db"
    settings = Settings(catalog="catalog.yaml", trace_jsonl="", trace_db=trace_db,
                        trace_stdout=False, auth_token="test-operator-token", db=":memory:",
                        fake_exec=True)
    app = create_app(settings=settings)
    with TestClient(app) as client:
        engine = sql_engine(app.state.gateway.writer)
        yield client, app, engine


def _seed(engine, **kw):
    row = dict(request_id="r", ts_start=datetime.now(timezone.utc).isoformat(), lane="frontier",
               runner_id="openrouter", model="or-sonnet-5", residency_class="cloud", status="ok")
    row.update(kw)
    with Session(engine) as s:
        s.add(TraceRow(**row))
        s.commit()


def _as(app, **kw):
    from toto_gateway.routes.deps import Identity, require_auth
    app.dependency_overrides[require_auth] = lambda: Identity(authenticated=True, **kw)


def _find(body, host):
    return next(p for p in body["providers"] if p["provider"] == host)


# --- snapshot unit (the read-only breaker view the route depends on) ---------------------------

def test_breaker_snapshot_states():
    from toto_gateway.breaker import CircuitBreaker, provider_key

    now = [0.0]
    b = CircuitBreaker(fail_threshold=2, reset_seconds=10.0, clock=lambda: now[0])
    k = provider_key("https://openrouter.ai/api/v1")
    assert b.snapshot() == {}                      # untouched → absent (route defaults to closed)
    b.on_failure(k)
    assert b.snapshot()[k] == {"state": "closed", "retry_in": None, "consecutive_failures": 1}
    b.on_failure(k)                                # trips OPEN
    assert b.snapshot()[k] == {"state": "open", "retry_in": 10.0, "consecutive_failures": 2}
    now[0] = 10.0
    assert b.snapshot()[k]["state"] == "half-open" and b.snapshot()[k]["retry_in"] == 0.0


# --- HTTP surface ------------------------------------------------------------------------------

def test_closed_defaults_with_no_traffic(health_app):
    client, app, _engine = health_app
    _as(app, user_id="admin1", org_id="o_a", role="admin")
    body = client.get("/v1/admin/providers/health").json()
    assert body["org_id"] == "o_a" and body["window_seconds"] == 3600 and body["trace_db"] is True
    p = _find(body, "openrouter.ai")               # or-sonnet-5 lives on openrouter in catalog.yaml
    assert p["state"] == "closed" and p["retry_in"] is None
    assert "or-sonnet-5" in p["models"]
    assert p["stats"] == {"requests": 0, "errors": 0, "error_rate": 0.0,
                          "latency_p50_ms": None, "latency_p95_ms": None, "latency_avg_ms": None}
    app.dependency_overrides.clear()


def test_open_breaker_surfaced_with_retry_in(health_app):
    client, app, _engine = health_app
    breaker = app.state.gateway._breaker
    for _ in range(breaker._threshold):            # trip openrouter.ai OPEN
        breaker.on_failure("openrouter.ai")
    _as(app, user_id="admin1", org_id="o_a", role="admin")
    p = _find(client.get("/v1/admin/providers/health").json(), "openrouter.ai")
    assert p["state"] == "open"
    assert p["retry_in"] > 0 and p["consecutive_failures"] == breaker._threshold
    app.dependency_overrides.clear()


def test_stats_computed_from_seeded_traces(health_app):
    client, app, engine = health_app
    for lat in (100, 200, 300, 400, 500):
        _seed(engine, org_id="o_a", model="or-sonnet-5", status="ok", latency_ms_total=lat)
    _seed(engine, org_id="o_a", model="or-sonnet-5", status="error", latency_ms_total=None)
    _seed(engine, org_id="o_b", model="or-sonnet-5", status="ok", latency_ms_total=9999)  # other org
    _as(app, user_id="admin1", org_id="o_a", role="admin")
    p = _find(client.get("/v1/admin/providers/health").json(), "openrouter.ai")
    assert p["stats"] == {"requests": 6, "errors": 1, "error_rate": round(1 / 6, 4),
                          "latency_p50_ms": 300, "latency_p95_ms": 500, "latency_avg_ms": 300}
    app.dependency_overrides.clear()


def test_window_excludes_old_rows(health_app):
    client, app, engine = health_app
    _seed(engine, org_id="o_a", ts_start="2020-01-01T00:00:00+00:00", latency_ms_total=42)
    _as(app, user_id="admin1", org_id="o_a", role="admin")
    p = _find(client.get("/v1/admin/providers/health", params={"window": 3600}).json(),
              "openrouter.ai")
    assert p["stats"]["requests"] == 0             # the 2020 row is outside the 1h window
    app.dependency_overrides.clear()


def test_non_admin_member_is_403(health_app):
    client, app, _engine = health_app
    _as(app, user_id="u1", org_id="o_a", role="member")
    assert client.get("/v1/admin/providers/health").status_code == 403
    app.dependency_overrides.clear()


def test_cross_org_stats_never_leak(health_app):
    client, app, engine = health_app
    _seed(engine, org_id="o_b", model="or-sonnet-5", status="ok", latency_ms_total=123)
    _as(app, user_id="admin1", org_id="o_a", role="admin")  # admin of A, naming B is refused
    assert client.get("/v1/admin/providers/health", params={"org_id": "o_b"}).status_code == 403
    p = _find(client.get("/v1/admin/providers/health").json(), "openrouter.ai")
    assert p["stats"]["requests"] == 0              # org B's row never counts for org A
    app.dependency_overrides.clear()


def test_operator_must_name_an_org(health_app):
    client, _app, _engine = health_app             # default client carries the operator bearer
    assert client.get("/v1/admin/providers/health").status_code == 400
    assert client.get("/v1/admin/providers/health", params={"org_id": "o_a"}).status_code == 200
