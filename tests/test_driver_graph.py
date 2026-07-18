"""End-to-end driver graph tests — the core.py + graph.py integration.

Drives the whole LangGraph with a fake `complete_fn` (no network, no LLM): proves both branches
(trivial / multistep), that the metadata classifier routes each task to the right lane/model,
that spans accumulate through the reducer (the bug LangGraph's contract would otherwise hide),
that the fail-closed guard blocks MNPI egress before dispatch, and that the checkpointer gives
us replayable state history.
"""

from __future__ import annotations

import json

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.driver.core import Driver, Exec
from toto_gateway.driver.graph import build_graph


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(
        models=[
            CatalogEntry(id="or-qwen3-coder-flash", lane="economy", endpoint="openai", residency_class="in_perimeter"),
            CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="openai", residency_class="cloud"),
            CatalogEntry(id="fake-x", lane="fake", endpoint="fake", residency_class="in_perimeter"),
        ]
    )


# Two decomposed tasks with opposite routing signals: one research (→ frontier), one redact
# under a local_only data policy (→ local, privacy wins).
_TASKS = [
    {
        "task": "Research the market",
        "description": "Research and analyze the competitive market landscape for context.",
        "metadata": {"scope": "research", "complexity": "high", "intent": "market analyzed",
                     "keywords": ["research", "market"],
                     "requires": {"tools": ["web_search"], "data_policy": "default"}},
    },
    {
        "task": "Redact the memo",
        "description": "Remove sensitive identifiers from the source memo for safe handling.",
        "metadata": {"scope": "backend", "complexity": "low", "intent": "memo redacted",
                     "keywords": ["redact"],
                     "requires": {"tools": [], "data_policy": "local_only"}},
    },
]


def make_fake(kind: str = "multistep"):
    """A complete_fn that keys off the system-prompt marker (or its absence = dispatch)."""

    async def fake(req):
        first = req.messages[0]
        sys = first.text() if first.role == "system" else ""
        m = req.model
        if "triage classifier" in sys:
            return Exec(text=json.dumps({"kind": kind, "reason": "t"}), model=m, lane="economy",
                        tokens_prompt=10, tokens_completion=5, cost_usd=0.0001, latency_ms=5)
        if "task decomposer" in sys:
            return Exec(text=json.dumps({"tasks": _TASKS}), model=m, lane="frontier",
                        tokens_prompt=50, tokens_completion=120, cost_usd=0.003, latency_ms=20)
        if "sub-task" in sys:
            return Exec(text="FINAL: combined answer", model=m, lane="frontier",
                        tokens_prompt=80, tokens_completion=40, cost_usd=0.002, latency_ms=15)
        if "request directly" in sys:  # direct-answer (trivial)
            return Exec(text="Direct trivial answer", model=m, lane="frontier",
                        tokens_prompt=8, tokens_completion=4, cost_usd=0.0002, latency_ms=3)
        # No system message → a task dispatch. Echo which model executed it.
        return Exec(text=f"result via {m}", model=m, lane="", tokens_prompt=30,
                    tokens_completion=20, cost_usd=0.0005, latency_ms=8)

    return fake


def make_driver(catalog, kind="multistep") -> Driver:
    return Driver(catalog=catalog, complete_fn=make_fake(kind), driver_model="or-sonnet-4.6",
                  triage_model="or-qwen3-coder-flash", toto=None)


@pytest.mark.asyncio
async def test_multistep_flow_routes_each_task(catalog):
    driver = make_driver(catalog, "multistep")
    result = await driver.run("compare two companies and redact the memo")

    assert result.kind == "multistep"
    assert result.answer == "FINAL: combined answer"
    assert len(result.tasks) == 2

    research, redact = result.tasks
    # research/high/web_search → frontier; redact/local_only → in-perimeter (privacy beats
    # everything and keys off RESIDENCY: or-qwen3-coder-flash is the in_perimeter box in this catalog).
    assert research["lane"] == "frontier" and research["model_id"] == "or-sonnet-4.6"
    assert redact["model_id"] == "or-qwen3-coder-flash"
    assert catalog.require(redact["model_id"]).residency_class == "in_perimeter"
    # every task carries execution provenance (metadata-only) and a result.
    for t in result.tasks:
        assert t["execution"]["outcome"] == "completed"
        assert t["result"].startswith("result via")


