"""Out-of-box catalog defaulting: an unset TOTO_GW_CATALOG resolves against the presence of
OPENROUTER_API_KEY, so a fresh clone with just that key gets working smart routing (the shipped
labels.yaml binds OpenRouter entries), while a key-less clone keeps the offline catalog.yaml and
label routing degrades with an actionable WARNING, not an ERROR."""

import logging

from toto_gateway.app import build_gateway
from toto_gateway.catalog import Catalog
from toto_gateway.config import Settings


def _clean_env(monkeypatch):
    monkeypatch.delenv("TOTO_GW_CATALOG", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def test_default_catalog_without_key_is_offline(monkeypatch):
    _clean_env(monkeypatch)
    assert Settings().catalog == "catalog.yaml"


def test_default_catalog_with_openrouter_key(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert Settings().catalog == "catalog.openrouter.yaml"


def test_explicit_catalog_beats_key_default(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("TOTO_GW_CATALOG", "catalog.yaml")
    assert Settings().catalog == "catalog.yaml"
    assert Settings(catalog="catalog.fireworks.yaml").catalog == "catalog.fireworks.yaml"


def test_key_default_boots_with_label_routing_live(monkeypatch):
    """The whole point: key present + zero extra env → labels + classifier resolve in the
    defaulted catalog, so smart routing is ON at boot."""
    _clean_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    s = Settings(fake_exec=True, trace_jsonl="", trace_db="", trace_stdout=False)
    gw = build_gateway(s)
    assert gw._labels is not None
    assert gw.catalog.get(s.label_classifier_model) is not None


def test_keyless_default_degrades_with_actionable_warning(monkeypatch, caplog):
    """No key at all: boot succeeds, label routing soft-disables at WARNING (not ERROR), and the
    log names the exact env var that turns smart routing on."""
    _clean_env(monkeypatch)
    s = Settings(fake_exec=True, trace_jsonl="", trace_db="", trace_stdout=False)
    with caplog.at_level(logging.WARNING, logger="toto_gateway.routing"):
        gw = build_gateway(s)
    assert gw._labels is None
    recs = [r for r in caplog.records if "smart task-type routing disabled" in r.message]
    assert recs and recs[0].levelno == logging.WARNING
    assert "OPENROUTER_API_KEY" in recs[0].getMessage()


def test_shipped_pairing_is_coherent():
    """labels.yaml + catalog.openrouter.yaml: every binding resolves, no fake-lane bindings, and
    the default spread actually uses both tiers (a one-model table would be a silent regression)."""
    from toto_gateway.routing.labels import LabelBindings

    cat = Catalog.load("catalog.openrouter.yaml")
    b = LabelBindings()
    assert b.validate(cat) == []
    lanes = {cat.get(b.model_for(label)).lane for label in b.vocab() if b.model_for(label)}
    assert lanes == {"economy", "frontier"}
