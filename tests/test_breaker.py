"""Unit tests for the per-provider circuit breaker (toto_gateway/breaker.py).

Fake clock throughout — no sleeping. The wire-level behavior (fast-fail through the real Gateway)
is in test_resilience_wire.py.
"""

from __future__ import annotations

from toto_gateway.breaker import CircuitBreaker, CircuitOpen, provider_key


def _b(**kw) -> CircuitBreaker:
    kw.setdefault("fail_threshold", 3)
    kw.setdefault("reset_seconds", 10.0)
    return CircuitBreaker(**kw)


def test_provider_key_is_the_host():
    assert provider_key("https://openrouter.ai/api/v1") == "openrouter.ai"
    assert provider_key("https://api.openai.com/v1") == "api.openai.com"
    assert provider_key(None) == "openai-default"       # OpenAI default host
    assert provider_key("") == "openai-default"


def test_opens_after_threshold_consecutive_failures():
    now = [0.0]
    b = _b(clock=lambda: now[0])
    k = "p"
    assert b.allow(k)
    assert b.on_failure(k) is False   # 1
    assert b.on_failure(k) is False   # 2
    assert b.on_failure(k) is True    # 3 → OPEN (trip edge returns True exactly once)
    assert b.is_open(k) and not b.allow(k)


def test_success_resets_the_failure_run():
    b = _b()
    k = "p"
    b.on_failure(k)
    b.on_failure(k)
    b.on_success(k)                   # streak broken
    assert b.on_failure(k) is False   # count restarted from 0 → still 2 short of the threshold
    assert b.on_failure(k) is False
    assert b.on_failure(k) is True


def test_short_circuits_while_open_then_half_open_after_reset():
    now = [0.0]
    b = _b(reset_seconds=10.0, clock=lambda: now[0])
    k = "p"
    for _ in range(3):
        b.on_failure(k)
    assert not b.allow(k)             # OPEN — short-circuit
    now[0] = 9.9
    assert not b.allow(k)             # still inside the reset window
    now[0] = 10.0
    assert b.allow(k)                 # HALF_OPEN — one trial allowed
    assert not b.is_open(k)


def test_half_open_success_closes():
    now = [0.0]
    b = _b(reset_seconds=5.0, clock=lambda: now[0])
    k = "p"
    for _ in range(3):
        b.on_failure(k)
    now[0] = 5.0
    assert b.on_success(k) is True    # closing a previously-open breaker (→ circuit_close)
    assert b.allow(k) and not b.is_open(k)
    assert b.on_success(k) is False   # already closed


def test_half_open_failure_reopens_and_does_not_re_emit():
    now = [0.0]
    b = _b(reset_seconds=5.0, clock=lambda: now[0])
    k = "p"
    for _ in range(3):
        b.on_failure(k)
    now[0] = 5.0
    assert b.allow(k)                 # HALF_OPEN trial
    assert b.on_failure(k) is False   # trial failed → re-arm, but NOT a fresh trip edge
    assert not b.allow(k)             # OPEN again from now (5.0)
    now[0] = 10.0
    assert b.allow(k)                 # reset window elapsed again


def test_breakers_are_independent_per_provider():
    b = _b()
    for _ in range(3):
        b.on_failure("dead")
    assert b.is_open("dead")
    assert b.allow("healthy") and not b.is_open("healthy")


def test_circuit_open_carries_the_key():
    exc = CircuitOpen("openrouter.ai")
    assert exc.key == "openrouter.ai" and "openrouter.ai" in str(exc)
