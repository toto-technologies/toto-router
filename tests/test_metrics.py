"""O1/O2 — the /metrics surface + emission (observability.md chunks 1–2).

Asserts: authed /metrics parses as valid Prometheus text with counters moving; non-operator → 403,
no creds → 401; a fake-exec gateway.complete moves the per-provider/model RED (calls/cost/latency/
tokens); a forced sink exception never breaks the request (fail-open, like the other trace sinks).
"""

from __future__ import annotations

import pytest
from prometheus_client.parser import text_string_to_metric_families

from toto_gateway.app import build_gateway
from toto_gateway.metrics import METRICS
from toto_gateway.schemas import ChatCompletionRequest, Message
from harness.appharness import default_settings, in_process_app

pytestmark = pytest.mark.asyncio


async def test_metrics_authed_parses_and_counters_move(app_client):
    client, _app = app_client
    await client.get("/readyz")  # drive one request so gw_requests_total has moved

    r = await client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=")  # prometheus_client bumps the version

    families = {f.name: f for f in text_string_to_metric_families(r.text)}
    # RED families present (parser strips the _total suffix from counter family names).
    assert "gw_requests" in families
    assert "gw_request_duration_seconds" in families
    assert "gw_in_flight" in families
    total = sum(s.value for s in families["gw_requests"].samples
                if s.name == "gw_requests_total")
    assert total > 0  # counters moved


async def test_metrics_rejects_non_operator_and_anon(app_client):
    client, app = app_client
    # no creds → 401 (login required, same posture as /statusz)
    assert (await client.get("/metrics", headers={"authorization": ""})).status_code == 401
    # authenticated but NOT the operator → 403 (the operator gate)
    uid = await app.state.auth.create_user("m@example.com", None, email_verified=True)
    token = await app.state.auth.mint_token(uid, "api", ttl_seconds=3600)
    r = await client.get("/metrics", headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_upstream_red_moves_on_complete():
    """Drive a fake-exec request through gateway.complete → per-provider/model series move."""
    gw = build_gateway(default_settings())  # real MultiTraceWriter incl. MetricsTraceWriter
    req = ChatCompletionRequest(model="echo-cloud",
                                messages=[Message(role="user", content="hello there")])
    reg = METRICS.registry
    labels = {"provider": "fake-echo-cloud", "model": "echo-cloud"}

    def val(name, **lbl):
        return reg.get_sample_value(name, lbl) or 0.0

    calls0 = val("gw_upstream_calls_total", lane="fake", status="ok", **labels)
    cost0 = val("gw_cost_usd_total", **labels)
    lat0 = val("gw_upstream_latency_seconds_count", **labels)
    tok0 = val("gw_tokens_total", kind="completion", **labels)

    res = await gw.complete(req, harness="test")
    assert res.trace.status == "ok"

    assert val("gw_upstream_calls_total", lane="fake", status="ok", **labels) == calls0 + 1
    assert val("gw_cost_usd_total", **labels) > cost0  # cost accrued
    assert val("gw_upstream_latency_seconds_count", **labels) == lat0 + 1
    assert val("gw_tokens_total", kind="completion", **labels) > tok0


async def test_metrics_sink_failure_is_fail_open(monkeypatch):
    """A metrics-sink exception must never break the response (MultiTraceWriter guards every sink)."""
    gw = build_gateway(default_settings())
    boom = lambda _rec: (_ for _ in ()).throw(RuntimeError("metrics down"))
    monkeypatch.setattr(METRICS, "observe_upstream", boom)
    req = ChatCompletionRequest(model="echo-cloud",
                                messages=[Message(role="user", content="still works?")])
    res = await gw.complete(req, harness="test")  # must NOT raise
    assert res.response.choices[0].message.content  # response served unaffected


async def test_metrics_endpoint_is_operator_gated_end_to_end():
    """The scrape carries the operator bearer; a fresh app exposes the surface only to it."""
    async with in_process_app() as (client, _app):
        assert (await client.get("/metrics")).status_code == 200  # bearer pre-stamped
