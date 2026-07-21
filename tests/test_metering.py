"""Metering taxonomy (control-plane C4) — rollup over `gateway_events`, org isolation, export seam.

The aggregation SQL runs on BOTH dialects via the `trace_engine` fixture (mirrors conftest's
`sql_store`): an unkeyed `pytest` exercises the sqlite param and skips pg; `pytest -m pg` (CI's
Postgres job, TOTO_GW_TEST_DATABASE_URL set) selects the Postgres param — so a PG-only break in the
rollup (a bad substr, a MAX(boolean), a lexical-range assumption) is caught pre-merge. Each test
uses a unique org id, so the shared PG needs no truncation.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from toto_gateway.metering import export_billing_records, rollup_usage
from toto_gateway.trace import TraceRow

_PG_URL = os.environ.get("TOTO_GW_TEST_DATABASE_URL")


def _pg_engine_url(url: str) -> str:
    # SQLAlchemy needs an explicit driver; the project ships psycopg3 (not psycopg2).
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@pytest.fixture(params=[
    pytest.param("sqlite", id="sqlite"),
    pytest.param("postgres", id="postgres", marks=[
        pytest.mark.pg,
        pytest.mark.skipif(not _PG_URL, reason="set TOTO_GW_TEST_DATABASE_URL for the PG lane"),
    ]),
])
def trace_engine(request):
    """A SQLAlchemy engine holding the `gateway_events` table on each dialect — the same rollup
    assertions must hold on both (that IS the dual-dialect check)."""
    if request.param == "sqlite":
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)  # one persistent in-memory conn
    else:
        engine = create_engine(_pg_engine_url(_PG_URL))
    SQLModel.metadata.create_all(engine)
    return engine


def _write(engine, **kw) -> None:
    row = dict(request_id="r", ts_start="2026-07-08T10:00:00+00:00", lane="frontier",
               runner_id="openrouter", model="or-sonnet", residency_class="cloud", status="ok")
    row.update(kw)
    with Session(engine) as s:
        s.add(TraceRow(**row))
        s.commit()


# --- the linchpin: a rollup total EQUALS the raw gateway_events sum for the same window ---------

def test_rollup_equals_raw_sum(trace_engine):
    org = "o_" + uuid.uuid4().hex
    other = "o_" + uuid.uuid4().hex
    rows = [
        dict(cost_usd=1.50, tokens_prompt=100, tokens_completion=40, frontier_baseline_usd=5.0),
        dict(cost_usd=0.25, tokens_prompt=10, tokens_completion=5, frontier_baseline_usd=1.0),
        dict(cost_usd=3.00, tokens_prompt=900, tokens_completion=300, frontier_baseline_usd=9.0),
    ]
    for r in rows:
        _write(trace_engine, org_id=org, **r)
    _write(trace_engine, org_id=other, cost_usd=99.0, tokens_prompt=1, tokens_completion=1)  # noise

    [total] = rollup_usage(trace_engine, org_id=org)  # no group_by → one total row
    assert total["requests"] == len(rows)
    assert total["cost_usd"] == pytest.approx(sum(r["cost_usd"] for r in rows))
    assert total["tokens_prompt"] == sum(r["tokens_prompt"] for r in rows)
    assert total["tokens"] == sum(r["tokens_prompt"] + r["tokens_completion"] for r in rows)
    assert total["frontier_baseline_usd"] == pytest.approx(sum(r["frontier_baseline_usd"] for r in rows))
    assert total["savings_usd"] == pytest.approx(15.0 - 4.75)  # baseline - cost


def test_per_dimension_slicing(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, model="or-sonnet", cost_usd=2.0, tokens_prompt=100)
    _write(trace_engine, org_id=org, model="or-sonnet", cost_usd=1.0, tokens_prompt=50)
    _write(trace_engine, org_id=org, model="local-mlx", cost_usd=0.0, tokens_prompt=200)

    by_model = {r["model"]: r for r in rollup_usage(trace_engine, org_id=org, group_by=["model"])}
    assert set(by_model) == {"or-sonnet", "local-mlx"}
    assert by_model["or-sonnet"]["cost_usd"] == pytest.approx(3.0)
    assert by_model["or-sonnet"]["requests"] == 2
    assert by_model["local-mlx"]["tokens"] == 200

    # multi-dimension: team x provider
    _write(trace_engine, org_id=org, team_id="t1", runner_id="anthropic", cost_usd=4.0)
    multi = rollup_usage(trace_engine, org_id=org, group_by=["team", "provider"])
    assert any(r["team"] == "t1" and r["provider"] == "anthropic" and r["cost_usd"] == pytest.approx(4.0)
               for r in multi)


def test_rollup_by_label_and_user(trace_engine):
    """Analytics A1: label (task type) and user are first-class group_by dimensions; the per-group
    totals still sum back to the raw total (rollup == raw-sum holds with the new dims)."""
    org = "o_" + uuid.uuid4().hex
    rows = [
        dict(label="code_generation", user_id="u1", cost_usd=1.0, tokens_prompt=10),
        dict(label="code_generation", user_id="u2", cost_usd=2.0, tokens_prompt=20),
        dict(label="brainstorming", user_id="u1", cost_usd=4.0, tokens_prompt=40),
        dict(label=None, user_id=None, cost_usd=0.5, tokens_prompt=5),  # unattributed old-style row
    ]
    for r in rows:
        _write(trace_engine, org_id=org, **r)

    by_label = {r["label"]: r for r in rollup_usage(trace_engine, org_id=org, group_by=["label"])}
    assert by_label["code_generation"]["cost_usd"] == pytest.approx(3.0)
    assert by_label["code_generation"]["requests"] == 2
    assert by_label["brainstorming"]["cost_usd"] == pytest.approx(4.0)
    assert None in by_label and by_label[None]["cost_usd"] == pytest.approx(0.5)  # NULL groups

    by_user = {r["user"]: r for r in rollup_usage(trace_engine, org_id=org, group_by=["user"])}
    assert by_user["u1"]["cost_usd"] == pytest.approx(5.0)  # code_gen + brainstorming
    assert by_user["u2"]["cost_usd"] == pytest.approx(2.0)

    # rollup == raw-sum with the new dims present: label x model grouping sums to the org total
    total = sum(r["cost_usd"] for r in rows)
    sliced = rollup_usage(trace_engine, org_id=org, group_by=["label", "model"])
    assert sum(r["cost_usd"] for r in sliced) == pytest.approx(total)
    [grand] = rollup_usage(trace_engine, org_id=org)
    assert grand["cost_usd"] == pytest.approx(total)


def test_time_bucket_and_window(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, ts_start="2026-07-08T09:00:00+00:00", cost_usd=1.0)
    _write(trace_engine, org_id=org, ts_start="2026-07-08T23:00:00+00:00", cost_usd=2.0)
    _write(trace_engine, org_id=org, ts_start="2026-07-09T01:00:00+00:00", cost_usd=4.0)

    by_day = {r["bucket"]: r for r in
              rollup_usage(trace_engine, org_id=org, granularity="day")}
    assert by_day["2026-07-08"]["cost_usd"] == pytest.approx(3.0)
    assert by_day["2026-07-09"]["cost_usd"] == pytest.approx(4.0)

    # half-open window [start, end): lexical ISO comparison
    [windowed] = rollup_usage(trace_engine, org_id=org,
                              start="2026-07-08T00:00:00+00:00", end="2026-07-09T00:00:00+00:00")
    assert windowed["cost_usd"] == pytest.approx(3.0)  # the 07-09 row is excluded


def test_rollup_sums_cached_tokens(trace_engine):
    """tokens_cached (provider-cache hits) sums like the other token counters — the per-model
    drill-down's prompt/completion/cached split reads straight off the rollup row."""
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, tokens_prompt=100, tokens_completion=40, tokens_cached=60)
    _write(trace_engine, org_id=org, tokens_prompt=50, tokens_completion=20, tokens_cached=10)
    [total] = rollup_usage(trace_engine, org_id=org)
    assert total["tokens_cached"] == 70
    assert total["tokens"] == 210  # cached is a subset of prompt, never double-counted into tokens
    [by_model] = rollup_usage(trace_engine, org_id=org, group_by=["model"])
    assert by_model["tokens_cached"] == 70


