"""Label routing: bindings load/validate, strict parser, kill-switch byte-identical,
binding displaces the benchmark pick, every miss falls back, privacy never egresses,
pins still win, app.py soft-degrade. No network — a fake complete_fn answers everything."""

from __future__ import annotations

import json

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.driver import prompts
from toto_gateway.driver.core import Driver, Exec
from toto_gateway.routing.labels import LabelBindings

_RAW = {"labels": {
    "code_generation": {"model": "or-qwen3-coder-flash", "desc": "write or debug code"},
    "brainstorming": {"model": "or-sonnet-4.6", "desc": "open-ended ideas"},
    "other": {"model": None, "desc": "none of the above"},
}}


def _cat(in_perimeter: bool = False):
    models = [
        CatalogEntry(id="or-qwen3-coder-flash", lane="economy", endpoint="openai", residency_class="cloud"),
        CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="openai", residency_class="cloud"),
    ]
    if in_perimeter:
        models.append(CatalogEntry(id="local-box", lane="economy", endpoint="http://localhost:1",
                                   residency_class="in_perimeter"))
    return Catalog(models=models)


# --- bindings -------------------------------------------------------------------------

def test_bindings_vocab_and_model_for():
    b = LabelBindings(_raw=_RAW)
    assert b.vocab() == ["brainstorming", "code_generation", "other"]
    assert b.model_for("code_generation") == "or-qwen3-coder-flash"
    assert b.model_for("other") is None          # unbound → fallback
    assert b.model_for("nonsense") is None       # unknown → fallback


def test_bindings_validate():
    cat = _cat()
    assert LabelBindings(_raw=_RAW).validate(cat) == []
    bad = LabelBindings(_raw={"labels": {
        "x": {"model": "no-such-model", "desc": ""},
        "y": {"model": "echo", "desc": ""},
    }})
    cat_with_fake = Catalog(models=[*cat.models, CatalogEntry(
        id="echo", lane="fake", endpoint="fake", residency_class="in_perimeter")])
    errs = bad.validate(cat_with_fake)
    assert any("no-such-model" in e for e in errs)
    assert any("fake-lane" in e for e in errs)


def test_shipped_bindings_are_valid_against_openrouter_catalog():
    """Guard the checked-in labels.yaml: every bound id must exist in the openrouter catalog."""
    cat = Catalog.load("catalog.openrouter.yaml")
    b = LabelBindings()  # default path = routing/labels.yaml
    assert b.validate(cat) == []
    assert len(b.vocab()) == 12  # NVIDIA's 11 + redact


# --- prompt + parser ------------------------------------------------------------------

def test_build_label_messages_enumerates_vocab():
    msgs = prompts.build_label_messages("do the thing", _RAW["labels"])
    sys = msgs[0]["content"]
    for name in _RAW["labels"]:
        assert f'"{name}"' in sys
    assert "STRICT JSON" in sys
    assert "{label_block}" not in sys           # marker substituted
    assert msgs[1] == {"role": "user", "content": "do the thing"}


def test_parse_label_strict():
    vocab = ["code_generation", "other"]
    assert prompts.parse_label('{"label": "code_generation", "reason": "r"}', vocab) == "code_generation"
    assert prompts.parse_label('```json\n{"label": "other"}\n```', vocab) == "other"
    assert prompts.parse_label('{"label": "Code Generation"}', vocab) is None  # not verbatim
    assert prompts.parse_label("the label is code_generation", vocab) is None  # prose
    assert prompts.parse_label("{broken", vocab) is None
    assert prompts.parse_label("", vocab) is None


# --- driver dispatch ------------------------------------------------------------------

def _make_fake_complete(label_reply: str | None = '{"label": "code_generation", "reason": "r"}',
                        raise_on_label: bool = False,
                        metadata: dict | None = None):
    """Fake complete_fn keyed on system-prompt content (test_embed_routing pattern).
    Records classifier calls + their request temperature."""
    md = metadata or {"scope": "backend", "complexity": "medium", "keywords": ["analyze", "market"]}
    calls = {"label": 0, "label_temps": []}

    async def fake(req):
        sys = req.messages[0].text() if req.messages[0].role == "system" else ""
        m = req.model
        if "label one piece of work" in sys:
            calls["label"] += 1
            calls["label_temps"].append(req.temperature)
            if raise_on_label:
                raise RuntimeError("classifier down")
            return Exec(text=label_reply or "", model=m)
        if "triage classifier" in sys:
            return Exec(text=json.dumps({"kind": "multistep", "reason": "t"}), model=m)
        if "task decomposer" in sys:
            return Exec(text=json.dumps({"tasks": [
                {"task": "T", "description": "make the checkout page load faster",
                 "metadata": md}]}), model=m)
        return Exec(text="final", model=m)

    fake.calls = calls
    return fake


