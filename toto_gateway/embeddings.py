"""Embedding layer for routing — task text → skill via nearest exemplar centroid.

OpenRouter's embeddings endpoint (existing OPENROUTER_API_KEY) via the AsyncOpenAI client, same
plumbing as the OpenAI runner. Everything degrades to None so the caller falls back to the
keyword classifier: no key, provider down, or a hard timeout all return None, never raise.
Pure-Python cosine — no numpy dep at our
scale (three centroids, dozens of tasks). Answers are never embedded; only task text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path

_EXEMPLARS_PATH = Path(__file__).parent / "eval" / "skill_exemplars.json"


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb + 1e-9)


def load_exemplars(path: Path = _EXEMPLARS_PATH) -> dict[str, list[str]]:
    return json.loads(path.read_text())


class Embedder:
    """Skill inference by embedding similarity, with an in-memory + optional DB cache and a hard
    per-call timeout. `infer_skill` returns a skill string or None (caller falls back)."""

    def __init__(self, *, api_key: str, model: str, base_url: str = "https://openrouter.ai/api/v1",
                 timeout_ms: int = 500, exemplars: dict[str, list[str]] | None = None,
                 store=None) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._timeout = timeout_ms / 1000.0
        self._exemplars = exemplars if exemplars is not None else load_exemplars()
        self._store = store            # optional RunStore for the durable embedding_cache
        self._client = None            # lazy AsyncOpenAI
        self._mem: dict[str, list[float]] = {}   # text -> vector (hot path)
        self._centroids: dict[str, list[float]] | None = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(base_url=self._base_url, api_key=self._api_key,
                                       default_headers={"X-Title": "toto-gateway"})
        return self._client

    def _hash(self, text: str) -> str:
        return hashlib.sha256(f"{self._model}\x00{text}".encode()).hexdigest()

    async def _embed_raw(self, texts: list[str], timeout: float | None = None) -> list[list[float]] | None:
        """One API call, hard-timed. None on any failure — never raises."""
        try:
            resp = await asyncio.wait_for(
                self._get_client().embeddings.create(model=self._model, input=texts),
                timeout if timeout is not None else self._timeout,
            )
            return [d.embedding for d in sorted(resp.data, key=lambda d: d.index)]
        except Exception:
            return None

    async def embed_one(self, text: str) -> list[float] | None:
        if text in self._mem:
            return self._mem[text]
        if self._store is not None:
            hit = await self._store.get_cached_embedding(self._hash(text))
            if hit is not None:
                self._mem[text] = hit
                return hit
        out = await self._embed_raw([text])
        if out is None:
            return None
        vec = out[0]
        self._mem[text] = vec
        if self._store is not None:
            try:
                await self._store.put_cached_embedding(self._hash(text), vec)
            except Exception:
                pass  # cache write is best-effort
        return vec

    async def embed_texts(self, texts: list[str]) -> list[list[float]] | None:
        """Batch-embed many chunks for one document (the memory index write path). Cache-aware:
        already-known chunks skip the wire, only the misses go up in ONE call. None on any
        failure (→ the caller stores rows with no vector: keyword-only, still recallable)."""
        if not texts:
            return []
        misses = [t for t in texts if t not in self._mem]
        if misses:
            got = await self._embed_raw(misses, timeout=max(self._timeout, 10.0))
            if got is None:
                return None
            for t, v in zip(misses, got):
                self._mem[t] = v
                if self._store is not None:
                    try:
                        await self._store.put_cached_embedding(self._hash(t), v)
                    except Exception:
                        pass
        return [self._mem[t] for t in texts]

    async def _ensure_centroids(self) -> dict[str, list[float]] | None:
        if self._centroids is not None:
            return self._centroids
        texts, order = [], []
        for skill, xs in self._exemplars.items():
            for x in xs:
                texts.append(x)
                order.append(skill)
        # One-time startup batch (not the hot path) — a generous timeout, not the per-call 500ms.
        vecs = await self._embed_raw(texts, timeout=max(self._timeout, 10.0))
        if vecs is None:
            return None  # retry next call; caller falls back meanwhile
        cents = {}
        for skill in self._exemplars:
            sv = [vecs[i] for i, s in enumerate(order) if s == skill]
            cents[skill] = [sum(col) / len(col) for col in zip(*sv)]
        self._centroids = cents
        return cents

    async def infer_skill(self, text: str) -> str | None:
        """Nearest-centroid skill, or None on any failure (→ keyword fallback)."""
        cents = await self._ensure_centroids()
        if cents is None:
            return None
        vec = await self.embed_one(text)
        if vec is None:
            return None
        return max(cents, key=lambda s: _cos(vec, cents[s]))


def build_embedder(settings, store=None) -> Embedder | None:
    """An Embedder when a key is configured, else None (routing/corpus silently degrade).
    Reads the OpenRouter key from the environment, same as the OpenAI runner."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    return Embedder(api_key=api_key, model=settings.embed_model,
                    timeout_ms=settings.embed_timeout_ms, store=store)
