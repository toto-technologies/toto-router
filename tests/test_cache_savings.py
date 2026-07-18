"""Cache P&L (multi-model-caching plan §6): the savings rollup + its org-scoped HTTP surface.

`metering.cache_savings` derives read savings / write premium / net from token COUNTS × the live
catalog price table (never from the stored cost_usd, which already reflects the read discount).
`GET /v1/admin/usage/cache-savings` serves it with the same admin + org-scoping floor as the sibling
usage endpoints. Both dialects exercise the aggregation via the sqlite param here (mirrors
test_metering's harness); the money math is dialect-independent Python.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from toto_gateway.catalog import Catalog, CatalogEntry, Price
from toto_gateway.metering import cache_savings
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


def _catalog() -> Catalog:
    """A model priced $2/1k prompt with the Anthropic-family multipliers (read 0.1, write 1.25),
    plus a $0-price local model that can never save anything."""
    return Catalog(models=[
        CatalogEntry(id="or-sonnet-5", lane="frontier", endpoint="openai", residency_class="cloud",
                     price_usd_per_1k=Price(prompt=2.0, completion=10.0,
                                            cache_read_multiplier=0.1, cache_write_multiplier=1.25)),
        CatalogEntry(id="local-mlx", lane="economy", endpoint="fake", residency_class="in_perimeter",
                     price_usd_per_1k=Price(prompt=0.0, completion=0.0)),
    ])


# --- rollup math -----------------------------------------------------------------


def test_savings_math_per_model(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, model="or-sonnet-5", tokens_cached=600, tokens_cache_write=100)
    _write(trace_engine, org_id=org, model="or-sonnet-5", tokens_cached=400, tokens_cache_write=300)
    out = cache_savings(trace_engine, catalog=_catalog(), org_id=org)

    [row] = out["models"]
    assert row["model"] == "or-sonnet-5" and row["lane"] == "frontier" and row["requests"] == 2
    assert row["tokens_cached"] == 1000 and row["tokens_cache_write"] == 400
    # read: 1000/1k * 2.0 * (1-0.1) = 1.8 ; write premium: 400/1k * 2.0 * 0.25 = 0.2 ; net = 1.6
    assert row["read_savings_usd"] == pytest.approx(1.8)
    assert row["write_premium_usd"] == pytest.approx(0.2)
    assert row["net_usd"] == pytest.approx(1.6)
    assert out["total"]["net_usd"] == pytest.approx(1.6)
    assert out["total"]["tokens_cached"] == 1000 and out["total"]["tokens_cache_write"] == 400


def test_totals_sum_across_models(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, model="or-sonnet-5", tokens_cached=1000, tokens_cache_write=0)
    _write(trace_engine, org_id=org, model="local-mlx", tokens_cached=5000, tokens_cache_write=500)
    out = cache_savings(trace_engine, catalog=_catalog(), org_id=org)
    by_model = {m["model"]: m for m in out["models"]}
    # $0-priced model can never save or cost anything, regardless of token counts
    assert by_model["local-mlx"]["net_usd"] == 0.0
    assert by_model["or-sonnet-5"]["net_usd"] == pytest.approx(1.8)  # 1000/1k*2*0.9
    assert out["total"]["net_usd"] == pytest.approx(1.8)


def test_unknown_model_prices_at_zero(trace_engine):
    """A retired/unknown model id contributes 0 (no guessing), not a crash."""
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, model="ghost-model", tokens_cached=9999, tokens_cache_write=9999)
    out = cache_savings(trace_engine, catalog=_catalog(), org_id=org)
    assert out["total"]["net_usd"] == 0.0
    assert out["models"][0]["read_savings_usd"] == 0.0


def test_org_isolation(trace_engine):
    a, b = "o_" + uuid.uuid4().hex, "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=a, tokens_cached=1000)
    _write(trace_engine, org_id=b, tokens_cached=1_000_000)  # noise
    out = cache_savings(trace_engine, catalog=_catalog(), org_id=a)
    assert out["total"]["read_savings_usd"] == pytest.approx(1.8)  # never sees org b


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


def test_endpoint_shape_and_math(usage_app):
    """or-sonnet-5 is $2/$10 per 1M (per-1k 0.002 in catalog.yaml — scale fixed 2026-07-14;
    this test's old pins had the pre-fix 1000x figures baked in) with the 1.25x write premium —
    the endpoint returns the P&L the console renders verbatim."""
    client, app, engine = usage_app
    _override_identity(app, org_id="o_a", role="admin")
    _seed(engine, org_id="o_a", model="or-sonnet-5", tokens_cached=1000, tokens_cache_write=400)
    r = client.get("/v1/admin/usage/cache-savings")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"total", "models", "from", "to"}
    assert set(body["total"]) == {"net_usd", "read_savings_usd", "write_premium_usd",
                                  "tokens_cached", "tokens_cache_write"}
    assert body["total"]["net_usd"] == pytest.approx(0.0016)  # 0.0018 read - 0.0002 write premium
    m = body["models"][0]
    assert m["model"] == "or-sonnet-5" and m["lane"] == "frontier"
    assert m["read_savings_usd"] == pytest.approx(0.0018) \
        and m["write_premium_usd"] == pytest.approx(0.0002)
    app.dependency_overrides.clear()


def test_endpoint_cross_org_denied(usage_app):
    client, app, _engine = usage_app
    _override_identity(app, org_id="o_a", role="admin")
    assert client.get("/v1/admin/usage/cache-savings", params={"org_id": "o_b"}).status_code == 403
    assert client.get("/v1/admin/usage/cache-savings", params={"org_id": "o_a"}).status_code == 200
    app.dependency_overrides.clear()


def test_endpoint_admin_required(usage_app):
    client, app, _engine = usage_app
    _override_identity(app, org_id="o_a", role="member")  # below admin
    assert client.get("/v1/admin/usage/cache-savings").status_code == 403
    app.dependency_overrides.clear()


def test_endpoint_operator_must_name_org(usage_app):
    client, _app, _engine = usage_app  # default bearer is the operator
    assert client.get("/v1/admin/usage/cache-savings").status_code == 400
    assert client.get("/v1/admin/usage/cache-savings", params={"org_id": "o_x"}).status_code == 200
