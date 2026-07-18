"""Wire-level resilience (Chunk H2).

The gap the fake-callable tests (test_driver_resilience.py) structurally can't cover: those raise
Python exceptions from a stub `run` callable, BYPASSING the OpenAI runner. Here every fault is an
HTTP reply on the REAL runner wire — it travels through the OpenAI SDK's status→exception mapping
(500→InternalServerError, 429→RateLimitError, 4xx→BadRequestError, timeout→APITimeoutError), which
is exactly what `driver.core._is_retryable` classifies on. So a bug in HOW the runner surfaces a
fault (wrong exception type, swallowed status) would show up HERE and nowhere else.

Two altitudes, both faulting the same wire:
  - Driver-level: build a real Driver whose complete_fn = a faulted Gateway; drive `_call`.
  - App-level:    create_app + the faulted Gateway; POST /v1/sessions; read the run's outcome.

WAVE 1 (not built yet): circuit breaker, passthrough-lane retry. Placeholder xfail at the bottom.
"""

from __future__ import annotations

import time

import openai
import pytest

from harness.appharness import default_settings, drive_to_terminal, in_process_app
from harness.asserts import assert_span, find_spans
from harness.faults import Faults
from toto_gateway.breaker import CircuitOpen
from toto_gateway.driver.core import Driver, Exec
from toto_gateway.routing.labels import LabelBindings
from toto_gateway.schemas import ChatCompletionRequest, Message

# The faults harness's curated OpenAI-only cloud catalog (harness/faults.curated_catalog).
# Fallback order is catalog order: wire-a → wire-b → wire-c (all cloud → same residency boundary).
_M0, _M1 = "wire-a", "wire-b"


def _req(model: str = _M0) -> ChatCompletionRequest:
    return ChatCompletionRequest(model=model, messages=[Message(role="user", content="x")])


def _driver(gateway, spans, **kw) -> Driver:
    """A real Driver whose OWN reasoning calls execute through the faulted gateway (mirrors
    app.build_driver's complete_fn), retries fast (no backoff sleep), spans → the list."""
    async def complete_fn(req) -> Exec:
        res = await gateway.complete(req, harness="driver")
        t = res.trace
        txt = res.response.choices[0].message.content if res.response.choices else ""
        return Exec(text=txt or "", model=t.model, lane=t.lane)

    return Driver(catalog=gateway.catalog, complete_fn=complete_fn, driver_model=_M0,
                  triage_model=_M0, toto=None, observe=spans.append,
                  provider_backoff_base=0.0, provider_retries=2, **kw)


# --- driver-level: real wire, real Driver._call ------------------------------------------

async def test_retryable_500_recovers_after_transient_blips():
    """A flaky provider (500, 500, then 200) must recover on the same model — no fallback. Killing
    the retry loop in core.py makes THIS red (the first 500 would raise)."""
    spans: list[dict] = []
    f = Faults()
    d = _driver(f.gateway(f.flaky(fail_first_n=2, status=500, content="done")), spans)
    ex, model, note = await d._call(_req(), d._complete, name="t")
    assert ex.text == "done" and model == _M0 and note is None
    assert not find_spans(spans, "model_fallback")  # recovered in-place


async def test_429_falls_back_across_models_emits_span_and_note():
    """First model 429s (retryable) → after retries, fall back to the next cloud model, which
    answers. The honest note + model_fallback span must reflect the real 429."""
    spans: list[dict] = []
    f = Faults()
    gw = f.gateway({_M0: f.http_429(retry_after=3), _M1: f.ok("ok")})
    d = _driver(gw, spans)
    ex, model, note = await d._call(_req(), d._complete, name="t")
    assert model == _M1
    assert note == f"fallback: {_M0} 429 → {_M1}"
    assert_span(spans, "model_fallback", **{"from": _M0, "to": _M1, "reason": "429", "attempt": 1})


async def test_non_retryable_400_raises_immediately_no_fallback():
    """A 4xx (validation/auth) is NOT retryable — raise at once, never burn a fallback on it."""
    spans: list[dict] = []
    f = Faults()
    gw = f.gateway({_M0: f.http_400(), _M1: f.ok("should-not-reach")})
    d = _driver(gw, spans)
    with pytest.raises(openai.BadRequestError):
        await d._call(_req(), d._complete, name="t")
    assert not find_spans(spans, "model_fallback")


async def test_total_outage_raises_original_error_after_exhausting_candidates():
    """Every cloud model 500s → exhaust all candidates, emit a fallback span per switch, then
    surface the ORIGINAL error (not an obscure last-candidate one)."""
    spans: list[dict] = []
    f = Faults()
    d = _driver(f.gateway(f.http_500()), spans)
    with pytest.raises(openai.InternalServerError):
        await d._call(_req(), d._complete, name="t")
    # gpt-4o → or-sonnet-4.6 → or-sonnet-5 : two switches, two spans.
    assert len(find_spans(spans, "model_fallback")) == 2