def test_org_isolation_rollup(trace_engine):
    a, b = "o_" + uuid.uuid4().hex, "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=a, cost_usd=1.0)
    _write(trace_engine, org_id=b, cost_usd=50.0)
    [row] = rollup_usage(trace_engine, org_id=a)
    assert row["cost_usd"] == pytest.approx(1.0)  # never sees org b's $50


def test_unknown_dimension_rejected(trace_engine):
    with pytest.raises(ValueError):
        rollup_usage(trace_engine, org_id="o_x", group_by=["ssn"])  # not in the allowlist


# --- export seam: well-formed Stripe-shaped records (NO invoicing) -----------------------------

def test_export_billing_records_shape(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, team_id="t1", model="or-sonnet",
           tokens_prompt=1000, tokens_completion=200, cost_usd=3.0, cost_estimated=False)
    _write(trace_engine, org_id=org, team_id="t1", model="or-sonnet",
           tokens_prompt=500, tokens_completion=100, cost_usd=1.5, cost_estimated=True)  # → estimated
    _write(trace_engine, org_id=org, team_id="t2", model="local-mlx",
           tokens_prompt=10, tokens_completion=0, cost_usd=0.0)
    _write(trace_engine, org_id="o_other", team_id="tX", model="or-sonnet", cost_usd=9.0)  # noise

    recs = export_billing_records(trace_engine, org, "2026-07")
    assert {(r.team_id, r.model) for r in recs} == {("t1", "or-sonnet"), ("t2", "local-mlx")}
    sonnet = next(r for r in recs if r.model == "or-sonnet")
    assert sonnet.org_id == org and sonnet.period == "2026-07" and sonnet.unit == "tokens"
    assert sonnet.quantity_tokens == 1800  # 1200 + 600
    assert sonnet.cost_usd == pytest.approx(4.5)
    assert sonnet.estimated is True  # any row in the group was estimated