def _driver(fake, *, cat=None, labels=_RAW, **kw):
    return Driver(catalog=cat or _cat(), complete_fn=fake, driver_model="or-sonnet-4.6",
                  triage_model="or-qwen3-coder-flash", toto=None,
                  labels=LabelBindings(_raw=labels) if labels else None,
                  label_model="or-qwen3-coder-flash", **kw)


@pytest.mark.asyncio
async def test_kill_switch_byte_identical():
    fake = _make_fake_complete()
    d = _driver(fake, labels=None)  # labels=None = flag off
    r = await d.run("go")
    assert fake.calls["label"] == 0
    assert "label" not in r.tasks[0]["execution"]["route_reason"]


@pytest.mark.asyncio
async def test_label_binding_displaces_benchmark_pick():
    fake = _make_fake_complete()  # metadata words say frontier; label says code_generation
    d = _driver(fake)
    r = await d.run("go")
    t = r.tasks[0]
    assert fake.calls["label"] == 1
    assert fake.calls["label_temps"] == [0.0]           # deterministic classifier call
    assert t["model_id"] == "or-qwen3-coder-flash"                # the binding, not the keyword pick
    assert t["execution"]["route_reason"] == "label:code_generation"
    assert {"model_id": "or-sonnet-4.6", "reason": "label binding outbid benchmarks"} in t["execution"]["rejected"]


@pytest.mark.asyncio
async def test_unknown_and_unbound_labels_fall_back():
    for reply in ('{"label": "made_up_label"}', '{"label": "other"}', "no json here"):
        fake = _make_fake_complete(label_reply=reply)
        d = _driver(fake)
        r = await d.run("go")
        t = r.tasks[0]
        assert t["model_id"] == "or-sonnet-4.6"           # keyword-ladder pick unchanged
        reason = t["execution"]["route_reason"]
        assert ";" in reason and "label:" in reason and reason.endswith(":fallback")


@pytest.mark.asyncio
async def test_local_pinned_never_calls_classifier():
    fake = _make_fake_complete(metadata={
        "scope": "backend", "complexity": "medium",
        "requires": {"data_policy": "local_only"}})
    d = _driver(fake, cat=_cat(in_perimeter=True))
    r = await d.run("go")
    assert fake.calls["label"] == 0                     # pinned text never reaches the classifier
    assert r.tasks[0]["model_id"] == "local-box"


@pytest.mark.asyncio
async def test_user_pin_displaces_label_with_receipt():
    fake = _make_fake_complete()
    d = _driver(fake, preferences=lambda _uid=None: {"pins": {"reasoning": "or-sonnet-4.6"}})
    r = await d.run("go")
    t = r.tasks[0]
    assert t["model_id"] == "or-sonnet-4.6"               # pin wins over the label binding
    assert {"model_id": "or-qwen3-coder-flash", "reason": "pin override"} in t["execution"]["rejected"]
    assert t["execution"]["route_reason"].startswith("label:code_generation")


@pytest.mark.asyncio
async def test_classifier_exception_falls_back():
    fake = _make_fake_complete(raise_on_label=True)
    d = _driver(fake, provider_retries=0)
    r = await d.run("go")
    t = r.tasks[0]
    assert t["model_id"] == "or-sonnet-4.6"               # classify() pick stands
    assert t["execution"]["route_reason"].endswith("label:none:fallback")
    assert t["execution"]["outcome"] == "completed"     # classifier down != routing down


