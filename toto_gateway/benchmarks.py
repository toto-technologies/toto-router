"""Benchmark-informed model selection — offline scores, runtime dict lookup.

External leaderboard scores (Artificial Analysis, HuggingFace Open LLM Leaderboard) AND live
OpenRouter pricing are fetched OFFLINE by scripts/fetch_benchmarks.py into benchmarks.yaml,
keyed by upstream model id. At routing time this module is a pure lookup + argmax over
catalog entries: no network, no models, no I/O after load — same input -> same pick.

The user-facing knob is `optimize`: quality | balanced | cost. It is implemented as the
width of the score bucket that counts as a tie — inside a bucket the cheaper model wins.
quality  -> 0.01 bucket: best score wins, price only breaks near-exact ties
balanced -> 0.05 bucket: the default trade-off
cost     -> 0.15 bucket: anything within 0.15 of the best score is "good enough", cheapest wins
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .catalog import CatalogEntry

SKILLS = ("code", "reasoning", "general")
OPTIMIZE = ("quality", "balanced", "cost")
NEUTRAL = 0.5  # unbenchmarked models compete at par instead of winning or vanishing
_BUCKETS = {"quality": 0.01, "balanced": 0.05, "cost": 0.15}


class Benchmarks:
    """{upstream_model -> {skill scores + live openrouter price}} with a deterministic best-pick."""

    def __init__(self, models: dict[str, dict] | None = None, asof: str = "") -> None:
        self.models = models or {}
        self.asof = asof

    @classmethod
    def load(cls, path: str | Path) -> "Benchmarks":
        """Missing file -> empty store (routing degrades to cheapest-in-lane, never errors)."""
        p = Path(path)
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text()) or {}
        return cls(models=data.get("models") or {}, asof=str(data.get("asof") or ""))

    def score(self, upstream_model: str, skill: str) -> float:
        row = self.models.get(upstream_model) or {}
        v = row.get(skill)
        if v is None:
            v = row.get("general")
        return float(v) if v is not None else NEUTRAL

    def price(self, entry: CatalogEntry) -> float:
        """Blended $/1k for ranking: live OpenRouter price when the fetch script captured one,
        else the hand-entered catalog price. (Billing/traces still use the catalog price.)"""
        row = self.models.get(entry.effective_upstream_model) or {}
        p = row.get("price_usd_per_1k")
        if isinstance(p, dict):
            return float(p.get("prompt", 0.0)) + float(p.get("completion", 0.0))
        return entry.price_usd_per_1k.prompt + entry.price_usd_per_1k.completion

    def best(self, entries: list[CatalogEntry], skill: str,
             optimize: str = "balanced") -> CatalogEntry | None:
        """Cheapest entry whose score is within the optimize bucket of the best score for
        the skill; final tie broken by catalog order. Pure comparisons, fully deterministic."""
        if not entries:
            return None
        bucket = _BUCKETS.get(optimize, _BUCKETS["balanced"])
        scored = [
            (self.score(e.effective_upstream_model, skill), self.price(e), i, e)
            for i, e in enumerate(entries)
        ]
        top = max(s for s, _, _, _ in scored)
        good = [t for t in scored if t[0] >= top - bucket]
        return min(good, key=lambda t: (t[1], t[2]))[3]
