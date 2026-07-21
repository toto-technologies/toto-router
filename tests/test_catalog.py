"""Tests for toto_gateway.catalog — load, get, require, frontier_reference, edge cases."""

from __future__ import annotations

import textwrap

import pytest

from toto_gateway.catalog import Catalog, UnknownModelError


# --- load ---


def test_load_real_catalog():
    """catalog.yaml loads without error and has at least one model."""
    cat = Catalog.load("catalog.yaml")
    assert len(cat.models) >= 1


def test_load_missing_models_key(tmp_path):
    """A YAML file without 'models' key raises ValueError."""
    f = tmp_path / "bad.yaml"
    f.write_text("foo: bar\n")
    with pytest.raises(ValueError, match="models"):
        Catalog.load(str(f))


def test_load_empty_yaml(tmp_path):
    """An empty YAML file raises ValueError."""
    f = tmp_path / "empty.yaml"
    f.write_text("\n")
    with pytest.raises(ValueError, match="models"):
        Catalog.load(str(f))


def test_load_minimal_catalog(tmp_path):
    """A minimal valid YAML catalog with one fake model loads correctly."""
    yaml_text = textwrap.dedent("""
        models:
          - id: test-model
            lane: fake
            endpoint: fake
            residency_class: in_perimeter
    """)
    f = tmp_path / "min.yaml"
    f.write_text(yaml_text)
    cat = Catalog.load(str(f))
    assert len(cat.models) == 1
    assert cat.models[0].id == "test-model"


# --- get ---


def test_get_existing_model(catalog: Catalog):
    """get() returns the entry for a known model id."""
    entry = catalog.get("echo-local")
    assert entry is not None
    assert entry.id == "echo-local"


def test_get_unknown_model_returns_none(catalog: Catalog):
    """get() returns None for an unknown model id (no exception)."""
    assert catalog.get("no-such-model-xyz") is None


def test_get_returns_correct_lane(catalog: Catalog):
    """echo-local is fake lane, echo-cloud is fake lane (demo models)."""
    assert catalog.get("echo-local").lane == "fake"
    assert catalog.get("echo-cloud").lane == "fake"


# --- require ---


def test_require_known_model(catalog: Catalog):
    """require() returns entry for a known model id."""
    entry = catalog.require("echo-cloud")
    assert entry.id == "echo-cloud"


def test_require_unknown_raises_unknown_model_error(catalog: Catalog):
    """require() raises UnknownModelError for an unknown model id."""
    with pytest.raises(UnknownModelError) as exc_info:
        catalog.require("nonexistent-model-abc")
    err = exc_info.value
    assert err.model_id == "nonexistent-model-abc"
    assert "nonexistent-model-abc" in str(err)
    # known models should be listed in the error message
    assert "echo-local" in err.known


def test_require_error_contains_known_models(catalog: Catalog):
    """UnknownModelError.known lists all catalog model ids."""
    with pytest.raises(UnknownModelError) as exc_info:
        catalog.require("ghost")
    known_ids = set(exc_info.value.known.split(", "))
    catalog_ids = {e.id for e in catalog.models}
    assert catalog_ids.issubset(known_ids)


# --- frontier_reference ---


def test_frontier_reference_returns_entry(catalog: Catalog):
    """frontier_reference() returns a non-None entry (catalog has frontier-residency models)."""
    ref = catalog.frontier_reference()
    assert ref is not None


def test_frontier_reference_is_frontier_residency(catalog: Catalog):
    """The frontier reference has residency_class == 'frontier'."""
    ref = catalog.frontier_reference()
    assert ref.residency_class == "cloud"


def test_frontier_reference_prefers_frontier_lane(tmp_path):
    """When a real frontier-lane entry exists, it takes priority over residency-only."""
    yaml_text = textwrap.dedent("""
        models:
          - id: fake-frontier-priced
            lane: fake
            endpoint: fake
            residency_class: cloud
            price_usd_per_1k: { prompt: 1.0, completion: 5.0 }

          - id: real-frontier
            lane: frontier
            endpoint: anthropic
            residency_class: cloud
            price_usd_per_1k: { prompt: 3.0, completion: 15.0 }
    """)
    f = tmp_path / "cat.yaml"
    f.write_text(yaml_text)
    cat = Catalog.load(str(f))
    ref = cat.frontier_reference()
    assert ref.id == "real-frontier"
    assert ref.lane == "frontier"