@pytest.mark.asyncio
async def test_user_label_override_beats_shipped_binding():
    fake = _make_fake_complete()  # classifier says code_generation (shipped binding: or-qwen3-coder-flash)
    d = _driver(fake, preferences=lambda _uid=None: {"label_models": {"code_generation": "or-sonnet-4.6"}})
    r = await d.run("go")
    t = r.tasks[0]
    assert t["model_id"] == "or-sonnet-4.6"               # the user's binding, not the shipped one
    assert t["execution"]["route_reason"] == "label:code_generation:user"


@pytest.mark.asyncio
async def test_user_label_override_binds_an_unbound_label():
    fake = _make_fake_complete(label_reply='{"label": "other"}')  # shipped: other -> null
    d = _driver(fake, preferences=lambda _uid=None: {"label_models": {"other": "or-qwen3-coder-flash"}})
    r = await d.run("go")
    t = r.tasks[0]
    assert t["model_id"] == "or-qwen3-coder-flash"
    assert t["execution"]["route_reason"] == "label:other:user"


@pytest.mark.asyncio
async def test_stale_user_override_falls_through_to_shipped_binding():
    fake = _make_fake_complete()  # label: code_generation, shipped binding or-qwen3-coder-flash
    d = _driver(fake, preferences=lambda _uid=None: {"label_models": {"code_generation": "left-the-catalog"}})
    r = await d.run("go")
    t = r.tasks[0]
    assert t["model_id"] == "or-qwen3-coder-flash"               # stale id ignored, shipped binding stands
    assert t["execution"]["route_reason"] == "label:code_generation"  # no :user marker


@pytest.mark.asyncio
async def test_knn_refines_within_label_lane():
    class _KNN:  # kNN proposes within the label's lane — label sets the tier, kNN refines
        async def propose(self, text, lane):
            assert lane == "economy"                   # the LABEL's lane, not the keyword pick's
            from toto_gateway.driver.knn import Proposal
            return Proposal("or-qwen3-coder-flash", "knn: 3 similar tasks favored or-qwen3-coder-flash")

    fake = _make_fake_complete()  # keyword pick would be frontier; label binds or-qwen3-coder-flash
    d = _driver(fake, knn=_KNN())
    r = await d.run("go")
    t = r.tasks[0]
    assert t["model_id"] == "or-qwen3-coder-flash"
    assert "knn:" in t["execution"]["route_reason"]    # kNN ran AFTER the label decision
    assert t["execution"]["route_reason"].startswith("label:code_generation")


@pytest.mark.asyncio
async def test_knn_never_overrides_user_label_binding():
    class _KNN:  # explicit user binding has pin-level authority: kNN must not even be consulted
        def __init__(self):
            self.calls = 0

        async def propose(self, text, lane):
            self.calls += 1
            from toto_gateway.driver.knn import Proposal
            return Proposal("or-qwen3-coder-flash", "knn: would override")

    knn = _KNN()
    fake = _make_fake_complete()  # label: code_generation
    d = _driver(fake, knn=knn,
                preferences=lambda _uid=None: {"label_models": {"code_generation": "or-sonnet-4.6"}})
    r = await d.run("go")
    t = r.tasks[0]
    assert knn.calls == 0                               # user intent > learned suggestion
    assert t["model_id"] == "or-sonnet-4.6"
    assert t["execution"]["route_reason"] == "label:code_generation:user"


@pytest.mark.asyncio
async def test_hung_classifier_times_out_to_fallback():
    import asyncio as aio

    async def slow(req):
        sys = req.messages[0].text() if req.messages[0].role == "system" else ""
        if "label one piece of work" in sys:
            await aio.sleep(0.5)                        # hangs past the 10ms cap below
        return await _make_fake_complete()(req)

    d = _driver(slow, label_timeout_ms=10)
    r = await d.run("go")
    t = r.tasks[0]
    assert t["execution"]["outcome"] == "completed"     # the task, not the run, absorbed it
    assert t["execution"]["route_reason"].endswith("label:none:fallback")
    assert t["model_id"] == "or-sonnet-4.6"               # keyword-ladder pick stood


def test_bindings_tolerate_null_labels_yaml():
    b = LabelBindings(_raw={"labels": None})
    assert b.vocab() == [] and b.model_for("anything") is None


# --- app.py wiring --------------------------------------------------------------------

