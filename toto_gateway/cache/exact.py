"""Exact-match response cache (Phase 1, context doc §8).

Key = sha256 of a normalized request tuple (tenant, model, messages, temperature,
max_tokens, tools). Tenants are namespaced via a key prefix so they never share hits.

Backed by an in-memory dict (bounded FIFO) by default; pass a sqlite_path for
persistence across process restarts (stdlib sqlite3 only — no external deps).

Ponytail: stdlib hashlib + json + collections.OrderedDict. No Redis, no cache lib.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import OrderedDict
from typing import Any

from ..schemas import ChatCompletionRequest, ChatCompletionResponse

_MAX_ENTRIES = 1000


def _normalize(req: ChatCompletionRequest, tenant: str) -> bytes:
    """Produce a stable bytes repr of the cache-relevant fields."""
    # Extract only what affects the completion outcome — skip stream/stream_options/user.
    data: dict[str, Any] = {
        "tenant": tenant,
        "model": req.model,
        "messages": [
            {"role": m.role, "content": m.text()}
            for m in req.messages
        ],
        "temperature": req.temperature,
        "max_tokens": req.max_tokens,
    }
    # Include tools if present in the extra fields (we don't model tools explicitly).
    extra = req.model_dump(exclude_none=True)
    if "tools" in extra:
        data["tools"] = extra["tools"]

    # sort_keys=True ensures key-ordering is deterministic across Python dicts.
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _tenant_of(req: ChatCompletionRequest) -> str:
    """Pull tenant from the request's extra fields, defaulting to 'default'."""
    extra = req.model_extra or {}
    return str(extra.get("tenant", "default"))


class ExactCache:
    """Exact-match response cache implementing the ResponseCache Protocol.

    - Per-tenant isolation: each tenant gets its own key prefix so caches never
      bleed across tenants even when the prompt is identical.
    - In-memory FIFO cap: bounded at _MAX_ENTRIES to prevent unbounded growth.
    - Optional SQLite persistence: pass sqlite_path to survive process restarts.
      The in-memory dict is the primary L1; SQLite is L2 (write-through on put,
      read-through on miss).
    - get() returns a deep copy so callers cannot mutate the cached object.
    """

    def __init__(self, sqlite_path: str | None = None, max_entries: int = _MAX_ENTRIES) -> None:
        self._max = max_entries
        # OrderedDict gives us O(1) FIFO eviction (move_to_end + popitem(last=False)).
        self._mem: OrderedDict[str, str] = OrderedDict()  # key -> response_json
        self._sqlite_path = sqlite_path
        self._db: sqlite3.Connection | None = None
        if sqlite_path is not None:
            self._db = sqlite3.connect(sqlite_path, check_same_thread=False)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS exact_cache"
                " (cache_key TEXT PRIMARY KEY, response_json TEXT)"
            )
            self._db.commit()

    def _key(self, req: ChatCompletionRequest) -> str:
        tenant = _tenant_of(req)
        # Prefix with tenant so different tenants never collide.
        raw = f"{tenant}:".encode() + _normalize(req, tenant)
        return _sha256(raw)

    # --- ResponseCache protocol -----------------------------------------------

    def get(self, req: ChatCompletionRequest) -> ChatCompletionResponse | None:
        key = self._key(req)

        # L1: in-memory
        raw = self._mem.get(key)
        if raw is not None:
            # Promote to most-recent so eviction doesn't expire hot entries.
            self._mem.move_to_end(key)
            # Fresh object every call — the store holds a JSON string, so no copy needed.
            return ChatCompletionResponse.model_validate_json(raw)

        # L2: SQLite
        if self._db is not None:
            row = self._db.execute(
                "SELECT response_json FROM exact_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is not None:
                raw = row[0]
                self._mem_set(key, raw)  # warm L1
                return ChatCompletionResponse.model_validate_json(raw)

        return None

    def put(self, req: ChatCompletionRequest, resp: ChatCompletionResponse) -> None:
        key = self._key(req)
        raw = resp.model_dump_json()
        self._mem_set(key, raw)
        if self._db is not None:
            self._db.execute(
                "INSERT OR REPLACE INTO exact_cache (cache_key, response_json) VALUES (?, ?)",
                (key, raw),
            )
            self._db.commit()

    # --- internals -----------------------------------------------------------

    def _mem_set(self, key: str, raw: str) -> None:
        if key in self._mem:
            self._mem.move_to_end(key)
        else:
            if len(self._mem) >= self._max:
                self._mem.popitem(last=False)  # evict oldest
            self._mem[key] = raw