@pytest.mark.asyncio
async def test_synthesize_stays_local_when_a_subtask_is_pinned_local(catalog):
    # #29 residual egress: a decomposed sub-task pinned local by its OWN data_policy=local_only
    # (the "Redact the memo" task in _TASKS) produces local-only OUTPUT. synthesize aggregates
    # every sub-task result — so it must run on the LOCAL lane, never egress that output to the
    # frontier driver_model. FAILS pre-fix (synthesize ran on or-sonnet-4.6); PASSES post-fix.
    driver = make_driver(catalog, "multistep")
    result = await driver.run("compare two companies and redact the memo")

    synth = next(s for s in result.spans if s["node"] == "synthesize")
    assert synth["model"] == "or-qwen3-coder-flash"  # local lane, NOT the frontier driver_model "or-sonnet-4.6"
    assert result.answer == "FINAL: combined answer"


@pytest.mark.asyncio
async def test_synthesize_skips_llm_on_single_result(catalog):
    # P4 (dayflow): 0/1 non-empty results → return it verbatim, no weave LLM call.
    calls: list[str] = []

    async def counting_fake(req):
        first = req.messages[0]
        sys = first.text() if first.role == "system" else ""
        calls.append(sys)
        return Exec(text="WOVEN", model=req.model, lane="", tokens_prompt=1,
                    tokens_completion=1, cost_usd=0.0, latency_ms=1)

    driver = Driver(catalog=catalog, complete_fn=counting_fake, driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None)

    one = await driver.synthesize({"query": "q", "tasks": [{"task": "t", "result": "the answer"}]})
    assert one["answer"] == "the answer"          # single result verbatim, unwoven
    assert calls == []                             # synthesize LLM never invoked
    assert one["spans"][0].get("skipped") is True

    zero = await driver.synthesize({"query": "q", "tasks": [{"task": "t", "result": ""}]})
    assert zero["answer"] == "" and calls == []    # 0 results → empty, still no call


@pytest.mark.asyncio
async def test_synthesize_weaves_multiple_results(catalog):
    # Contract unchanged for ≥2 non-empty results: the weave LLM IS called.
    calls: list[str] = []

    async def counting_fake(req):
        calls.append("call")
        return Exec(text="WOVEN", model=req.model, lane="", tokens_prompt=1,
                    tokens_completion=1, cost_usd=0.0, latency_ms=1)

    driver = Driver(catalog=catalog, complete_fn=counting_fake, driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None)
    out = await driver.synthesize({"query": "q", "tasks": [
        {"task": "a", "result": "r1"}, {"task": "b", "result": "r2"}]})
    assert out["answer"] == "WOVEN" and len(calls) == 1


@pytest.mark.asyncio
async def test_trivial_flow_answers_directly(catalog):
    driver = make_driver(catalog, "trivial")
    result = await driver.run("what is 2+2")

    assert result.kind == "trivial"
    assert result.answer == "Direct trivial answer"
    assert len(result.tasks) == 1
    assert result.tasks[0]["execution"]["outcome"] == "completed"


@pytest.mark.asyncio
async def test_raw_prompt_never_leaks_to_toto_description(catalog):
    # Content boundary: the task description POSTed to Toto must never carry the raw prompt.
    secret = "Summarize this memo: PROJECT-BLUEBIRD acquisition, PII SSN 123-45-6789"

    trivial = await make_driver(catalog, "trivial").run(secret)
    assert secret not in trivial.tasks[0]["description"]

    # decompose fallback path (both decompose attempts unparseable) also builds a task from q
    base = make_fake("multistep")

    async def fake(req):
        first = req.messages[0]
        if first.role == "system" and "task decomposer" in first.text():
            return Exec(text="no json here, ever", model=req.model)
        return await base(req)

    driver = Driver(catalog=catalog, complete_fn=fake, driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None)
    fallback = await driver.run(secret)
    assert secret not in fallback.tasks[0]["description"]


@pytest.mark.asyncio
async def test_spans_accumulate_through_reducer(catalog):
    # Guards the LangGraph contract: nodes must RETURN span updates; in-place mutation is lost.
    driver = make_driver(catalog, "multistep")
    result = await driver.run("do a multistep thing")
    nodes = [s["node"] for s in result.spans]
    for expected in ("triage", "decompose", "dispatch", "synthesize"):
        assert expected in nodes, f"missing span {expected}: {nodes}"
    assert len(result.spans) >= 4


