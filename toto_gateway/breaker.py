"""Per-provider circuit breaker (Wave 1).

A tiny 3-state breaker keyed by PROVIDER (the base_url host) so ALL models on a dead provider
break together — not per-model. CLOSED normally; after N consecutive transient failures it trips
OPEN and short-circuits calls for reset_seconds; then it goes HALF_OPEN and allows one trial that
either closes it (success) or re-arms the open window (failure).

In-process, per-replica by default. Wave 2 R1 adds an OPTIONAL Redis coordination tier: pass a
`redis.asyncio` client and a trip on one replica is recorded in Redis (key per provider, TTL =
reset_seconds) so peer replicas fast-fail too via peer_open(). The sync allow()/is_open()/
on_failure()/on_success() state machine is UNCHANGED and remains the source of truth for the local
path — Redis is a purely additive shared-fast-fail signal.

ponytail: the cross-replica view is EVENTUALLY CONSISTENT — a peer's OPEN is visible only after its
record_open() SET lands, and clears when the reset_seconds TTL expires (self-healing if that replica
dies). Replicas converge within reset_seconds. A Redis outage fails OPEN (degrades to per-replica),
never blocks the request path. Upgrade path if tighter consistency is ever needed: a pub/sub
invalidation channel instead of TTL polling.

The breaker itself does NOT classify errors — the caller records on_failure() only for transient
failures (resilience.is_retryable: 429/5xx/timeout/connection), never a 4xx. So a stream of client
errors can never trip it.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse


def provider_key(base_url: str | None) -> str:
    """Group models by provider = the base_url host (openrouter.ai, api.openai.com, ...). None
    base_url = the OpenAI default host. Unparseable → the raw string, so the key is still stable."""
    if not base_url:
        return "openai-default"
    return (urlparse(base_url).netloc or base_url).lower()


class CircuitOpen(Exception):
    """A call was short-circuited because the provider's breaker is OPEN (fast-fail, no wire)."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"circuit open for provider '{key}'")


class _State:
    __slots__ = ("fails", "opened_at")

    def __init__(self) -> None:
        self.fails = 0
        self.opened_at: float | None = None  # None == CLOSED


_OPEN_KEY = "breaker:open:"  # Redis key prefix for a shared-OPEN provider


class CircuitBreaker:
    def __init__(self, *, fail_threshold: int = 5, reset_seconds: float = 30.0,
                 clock=time.monotonic, redis=None) -> None:
        self._threshold = fail_threshold
        self._reset = reset_seconds
        self._clock = clock
        self._states: dict[str, _State] = {}
        self._redis = redis  # optional redis.asyncio client — cross-replica OPEN coordination

    def _st(self, key: str) -> _State:
        st = self._states.get(key)
        if st is None:
            st = self._states[key] = _State()
        return st

    def allow(self, key: str) -> bool:
        """May a call to this provider proceed? True when CLOSED or once the reset window has
        elapsed (HALF_OPEN trial); False while OPEN and still inside reset_seconds."""
        st = self._st(key)
        if st.opened_at is None:
            return True
        return (self._clock() - st.opened_at) >= self._reset  # HALF_OPEN trial vs OPEN

    def is_open(self, key: str) -> bool:
        st = self._states.get(key)
        return bool(st and st.opened_at is not None and (self._clock() - st.opened_at) < self._reset)

    def on_success(self, key: str) -> bool:
        """Record a success. Returns True iff this CLOSED a previously-open breaker (→ emit
        circuit_close)."""
        st = self._st(key)
        was_open = st.opened_at is not None
        st.fails = 0
        st.opened_at = None
        return was_open

    def on_failure(self, key: str) -> bool:
        """Record a transient failure. Returns True iff this transition just OPENED the breaker
        (→ emit circuit_open). A failed HALF_OPEN trial re-arms the open window instead."""
        st = self._st(key)
        if st.opened_at is not None:
            st.opened_at = self._clock()  # trial failed — re-open from now
            return False
        st.fails += 1
        if st.fails >= self._threshold:
            st.opened_at = self._clock()
            return True
        return False

    def snapshot(self) -> dict[str, dict]:
        """Read-only view of every provider the breaker has TOUCHED (admin health route). No state
        machine side effects. Per key: state (closed|open|half-open), retry_in (seconds left in the
        OPEN window, 0 once the HALF_OPEN trial is due, None when closed), and consecutive_failures.
        A provider with no recorded failure yet is simply absent — the caller defaults it to closed."""
        now = self._clock()
        out: dict[str, dict] = {}
        for key, st in self._states.items():
            if st.opened_at is None:
                out[key] = {"state": "closed", "retry_in": None, "consecutive_failures": st.fails}
                continue
            left = self._reset - (now - st.opened_at)
            if left > 0:
                out[key] = {"state": "open", "retry_in": round(left, 2),
                            "consecutive_failures": st.fails}
            else:
                out[key] = {"state": "half-open", "retry_in": 0.0,
                            "consecutive_failures": st.fails}
        return out

    # --- cross-replica coordination (Wave 2 R1) --------------------------------------
    # All fail-open: no client, or ANY Redis error, degrades to the in-process state above.

    async def peer_open(self, key: str) -> bool:
        """True iff a PEER replica has this provider recorded OPEN in Redis (shared fast-fail).
        No client / Redis error → False, so the caller falls back to the local allow()/is_open()."""
        if self._redis is None:
            return False
        try:
            return (await self._redis.get(_OPEN_KEY + key)) is not None
        except Exception:
            return False  # ponytail: Redis outage → per-replica behaviour, never blocks the request

    async def record_open(self, key: str) -> None:
        """Publish a trip to peers: SET the provider key with TTL=reset_seconds, so peers fast-fail
        and the signal self-expires (no stuck-open if this replica dies mid-window). Best-effort."""
        if self._redis is None:
            return
        try:
            await self._redis.set(_OPEN_KEY + key, "1", ex=max(1, int(self._reset)))
        except Exception:
            pass

    async def clear_open(self, key: str) -> None:
        """Clear the shared OPEN on a close (breaker recovered). Best-effort; the TTL clears it anyway."""
        if self._redis is None:
            return
        try:
            await self._redis.delete(_OPEN_KEY + key)
        except Exception:
            pass


def _demo() -> None:
    """Self-check with a fake clock: open after N, short-circuit, half-open trial, close, recover."""
    now = [0.0]
    b = CircuitBreaker(fail_threshold=3, reset_seconds=10.0, clock=lambda: now[0])
    k = provider_key("https://openrouter.ai/api/v1")
    assert k == "openrouter.ai"
    assert b.allow(k)                          # CLOSED
    assert not b.on_failure(k) and not b.on_failure(k)  # 2 fails, still closed
    assert b.on_failure(k)                     # 3rd fail OPENS (returns True once)
    assert b.is_open(k) and not b.allow(k)     # short-circuit while OPEN
    assert b.snapshot()[k] == {"state": "open", "retry_in": 10.0, "consecutive_failures": 3}
    now[0] = 10.0                              # reset elapsed
    assert b.allow(k) and not b.is_open(k)     # HALF_OPEN: one trial allowed
    assert b.snapshot()[k]["state"] == "half-open"
    assert b.on_success(k)                     # trial succeeds → CLOSED (returns True once)
    assert b.allow(k) and b.on_success(k) is False
    # 4xx-style errors never reach on_failure (caller's job) → breaker stays closed by construction.
    print("breaker self-check OK")


if __name__ == "__main__":
    _demo()