async def test_hung_provider_hits_wall_clock_cap_not_sdk_timeout():
    """A HUNG provider (2s) must degrade to the fallback ladder at the label wall-clock cap
    (~100ms), never stall the sub-task for the SDK's default timeout. _classify_label returns
    None and the whole call returns well under the provider's 2s."""
    spans: list[dict] = []
    f = Faults()
    labels = LabelBindings(_raw={"labels": {
        "code_generation": {"model": _M1, "desc": "write or debug code"},
        "other": {"model": None, "desc": "none of the above"},
    }})
    d = _driver(f.gateway(f.slow(2000)), spans, labels=labels, label_model=_M0,
                label_timeout_ms=100)
    t0 = time.perf_counter()
    label, meta = await d._classify_label("write a python function")
    elapsed = time.perf_counter() - t0
    assert label is None                 # classifier down → None (the ladder decides, not a stall)
    assert meta is None                  # timeout → no metadata captured either
    assert elapsed < 1.5, elapsed        # capped at ~0.1s, nowhere near the provider's 2s


async def test_partial_stream_is_tolerated_by_runner():
    """A stream the upstream drops mid-flight (no `data: [DONE]`) must not crash the runner — it
    yields the chunks it got and terminates. Deeper mid-stream RESUME is Wave 1."""
    from harness.faults import curated_catalog
    from toto_gateway.runners.openai import OpenAIRunner

    f = Faults()
    entry = curated_catalog().get(_M0)
    runner = OpenAIRunner(entry, client=f.client(f.partial_stream(cut_after_n=3)))
    texts = []
    async for chunk in runner.stream(_req(), entry):
        for ch in chunk.choices:
            if ch.delta and ch.delta.content:
                texts.append(ch.delta.content)
    assert texts == ["tok0", "tok1", "tok2"]  # got exactly what arrived before the cut, no raise


# --- app-level: the same wire, driven through the real create_app ------------------------

async def test_run_survives_provider_429_via_fallback_through_app():
    """POST /v1/sessions with the driver's model 429ing on the wire: the run completes because
    every reasoning call falls back to a healthy cloud model. Proves the WHOLE app path (route →
    background task → driver → gateway → runner) rides out a wire fault, and the fallback is
    visible in the run's event log."""
    f = Faults()
    gw = f.gateway({_M0: f.http_429()})  # or-sonnet-4.6 / or-sonnet-5 default to ok()
    settings = default_settings(fake_exec=False, driver_model=_M0, triage_model=_M0,
                                provider_backoff_base=0.0)
    async with in_process_app(gw, settings=settings) as (client, app):
        r = await client.post("/v1/sessions", json={"query": "hello"})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        row = await drive_to_terminal(app, run_id)
        assert row.get("status") == "done", row
        events = await app.state.runs.events_after(run_id)
        assert [e for e in events if e["kind"] == "model_fallback"], \
            sorted({e["kind"] for e in events})


async def test_total_outage_fails_run_cleanly_through_app():
    """Every model 500s → the run reaches a TERMINAL failed state, never silence (an SSE client
    gets run_failed, not a hang). This is the 'no task leak / always terminal' guarantee."""
    f = Faults()
    settings = default_settings(fake_exec=False, driver_model=_M0, triage_model=_M0,
                                provider_backoff_base=0.0)
    async with in_process_app(f.gateway(f.http_500()), settings=settings) as (client, app):
        r = await client.post("/v1/sessions", json={"query": "hello"})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        row = await drive_to_terminal(app, run_id)
        assert row.get("status") == "failed", row


# --- P3: passthrough plane — Gateway.complete(resilient=True) + chat.py surfacing --------

async def test_passthrough_retries_transient_then_succeeds():
    """resilient=True: a flaky provider (500, 500, 200) recovers on the SAME model via retry —
    the passthrough surface no longer turns a transient blip into a 502."""
    f = Faults()
    gw = f.gateway(f.flaky(fail_first_n=2, status=500, content="done"), backoff_base=0.0)
    res = await gw.complete(_req(), resilient=True)
    assert res.response.choices[0].message.content == "done"
    assert res.trace.model == _M0 and res.trace.status == "ok"


async def test_passthrough_falls_back_to_same_residency_provider():
    """Provider A hard-down (500) → fall back to same-residency provider B, which answers. The
    SERVED model is the one on the returned trace (chat.py surfaces it as x_toto.model)."""
    f = Faults()
    gw = f.gateway({_M0: f.http_500(), _M1: f.ok("via-b")}, backoff_base=0.0)
    res = await gw.complete(_req(), resilient=True)
    assert res.response.choices[0].message.content == "via-b"
    assert res.trace.model == _M1
    assert res.trace.route_reason.startswith(f"fallback: {_M0} 500 → {_M1}")