def test_build_driver_soft_disables_without_classifier_model():
    from toto_gateway.app import build_driver, build_gateway
    from toto_gateway.config import Settings

    # catalog.yaml has no or-haiku-4.5 → label routing soft-disables, boot succeeds
    s = Settings(catalog="catalog.yaml", fake_exec=True, driver=True)
    d = build_driver(s, build_gateway(s))
    assert d._labels is None

    # openrouter catalog has or-haiku-4.5 → label routing active
    s2 = Settings(catalog="catalog.openrouter.yaml", fake_exec=True, driver=True)
    d2 = build_driver(s2, build_gateway(s2))
    assert d2._labels is not None
    assert d2._label_model == "or-haiku-4.5"


# --- preferences API ------------------------------------------------------------------

def _client(catalog_path: str):
    from fastapi.testclient import TestClient

    from toto_gateway.app import create_app
    from toto_gateway.config import Settings

    s = Settings(catalog=catalog_path, trace_jsonl="", trace_db="", trace_stdout=False,
                 auth_token="", driver=True, fake_exec=True, db=":memory:",
                 driver_model="echo-cloud", triage_model="echo-local",
                 driver_spans_jsonl="/dev/null", toto_token="")
    return TestClient(create_app(settings=s), raise_server_exceptions=True)


def test_routing_labels_endpoint_and_label_model_prefs():
    # /v1/routing/labels + label prefs live on the app plane (routes/preferences) — absent
    # in the OSS export tree, so this test skips there; a no-op wherever the module exists.
    pytest.importorskip("toto_gateway.routes.preferences")
    with _client("catalog.openrouter.yaml") as c:
        # vocabulary + defaults exposed for the Settings UI
        body = c.get("/v1/routing/labels").json()
        assert body["active"] is True
        by_name = {row["label"]: row for row in body["labels"]}
        assert len(by_name) == 12
        assert by_name["code_generation"]["default_model"] == "or-qwen3-coder-flash"
        assert by_name["other"]["default_model"] == "or-sonnet-4.6"  # the shipped GENERALIST (catch-all) since 2026-07-12
        assert by_name["extraction"]["desc"]
        # a valid override round-trips; junk label / junk model are rejected
        r = c.put("/v1/preferences", json={"label_models": {"code_generation": "or-sonnet-4.6"}})
        assert r.status_code == 200
        assert r.json()["label_models"] == {"code_generation": "or-sonnet-4.6"}
        assert c.put("/v1/preferences", json={"label_models": {"vibes": "or-sonnet-4.6"}}).status_code == 400
        assert c.put("/v1/preferences", json={"label_models": {"chatbot": "not-a-model"}}).status_code == 400


def test_label_model_prefs_ignored_when_routing_inactive():
    # /v1/routing/labels + label prefs live on the app plane (routes/preferences) — absent
    # in the OSS export tree, so this test skips there; a no-op wherever the module exists.
    pytest.importorskip("toto_gateway.routes.preferences")
    """Inactive routing must neither 400 (bricks every Settings save for a user with stored
    overrides) nor write (the UI sends {} while the section is hidden — would wipe them)."""
    with _client("catalog.yaml") as c:  # no or-haiku-4.5 → label routing soft-disabled
        assert c.get("/v1/routing/labels").json() == {"active": False, "labels": []}
        r = c.put("/v1/preferences", json={"optimize": "cost", "label_models": {"chatbot": "echo-local"}})
        assert r.status_code == 200                      # save succeeds…
        assert r.json()["label_models"] == {}            # …but the field was ignored, not stored
        assert r.json()["optimize"] == "cost"


def test_fake_lane_rejected_for_pins_and_label_models():
    # /v1/routing/labels + label prefs live on the app plane (routes/preferences) — absent
    # in the OSS export tree, so this test skips there; a no-op wherever the module exists.
    pytest.importorskip("toto_gateway.routes.preferences")
    with _client("catalog.openrouter.yaml") as c:  # echo-local is a fake-lane entry
        assert c.put("/v1/preferences", json={"pins": {"code": "echo-local"}}).status_code == 400
        assert c.put("/v1/preferences", json={"label_models": {"chatbot": "echo-local"}}).status_code == 400
        # real entries still bind fine
        assert c.put("/v1/preferences", json={"label_models": {"chatbot": "or-gemini-2.5-flash"}}).status_code == 200
