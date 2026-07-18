"""conversation_key: the multi-turn grouping fingerprint on the trace + x_toto (trace-correlation).

A conversation's turns share a system prompt + opening user message, so the key is stable across
turns regardless of later messages; a different opening user message is a different conversation.
The route surfaces request_id + conversation_key on x_toto so a client can join a served response
to the gateway trace record (request_id == X-Request-ID) and group turns (conversation_key).
"""

from __future__ import annotations

import pytest

from harness.appharness import in_process_app
from toto_gateway.gateway import Gateway, _conversation_key
from toto_gateway.schemas import ChatCompletionRequest, Message


def _turn(*msgs: tuple[str, str], stream: bool = False) -> ChatCompletionRequest:
    req = ChatCompletionRequest(
        model="echo-local", messages=[Message(role=r, content=c) for r, c in msgs])
    req.stream = stream
    return req


# --- the pure fingerprint --------------------------------------------------------------

def test_same_system_and_first_user_same_key():
    """Two turns of one chat: same system + first user, different LAST message → same key."""
    turn1 = [Message(role="system", content="You are Toto."),
             Message(role="user", content="hello")]
    turn2 = turn1 + [Message(role="assistant", content="hi"),
                     Message(role="user", content="and now something totally different")]
    assert _conversation_key(turn1) == _conversation_key(turn2)
    assert len(_conversation_key(turn1)) == 16


def test_different_first_user_different_key():
    a = [Message(role="system", content="You are Toto."), Message(role="user", content="hello")]
    b = [Message(role="system", content="You are Toto."), Message(role="user", content="goodbye")]
    assert _conversation_key(a) != _conversation_key(b)


def test_system_participates_in_key():
    a = [Message(role="system", content="persona A"), Message(role="user", content="hello")]
    b = [Message(role="system", content="persona B"), Message(role="user", content="hello")]
    assert _conversation_key(a) != _conversation_key(b)


def test_no_user_message_is_none():
    assert _conversation_key([Message(role="system", content="just a system prompt")]) is None


# --- stamped on the trace (complete + stream) ------------------------------------------

@pytest.mark.asyncio
async def test_trace_carries_conversation_key_complete(gateway: Gateway):
    res = await gateway.complete(_turn(("system", "S"), ("user", "first"), ("user", "second")))
    assert res.trace.conversation_key == _conversation_key(
        [Message(role="system", content="S"), Message(role="user", content="first")])


@pytest.mark.asyncio
async def test_trace_carries_conversation_key_stream(gateway: Gateway):
    captured = {}
    async for _ in gateway.stream(_turn(("system", "S"), ("user", "first"), stream=True),
                                  on_trace=lambda t: captured.setdefault("trace", t)):
        pass
    assert captured["trace"].conversation_key == _conversation_key(
        [Message(role="system", content="S"), Message(role="user", content="first")])


# --- surfaced on x_toto, and request_id == X-Request-ID (the join) ----------------------

@pytest.mark.asyncio
async def test_x_toto_carries_request_id_and_conversation_key():
    async with in_process_app() as (client, _app):
        r = await client.post("/v1/chat/completions", json={
            "model": "echo-cloud",
            "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    x = r.json()["x_toto"]
    # the request_id on x_toto IS the X-Request-ID header — that's what makes the join work.
    assert x["request_id"] == r.headers["x-request-id"]
    assert x["conversation_key"] == _conversation_key(
        [Message(role="system", content="S"), Message(role="user", content="hi")])
