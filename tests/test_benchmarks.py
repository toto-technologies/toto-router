"""Benchmark-informed model selection — pure lookups, so every test is deterministic."""

from __future__ import annotations

from toto_gateway.benchmarks import Benchmarks
from toto_gateway.catalog import Catalog, CatalogEntry, Price
from toto_gateway.driver.classify import classify, infer_skill


def entry(id: str, lane: str = "economy", upstream: str | None = None,
          prompt: float = 0.001, completion: float = 0.001) -> CatalogEntry:
    return CatalogEntry(
        id=id, lane=lane, endpoint="openai", residency_class="cloud",
        upstream_model=upstream, price_usd_per_1k=Price(prompt=prompt, completion=completion),
    )


BENCH = Benchmarks(models={
    "coder-model": {"code": 0.85, "reasoning": 0.50, "general": 0.60},
    "general-model": {"code": 0.55, "reasoning": 0.65, "general": 0.75},
})


# --- Benchmarks.best ----------------------------------------------------------


def test_best_picks_highest_score_for_skill():
    coder, generalist = entry("a", upstream="coder-model"), entry("b", upstream="general-model")
    assert BENCH.best([coder, generalist], "code") is coder
    assert BENCH.best([coder, generalist], "general") is generalist
    assert BENCH.best([coder, generalist], "reasoning") is generalist


def test_tie_goes_to_cheaper_model():
    b = Benchmarks(models={"x": {"code": 0.80}, "y": {"code": 0.81}})  # same 0.05 bucket
    expensive = entry("exp", upstream="y", prompt=0.01, completion=0.01)
    cheap = entry("chp", upstream="x", prompt=0.0001, completion=0.0001)
    assert b.best([expensive, cheap], "code") is cheap


def test_unknown_model_scores_neutral_and_order_breaks_final_tie():
    b = Benchmarks()
    first, second = entry("first", prompt=0.001), entry("second", prompt=0.001)
    assert b.best([first, second], "code") is first  # all neutral, same price -> catalog order
    assert b.best([], "code") is None


def test_skill_falls_back_to_general_then_neutral():
    b = Benchmarks(models={"m": {"general": 0.9}})
    assert b.score("m", "code") == 0.9      # no code score -> general
    assert b.score("nope", "code") == 0.5   # unknown model -> neutral


def test_load_missing_file_is_empty(tmp_path):
    b = Benchmarks.load(tmp_path / "nope.yaml")
    assert b.models == {}
    assert b.score("anything", "code") == 0.5


def test_load_repo_seed_file():
    b = Benchmarks.load("benchmarks.yaml")
    assert b.models, "seed benchmarks.yaml should ship non-empty"
    assert 0.0 <= b.score("anthropic/claude-sonnet-4.6", "code") <= 1.0


# --- optimize knob --------------------------------------------------------------


def test_optimize_widens_or_narrows_the_tie_bucket():
    b = Benchmarks(models={"strong": {"code": 0.90}, "cheap": {"code": 0.80}})
    strong = entry("strong", upstream="strong", prompt=0.003, completion=0.015)
    cheap = entry("cheap", upstream="cheap", prompt=0.0001, completion=0.0003)
    # 0.10 apart: quality + balanced see a real gap; cost calls it good-enough -> cheapest.
    assert b.best([strong, cheap], "code", "quality") is strong
    assert b.best([strong, cheap], "code", "balanced") is strong
    assert b.best([strong, cheap], "code", "cost") is cheap


def test_classify_optimize_request_level_and_per_task_override():
    b = Benchmarks(models={"coder-model": {"code": 0.85}, "general-model": {"code": 0.75}})
    cat = Catalog(models=[
        entry("pricey-coder", upstream="coder-model", prompt=0.003, completion=0.015),
        entry("cheap-general", upstream="general-model", prompt=0.0001, completion=0.0003),
    ])
    md = {"intent": "refactor the sql query", "complexity": "low"}
    assert classify(md, cat, b, "quality").model_id == "pricey-coder"
    assert classify(md, cat, b, "cost").model_id == "cheap-general"
    # per-task metadata beats the request-level knob
    md_pinned = {**md, "requires": {"optimize": "quality"}}
    assert classify(md_pinned, cat, b, "cost").model_id == "pricey-coder"
    # junk value degrades to balanced instead of erroring
    assert classify(md, cat, b, "turbo").model_id == "pricey-coder"


def test_openrouter_price_in_benchmarks_beats_catalog_price():
    # Catalog says both are free; live OpenRouter price says "x" is expensive -> tie goes to "y".
    b = Benchmarks(models={
        "x": {"code": 0.80, "price_usd_per_1k": {"prompt": 0.003, "completion": 0.015}},
        "y": {"code": 0.80, "price_usd_per_1k": {"prompt": 0.0001, "completion": 0.0003}},
    })
    ex, ey = entry("x", upstream="x", prompt=0.0, completion=0.0), entry("y", upstream="y", prompt=0.0, completion=0.0)
    assert b.best([ex, ey], "code") is ey
    assert b.price(ex) == 0.018


# --- skill inference ----------------------------------------------------------


def test_infer_skill():
    assert infer_skill({"refactor", "the", "sql"}, []) == "code"
    assert infer_skill(set(), ["code_exec"]) == "code"
    assert infer_skill({"research", "market"}, []) == "reasoning"
    assert infer_skill({"summarize", "this"}, []) == "general"


# --- classify integration ------------------------------------------------------


def two_local_catalog() -> Catalog:
    return Catalog(models=[
        entry("or-qwen3-coder-flash", upstream="coder-model"),
        entry("or-llama-3.3-70b", upstream="general-model"),
        entry("or-sonnet-4.6", lane="frontier", upstream="big-model"),
    ])


def test_classify_routes_code_task_to_coder_within_lane():
    d = classify({"intent": "refactor the sql query", "complexity": "low"},
                 two_local_catalog(), BENCH)
    assert d.lane == "economy"
    assert d.model_id == "or-qwen3-coder-flash"
    assert "skill=code" in d.reason


def test_classify_routes_general_task_to_generalist_within_lane():
    d = classify({"intent": "summarize the notes", "complexity": "low"},
                 two_local_catalog(), BENCH)
    assert d.lane == "economy"
    assert d.model_id == "or-llama-3.3-70b"
    assert "skill=general" in d.reason


def test_classify_without_benchmarks_keeps_old_shape():
    d = classify({"intent": "summarize the notes", "complexity": "low"}, two_local_catalog())
    assert d.lane == "economy"
    assert d.model_id in ("or-qwen3-coder-flash", "or-llama-3.3-70b")
    assert "bench:" not in d.reason  # no scores loaded -> no bench noise in the trace


def test_classify_is_deterministic():
    md = {"intent": "refactor the sql query", "complexity": "low"}
    picks = {classify(md, two_local_catalog(), BENCH).model_id for _ in range(20)}
    assert picks == {"or-qwen3-coder-flash"}
