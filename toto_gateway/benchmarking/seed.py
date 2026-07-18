"""Seed the BenchmarkStore from today's flat benchmarks.yaml.

The migration is deliberately narrow: only the three per-skill scores (code/reasoning/general)
become facts, carried under the `legacy_*` pseudo-benchmarks at source='seed'. Price and
context_window stay in the yaml/catalog for now — routing still reads them from there (benchmarks.py
is untouched). Aliases seed the join layer: catalog entry ids → their upstream model, plus an
identity mapping for each yaml key so a canonical name resolves to itself.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..catalog import Catalog
from .store import BenchmarkStore

_SKILL_BENCHMARK = {"code": "legacy_code", "reasoning": "legacy_reasoning",
                    "general": "legacy_general"}


def _asof_epoch(asof: str) -> float:
    """yaml `asof` ('2026-07-01') → epoch seconds; unparseable/empty → now (never fails the seed)."""
    if asof:
        try:
            return datetime.strptime(asof, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return time.time()


def seed_rows(benchmarks_yaml: dict, catalogs: list) -> tuple[list[dict], list[dict]]:
    """Pure: (score_rows, alias_rows) from the parsed benchmarks.yaml dict + catalog entries.

    `benchmarks_yaml` is the top-level dict ({asof, models}); `catalogs` is an iterable of catalog
    entries (objects with `.id` and `.effective_upstream_model`). Deterministic — same input, same
    output, no I/O."""
    retrieved_at = _asof_epoch(str(benchmarks_yaml.get("asof") or ""))
    models = benchmarks_yaml.get("models") or {}

    score_rows: list[dict] = []
    for upstream_id, row in models.items():
        for skill, benchmark_id in _SKILL_BENCHMARK.items():
            v = row.get(skill)
            if v is None:
                continue
            score_rows.append({
                "canonical_id": upstream_id, "benchmark_id": benchmark_id, "source": "seed",
                "value": float(v), "unit": "fraction", "redistributable": 1, "license": "",
                "retrieved_at": retrieved_at,
            })

    alias_rows: list[dict] = [
        {"source": "catalog", "source_model_id": e.id,
         "canonical_id": e.effective_upstream_model, "confidence": 1.0}
        for e in catalogs
    ]
    alias_rows += [
        {"source": "seed", "source_model_id": k, "canonical_id": k, "confidence": 1.0}
        for k in models
    ]
    return score_rows, alias_rows


async def seed(store: BenchmarkStore, settings) -> dict:
    """Load Settings' benchmarks yaml + composed catalog, upsert into `store`, return counts."""
    raw = yaml.safe_load(Path(settings.benchmarks).read_text()) or {}
    catalog = Catalog.load(settings.catalog)
    score_rows, alias_rows = seed_rows(raw, catalog.models)
    n_scores = await store.upsert_scores(score_rows)
    n_aliases = await store.upsert_aliases(alias_rows)
    return {"scores": n_scores, "aliases": n_aliases, "coverage": await store.coverage()}


async def _main() -> None:
    from ..config import get_settings

    s = get_settings()
    store = BenchmarkStore(s.db, s.database_url,
                           pool={"pool_min": s.pool_min, "pool_max": s.pool_max,
                                 "pool_timeout": s.pool_timeout})
    try:
        print(json.dumps(await seed(store, s)))
    finally:
        await store.close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