@pytest.mark.asyncio
async def test_dispatch_span_carries_route_reason(catalog, monkeypatch):
    # The routing DECISION (route_reason/skill/lane) must ride on the LangSmith dispatch span's
    # metadata — not just the JSONL observer + Toto record. Fake tracing on and capture rt.end().
    import contextlib
    import toto_gateway.driver.core as core

    captured: list[dict] = []

    class _RT:
        def end(self, **kw):
            captured.append(kw.get("metadata") or {})

    @contextlib.contextmanager
    def fake_trace(*, name, run_type, inputs):
        yield _RT()

    monkeypatch.setattr(core, "_ls_enabled", lambda: True)
    monkeypatch.setattr("langsmith.trace", fake_trace)

    driver = make_driver(catalog, "multistep")
    await driver.run("compare two companies and redact the memo")

    dispatch_meta = [m for m in captured if "route_reason" in m]
    assert len(dispatch_meta) == 2, f"both dispatch spans should carry route_reason: {captured}"
    for m in dispatch_meta:
        assert m["route_reason"] and m["skill"] and m["lane"]
    # the frontier task's reason reflects the actual routing decision, not a placeholder
    assert any("frontier" in m["route_reason"] for m in dispatch_meta)


@pytest.mark.asyncio
async def test_run_traced_surfaces_upstream_model(catalog, monkeypatch):
    # The served OpenRouter model/provider/generation-id must reach the LangSmith metadata, and
    # ls_model_name must prefer the SERVED model over the internal catalog alias.
    import contextlib
    from toto_gateway.schemas import ChatCompletionRequest, Message
    import toto_gateway.driver.core as core

    captured: dict = {}

    class _RT:
        def end(self, **kw):
            captured.update(kw.get("metadata") or {})

    @contextlib.contextmanager
    def fake_trace(*, name, run_type, inputs):
        yield _RT()

    monkeypatch.setattr(core, "_ls_enabled", lambda: True)
    monkeypatch.setattr("langsmith.trace", fake_trace)

    driver = make_driver(catalog, "trivial")
    req = ChatCompletionRequest(model="or-sonnet-5", messages=[Message(role="user", content="hi")])

    async def run(_req):
        return Exec(text="ok", model="or-sonnet-5", lane="frontier",
                    upstream_model="anthropic/claude-sonnet-5", provider="Anthropic",
                    generation_id="gen-xyz")

    await driver._run_traced(name="dispatch", req=req, run=run)

    assert captured["openrouter_model"] == "anthropic/claude-sonnet-5"
    assert captured["provider"] == "Anthropic"
    assert captured["generation_id"] == "gen-xyz"
    assert captured["ls_model_name"] == "anthropic/claude-sonnet-5"  # served, not "or-sonnet-5"
    assert captured["ls_provider"] == "Anthropic"


@pytest.mark.asyncio
async def test_run_traced_omits_absent_upstream(catalog, monkeypatch):
    # Fakes/providers without provenance → keys omitted, ls_model_name falls back to the alias.
    import contextlib
    from toto_gateway.schemas import ChatCompletionRequest, Message
    import toto_gateway.driver.core as core

    captured: dict = {}

    class _RT:
        def end(self, **kw):
            captured.update(kw.get("metadata") or {})

    @contextlib.contextmanager
    def fake_trace(*, name, run_type, inputs):
        yield _RT()

    monkeypatch.setattr(core, "_ls_enabled", lambda: True)
    monkeypatch.setattr("langsmith.trace", fake_trace)

    driver = make_driver(catalog, "trivial")
    req = ChatCompletionRequest(model="fake-x", messages=[Message(role="user", content="hi")])

    async def run(_req):
        return Exec(text="ok", model="fake-x", lane="fake")  # no upstream provenance

    await driver._run_traced(name="dispatch", req=req, run=run)

    assert "openrouter_model" not in captured
    assert "provider" not in captured
    assert "generation_id" not in captured
    assert captured["ls_model_name"] == "fake-x"  # fell back to alias


@pytest.mark.asyncio
async def test_provenance_rollup(catalog):
    driver = make_driver(catalog, "multistep")
    result = await driver.run("multi")
    prov = result.provenance()
    assert prov["kind"] == "multistep"
    assert prov["n_tasks"] == 2
    assert prov["cost_usd"] > 0
    lanes = {t["lane"] for t in prov["tasks"]}
    assert lanes == {"frontier", "economy"}