def test_frontier_reference_falls_back_to_residency(tmp_path):
    """When no real frontier-lane entry, falls back to first frontier-residency."""
    yaml_text = textwrap.dedent("""
        models:
          - id: local-model
            lane: economy
            endpoint: http://localhost:8081/v1
            residency_class: in_perimeter

          - id: priced-fake-frontier
            lane: fake
            endpoint: fake
            residency_class: cloud
            price_usd_per_1k: { prompt: 3.0, completion: 15.0 }
    """)
    f = tmp_path / "cat.yaml"
    f.write_text(yaml_text)
    cat = Catalog.load(str(f))
    ref = cat.frontier_reference()
    assert ref.id == "priced-fake-frontier"


def test_frontier_reference_none_when_no_frontier(tmp_path):
    """Returns None when catalog has no frontier-lane or frontier-residency entries."""
    yaml_text = textwrap.dedent("""
        models:
          - id: only-local
            lane: economy
            endpoint: http://localhost:8081/v1
            residency_class: in_perimeter
    """)
    f = tmp_path / "cat.yaml"
    f.write_text(yaml_text)
    cat = Catalog.load(str(f))
    assert cat.frontier_reference() is None


# --- effective_upstream_model ---


def test_effective_upstream_model_with_upstream_model(tmp_path):
    """effective_upstream_model returns upstream_model when set."""
    yaml_text = textwrap.dedent("""
        models:
          - id: my-alias
            lane: economy
            endpoint: http://localhost:8081/v1
            residency_class: in_perimeter
            upstream_model: qwen2.5-coder-32b-instruct
    """)
    f = tmp_path / "cat.yaml"
    f.write_text(yaml_text)
    cat = Catalog.load(str(f))
    entry = cat.require("my-alias")
    assert entry.effective_upstream_model == "qwen2.5-coder-32b-instruct"


def test_effective_upstream_model_fallback_to_id(catalog: Catalog):
    """effective_upstream_model falls back to the entry id when upstream_model is None."""
    # echo-local has no upstream_model
    entry = catalog.require("echo-local")
    assert entry.upstream_model is None
    assert entry.effective_upstream_model == "echo-local"


def test_effective_upstream_model_real_local_entry(catalog: Catalog):
    """qwen2.5-coder-32b-mlx maps upstream_model to qwen2.5-coder-32b-instruct."""
    entry = catalog.get("qwen2.5-coder-32b-mlx")
    if entry is None:
        pytest.skip("local entry not present in catalog")
    assert entry.effective_upstream_model == "qwen2.5-coder-32b-instruct"


# --- residency and price defaults ---


def test_default_price_is_zero(tmp_path):
    """Models without explicit price default to 0.0/0.0."""
    yaml_text = textwrap.dedent("""
        models:
          - id: free-model
            lane: fake
            endpoint: fake
            residency_class: in_perimeter
    """)
    f = tmp_path / "cat.yaml"
    f.write_text(yaml_text)
    cat = Catalog.load(str(f))
    entry = cat.require("free-model")
    assert entry.price_usd_per_1k.prompt == 0.0
    assert entry.price_usd_per_1k.completion == 0.0


def test_load_composes_comma_separated_files():
    """TOTO_GW_CATALOG may list several files: their models merge left-to-right, so each provider
    can own a fragment (catalog.fireworks.yaml) instead of living in another provider's file."""
    cat = Catalog.load("catalog.openrouter.yaml,catalog.fireworks.yaml")
    ids = {m.id for m in cat.models}
    assert {"fw-glm-5.2", "fw-deepseek-v4-pro"} <= ids   # fireworks fragment merged in
    assert any(m.id.startswith("or-") for m in cat.models)  # openrouter base still present