async def test_passthrough_opt_out_disables_fallback():
    """allow_fallback=False: a pinned model that is down raises rather than silently substituting
    a different provider — the caller who pinned the model opted out."""
    f = Faults()
    gw = f.gateway(f.http_500(), backoff_base=0.0)
    with pytest.raises(openai.InternalServerError):
        await gw.complete(_req(), resilient=True, allow_fallback=False)


async def test_passthrough_non_retryable_400_raises_no_fallback():
    """A 4xx is never retried and never burns a fallback (would just fail identically)."""
    f = Faults()
    gw = f.gateway({_M0: f.http_400(), _M1: f.ok("nope")}, backoff_base=0.0)
    with pytest.raises(openai.BadRequestError):
        await gw.complete(_req(), resilient=True)


async def test_passthrough_fallback_never_crosses_residency():
    """A residency-pinned (in_perimeter) model that is down never falls to a cloud provider —
    fallback is residency-bounded, which is also the privacy boundary."""
    from toto_gateway.catalog import Catalog, CatalogEntry

    cat = Catalog(models=[
        CatalogEntry(id="local-a", lane="economy", endpoint="openai", residency_class="in_perimeter"),
        CatalogEntry(id="cloud-b", lane="frontier", endpoint="openai", residency_class="cloud"),
    ])
    f = Faults()
    gw = f.gateway({"local-a": f.http_500(), "cloud-b": f.ok("leaked")}, catalog=cat,
                   backoff_base=0.0)
    with pytest.raises(openai.InternalServerError):
        await gw.complete(_req("local-a"), resilient=True)