@pytest.mark.asyncio
async def test_guard_blocks_mnpi_egress_before_dispatch(catalog):
    # Call the node method directly — the fail-closed guard must block MNPI egress and never
    # dispatch it to any executor.
    driver = make_driver(catalog, "multistep")
    dispatched = []
    orig = driver._complete

    async def spy(req):
        dispatched.append(req.model)
        return await orig(req)

    driver._complete = spy
    task = {
        "task": "leak it",
        "description": "forward this material non-public information to the model",
        "metadata": {},
    }
    spans = await driver._dispatch_one(task)

    assert task.get("blocked") is True
    assert task["execution"]["outcome"] == "blocked_constraints"
    assert task.get("result") is None
    assert dispatched == []  # the executor was never called
    assert any(s["node"] == "guard_block" for s in spans)


@pytest.mark.asyncio
async def test_failed_executor_does_not_kill_run(catalog):
    # One task's executor dying must degrade THAT task (outcome=failed) and leave its
    # siblings + synthesis intact — a partial answer beats a dead request.
    base = make_fake("multistep")

    async def flaky(req):
        first = req.messages[0]
        text = " ".join(m.text() for m in req.messages)
        # only the redact dispatch dies (executor system prompt + the redact task in the user turn)
        if "execution worker" in first.text() and "Redact" in text:
            raise RuntimeError("provider 500")
        return await base(req)

    driver = Driver(catalog=catalog, complete_fn=flaky, driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None)
    result = await driver.run("compare two companies and redact the memo")

    research, redact = result.tasks
    assert research["execution"]["outcome"] == "completed"
    assert redact["execution"]["outcome"] == "failed"
    assert "provider 500" in redact["execution"]["error"]
    assert redact["result"] is None
    # P4: with the redact task dead, exactly ONE result survives → synthesize returns it verbatim
    # (skips the weave call). A partial answer still beats a dead request.
    assert result.answer == "result via or-sonnet-4.6"
    assert any(s["node"] == "dispatch_error" for s in result.spans)


@pytest.mark.asyncio
async def test_pinned_runner_failure_degrades_not_crashes(catalog):
    # A subagent adapter failure (here: pinned pi with a missing binary, SubagentError) must
    # become outcome=failed on the task, not a dead run. Uses the flag-ON registry — the
    # flag-OFF default has no subagent adapters at all (and parse strips the pin anyway).
    from toto_gateway.driver.adapters import AdapterRegistry

    driver = Driver(catalog=catalog, complete_fn=make_fake("multistep"),
                    driver_model="or-sonnet-4.6", triage_model="or-qwen3-coder-flash", toto=None,
                    adapters=AdapterRegistry.with_subagents(
                        make_fake("multistep"), gateway_base_url="http://127.0.0.1:1/v1",
                        pi_bin="no-such-pi-binary"))
    task = {
        "task": "Escalate via Pi",
        "description": "Needs cross-provider handoff.",
        "metadata": {"complexity": "high", "requires": {"runner": "pi"}},
    }
    spans = await driver._dispatch_one(task)

    assert task["execution"]["outcome"] == "failed"
    assert "SubagentError" in task["execution"]["error"]
    assert "no-such-pi-binary" in task["execution"]["error"]
    assert any(s["node"] == "dispatch_error" for s in spans)


@pytest.mark.asyncio
async def test_decompose_retry_recovers_bad_json(catalog):
    # First decompose reply is prose → one strict-JSON retry recovers the real tasks.
    calls = {"decompose": 0}
    base = make_fake("multistep")

    async def fake(req):
        first = req.messages[0]
        if first.role == "system" and "task decomposer" in first.text():
            calls["decompose"] += 1
            if calls["decompose"] == 1:
                return Exec(text="Sure! Here are the tasks you asked for.", model=req.model)
        return await base(req)

    driver = Driver(catalog=catalog, complete_fn=fake, driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None)
    result = await driver.run("compare two companies and redact the memo")

    assert calls["decompose"] == 2
    assert len(result.tasks) == 2  # the retry's real decomposition, not the fallback
    retry_spans = [s for s in result.spans if s["node"] == "decompose_retry"]
    assert retry_spans and retry_spans[0]["outcome"] == "recovered"


