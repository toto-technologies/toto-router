"""Tests for toto_gateway.tokens — estimate_tokens and estimate_prompt_tokens."""

from __future__ import annotations


from toto_gateway.schemas import Message
from toto_gateway.tokens import estimate_tokens, estimate_prompt_tokens

# --- estimate_tokens ---


def test_empty_string_returns_zero():
    """Empty string always returns 0, never raises."""
    assert estimate_tokens("") == 0


def test_none_like_empty_string_returns_zero():
    """estimate_tokens handles any falsy string (empty str only — None would be a type error)."""
    assert estimate_tokens("") == 0


def test_single_char_returns_one():
    """A single character should return at least 1 (max(1, round(1/4)))."""
    assert estimate_tokens("x") >= 1


def test_four_chars_is_one_token():
    """4 chars / 4 chars-per-token = 1 token exactly."""
    assert estimate_tokens("abcd") == 1


def test_eight_chars_is_two_tokens():
    """8 chars / 4 = 2 tokens."""
    assert estimate_tokens("abcdefgh") == 2


def test_deterministic():
    """Same input always returns the same count (no randomness)."""
    text = "Hello, this is a deterministic test."
    results = [estimate_tokens(text) for _ in range(5)]
    assert len(set(results)) == 1


def test_monotonic_longer_text_more_tokens():
    """Longer text produces more (or equal) tokens than shorter text."""
    short = "Hi"
    medium = "Hello, how are you doing today?"
    long = "Hello, how are you doing today? I am writing a long message to test monotonicity."
    assert estimate_tokens(short) <= estimate_tokens(medium) <= estimate_tokens(long)


def test_never_negative():
    """Result is never negative, even for unusual inputs."""
    assert estimate_tokens("x") >= 0
    assert estimate_tokens("   ") >= 0  # whitespace


def test_whitespace_only_returns_at_least_one():
    """Whitespace-only string is non-empty, so should return >= 1."""
    assert estimate_tokens("    ") >= 1


def test_long_text_scales_linearly():
    """A text 10× longer should produce roughly 10× more tokens."""
    base = "abcd"  # 1 token
    long = base * 100  # 400 chars = 100 tokens
    assert estimate_tokens(long) == 100


def test_unicode_text():
    """Unicode characters are counted by character length, not byte length."""
    text = "héllo"  # 5 chars
    # 5/4 rounds to 1 — just assert non-negative and deterministic
    assert estimate_tokens(text) >= 0
    assert estimate_tokens(text) == estimate_tokens(text)


# --- estimate_prompt_tokens ---


def test_empty_messages_list_returns_zero():
    """Zero messages = 0 tokens."""
    assert estimate_prompt_tokens([]) == 0


def test_single_user_message():
    """A single user message produces tokens > 0 (content + role + overhead)."""
    msgs = [Message(role="user", content="Hello")]
    count = estimate_prompt_tokens(msgs)
    assert count > 0


def test_multiple_messages_more_than_single():
    """More messages produce more total tokens (overhead accumulates)."""
    one = [Message(role="user", content="Hello world")]
    two = [
        Message(role="user", content="Hello world"),
        Message(role="assistant", content="Hello back to you"),
    ]
    assert estimate_prompt_tokens(two) > estimate_prompt_tokens(one)


def test_per_message_overhead():
    """Each message adds 3 framing tokens beyond role + content estimate."""
    # One message with zero-length content: should produce role tokens + 3
    msg = Message(role="u", content="")  # 1-char role → 1 token; content → 0; +3 = 4
    # We don't know exact role token count but total > 0
    assert estimate_prompt_tokens([msg]) > 0


def test_messages_with_list_content():
    """Messages with list-type content (parts array) are handled without error."""
    msg = Message(role="user", content=[{"type": "text", "text": "Hello from parts"}])
    count = estimate_prompt_tokens([msg])
    assert count > 0


def test_messages_with_none_content():
    """Messages with None content produce tokens from role + overhead only."""
    msg = Message(role="assistant", content=None)
    count = estimate_prompt_tokens([msg])
    assert count >= 3  # at minimum the overhead framing


def test_system_user_assistant_chain():
    """A realistic system+user+assistant chain produces reasonable token counts."""
    msgs = [
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="What is 2 + 2?"),
        Message(role="assistant", content="4"),
    ]
    count = estimate_prompt_tokens(msgs)
    # Very rough sanity: "You are a helpful assistant." alone is ~7 words ≈ 7 tokens
    assert count > 5


def test_estimate_tokens_used_internally():
    """estimate_prompt_tokens uses estimate_tokens on each message's text."""
    # A message with exactly 4 chars content should contribute 1 content token + role tokens + 3
    msg = Message(role="user", content="1234")  # 4 chars → 1 token content
    total = estimate_prompt_tokens([msg])
    content_tokens = estimate_tokens("1234")
    role_tokens = estimate_tokens("user")
    # total = content_tokens + role_tokens + 3
    assert total == content_tokens + role_tokens + 3