async def test_chat_completions_survives_transient_via_fallback_through_app():
    """POST /v1/chat/completions with the primary model 500ing → the client gets 200 via
    fallback, and x_toto.model reports the model that ACTUALLY served (not the requested one)."""
    f = Faults()
    gw = f.gateway({_M0: f.http_500(), _M1: f.ok("ok-b")}, backoff_base=0.0)
    settings = default_settings(driver=False, provider_backoff_base=0.0)
    async with in_process_app(gw, settings=settings) as (client, _app):
        r = await client.post("/v1/chat/completions",
                              json={"model": _M0, "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["choices"][0]["message"]["content"] == "ok-b"
        assert body["x_toto"]["model"] == _M1


async def test_chat_completions_terminal_429_passes_status_and_retry_after():
    """Every candidate 429s → the client gets a 429 (not the old blanket 502) WITH the upstream
    Retry-After passed through. Competing on reliability means honest backpressure."""
    f = Faults()
    # retries=0 keeps the test fast: honoring the 7s Retry-After across retries×candidates is
    # correct but slow — the honor-Retry-After sleep itself is unit-tested in test_retry_authority.
    gw = f.gateway(f.http_429(retry_after=7), retries=0)
    settings = default_settings(driver=False)
    async with in_process_app(gw, settings=settings) as (client, _app):
        r = await client.post("/v1/chat/completions",
                              json={"model": _M0, "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 429, r.text
        assert r.headers.get("retry-after") == "7"


async def test_chat_completions_no_fallback_header_opts_out_through_app():
    """`x-toto-no-fallback` header: the down primary is NOT substituted; the 500 surfaces as a
    generic 502 (would have been a 200 via fallback without the opt-out)."""
    f = Faults()
    gw = f.gateway({_M0: f.http_500(), _M1: f.ok("ok-b")}, backoff_base=0.0)
    settings = default_settings(driver=False, provider_backoff_base=0.0)
    async with in_process_app(gw, settings=settings) as (client, _app):
        r = await client.post("/v1/chat/completions",
                              json={"model": _M0, "messages": [{"role": "user", "content": "hi"}]},
                              headers={"x-toto-no-fallback": "1"})
        assert r.status_code == 502, r.text


# --- P2: single retry authority + Retry-After --------------------------------------------

def _solo_cloud_catalog():
    """One cloud model, no fallback siblings — isolates the per-model retry COUNT."""
    from toto_gateway.catalog import Catalog, CatalogEntry
    return Catalog(models=[
        CatalogEntry(id=_M0, lane="frontier", endpoint="openai", residency_class="cloud")])


async def test_single_retry_authority_no_sdk_multiplier():
    """Our layer is the SOLE retry authority: a dead model is hit exactly provider_retries+1 = 3
    times, NOT 9 (the old SDK max_retries=2 × Driver retries stack). The faults client pins
    max_retries=0; production clients do the same (asserted in test_provider_timeouts)."""
    import httpx

    hits = {"n": 0}

    def h(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(500, json={"error": {"message": "boom", "type": "server_error"}})

    f = Faults()
    gw = f.gateway(h, catalog=_solo_cloud_catalog(), backoff_base=0.0)
    d = _driver(gw, [])  # provider_retries=2
    with pytest.raises(openai.InternalServerError):
        await d._call(_req(), d._complete, name="t")
    assert hits["n"] == 3, hits["n"]


async def test_backoff_honors_upstream_retry_after(monkeypatch):
    """A 429 carrying `Retry-After: 5` makes the next attempt wait ~5s (the provider's advertised
    cooldown), not the ~0s exp-backoff — so we don't re-trip the 429 before it clears."""
    import asyncio as _asyncio

    import httpx

    slept: list[float] = []

    async def fake_sleep(s):  # capture the honored delay without actually waiting
        slept.append(s)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)

    f = Faults()
    ok = f.ok("done")
    calls = {"n": 0}

    def h(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "rl", "type": "rate_limit_error"}},
                                  headers={"retry-after": "5"})
        return ok(request)

    gw = f.gateway(h, catalog=_solo_cloud_catalog(), backoff_base=0.0)
    d = _driver(gw, [])
    ex, model, note = await d._call(_req(), d._complete, name="t")
    assert calls["n"] == 2 and ex.text == "done"
    assert 5.0 in slept, slept  # honored the header, not the (base=0) exp backoff


# --- P4: per-provider circuit breaker (Wave 1 — was the xfail placeholder) ----------------

async def test_breaker_fast_fails_dead_provider_after_threshold():
    """A dead provider trips the breaker: the FIRST request pays the retry budget (3 wire hits at
    threshold=3) and trips it; the NEXT request short-circuits with ZERO wire hits (fast-fail,
    no full timeout×retries) and a circuit_open span was emitted."""
    import httpx

    hits = {"n": 0}

    def h(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(500, json={"error": {"message": "boom", "type": "server_error"}})

    spans: list[dict] = []
    f = Faults()
    gw = f.gateway(h, catalog=_solo_cloud_catalog(), backoff_base=0.0,
                   breaker_fail_threshold=3, observe=spans.append)

    with pytest.raises(openai.InternalServerError):
        await gw.complete(_req(), resilient=True, allow_fallback=False)
    assert hits["n"] == 3, hits["n"]                 # paid the retry budget, tripped on the 3rd
    assert find_spans(spans, "circuit_open"), spans

    before = hits["n"]
    with pytest.raises(CircuitOpen):
        await gw.complete(_req(), resilient=True, allow_fallback=False)
    assert hits["n"] == before, "breaker OPEN must short-circuit — no new wire calls"


async def test_breaker_recovers_after_reset_via_half_open_trial():
    """reset_seconds=0 → the breaker goes HALF_OPEN immediately; once the provider recovers, the
    trial succeeds and the breaker closes (circuit_close span), serving the request normally."""
    import httpx

    state = {"down": True}
    ok = Faults().ok("back")

    def h(request: httpx.Request) -> httpx.Response:
        if state["down"]:
            return httpx.Response(500, json={"error": {"message": "boom", "type": "server_error"}})
        return ok(request)

    spans: list[dict] = []
    f = Faults()
    gw = f.gateway(h, catalog=_solo_cloud_catalog(), backoff_base=0.0,
                   breaker_fail_threshold=3, breaker_reset_seconds=0.0, observe=spans.append)

    with pytest.raises(openai.InternalServerError):
        await gw.complete(_req(), resilient=True, allow_fallback=False)
    assert find_spans(spans, "circuit_open"), spans

    state["down"] = False  # provider recovers; reset=0 → immediate HALF_OPEN trial closes it
    res = await gw.complete(_req(), resilient=True, allow_fallback=False)
    assert res.response.choices[0].message.content == "back"
    assert find_spans(spans, "circuit_close"), spans


async def test_breaker_never_trips_on_4xx_client_errors():
    """A stream of 400s must NOT open the breaker (only transient 429/5xx do) — a bad-request
    burst can't take a provider out of rotation. Each 400 raises immediately, no short-circuit."""
    import httpx

    hits = {"n": 0}

    def h(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(400, json={"error": {"message": "bad", "type": "invalid_request_error"}})

    spans: list[dict] = []
    f = Faults()
    gw = f.gateway(h, catalog=_solo_cloud_catalog(), backoff_base=0.0,
                   breaker_fail_threshold=3, observe=spans.append)
    for _ in range(5):
        with pytest.raises(openai.BadRequestError):
            await gw.complete(_req(), resilient=True, allow_fallback=False)
    assert hits["n"] == 5, "every 400 reached the wire — breaker never short-circuited"
    assert not find_spans(spans, "circuit_open"), spans
