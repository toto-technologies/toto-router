"""Facts → per-(model, category) quality scores, and the overlay that feeds them to routing.

`aggregate` is PURE (no I/O) — it's the request-time-adjacent math. `overlay_benchmarks` is the
boot/refresh wiring that reads the store once and merges the result onto the yaml-loaded Benchmarks;
it never runs at request time, so routing stays deterministic.

Normalization to 0..1 (source-native units in, comparable scores out):
  fraction → as-is ; pct,index → /100 ; elo → PERCENTILE RANK within the models present for that
  (benchmark_id, source) — robust to Elo scale drift, no magic constants.
Operational units (tok_s, ms, usd_per_mtok) are EXCLUDED from quality categories: speed/cost aren't
quality, and price already lives in the optimize band.

Variant policy (benchmark_version): prefer the default '' run; if only tiered variants exist for a
(model, benchmark), take the MEDIAN across tiers — capability under unspecified effort, not the
cherry-picked ceiling.

Emits the legacy triple (code/reasoning/general) alongside categories so classify()/smart keep
working unchanged: code=coding, reasoning=reasoning, general=mean of the quality categories present.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean, median

_OPERATIONAL_UNITS = frozenset({"tok_s", "ms", "usd_per_mtok"})


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _percentile(value: float, population: list[float]) -> float:
    """Percentile rank of `value` in `population`: (below + 0.5*equal)/n. n=1 → 0.5 (no signal).
    Never exactly 0/1, tie-symmetric — the robust Elo→0..1 map."""
    n = len(population)
    if n <= 1:
        return 0.5
    below = sum(1 for p in population if p < value)
    equal = sum(1 for p in population if p == value)
    return (below + 0.5 * equal) / n


def _norm(fact: dict, elo_pop: dict) -> float | None:
    unit, v = fact["unit"], float(fact["value"])
    if unit == "fraction":
        return _clamp01(v)
    if unit in ("pct", "index"):
        return _clamp01(v / 100.0)
    if unit in ("elo", "arena"):  # both are cohort-relative → percentile-rank within the group
        return _percentile(v, elo_pop[(fact["benchmark_id"], fact["source"])])
    return None  # unknown unit → skip (never fabricate)


def aggregate(facts: list[dict], registry, *,
              include_non_redistributable: bool = True) -> dict[str, dict[str, dict]]:
    """dict[canonical_id][category] = {"score": 0..1, "n": benchmark_count}. Routing calls with
    the default (AA facts included — ratified internal use); DISPLAY (B4) passes False to exclude
    every non-redistributable fact so no AA-derived number reaches a customer surface."""
    facts = [f for f in facts if f["unit"] not in _OPERATIONAL_UNITS
             and (include_non_redistributable or f.get("redistributable", 1))]

    elo_pop: dict[tuple, list[float]] = defaultdict(list)
    for f in facts:
        if f["unit"] in ("elo", "arena"):
            elo_pop[(f["benchmark_id"], f["source"])].append(float(f["value"]))

    # (cid, benchmark_id, source) → {version: [normalized values]}
    groups: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for f in facts:
        n = _norm(f, elo_pop)
        if n is None:
            continue
        groups[(f["canonical_id"], f["benchmark_id"], f["source"])][f.get("benchmark_version", "")].append(n)

    # variant collapse → one value per (cid, benchmark, source), then mean across sources
    per_bench: dict[tuple, list[float]] = defaultdict(list)  # (cid, benchmark_id) → [source values]
    for (cid, bid, _source), by_ver in groups.items():
        if "" in by_ver:
            val = mean(by_ver[""])                         # prefer the default (unspecified-effort) run
        else:
            val = median(v for vs in by_ver.values() for v in vs)  # only tiers → median across them
        per_bench[(cid, bid)].append(val)

    # per category → mean of its benchmarks present; n = benchmark count
    cats: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (cid, bid), source_vals in per_bench.items():
        b = registry.get(bid)
        if b is None:            # uncatalogued benchmark → no category, skip (storable, not scored)
            continue
        cats[cid][b.category].append(mean(source_vals))

    out: dict[str, dict[str, dict]] = {}
    for cid, by_cat in cats.items():
        catmap = {cat: {"score": mean(vals), "n": len(vals)} for cat, vals in by_cat.items()}
        # legacy triple: code=coding, general=mean of the quality categories present (reasoning is
        # already a category key when present). n=0 never fabricated — absent categories stay absent.
        quality = [c["score"] for c in catmap.values()]  # real categories only (before legacy keys)
        if "coding" in catmap:
            catmap["code"] = dict(catmap["coding"])
        catmap["general"] = {"score": mean(quality), "n": len(quality)}
        out[cid] = catmap
    return out


async def overlay_benchmarks(benchmarks, settings, store, registry, catalog_upstreams=()) -> int:
    """Read store facts, aggregate (routing overlay → AA included), merge onto a FRESH yaml load,
    and atomically swap `benchmarks.models`. One DB read, no network. Empty store → yaml-only, so
    routing is byte-identical to before. Returns the number of models overlaid. Shared object →
    both the Gateway and the Driver see the swap.

    `catalog_upstreams` bridges legacy bare catalog names ('gpt-4o') onto their scored OpenRouter-
    slug canonical ('openai/gpt-4o') by norm, so routing scores catalog models from evidence even
    when catalog.yaml predates the slug convention."""
    from ..benchmarks import Benchmarks
    from .ingest import norm

    facts = await store.scores()
    agg = aggregate(facts, registry, include_non_redistributable=True)
    fresh = Benchmarks.load(settings.benchmarks)
    flat = {cid: {k: v["score"] for k, v in catmap.items()} for cid, catmap in agg.items()}
    for cid, scores in flat.items():
        fresh.models.setdefault(cid, {}).update(scores)
    by_norm = {}
    for cid in flat:
        by_norm.setdefault(norm(cid), cid)
    for up in catalog_upstreams:
        canon = by_norm.get(norm(up))
        if canon and canon != up:
            fresh.models.setdefault(up, {}).update(flat[canon])
    benchmarks.models = fresh.models   # atomic reference swap
    return len(agg)