def test_load_fireworks_fragment_alone():
    """The fragment is valid on its own (self-contained models list)."""
    cat = Catalog.load("catalog.fireworks.yaml")
    assert cat.get("fw-glm-5.2").upstream_model.startswith("accounts/fireworks/")


def test_shared_fireworks_fragment_is_public_only():
    """Paradigm lock (Alex, 2026-07-12): the shared catalog is the platform's public offering.
    An account-scoped upstream (accounts/<customer>/...) is tenant data — it reaches callers via
    credential-scoped inventory discovery, never the YAML every caller can see."""
    cat = Catalog.load("catalog.fireworks.yaml")
    for m in cat.models:
        assert m.upstream_model.startswith("accounts/fireworks/"), \
            f"{m.id}: account-scoped upstream {m.upstream_model!r} belongs in scoped discovery"


def test_catalog_ids_tell_the_truth_about_their_upstream():
    """Drift lock (Alex's rule, 2026-07-09): a catalog id IS the real model name — it must never
    lie about what it dispatches. This pins the shipped id ↔ upstream pairs; anyone who swaps an
    entry's upstream_model in place (making the name lie) or adds a tier-word id has to come
    through this test and be told the rule: model swaps happen by ADDING a real-name entry and
    repointing bindings (console, real-time), never by mutating an existing id's upstream."""
    expected = {
        "catalog.openrouter.yaml": {
            "or-qwen3-coder-flash": "qwen/qwen3-coder-flash",
            "or-gemini-2.5-flash": "google/gemini-2.5-flash",
            "or-haiku-4.5": "anthropic/claude-haiku-4.5",
            "or-sonnet-4.6": "anthropic/claude-sonnet-4.6",
            "or-sonnet-5": "anthropic/claude-sonnet-5",
        },
        "catalog.fireworks.yaml": {
            "fw-glm-5.2": "accounts/fireworks/models/glm-5p2",
            "fw-deepseek-v4-pro": "accounts/fireworks/models/deepseek-v4-pro",
        },
        "catalog.cloudflare.yaml": {
            "cf-llama-3.1-8b-fp8": "@cf/meta/llama-3.1-8b-instruct-fp8-fast",
            "cf-qwen3-30b-a3b": "@cf/qwen/qwen3-30b-a3b-fp8",
            "cf-gpt-oss-20b": "@cf/openai/gpt-oss-20b",
            "cf-gpt-oss-120b": "@cf/openai/gpt-oss-120b",
            "cf-llama-3.3-70b-fp8": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        },
        "catalog.yaml": {
            "claude-sonnet-4.6": "claude-sonnet-4-6",
            "gpt-4o": "gpt-4o",
            "or-sonnet-4.6": "anthropic/claude-sonnet-4.6",
            "or-sonnet-5": "anthropic/claude-sonnet-5",
            "qwen2.5-coder-32b-mlx": "qwen2.5-coder-32b-instruct",
        },
        "catalog.directlabs.yaml": {
            "mistral-medium-3.5": "mistral-medium-3-5-26-04",
            "mistral-small-4": "mistral-small-2603",
            "grok-4.3": "grok-4.3",
            "grok-4.5": "grok-4.5",
            "deepseek-v4-flash": "deepseek-v4-flash",
            "deepseek-v4-pro": "deepseek-v4-pro",
            "sonar": "sonar",
            "sonar-pro": "sonar-pro",
            "minimax-m3": "MiniMax-M3",
            "gemini-3.5-flash": "gemini-3.5-flash",
            "gemini-3.1-flash-lite": "gemini-3.1-flash-lite",
            "qwen3.7-max": "qwen3.7-max",
            "qwen3.6-flash": "qwen3.6-flash",
            "kimi-k2.6": "kimi-k2.6",
            "kimi-k2.7-code": "kimi-k2.7-code",
            "glm-5.2": "glm-5.2",
            "glm-4.7": "glm-4.7",
            "gpt-5.6-sol": "gpt-5.6-sol",
            "gpt-5.6-luna": "gpt-5.6-luna",
            "claude-sonnet-5": "claude-sonnet-5",
            "claude-opus-4.8": "claude-opus-4-8",
            "claude-haiku-4.5": "claude-haiku-4-5",
        },
    }
    banned_tier_words = ("economy", "frontier", "premium", "cheap")
    for path, pairs in expected.items():
        cat = Catalog.load(path)
        real = {m.id: m.upstream_model for m in cat.models if m.endpoint != "fake"}
        assert real == pairs, f"{path}: id↔upstream drifted — see this test's docstring for the rule"
        for m in cat.models:
            if m.endpoint == "fake":
                continue  # echo-local/echo-cloud are honest fakes, not provider models
            assert not any(w in m.id for w in banned_tier_words), \
                f"{path}: {m.id} smuggles a tier word back into an id (lane is the tier concept)"


