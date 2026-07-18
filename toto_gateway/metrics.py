"""Prometheus metric surface — the queryable RED/USE series behind operator-gated /metrics.

One registry, one endpoint (plan observability.md §5 owns the namespace so siblings emit into
these exact names — never a second registry or /metrics2). All emission is fail-open: a metrics
error NEVER breaks a request (same discipline as the trace sinks, trace.py:169).

Fed by two fan-out points that already see everything:
  - per upstream call  → MetricsTraceWriter (a TraceWriter sink in the existing MultiTraceWriter)
  - per HTTP request   → RequestContextMiddleware (obs.py) observes duration + in-flight gauge
  - live gauges        → bind_live() wires the already-tracked run/SSE/semaphore counts

ponytail: a module-level singleton registry. One process, one metric surface; tests read deltas
off the shared registry (counters only move up). No per-app registry churn.
"""

from __future__ import annotations

from typing import Any, Callable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Latency buckets tuned for a gateway: sub-ms routing tax up through slow upstream calls.
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry

        # --- per-request RED (from the request middleware) -----------------------
        self.requests_total = Counter(
            "gw_requests_total", "HTTP requests by route/method/status",
            ["route", "method", "status"], registry=r)
        self.request_duration = Histogram(
            "gw_request_duration_seconds", "HTTP request duration by route",
            ["route"], buckets=_LATENCY_BUCKETS, registry=r)
        self.in_flight = Gauge(
            "gw_in_flight", "In-flight HTTP requests by route", ["route"], registry=r)

        # --- per upstream call RED (from every TraceRecord) ----------------------
        self.upstream_calls_total = Counter(
            "gw_upstream_calls_total", "Upstream provider calls",
            ["provider", "model", "lane", "status"], registry=r)
        self.upstream_latency = Histogram(
            "gw_upstream_latency_seconds", "Upstream call latency",
            ["provider", "model"], buckets=_LATENCY_BUCKETS, registry=r)
        self.cost_usd_total = Counter(
            "gw_cost_usd_total", "Cumulative upstream cost in USD",
            ["provider", "model"], registry=r)
        self.tokens_total = Counter(
            "gw_tokens_total", "Tokens by provider/model/kind (prompt|completion|cached)",
            ["provider", "model", "kind"], registry=r)
        # cache hit-rate rides the same trace seam — no ExactCache instrumentation needed.
        self.cache_lookups_total = Counter(
            "gw_cache_lookups_total", "Exact-cache lookups (non-stream calls)", registry=r)
        self.cache_hits_total = Counter(
            "gw_cache_hits_total", "Exact-cache hits", registry=r)

        # --- live gauges (USE) — sources wired by bind_live() --------------------
        self.in_flight_runs = Gauge(
            "gw_in_flight_runs", "Live driver runs on this replica", registry=r)
        self.sse_connections = Gauge(
            "gw_sse_connections", "Live SSE subscribe generators on this replica", registry=r)
        self.llm_semaphore_inflight = Gauge(
            "gw_llm_semaphore_inflight", "Concurrent outbound LLM calls held by the valve",
            registry=r)

    # --- emission (all fail-open) -------------------------------------------------

    def observe_request(self, route: str, method: str, status: int, duration_s: float) -> None:
        try:
            self.requests_total.labels(route, method, str(status)).inc()
            self.request_duration.labels(route).observe(duration_s)
        except Exception:  # metrics NEVER break a request
            pass

    def observe_upstream(self, rec: Any) -> None:
        """Fold one TraceRecord into the per-provider/model series. runner_id == the provider box."""
        try:
            provider, model, lane = rec.runner_id, rec.model, rec.lane
            self.upstream_calls_total.labels(provider, model, lane, rec.status).inc()
            if rec.latency_ms_total is not None:
                self.upstream_latency.labels(provider, model).observe(rec.latency_ms_total / 1000.0)
            if rec.cost_usd:
                self.cost_usd_total.labels(provider, model).inc(rec.cost_usd)
            for kind, n in (("prompt", rec.tokens_prompt), ("completion", rec.tokens_completion),
                            ("cached", rec.tokens_cached)):
                if n:
                    self.tokens_total.labels(provider, model, kind).inc(n)
            if not rec.stream:  # cache is consulted on non-stream calls only
                self.cache_lookups_total.inc()
                if rec.cache_hit:
                    self.cache_hits_total.inc()
        except Exception:
            pass

    def bind_live(self, **sources: Callable[[], float]) -> None:
        """Wire live-gauge sources (called from create_app). set_function is idempotent — a second
        app in tests just rebinds to the newest sources. Each callback is guarded at collect time."""
        for name, fn in sources.items():
            gauge = getattr(self, name, None)
            if gauge is not None:
                gauge.set_function(_guarded(fn))


def _guarded(fn: Callable[[], float]) -> Callable[[], float]:
    def wrapped() -> float:
        try:
            return float(fn())
        except Exception:
            return 0.0
    return wrapped


# The one process-wide surface. Every emitter imports this; /metrics scrapes its registry.
METRICS = Metrics()


class MetricsTraceWriter:
    """TraceWriter sink: every upstream call folds into the per-provider/model RED. Added into the
    existing MultiTraceWriter so it inherits the fail-open fan-out (trace.py:169) — and it's free,
    the record already exists (gateway.py _account)."""

    def __init__(self, metrics: Metrics = METRICS) -> None:
        self._m = metrics

    def write(self, record: Any) -> None:
        self._m.observe_upstream(record)


def demo() -> None:
    """ponytail self-check: names exist + exposition parses + counters move."""
    from prometheus_client import generate_latest
    from prometheus_client.parser import text_string_to_metric_families

    m = Metrics()
    m.observe_request("/v1/x", "GET", 200, 0.01)

    class _Rec:
        runner_id, model, lane, status = "wire-a", "m1", "economy", "ok"
        latency_ms_total, cost_usd = 120, 0.002
        tokens_prompt, tokens_completion, tokens_cached, stream, cache_hit = 10, 5, 0, False, False

    m.observe_upstream(_Rec())
    m.bind_live(in_flight_runs=lambda: 3)
    text = generate_latest(m.registry).decode()
    # the parser strips the _total suffix from counter FAMILY names; samples keep it.
    samples = {s.name: s for fam in text_string_to_metric_families(text) for s in fam.samples}
    assert "gw_requests_total" in samples, sorted(samples)
    assert "gw_upstream_calls_total" in samples
    ok = [s for s in samples.values()
          if s.name == "gw_upstream_calls_total" and s.labels.get("status") == "ok"]
    assert ok and ok[0].value == 1.0, ok
    assert samples["gw_in_flight_runs"].value == 3.0  # live gauge via set_function
    print("metrics demo ok")


if __name__ == "__main__":
    demo()