@pytest.mark.asyncio
async def test_decompose_fallback_escalates_to_frontier(catalog):
    # Both decompose attempts unparseable → single fallback task must route FRONTIER
    # (triage already judged the query multistep; degrading twice would answer it locally).
    base = make_fake("multistep")

    async def fake(req):
        first = req.messages[0]
        if first.role == "system" and "task decomposer" in first.text():
            return Exec(text="no json here, ever", model=req.model)
        return await base(req)

    driver = Driver(catalog=catalog, complete_fn=fake, driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None)
    result = await driver.run("build a full competitive analysis")

    assert len(result.tasks) == 1
    assert result.tasks[0]["lane"] == "frontier"
    assert result.tasks[0]["model_id"] == "or-sonnet-4.6"
    assert any(s["node"] == "decompose_fallback" for s in result.spans)


@pytest.mark.asyncio
async def test_run_cleans_up_checkpoint_thread(catalog):
    # Long-lived server: each run's checkpoint thread is dropped after the run completes.
    driver = make_driver(catalog, "multistep")
    await driver.run("multi one")
    await driver.run("multi two")
    assert len(driver._graph.checkpointer.storage) == 0


@pytest.mark.asyncio
async def test_checkpointer_gives_replayable_history(catalog):
    # The "state monitoring + testing" requirement: compiled with InMemorySaver → replayable.
    driver = make_driver(catalog, "multistep")
    graph = build_graph(driver)
    config = {"configurable": {"thread_id": "replay-1"}}
    await graph.ainvoke({"query": "multi", "spans": []}, config)

    history = list(graph.get_state_history(config))
    assert len(history) > 1  # one snapshot per superstep
    snap = graph.get_state(config)
    assert snap.next == ()  # run reached END
    assert snap.values["kind"] == "multistep"


# --- speed fix: per-role max_tokens caps + decompose task clamp --------------------

_CAPS = {"triage": 200, "answer": 1200, "decompose": 600, "dispatch": 1500, "synthesize": 1200}


def _capturing_fake(seen: dict, kind: str = "multistep"):
    """Like make_fake but records req.max_tokens under the role it identifies."""
    async def fake(req):
        first = req.messages[0]
        sys = first.text() if first.role == "system" else ""
        m = req.model
        if "triage classifier" in sys:
            seen["triage"] = req.max_tokens
            return Exec(text=json.dumps({"kind": kind, "reason": "t"}), model=m, cost_usd=0.0)
        if "task decomposer" in sys:
            seen["decompose"] = req.max_tokens
            return Exec(text=json.dumps({"tasks": _TASKS}), model=m, cost_usd=0.0)
        if "sub-task" in sys:
            seen["synthesize"] = req.max_tokens
            return Exec(text="FINAL", model=m, cost_usd=0.0)
        if "request directly" in sys:
            seen["answer"] = req.max_tokens
            return Exec(text="direct", model=m, cost_usd=0.0)
        seen["dispatch"] = req.max_tokens  # no system message → a dispatch
        return Exec(text=f"result via {m}", model=m, cost_usd=0.0)
    return fake


@pytest.mark.asyncio
async def test_max_tokens_per_role_on_requests(catalog):
    seen: dict = {}
    driver = Driver(catalog=catalog, complete_fn=_capturing_fake(seen, "multistep"),
                    driver_model="or-sonnet-4.6", triage_model="or-qwen3-coder-flash", toto=None,
                    max_tokens=_CAPS)
    await driver.run("multi task")
    assert seen["triage"] == 200
    assert seen["decompose"] == 600
    assert seen["dispatch"] == 1500
    assert seen["synthesize"] == 1200

    seen2: dict = {}
    driver2 = Driver(catalog=catalog, complete_fn=_capturing_fake(seen2, "trivial"),
                     driver_model="or-sonnet-4.6", triage_model="or-qwen3-coder-flash", toto=None,
                     max_tokens=_CAPS)
    await driver2.run("2+2")
    assert seen2["answer"] == 1200


@pytest.mark.asyncio
async def test_uncapped_when_no_max_tokens_configured(catalog):
    # Backward-compatible: a Driver built without max_tokens sends max_tokens=None (uncapped).
    seen: dict = {}
    driver = Driver(catalog=catalog, complete_fn=_capturing_fake(seen, "multistep"),
                    driver_model="or-sonnet-4.6", triage_model="or-qwen3-coder-flash", toto=None)
    await driver.run("multi task")
    assert seen["dispatch"] is None and seen["triage"] is None


