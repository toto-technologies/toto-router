"""Puppetmaster pattern steals, driver side: typed artifact receipts on executor outputs
(STEAL 1) + persisted routing-rejection receipts (STEAL 2). No network — fake complete_fn.
"""

from __future__ import annotations

import hashlib

import pytest

from toto_gateway.benchmarks import Benchmarks
from toto_gateway.catalog import Catalog, CatalogEntry, Price
from toto_gateway.driver.classify import classify
from toto_gateway.driver.core import Driver, Exec
from toto_gateway.routes.sessions import _public_tasks


def _cat() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id="l-econ", lane="economy", endpoint="openai", residency_class="in_perimeter"),
        CatalogEntry(id="f-frontier", lane="frontier", endpoint="openai", residency_class="cloud"),
    ])


def _driver(cat) -> Driver:
    async def complete(req):  # echoes the model that ran so the hash is predictable
        return Exec(text=f"result via {req.model}", model=req.model, lane="", cost_usd=0.0)

    return Driver(catalog=cat, complete_fn=complete, driver_model="f-frontier",
                  triage_model="l-econ", toto=None)


# --- STEAL 1: typed artifact ------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_attaches_artifact_hash():
    d = _driver(_cat())
    t = {"task": "do work", "description": "a task", "metadata": {"complexity": "high"}}
    await d._dispatch_one(t)
    art = t["execution"]["artifact"]
    assert art["type"] == "task_result"
    assert art["sha256"] == hashlib.sha256(t["result"].encode("utf-8")).hexdigest()
    assert art["produced_by"] == t["model_id"]  # the model that actually ran


# --- STEAL 2: classify rejected receipts ------------------------------------

def _bench_cat() -> Catalog:
    return Catalog(models=[
        CatalogEntry(id="f-strong", lane="frontier", endpoint="openai", residency_class="cloud",
                     upstream_model="strong", price_usd_per_1k=Price(prompt=0.001, completion=0.001)),
        CatalogEntry(id="f-weak", lane="frontier", endpoint="openai", residency_class="cloud",
                     upstream_model="weak", price_usd_per_1k=Price(prompt=0.001, completion=0.001)),
        CatalogEntry(id="l-econ", lane="economy", endpoint="openai", residency_class="in_perimeter"),
    ])


def test_classify_populates_rejected_from_benchmarks():
    bench = Benchmarks(models={"strong": {"reasoning": 0.9}, "weak": {"reasoning": 0.5}})
    d = classify({"complexity": "high", "intent": "analyze the market"}, _bench_cat(), bench, "quality")
    assert d.model_id == "f-strong"  # higher reasoning score wins
    assert len(d.rejected) == 1
    assert d.rejected[0]["model_id"] == "f-weak"
    assert "lost on benchmark score" in d.rejected[0]["reason"]


def test_classify_rejected_empty_without_benchmarks():
    d = classify({"complexity": "high", "intent": "analyze the market"}, _bench_cat())
    assert d.rejected == []


# --- STEAL 2: override path appends the displaced pick ----------------------

@pytest.mark.asyncio
async def test_guard_downgrade_records_rejection():
    d = _driver(_cat())
    # SSN in the prompt → guard DOWNGRADE_LOCAL; complexity=high → classify picks frontier.
    t = {"task": "Handle SSN 123-45-6789", "description": "process it",
         "metadata": {"complexity": "high"}}
    await d._dispatch_one(t)
    assert t["lane"] == "economy" and t["model_id"] == "l-econ"  # guard forced local
    assert {"model_id": "f-frontier", "reason": "privacy guard: downgrade_local"} \
        in t["execution"]["rejected"]


# --- Surface: _public_tasks carries both fields -----------------------------

def test_public_tasks_exposes_artifact_and_rejected():
    tasks = [
        {"task": "a", "execution": {"outcome": "completed",
                                    "artifact": {"type": "task_result", "sha256": "abc"},
                                    "rejected": [{"model_id": "x", "reason": "pin override"}]}},
        {"task": "b", "execution": {"outcome": "blocked_constraints"}},  # no artifact/rejected
    ]
    out = _public_tasks(tasks)
    assert out[0]["artifact"]["sha256"] == "abc"
    assert out[0]["rejected"] == [{"model_id": "x", "reason": "pin override"}]
    assert out[1]["artifact"] is None and out[1]["rejected"] == []
