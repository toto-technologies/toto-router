"""Binding precedence: an explicit binding governs ALL traffic for its label (tools or not); the
optimizer (benchmark_best) is demoted to an advisor and only routes UNBOUND labels. A per-policy
escape hatch restores the old benchmark-steers-tools behavior. The tools guard still applies after
precedence — a bound non-tool model takes the guard/fallback, never a silent benchmark override."""

from __future__ import annotations

import json
import types

import pytest

from toto_gateway.catalog import Catalog, CatalogEntry
from toto_gateway.routing.labels import LabelBindings
from toto_gateway.routing.smart import smart_route

# code_generation is globally bound to a NON-tool model (the crux: tools traffic used to silently
# benchmark-override it); analysis is unbound (model: null) but has a category → benchmark best.
_LABELS = LabelBindings(_raw={"labels": {
    "code_generation": {"model": "bound-notools", "desc": "write code", "category": "coding"},
    "analysis": {"model": None, "desc": "analyze data", "category": "coding"},
    "other": {"model": "generalist", "desc": "none of the above"},
}})


def _cat() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id="bound-notools", lane="economy", endpoint="fake", residency_class="cloud", tools=False),
        CatalogEntry(id="bound-tools", lane="economy", endpoint="cloud", residency_class="cloud", tools=True),
        CatalogEntry(id="bench-model", lane="frontier", endpoint="cloud", residency_class="cloud", tools=True),
        CatalogEntry(id="generalist", lane="frontier", endpoint="cloud", residency_class="cloud", tools=True),
    ])


class _Bench:
    """benchmark_best for the 'coding' skill → bench-model (a tools-capable non-fake entry)."""

    def best(self, entries, skill, optimize="balanced"):
        return next((e for e in entries if e.id == "bench-model"), None)


def _policy(**kw):
    return types.SimpleNamespace(
        label_bindings=kw.get("bindings", {}), custom_labels=[], optimize=None,
        optimizer_steers_tools=kw.get("steer", False), taxonomy={})


def _classify(label):
    async def fn(messages, model_id):
        return json.dumps({"label": label})
    return fn


async def _route(label, *, bindings, require_tools, steer=False, text=""):
    return await smart_route(
        text or f"do some {label} {require_tools}{steer}", catalog=_cat(), labels=_LABELS,
        benchmarks=_Bench(), classifier_model="generalist",
        policy=_policy(bindings=bindings, steer=steer),
        classify_fn=_classify(label), timeout_s=1.0, require_tools=require_tools)


@pytest.mark.asyncio
async def test_bound_label_with_tools_routes_to_binding():
    # code_generation bound to a TOOLS-capable model; a tools request routes to the binding, not
    # the benchmark best — and the advisor (bench-model, which differs) is recorded.
    r = await _route("code_generation", bindings={"code_generation": "bound-tools"}, require_tools=True)
    assert r.model_id == "bound-tools"
    assert r.route_reason == "label:code_generation:team"
    assert (r.label_metadata or {}).get("benchmark_pick") == "bench-model"


@pytest.mark.asyncio
async def test_unbound_label_with_tools_routes_benchmark_best():
    # analysis has no binding anywhere → the optimizer governs: benchmark_best on its category.
    r = await _route("analysis", bindings={}, require_tools=True)
    assert r.model_id == "bench-model"
    assert r.route_reason == "label:analysis:benchmark_best:coding"


@pytest.mark.asyncio
async def test_bound_nontool_model_takes_tools_guard_not_benchmark():
    # code_generation bound to a NON-tool model + a tools request. Default (steer off): the binding
    # stands as intent, the tools guard picks a tools-capable fallback, reason records the guard —
    # NOT a silent benchmark override.
    r = await _route("code_generation", bindings={"code_generation": "bound-notools"}, require_tools=True)
    assert r.model_id == "generalist"                      # tools-capable fallback (the 'other' binding)
    assert r.route_reason == "label:code_generation:tools_guard"


@pytest.mark.asyncio
async def test_escape_hatch_restores_benchmark_for_tools():
    # Same as above but the policy toggles the optimizer back on for tool traffic → old behavior.
    r = await _route("code_generation", bindings={"code_generation": "bound-notools"},
                     require_tools=True, steer=True)
    assert r.model_id == "bench-model"
    assert r.route_reason == "label:code_generation:benchmark_best:coding"


@pytest.mark.asyncio
async def test_bound_label_no_tools_needed_still_routes_to_binding():
    # No tools on the request: a non-tool bound model is served directly (tools guard never fires).
    r = await _route("code_generation", bindings={"code_generation": "bound-notools"}, require_tools=False)
    assert r.model_id == "bound-notools"
    assert r.route_reason == "label:code_generation:team"


@pytest.mark.asyncio
async def test_benchmark_pick_absent_when_it_matches_binding():
    # Binding == the benchmark best → no advisor noise recorded.
    r = await _route("code_generation", bindings={"code_generation": "bench-model"}, require_tools=True)
    assert r.model_id == "bench-model"
    assert "benchmark_pick" not in (r.label_metadata or {})
