"""The production classifier prompt variant (settings.label_prompt_variant → prompts seam).

fewshot is the shipped default — the 2026-07-10 prompt x model matrix measured it +2 macro-F1
over baseline on or-haiku-4.5 (1,040-row synth set). These tests pin: the default reaches both
production call paths, baseline restores the pre-variant prompt byte-identically, and a typo'd
variant fails the boot instead of a request.
"""

import pytest

from toto_gateway.config import Settings
from toto_gateway.driver import prompts

LABELS = {"code_generation": {"desc": "write code"}, "other": {"desc": "none fits"}}


@pytest.fixture(autouse=True)
def _restore_variant():
    default = prompts._LABEL_VARIANT_DEFAULT
    yield
    prompts.set_label_variant(default)


def test_config_default_is_fewshot_and_module_mirrors_it():
    assert Settings(_env_file=None).label_prompt_variant == "fewshot"
    assert prompts._LABEL_VARIANT_DEFAULT == "fewshot"


def test_default_build_includes_fewshot_block():
    system = prompts.build_label_messages("q", LABELS)[0]["content"]
    assert "Worked examples" in system
    assert "Return STRICT JSON only." in system  # output contract intact


def test_baseline_restores_pre_variant_prompt_byte_identically():
    prompts.set_label_variant("baseline")
    got = prompts.build_label_messages("q", LABELS)
    explicit = prompts.build_label_messages("q", LABELS, variant="baseline")
    assert got == explicit
    assert "Worked examples" not in got[0]["content"]


def test_unknown_variant_fails_loud_at_set_time():
    with pytest.raises(ValueError, match="unknown label prompt variant"):
        prompts.set_label_variant("fewshto")


def test_build_gateway_threads_the_setting(tmp_path, monkeypatch):
    from toto_gateway.app import build_gateway

    monkeypatch.setenv("TOTO_GW_FAKE_EXEC", "1")
    build_gateway(Settings(_env_file=None, fake_exec=True, label_prompt_variant="rules"))
    assert prompts._LABEL_VARIANT_DEFAULT == "rules"
    with pytest.raises(ValueError):
        build_gateway(Settings(_env_file=None, fake_exec=True, label_prompt_variant="nope"))