def test_export_excludes_other_periods(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, model="m", ts_start="2026-07-15T00:00:00+00:00", cost_usd=2.0)
    _write(trace_engine, org_id=org, model="m", ts_start="2026-08-01T00:00:00+00:00", cost_usd=9.0)
    recs = export_billing_records(trace_engine, org, "2026-07")
    assert len(recs) == 1 and recs[0].cost_usd == pytest.approx(2.0)  # August excluded


# --- HTTP surface: require_role admin + org-scoping (cross-org denied) --------------------------


@pytest.fixture()
def usage_app(tmp_path):
    """A full app whose gateway writes traces to a SQL sink, so /v1/admin/usage can read them."""
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
               runner_id="openrouter", model="or-sonnet", residency_class="cloud", status="ok")
    row.update(kw)
    with Session(engine) as s:
        s.add(TraceRow(**row))
        s.commit()


def test_operator_defaults_to_local_org_in_oss(usage_app):
    client, _app, _engine = usage_app  # default client sends the operator bearer; oss edition
    # OSS binds the operator to the single `local` tenant, so an org-less call resolves there
    # (no 400). A named org still wins for an operator that asks for one.
    r0 = client.get("/v1/admin/usage")
    assert r0.status_code == 200 and r0.json()["org_id"] == "local"
    r = client.get("/v1/admin/usage", params={"org_id": "o_1"})
    assert r.status_code == 200 and r.json()["org_id"] == "o_1"


def test_operator_usage_returns_rollup(usage_app):
    client, _app, engine = usage_app
    _seed(engine, org_id="o_acme", model="or-sonnet", cost_usd=2.0, tokens_prompt=100)
    _seed(engine, org_id="o_acme", model="or-sonnet", cost_usd=1.0, tokens_prompt=50)
    r = client.get("/v1/admin/usage", params={"org_id": "o_acme", "group_by": "model"})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows[0]["model"] == "or-sonnet" and rows[0]["cost_usd"] == pytest.approx(3.0)


def _override_identity(app, **kw):
    from toto_gateway.routes.deps import Identity, require_auth
    ident = Identity(user_id="u1", authenticated=True, **kw)
    app.dependency_overrides[require_auth] = lambda: ident


def test_cross_org_denied(usage_app):
    client, app, _engine = usage_app
    _override_identity(app, org_id="o_a", role="admin")
    assert client.get("/v1/admin/usage", params={"org_id": "o_b"}).status_code == 403  # not my org
    assert client.get("/v1/admin/usage", params={"org_id": "o_a"}).status_code == 200  # my org ok
    assert client.get("/v1/admin/usage").status_code == 200  # defaults to my org
    app.dependency_overrides.clear()


def test_admin_role_required(usage_app):
    client, app, _engine = usage_app
    _override_identity(app, org_id="o_a", role="member")  # below admin
    assert client.get("/v1/admin/usage", params={"org_id": "o_a"}).status_code == 403
    app.dependency_overrides.clear()


def test_export_endpoint_shape(usage_app):
    client, app, engine = usage_app
    _override_identity(app, org_id="o_a", role="admin")
    _seed(engine, org_id="o_a", team_id="t1", model="or-sonnet",
          tokens_prompt=1000, tokens_completion=200, cost_usd=3.0)
    r = client.get("/v1/admin/usage/export", params={"period": "2026-07"})
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == "2026-07" and body["org_id"] == "o_a"
    li = body["line_items"][0]
    assert li["quantity_tokens"] == 1200 and li["unit"] == "tokens" and li["team_id"] == "t1"
    app.dependency_overrides.clear()
