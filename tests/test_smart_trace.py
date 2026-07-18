"""LangSmith trace for the smart passthrough route (routing.smart_trace).

The emit is best-effort observability, so the contract is narrow but load-bearing:
- when it runs it posts a `toto/smart` chain with a `classify` child, the served model on the
  conventional ls_ keys, and token usage in OUTPUTS (what LangSmith rolls up to cost);
- it NEVER raises — a broken tracer must not break a served request.
"""

import sys

from toto_gateway.routing import smart_trace


class _FakeRun:
    """Records what emit() builds instead of hitting LangSmith."""

    def __init__(self, **kw):
        self.kw = kw
        self.children = []
        self.ended = None
        self.posted = False
        self.patched = False

    def post(self, *a, **k):
        self.posted = True

    def patch(self, *a, **k):
        self.patched = True

    def create_child(self, name, run_type="chain", **kw):
        c = _FakeRun(name=name, run_type=run_type, **kw)
        self.children.append(c)
        return c

    def end(self, **kw):
        self.ended = kw


def _install_fake(monkeypatch):
    created = []

    def _factory(**kw):
        r = _FakeRun(**kw)
        created.append(r)
        return r

    fake_mod = type(sys)("langsmith")
    fake_mod.RunTree = _factory
    monkeypatch.setitem(sys.modules, "langsmith", fake_mod)
    return created


_ARGS = dict(
    messages=[{"role": "user", "content": "write a python quicksort"}],
    classifier_model="or-gemini-2.5-flash",
    label="code_generation",
    route_reason="label:code_generation:team",
    resolved_model="or-qwen3-coder-flash",
    served_model="anthropic/claude-sonnet-5",   # differs from resolved: a fallback served it
    content="def quicksort(a): ...",
    tokens_prompt=12,
    tokens_completion=40,
    cost_usd=0.0009,
    latency_ms=850,
    classify_ms=120.0,
    request_id="abc123def456",
    conversation_key="9be492e16920a73c",
)


def test_emit_builds_toto_smart_run_with_classify_child(monkeypatch):
    created = _install_fake(monkeypatch)
    smart_trace.emit(**_ARGS)

    assert len(created) == 1, "one root run per smart request"
    root = created[0]
    assert root.kw["name"] == "toto/smart"
    assert root.kw["run_type"] == "chain"
    assert root.posted and root.patched
    # user prompt is the root input (content boundary dropped for observability).
    assert root.kw["inputs"]["messages"][0]["content"] == "write a python quicksort"

    # classify child (the Gemini-Flash tagging step) with the classifier model + label out.
    assert len(root.children) == 1
    child = root.children[0]
    assert child.kw["name"] == "classify" and child.kw["run_type"] == "llm"
    assert child.kw["inputs"]["model"] == "or-gemini-2.5-flash"
    assert child.ended["outputs"] == {"label": "code_generation"}

    # routing decision on metadata; SERVED model on the conventional ls_ keys (not resolved).
    md = root.ended["metadata"]
    assert md["classified_as"] == "code_generation"
    assert md["route_reason"] == "label:code_generation:team"
    assert md["resolved_model"] == "or-qwen3-coder-flash"
    assert md["ls_model_name"] == "anthropic/claude-sonnet-5"
    assert md["ls_provider"] == "anthropic"
    # correlation join keys: request_id → the gateway trace record; conversation_key → the chat.
    assert md["request_id"] == "abc123def456"
    assert md["conversation_key"] == "9be492e16920a73c"
    # usage in OUTPUTS is what LangSmith aggregates into trace-level token/cost columns.
    usage = root.ended["outputs"]["usage_metadata"]
    assert usage == {"input_tokens": 12, "output_tokens": 40, "total_tokens": 52}


def test_emit_never_raises_when_tracer_blows_up(monkeypatch):
    """A RunTree that raises on construction must be swallowed — tracing is never fatal."""
    def _boom(**kw):
        raise RuntimeError("langsmith exploded")

    fake_mod = type(sys)("langsmith")
    fake_mod.RunTree = _boom
    monkeypatch.setitem(sys.modules, "langsmith", fake_mod)

    smart_trace.emit(**_ARGS)  # must not raise


def test_tracing_enabled_false_without_env(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    assert smart_trace.tracing_enabled() is False
