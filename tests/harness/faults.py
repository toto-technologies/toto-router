"""Failure-injection library (Chunk H2).

Composable provider faults on the OPENAI RUNNER WIRE — the gap the fake-callable resilience
tests can't cover. Each fault is an `httpx.MockTransport` handler (the same technique already in
tests/test_mlx_integration.py:66). We inject it as the `http_client` of a real `AsyncOpenAI`
client, so a faulted HTTP reply travels through the REAL OpenAI SDK exception mapping
(500→InternalServerError, 429→RateLimitError, timeout→APITimeoutError, 4xx→BadRequestError) —
exactly the types `driver.core._is_retryable` classifies on. `max_retries=0` on the client makes
`Driver._call` the single retry authority (matching runners/openai.py).

Usage:
    f = Faults()
    gw = f.gateway(f.http_429(), models=("gpt-4o",))          # all models 429
    gw = f.gateway({"gpt-4o": f.http_500(), "or-sonnet-4.6": f.ok()})  # per-model
    # then: create_app(settings, gw)  OR  drive gw.complete() / a Driver directly.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import httpx

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.gateway import Gateway
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.trace import MemoryTraceWriter

Handler = Callable[[httpx.Request], httpx.Response]


def curated_catalog() -> Catalog:
    """A focused OpenAI-only cloud catalog for wire faults (same idea as
    test_driver_resilience._cat(), but every entry routes to the OpenAI runner so the WHOLE
    fallback ladder is faultable). The real catalog.yaml mixes an anthropic entry into the cloud
    residency band, which would route a fallback off the wire under test."""
    def gpt(i: str) -> CatalogEntry:
        return CatalogEntry(id=i, lane="frontier", endpoint="openai", residency_class="cloud")
    return Catalog(models=[
        gpt("wire-a"), gpt("wire-b"), gpt("wire-c"),           # cloud fallback ladder, in order
        CatalogEntry(id="wire-fake", lane="fake", endpoint="fake", residency_class="in_perimeter"),
    ])


def _ok_body(content: str = "ok", model: str = "mock-upstream") -> dict:
    return {
        "id": "chatcmpl-mock", "object": "chat.completion", "created": 1700000000, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def _err_body(message: str, typ: str) -> dict:
    return {"error": {"message": message, "type": typ, "code": None}}


class Faults:
    """Factory of MockTransport handlers + a helper that wires them into a real Gateway."""

    # --- primitive fault behaviors (each returns an httpx MockTransport handler) ------------

    def ok(self, content: str = "ok") -> Handler:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body(content))
        return h

    def http_500(self) -> Handler:
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json=_err_body("upstream boom", "server_error"))
        return h

    def http_429(self, retry_after: int | None = None) -> Handler:
        def h(request: httpx.Request) -> httpx.Response:
            headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
            return httpx.Response(429, json=_err_body("rate limited", "rate_limit_error"),
                                  headers=headers)
        return h

    def http_400(self) -> Handler:
        """A non-retryable client error (auth/validation) — must raise immediately, no fallback."""
        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json=_err_body("bad request", "invalid_request_error"))
        return h

    def timeout(self) -> Handler:
        def h(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("mock read timeout", request=request)
        return h

    def slow(self, ms: int, then: Handler | None = None) -> Handler:
        """A hung/slow provider: sleep `ms` then reply (200 by default). Async handler — httpx
        MockTransport awaits a coroutine handler on the async path."""
        inner = then or self.ok()

        async def h(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(ms / 1000.0)
            return inner(request)
        return h

    def flaky(self, fail_first_n: int, status: int = 500, content: str = "ok") -> Handler:
        """Fail the first N calls (transient), succeed after — proves same-model retry recovers."""
        calls = {"n": 0}

        def h(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] <= fail_first_n:
                return httpx.Response(status, json=_err_body("flaky", "server_error"))
            return httpx.Response(200, json=_ok_body(content))
        return h

    def partial_stream(self, cut_after_n: int) -> Handler:
        """An SSE stream that emits `cut_after_n` content chunks then ends WITHOUT a [DONE]
        sentinel (upstream dropped mid-stream). Exercises the runner's stream tolerance."""
        chunks = []
        for i in range(cut_after_n):
            chunks.append(
                'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m",'
                '"choices":[{"index":0,"delta":{"content":"tok%d"},"finish_reason":null}]}\n\n' % i
            )
        body = "".join(chunks).encode()  # note: no `data: [DONE]` — the cut

        def h(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body,
                                  headers={"content-type": "text/event-stream"})
        return h

    # --- wiring: a real Gateway whose OpenAI runners are faulted ----------------------------

    def client(self, handler: Handler):
        """A real AsyncOpenAI client whose HTTP transport is the mock handler. max_retries=0 so
        Driver._call owns retries (an SDK-internal retry would mask the wire fault)."""
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            base_url="http://mock.local/v1", api_key="test", max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    def gateway(self, faults, *, models=None, writer: MemoryTraceWriter | None = None,
                catalog: Catalog | None = None, **gw_kwargs) -> Gateway:
        """Gateway wired to the real catalog, where every OpenAI-endpoint entry runs a faulted
        OpenAIRunner. `faults` is one handler (all models) or a {model_id: handler} map; models
        absent from the map fall back to a plain `ok()` handler (healthy fallback target)."""
        catalog = catalog or curated_catalog()
        as_map = faults if isinstance(faults, dict) else None

        def factory(entry: CatalogEntry):
            if entry.endpoint == "fake":
                from toto_gateway.runners.fake import FakeRunner
                return FakeRunner(entry)
            if entry.endpoint == "openai":
                from toto_gateway.runners.openai import OpenAIRunner
                h = (as_map.get(entry.id, self.ok())) if as_map is not None else faults
                return OpenAIRunner(entry, client=self.client(h))
            # Anthropic/MLX bare endpoints aren't the wire under test — never reached in these
            # suites (driver_model/triage_model are always openai entries). Fail loud if they are.
            raise RuntimeError(f"faults harness: unfaulted endpoint {entry.endpoint} ({entry.id})")

        registry = RunnerRegistry(factory=factory)
        return Gateway(catalog=catalog, registry=registry,
                       writer=writer or MemoryTraceWriter(), **gw_kwargs)
