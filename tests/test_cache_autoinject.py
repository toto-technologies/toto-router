"""Auto-inject of Anthropic top-level prompt caching for continuing conversations.

Asserts on the openai_to_anthropic output dict directly — pure/local, no network. The injection
condition: no client cache_control breakpoint anywhere AND (tools present OR message count >= the
configured minimum). Client breakpoints always win; a one-shot is left uncached.
"""

from __future__ import annotations

from toto_gateway.config import reset_settings_cache
from toto_gateway.runners.frontier import openai_to_anthropic
from toto_gateway.schemas import ChatCompletionRequest, Message

_MODEL = "claude-sonnet-4-5-20250101"
_CC = {"type": "ephemeral"}


def _req(messages: list[dict], **extra) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model="claude-sonnet-4-5", messages=[Message(**m) for m in messages], **extra
    )


def test_tools_present_injects_even_single_message():
    """A tool-bearing request is continuous by construction — inject regardless of message count."""
    req = _req(
        [{"role": "user", "content": "What's the weather in SF?"}],
        tools=[{"type": "function", "function": {"name": "get_weather",
                                                 "parameters": {"type": "object"}}}],
    )
    assert openai_to_anthropic(req, _MODEL).get("cache_control") == _CC


def test_multi_message_conversation_injects():
    req = _req([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "tell me more"},
    ])
    assert openai_to_anthropic(req, _MODEL).get("cache_control") == _CC


def test_single_message_oneshot_not_injected():
    req = _req([{"role": "user", "content": "one and done"}])
    assert "cache_control" not in openai_to_anthropic(req, _MODEL)


def test_client_breakpoint_wins_no_injection():
    """A client-sent breakpoint (here on a system part) means the client owns caching — never add
    the top-level field on top of it."""
    req = _req([
        {"role": "system", "content": [
            {"type": "text", "text": "big system prompt", "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
    ])
    assert "cache_control" not in openai_to_anthropic(req, _MODEL)


def test_setting_off_never_injects(monkeypatch):
    monkeypatch.setenv("TOTO_GW_ANTHROPIC_AUTO_CACHE", "false")
    reset_settings_cache()
    try:
        req = _req([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "more"},
        ])
        assert "cache_control" not in openai_to_anthropic(req, _MODEL)
    finally:
        reset_settings_cache()  # env unset by monkeypatch teardown; drop the cached read


def test_min_messages_threshold_env(monkeypatch):
    """Raising the threshold above the dialogue length suppresses injection."""
    monkeypatch.setenv("TOTO_GW_ANTHROPIC_AUTO_CACHE_MIN_MESSAGES", "5")
    reset_settings_cache()
    try:
        req = _req([
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "more"},
        ])
        assert "cache_control" not in openai_to_anthropic(req, _MODEL)
    finally:
        reset_settings_cache()
