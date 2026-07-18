"""Provider resilience: retry transient upstream failures on the same model, then fall back
across catalog entries within the residency boundary. Motivated by a prod run that died when
triage's upstream returned 429.  No network — fake run callables raise the error types.
"""

from __future__ import annotations

import json

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.driver.core import Driver, Exec
from toto_gateway.schemas import ChatCompletionRequest, Message


class _Rate(Exception):
    """Stand-in for openai.RateLimitError — classified by status_code, like the SDK type."""
    status_code = 429


def _cat() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id="or-qwen3-coder-flash", lane="economy", endpoint="openai", residency_class="cloud"),
        CatalogEntry(id="or-haiku-4.5", lane="economy", endpoint="openai", residency_class="cloud"),
        CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="openai", residency_class="cloud"),
        CatalogEntry(id="local-secure", lane="economy", endpoint="openai", residency_class="in_perimeter"),
        CatalogEntry(id="fake-x", lane="fake", endpoint="fake", residency_class="in_perimeter"),
    ])


def _driver(cat, spans, **kw):
    return Driver(catalog=cat, complete_fn=None, driver_model="or-sonnet-4.6",
                  triage_model="or-qwen3-coder-flash", toto=None, observe=spans.append,
                  provider_backoff_base=0.0, provider_retries=2, **kw)


def _req(model="or-qwen3-coder-flash"):
    return ChatCompletionRequest(model=model, messages=[Message(role="user", content="x")])


@pytest.mark.asyncio
async def test_retry_same_model_then_success():
    spans = []
    d = _driver(_cat(), spans)
    calls = [0]

    async def run(req):
        calls[0] += 1
        if calls[0] <= 2:  # two transient blips, success on the 3rd (within retries+1)
            raise ConnectionError("blip")
        return Exec(text="ok", model=req.model)

    ex, model, note = await d._call(_req(), run, name="t")
    assert ex.text == "ok" and model == "or-qwen3-coder-flash" and note is None
    assert calls[0] == 3
    assert not [s for s in spans if s["node"] == "model_fallback"]  # no fallback needed


@pytest.mark.asyncio
async def test_fallback_across_models_emits_span_and_honest_note():
    spans = []
    d = _driver(_cat(), spans)

    async def run(req):
        if req.model == "or-qwen3-coder-flash":
            raise _Rate("rate limited")
        return Exec(text="ok", model=req.model)

    ex, model, note = await d._call(_req(), run, name="t")
    assert model == "or-haiku-4.5"  # next same-residency candidate in catalog order
    assert note == "fallback: or-qwen3-coder-flash 429 → or-haiku-4.5"
    fb = [s for s in spans if s["node"] == "model_fallback"]
    assert len(fb) == 1
    assert fb[0]["from"] == "or-qwen3-coder-flash" and fb[0]["to"] == "or-haiku-4.5"
    assert fb[0]["reason"] == "429" and fb[0]["attempt"] == 1


@pytest.mark.asyncio
async def test_all_candidates_fail_raises_original_error():
    spans = []
    d = _driver(_cat(), spans)

    async def run(req):
        raise _Rate(f"429 on {req.model}")

    with pytest.raises(_Rate) as ei:
        await d._call(_req(), run, name="t")
    assert "or-qwen3-coder-flash" in str(ei.value)  # ORIGINAL error, not an obscure last-fallback one


@pytest.mark.asyncio
async def test_non_retryable_raises_immediately():
    spans = []
    d = _driver(_cat(), spans)
    calls = [0]

    async def run(req):
        calls[0] += 1
        raise ValueError("400 bad request")  # 4xx-class → not retryable

    with pytest.raises(ValueError):
        await d._call(_req(), run, name="t")
    assert calls[0] == 1  # no retries, no fallback
    assert not [s for s in spans if s["node"] == "model_fallback"]


@pytest.mark.asyncio
async def test_privacy_never_falls_back_across_residency():
    spans = []
    d = _driver(_cat(), spans)
    # local-secure (in_perimeter) is the only in-perimeter model → privacy has NO fallback.
    assert d._fallbacks("local-secure", privacy=True) == []
    # control: a non-privacy frontier model CAN fall back.
    assert "or-haiku-4.5" in d._fallbacks("or-qwen3-coder-flash", privacy=False)

    tried = []

    async def run(req):
        tried.append(req.model)
        raise _Rate("429")

    with pytest.raises(_Rate):
        await d._call(_req("local-secure"), run, name="t", privacy=True)
    assert set(tried) == {"local-secure"}  # fails rather than leaking to a frontier model


@pytest.mark.asyncio
async def test_run_survives_transient_triage_429():
    """The prod scenario: triage's upstream 429s. It must retry and the run must complete."""
    spans = []
    state = {"triage": 0}

    async def complete(req):
        sys = req.messages[0].text() if req.messages[0].role == "system" else ""
        if "triage classifier" in sys:
            state["triage"] += 1
            if state["triage"] <= 2:
                raise _Rate("429 temporarily rate-limited upstream")
            return Exec(text=json.dumps({"kind": "trivial", "reason": "t"}), model=req.model)
        if "request directly" in sys:
            return Exec(text="answer", model=req.model)
        return Exec(text="x", model=req.model)

    d = Driver(catalog=_cat(), complete_fn=complete, driver_model="or-sonnet-4.6",
               triage_model="or-qwen3-coder-flash", toto=None, observe=spans.append,
               provider_backoff_base=0.0)
    result = await d.run("hi")
    assert result.kind == "trivial" and result.answer == "answer"
    assert state["triage"] == 3  # 2 failures + 1 success, all on or-qwen3-coder-flash (retry, no fallback)
