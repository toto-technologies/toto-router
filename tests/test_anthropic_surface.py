"""Pins the nemo-switchyard translation engine behavior the /v1/messages surface relies on.

These tests intentionally couple to the third-party engine: if an upgrade changes format ids,
stream direction, or event vocabulary, this file fails before any route does.
"""
from switchyard_rust.translation import TranslationEngine


def _openai_chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0, "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def test_request_translation_anthropic_to_openai_chat():
    eng = TranslationEngine()
    body = {
        "model": "echo-local", "max_tokens": 64, "system": "be brief",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }
    out = eng.translate_request("anthropic_messages", "openai_chat", body)
    assert out["model"] == "echo-local"
    roles = [m["role"] for m in out["messages"]]
    assert "user" in roles
    # Anthropic's top-level `system` must land as a system message, not be dropped.
    assert "system" in roles


def test_response_translation_openai_chat_to_anthropic():
    eng = TranslationEngine()
    resp = {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "echo-local",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    out = eng.translate_response("openai_chat", "anthropic_messages", resp)
    assert out["role"] == "assistant"
    assert out["content"][0]["type"] == "text"
    assert out["content"][0]["text"] == "hello"
    assert out["stop_reason"] == "end_turn"


async def test_stream_translation_openai_chunks_to_anthropic_events():
    async def source():
        yield _openai_chunk({"role": "assistant", "content": "he"})
        yield _openai_chunk({"content": "llo"})
        yield _openai_chunk({}, finish="stop")

    eng = TranslationEngine()
    events = [
        e async for e in eng.translate_stream(
            "openai_chat", "anthropic_messages", source(), model="echo-local"
        )
    ]
    types = [e["type"] for e in events]
    assert "message_start" in types
    assert "content_block_delta" in types
    assert types[-1] == "message_stop"


import pytest

from harness.appharness import OP_TOKEN, in_process_app


async def test_x_api_key_header_authenticates_like_bearer():
    """Anthropic SDK clients send x-api-key, not Authorization. Same tokens must work."""
    async with in_process_app() as (client, _):
        # This client fixture normally injects the bearer header; override per-request.
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "echo-local",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": "", "x-api-key": OP_TOKEN},
        )
        assert r.status_code == 200, r.text

        r = await client.post(
            "/v1/chat/completions",
            json={"model": "echo-local",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": "", "x-api-key": "not-a-real-token"},
        )
        assert r.status_code == 401


from toto_gateway import anthropic_surface as surf
from toto_gateway.schemas import ChatCompletionChunk, ChatCompletionResponse


def test_to_chat_request_translates_and_validates():
    req = surf.to_chat_request({
        "model": "echo-local", "max_tokens": 32,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert req.model == "echo-local"
    assert req.messages[-1].role == "user"
    assert req.stream is False


def test_to_chat_request_strips_output_config_format():
    # Claude Code 2.1.1x sends output_config.format; upstreams 400 on it.
    req = surf.to_chat_request({
        "model": "echo-local", "max_tokens": 32,
        "output_config": {"format": {"type": "json_schema"}},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert req.model == "echo-local"


def test_to_anthropic_response_shape():
    resp = ChatCompletionResponse.model_validate({
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "echo-local",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    })
    out = surf.to_anthropic_response(resp)
    assert out["type"] == "message"
    assert out["content"][0]["text"] == "hello"
    assert out["usage"]["output_tokens"] == 2


def test_anthropic_error_envelope():
    body = surf.anthropic_error("invalid_request_error", "no such model")
    assert body == {"type": "error",
                    "error": {"type": "invalid_request_error", "message": "no such model"}}


async def test_stream_events_translates_chunks_and_flushes_finish():
    async def chunks():
        for delta, finish in ((({"role": "assistant", "content": "he"}), None),
                              ({"content": "llo"}, None), ({}, "stop")):
            yield ChatCompletionChunk.model_validate({
                "id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 0,
                "model": "echo-local",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            })

    events = [e async for e in surf.stream_events(chunks(), model="echo-local")]
    types = [e["type"] for e in events]
    assert types[0] == "message_start"
    assert "content_block_delta" in types
    assert types[-1] == "message_stop"


ANTHROPIC_HEADERS = {"authorization": "", "x-api-key": OP_TOKEN,
                     "anthropic-version": "2023-06-01"}


async def test_messages_non_streaming_round_trip():
    async with in_process_app() as (client, _):
        r = await client.post("/v1/messages", headers=ANTHROPIC_HEADERS, json={
            "model": "echo-local", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hello gateway"}],
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["content"][0]["type"] == "text"
        assert body["usage"]["output_tokens"] >= 0
        # Provenance rides headers, never the Anthropic body.
        assert r.headers["x-toto-model"]
        assert r.headers["x-toto-request-id"]
        assert "x_toto" not in body


async def test_messages_unknown_model_is_anthropic_enveloped_404():
    async with in_process_app() as (client, _):
        r = await client.post("/v1/messages", headers=ANTHROPIC_HEADERS, json={
            "model": "no-such-model", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 404
        body = r.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "invalid_request_error"


import json


def _anthropic_sse_events(text: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in text.splitlines()
            if line.startswith("data: ")]


async def test_messages_streaming_emits_named_anthropic_events():
    async with in_process_app() as (client, _):
        r = await client.post("/v1/messages", headers=ANTHROPIC_HEADERS, json={
            "model": "echo-local", "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = _anthropic_sse_events(r.text)
        types = [e["type"] for e in events]
        assert types[0] == "message_start"
        assert "content_block_delta" in types
        assert types[-1] == "message_stop"
        # Named-event framing: every data line is preceded by its event: line.
        assert "event: message_start" in r.text


async def test_messages_streaming_policy_block_surfaces_error_event():
    """SSE cannot change status mid-stream; failures must be an error event, never a bare EOF."""
    async with in_process_app() as (client, _):
        r = await client.post("/v1/messages", headers=ANTHROPIC_HEADERS, json={
            "model": "no-such-model", "max_tokens": 64, "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })
        # Unknown model resolves BEFORE the stream opens -> clean Anthropic 404, not SSE.
        assert r.status_code == 404
        assert r.json()["type"] == "error"
