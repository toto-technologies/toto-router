"""Schema regression tests — passthrough_params must not leak SDK request-control kwargs."""

from __future__ import annotations

from toto_gateway.schemas import ChatCompletionRequest, Message


def _req(**extra):
    return ChatCompletionRequest(
        model="or-qwen3-coder-flash",
        messages=[Message(role="user", content="hi")],
        **extra,
    )


def test_passthrough_keeps_generation_params():
    out = ChatCompletionRequest(
        model="or-qwen3-coder-flash",
        messages=[Message(role="user", content="hi")],
        temperature=0.5,
        top_p=0.9,
        max_tokens=128,
    ).passthrough_params()
    assert out == {"temperature": 0.5, "top_p": 0.9, "max_tokens": 128}


def test_passthrough_strips_sdk_request_control_kwargs():
    # A tenant tries to hijack the outbound provider request.
    out = _req(
        extra_headers={"X-Title": "someone-else"},
        extra_body={"provider": {"order": ["trains-on-data"]}},
        extra_query={"foo": "bar"},
        timeout=999999,
        temperature=0.7,
    ).passthrough_params()
    assert "extra_headers" not in out
    assert "extra_body" not in out
    assert "extra_query" not in out
    assert "timeout" not in out
    assert out["temperature"] == 0.7
