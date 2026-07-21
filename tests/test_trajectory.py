"""Trajectory scoring: normalized run-stage dimensions from an agentic conversation's
tool history, after NVIDIA NeMo Switchyard's stage-router design (Apache-2.0)."""
from toto_gateway.routing import trajectory
from toto_gateway.schemas import Message


def _turns(*payloads: tuple[str, str]) -> list[Message]:
    """(role, content) tuples -> Message list; tool results get a tool_call_id."""
    out = [Message(role="system", content="You are pi."),
           Message(role="user", content="fix the failing test")]
    for i, (role, content) in enumerate(payloads):
        if role == "tool":
            out.append(Message(role="tool", content=content, tool_call_id=f"call_{i}"))
        else:
            out.append(Message(role=role, content=content))
    return out


def test_no_tool_history_returns_none():
    messages = [Message(role="user", content="hello")]
    assert trajectory.extract(messages) is None


def test_error_streak_scores_capable():
    messages = _turns(*[("tool", "Traceback (most recent call last):\n  Error: boom")] * 4)
    sig = trajectory.extract(messages)
    assert sig is not None
    result = trajectory.score(sig)
    assert result.score > 0, "repeated tool errors must lean capable"
    assert result.confidence == abs(result.score)


def test_passing_tests_scores_efficient():
    messages = _turns(
        ("tool", "wrote file ok"),
        ("tool", "===== 12 passed in 0.41s ====="),
    )
    result = trajectory.score(trajectory.extract(messages))
    assert result.score < 0, "green tests + landing writes must lean efficient"


def test_confidence_is_clipped_to_unit_interval():
    messages = _turns(*[("tool", "Error: exploded")] * 50)
    result = trajectory.score(trajectory.extract(messages))
    assert -1.0 <= result.score <= 1.0
    assert 0.0 <= result.confidence <= 1.0


import pytest

from harness.appharness import in_process_app


@pytest.mark.asyncio
async def test_agentic_turn_stamps_trajectory_on_trace():
    async with in_process_app() as (client, app):
        r = await client.post("/v1/chat/completions", json={
            "model": "echo-local",
            "messages": [
                {"role": "system", "content": "You are pi."},
                {"role": "user", "content": "fix the failing test"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "bash", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1",
                 "content": "Error: 1 failed: test_x"},
            ],
        })
        assert r.status_code == 200
        x = r.json()["x_toto"]
        # Provenance block mirrors the trace; an agentic turn must carry a score.
        assert x["trajectory_score"] is not None
        assert 0.0 <= x["trajectory_confidence"] <= 1.0


@pytest.mark.asyncio
async def test_plain_chat_carries_null_trajectory():
    async with in_process_app() as (client, _):
        r = await client.post("/v1/chat/completions", json={
            "model": "echo-local",
            "messages": [{"role": "user", "content": "hello"}],
        })
        assert r.status_code == 200
        assert r.json()["x_toto"]["trajectory_score"] is None