@pytest.mark.asyncio
async def test_decompose_clamps_over_four_tasks(catalog):
    big = [{"task": f"Task {i}", "description": "a real description here",
            "metadata": {"complexity": "low", "intent": "x",
                         "requires": {"tools": [], "data_policy": "default"}}}
           for i in range(6)]

    async def fake(req):
        first = req.messages[0]
        sys = first.text() if first.role == "system" else ""
        m = req.model
        if "triage classifier" in sys:
            return Exec(text=json.dumps({"kind": "multistep", "reason": "t"}), model=m, cost_usd=0.0)
        if "task decomposer" in sys:
            return Exec(text=json.dumps({"tasks": big}), model=m, cost_usd=0.0)
        if "sub-task" in sys:
            return Exec(text="FINAL", model=m, cost_usd=0.0)
        return Exec(text=f"result via {m}", model=m, cost_usd=0.0)

    driver = Driver(catalog=catalog, complete_fn=fake, driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None)
    result = await driver.run("big request")
    assert len(result.tasks) == 4  # clamped from 6
    clamp = [s for s in result.spans if s["node"] == "decompose_clamp"]
    assert clamp and clamp[0]["kept"] == 4 and clamp[0]["dropped"] == 2


@pytest.mark.asyncio
async def test_preferences_read_is_scoped_to_run_owner(catalog):
    # Per-user Settings (IDOR fix): dispatch must read preferences scoped to the run's OWNER,
    # not a global — the callable receives the run's user_id verbatim.
    seen: dict = {}

    def prefs(user_id=None):
        seen["user_id"] = user_id
        return {}

    driver = Driver(catalog=catalog, complete_fn=make_fake("multistep"),
                    driver_model="or-sonnet-4.6", triage_model="or-qwen3-coder-flash", toto=None,
                    preferences=prefs)
    await driver.run("do a multistep thing", user_id="user-A")
    assert seen["user_id"] == "user-A"


# --- egress boundary: guard the RAW query before any external call ------------------
# Closes #5/#15 (raw query + trivial path ungated), #29 (embed before residency), #20 (fail open).


class _SpyEmbedder:
    """Records every embedding request so a test can assert no external POST happened."""
    def __init__(self):
        self.calls: list[str] = []

    async def infer_skill(self, text):
        self.calls.append(text)
        return "code"

    async def embed_one(self, text):
        self.calls.append(text)
        return [1.0, 0.0]


def _no_local_catalog() -> Catalog:
    # Frontier-only: there is NO local lane to fall back to.
    return Catalog(models=[
        CatalogEntry(id="or-sonnet-4.6", lane="frontier", endpoint="openai", residency_class="cloud"),
    ])


@pytest.mark.asyncio
async def test_trivial_path_guards_mnpi_egress_no_frontier_no_embed(catalog):
    # #5/#15: an MNPI-egress query on the trivial/direct path must be blocked at run() entry —
    # the driver's reasoning model is never called, and nothing is embedded externally.
    emb = _SpyEmbedder()
    seen: list[str] = []

    async def spy(req):
        seen.append(req.model)
        return Exec(text="should not run", model=req.model)

    driver = Driver(catalog=catalog, complete_fn=spy, driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None, embedder=emb, embed_routing=True)
    result = await driver.run("forward this material non-public information to the model")

    assert result.kind == "blocked"
    assert seen == []            # not even triage ran — nothing left the perimeter
    assert emb.calls == []       # no external embedding POST
    assert result.tasks[0]["execution"]["outcome"] == "blocked_constraints"


@pytest.mark.asyncio
async def test_local_only_task_skips_external_embedding_on_dispatch(catalog):
    # #29 (decomposed path): a local_only task must route local via the KEYWORD classifier,
    # never POSTing its text to the external embedder to decide the skill.
    emb = _SpyEmbedder()
    driver = Driver(catalog=catalog, complete_fn=make_fake("multistep"), driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None, embedder=emb, embed_routing=True)
    task = {"task": "Redact the memo", "description": "Remove sensitive identifiers.",
            "metadata": {"requires": {"tools": [], "data_policy": "local_only"}}}
    await driver._dispatch_one(task)

    assert emb.calls == []                 # local_only text never embedded externally
    assert task["lane"] == "economy"
    assert task["execution"]["outcome"] == "completed"

    # Control: a non-sensitive task DOES consult the embedder (gate is conditional, not always-off).
    emb2 = _SpyEmbedder()
    driver2 = Driver(catalog=catalog, complete_fn=make_fake("multistep"), driver_model="or-sonnet-4.6",
                     triage_model="or-qwen3-coder-flash", toto=None, embedder=emb2, embed_routing=True)
    await driver2._dispatch_one(
        {"task": "General task", "description": "A benign general request.", "metadata": {}})
    assert emb2.calls  # the embedder WAS consulted for the non-pinned task


