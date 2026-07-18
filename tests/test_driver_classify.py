"""Golden routing tests for the metadata classifier — deterministic, in-memory catalog."""

from __future__ import annotations

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry

from toto_gateway.driver.classify import classify


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(
        models=[
            CatalogEntry(id="or-qwen3-coder-flash", lane="economy", endpoint="openai",
                         residency_class="in_perimeter"),
            CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="openai",
                         residency_class="cloud"),
            CatalogEntry(id="fake-frontier", lane="frontier", endpoint="fake",
                         residency_class="cloud"),
        ]
    )


def test_research_intent_goes_frontier(catalog):
    d = classify({"intent": "research the market and synthesize a thesis"}, catalog)
    assert d.lane == "frontier"
    assert d.model_id == "or-sonnet-4.6"


def test_mechanical_intent_goes_local(catalog):
    for intent in ("redact PII from the log", "classify these tickets", "grep for errors"):
        d = classify({"intent": intent}, catalog)
        assert d.lane == "economy", intent
        assert d.model_id == "or-qwen3-coder-flash", intent


def test_privacy_beats_frontier_signal(catalog):
    d = classify(
        {"intent": "analyze and compare the valuation", "requires": {"data_policy": "local_only"}},
        catalog,
    )
    assert d.lane == "economy"
    assert d.model_id == "or-qwen3-coder-flash"
    assert "privacy" in d.reason


def test_web_search_tool_forces_frontier(catalog):
    d = classify({"intent": "look something up", "requires": {"tools": ["web_search"]}}, catalog)
    assert d.lane == "frontier"
    assert "web_search" in d.tools_required


def test_empty_metadata_defaults_local(catalog):
    assert classify({}, catalog).lane == "economy"


def test_bogus_tool_filtered_out(catalog):
    d = classify({"requires": {"tools": ["filesystem", "telepathy", "code_exec"]}}, catalog)
    assert d.tools_required == ["filesystem", "code_exec"]


def test_complexity_high_frontier_low_local(catalog):
    assert classify({"complexity": "high"}, catalog).lane == "frontier"
    assert classify({"complexity": "low"}, catalog).lane == "economy"


def test_fake_entry_never_chosen(catalog):
    # frontier route must skip the fake entry and pick the real one.
    d = classify({"complexity": "high"}, catalog)
    assert d.model_id == "or-sonnet-4.6"
    assert d.model_id != "fake-frontier"


def test_junk_requires_shape_tolerated(catalog):
    # LLM metadata can come back malformed — a non-dict `requires` must not crash routing.
    d = classify({"requires": ["web_search"], "complexity": "high"}, catalog)
    assert d.lane == "frontier"
    assert d.tools_required == []


# --- adversarial goldens (handoff §5: word-list gaps + precedence hazards) ----

def test_dcf_task_without_generic_frontier_word(catalog):
    # A genuinely hard finance task whose tokens miss the generic reasoning words.
    d = classify({"intent": "build a dcf with wacc assumptions",
                  "complexity": "medium", "keywords": ["dcf", "wacc"]}, catalog)
    assert d.lane == "frontier"


def test_investment_memo_goes_frontier(catalog):
    d = classify({"intent": "draft the investment memo with a buy recommendation",
                  "complexity": "medium", "keywords": ["memo", "recommendation"]}, catalog)
    assert d.lane == "frontier"


def test_frontier_noun_in_filename_stays_local_when_low(catalog):
    # 'market' leaks in from a filename; the task is explicitly low-complexity grep.
    d = classify({"intent": "grep market_report.csv for tickers",
                  "complexity": "low", "keywords": ["grep", "market"]}, catalog)
    assert d.lane == "economy"


def test_tool_need_beats_low_complexity(catalog):
    # Even a low-complexity task that needs live web data can't run on the economy lane.
    d = classify({"intent": "look up the closing price",
                  "complexity": "low", "requires": {"tools": ["web_search"]}}, catalog)
    assert d.lane == "frontier"


# --- tier / residency split (the axes are orthogonal) ------------------------

def _split_catalog() -> Catalog:
    """A cheap CLOUD economy model AND an in-perimeter box — so privacy routing has to choose
    by residency, not tier. The two economy entries differ only in where the data lands."""
    return Catalog(
        models=[
            CatalogEntry(id="fw-glm-5.2", lane="economy", endpoint="openai",
                         residency_class="cloud"),
            CatalogEntry(id="onprem-box", lane="economy", endpoint="http://127.0.0.1:8081/v1",
                         residency_class="in_perimeter"),
            CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="openai",
                         residency_class="cloud"),
        ]
    )


def test_cloud_economy_entry_is_honest():
    """A Fireworks-style entry reads economy TIER + cloud RESIDENCY — no contradiction."""
    e = _split_catalog().require("fw-glm-5.2")
    assert e.lane == "economy" and e.residency_class == "cloud"


def test_privacy_selects_in_perimeter_not_cheap_cloud():
    """Sensitive work must key off RESIDENCY: it lands on the in-perimeter box, never the
    cheaper CLOUD economy model that shares its tier."""
    d = classify({"intent": "redact the memo", "requires": {"data_policy": "local_only"}},
                 _split_catalog())
    assert d.model_id == "onprem-box"
    assert _split_catalog().require(d.model_id).residency_class == "in_perimeter"
    assert "privacy" in d.reason


def test_privacy_without_in_perimeter_fails_loudly():
    """No in-perimeter model exists → do not silently route secrets to cloud; the broken
    guarantee is surfaced loudly in the reason (the guard layer does the actual blocking)."""
    cloud_only = Catalog(models=[
        CatalogEntry(id="fw-glm-5.2", lane="economy", endpoint="openai", residency_class="cloud"),
        CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="openai", residency_class="cloud"),
    ])
    d = classify({"intent": "redact the memo", "requires": {"data_policy": "local_only"}}, cloud_only)
    assert "no in-perimeter model available" in d.reason