def test_legacy_tier_word_ids_are_dead():
    """2026-07-10 (Alex): the tier-word convention is STRIPPED, not aliased. Retired ids resolve
    to nothing at request time — repair happens only at data boundaries (trace backfill, policy
    blob normalization) via the closed LEGACY_MODEL_IDS historical map."""
    from toto_gateway.catalog import LEGACY_MODEL_IDS, normalize_legacy_id

    cat = Catalog.load("catalog.openrouter.yaml,catalog.fireworks.yaml")
    for legacy in ("or-economy", "or-flash", "fw-economy", "or-frontier"):
        assert cat.get(legacy) is None
    assert cat.get("or-sonnet-5").id == "or-sonnet-5"
    # the historical map repairs, never routes
    assert normalize_legacy_id("or-economy") == "or-qwen3-coder-flash"
    assert normalize_legacy_id("or-qwen3-coder-flash") == "or-qwen3-coder-flash"
    assert all(cat.get(k) is None for k in LEGACY_MODEL_IDS)


BANNED_TIER_WORDS = {"economy", "frontier", "flagship", "premium", "value", "balanced",
                     "general", "smart", "cheap", "fast", "best"}


def test_catalog_ids_never_use_tier_words():
    """Naming guard: catalog ids name REAL models, never tiers. A new entry whose id contains a
    tier word as a segment (or-economy, fw-value-general, …) fails CI here — the obfuscating
    convention cannot come back. Real model names that happen to be words (haiku, flash-as-in-
    gemini-flash) are fine because the guard checks OUR tier vocabulary, not model vocabulary."""
    cat = Catalog.load("catalog.yaml,catalog.openrouter.yaml,catalog.fireworks.yaml,"
                       "catalog.directlabs.yaml,catalog.cloudflare.yaml")
    for entry in cat.models:
        segments = set(entry.id.replace(".", "-").split("-"))
        bad = segments & BANNED_TIER_WORDS
        assert not bad, f"catalog id {entry.id!r} uses banned tier word(s) {sorted(bad)}"


def test_shared_ids_price_identically_across_fragments():
    """Composition overrides by id, later file wins (Catalog.load) — so an id defined in two
    shipped fragments with different prices is a silent price fork: which one bills depends on
    TOTO_GW_CATALOG order. Caught live 2026-07-14: catalog.openrouter.yaml's echo-cloud kept the
    pre-fix 1000x scale and overrode catalog.yaml's corrected row. Any legitimately diverging
    price belongs on ONE fragment's row (or a price override), never on a duplicated id."""
    fragments = ["catalog.yaml", "catalog.openrouter.yaml", "catalog.fireworks.yaml",
                 "catalog.directlabs.yaml", "catalog.cloudflare.yaml"]
    seen: dict[str, tuple[str, tuple]] = {}
    for path in fragments:
        for m in Catalog.load(path).models:
            price = (m.price_usd_per_1k.prompt, m.price_usd_per_1k.completion,
                     m.price_usd_per_1k.cache_read_multiplier,
                     m.price_usd_per_1k.cache_write_multiplier)
            if m.id in seen:
                prior_path, prior_price = seen[m.id]
                assert price == prior_price, (
                    f"{m.id!r} priced differently in {prior_path} {prior_price} vs "
                    f"{path} {price} — composition makes the later file silently win")
            seen[m.id] = (path, price)