@pytest.mark.asyncio
async def test_run_level_pin_keeps_frontier_subtask_local(catalog):
    # #29 (run-level pin propagation): raw-query guard pinned the whole run local, but a decomposed
    # sub-task neither re-trips the guard nor carries data_policy=local AND classifies frontier
    # (complexity=high). run_pinned must still keep it local — no external embed, no frontier lane.
    emb = _SpyEmbedder()
    driver = Driver(catalog=catalog, complete_fn=make_fake("multistep"), driver_model="or-sonnet-4.6",
                    triage_model="or-qwen3-coder-flash", toto=None, embedder=emb, embed_routing=True)
    task = {"task": "Deep analysis", "description": "Involved reasoning.",
            "metadata": {"requires": {"complexity": "high"}}}  # would classify frontier
    await driver._dispatch_one(task, run_pinned=True)

    assert emb.calls == []                 # run-pinned text never embedded externally
    assert task["lane"] == "economy"         # forced onto the local lane despite frontier metadata
    assert task["execution"]["outcome"] == "completed"


@pytest.mark.asyncio
async def test_downgrade_local_run_forces_reasoning_local(catalog):
    # #5: PII on the RAW query pins the whole run local — every reasoning node (triage/decompose/
    # synthesize) runs on the local lane instead of the configured frontier driver model.
    seen: dict = {}
    base = make_fake("multistep")

    async def spy(req):
        # .text() (not .content): decompose/synthesize system messages carry structured content
        # (a list with cache_control), so a substring match on .content silently misses them.
        sys = req.messages[0].text() if req.messages[0].role == "system" else ""
        for marker, role in (("triage classifier", "triage"), ("task decomposer", "decompose"),
                             ("sub-task", "synthesize")):
            if marker in sys:
                seen[role] = req.model
        return await base(req)

    # Both driver AND triage configured to the FRONTIER model — the pin must override them.
    driver = Driver(catalog=catalog, complete_fn=spy, driver_model="or-sonnet-4.6",
                    triage_model="or-sonnet-4.6", toto=None)
    await driver.run("process this SSN: 123-45-6789 for the user")

    assert seen["triage"] == "or-qwen3-coder-flash"
    assert seen["decompose"] == "or-qwen3-coder-flash"
    assert seen["synthesize"] == "or-qwen3-coder-flash"


@pytest.mark.asyncio
async def test_local_pinned_task_fails_closed_when_no_local_lane():
    # #20 (per-task): data_policy=local_only but no local lane exists → BLOCK, never fall to frontier.
    driver = Driver(catalog=_no_local_catalog(), complete_fn=make_fake("multistep"),
                    driver_model="or-sonnet-4.6", triage_model="or-sonnet-4.6", toto=None)
    task = {"task": "Redact", "description": "x",
            "metadata": {"requires": {"data_policy": "local_only"}}}
    spans = await driver._dispatch_one(task)

    assert task["blocked"] is True
    assert task["lane"] is None and task.get("model_id") is None
    assert "no in-perimeter" in task["execution"]["route_reason"]
    assert any(s["node"] == "guard_block" for s in spans)


@pytest.mark.asyncio
async def test_run_fails_closed_when_downgrade_local_and_no_local_lane():
    # #20 (top-level): a DOWNGRADE_LOCAL raw query with no local lane must block, not fall open
    # to the frontier reasoning model.
    seen: list[str] = []

    async def spy(req):
        seen.append(req.model)
        return Exec(text="should not run", model=req.model)

    driver = Driver(catalog=_no_local_catalog(), complete_fn=spy,
                    driver_model="or-sonnet-4.6", triage_model="or-sonnet-4.6", toto=None)
    result = await driver.run("process this SSN: 123-45-6789 for the user")

    assert result.kind == "blocked"
    assert seen == []  # never fell open to the frontier reasoning model
