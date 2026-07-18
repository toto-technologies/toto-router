"""Routing-decision API — the gateway's context-aware routing as a product surface.

Three read-only surfaces over the SAME decision the driver executes (never a second router):

  POST /v1/routing/decide   — messages/task in → decision out, WITHOUT executing (the flagship).
  GET  /v1/routing/catalog  — the routable model set + the properties routing actually uses.
  POST /v1/routing/explain  — the recorded decision for a past run_id (own runs only).

`decide` calls Driver._decide_one — literally the function _dispatch_one runs before it executes —
so a preview can never diverge from what /v1/route picks for the same input. Feedback/outcomes are
NOT here: POST /v1/feedback already owns that plane. See docs/plans/2026-07-07-routing-api.md.
"""

from __future__ import annotations

import inspect

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ..benchmarks import OPTIMIZE, Benchmarks
from ..catalog import Catalog, CatalogEntry
from .deps import Identity, require_auth

router = APIRouter()

MAX_BATCH = 32


def _error(status: int, message: str, err_type: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": err_type}})


# --- request models ----------------------------------------------------------


class DecideInput(BaseModel):
    """One routing input. `messages` (OpenAI shape) OR `metadata` (structured task) — at least
    one must carry text/signal. extra allowed so an OpenAI client's other knobs are ignored."""

    model_config = ConfigDict(extra="allow")
    messages: list[dict] | None = None
    optimize: str | None = None
    metadata: dict | None = None


class DecideBody(DecideInput):
    """Single input (top-level fields) OR a batch (`inputs[]`, ≤ MAX_BATCH)."""

    inputs: list[DecideInput] | None = None


class ExplainBody(BaseModel):
    run_id: str


# --- serialization helpers ---------------------------------------------------


def _last_user_text(messages: list[dict] | None) -> str:
    for m in reversed(messages or []):
        if (m or {}).get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else ("".join(
                p.get("text", "") for p in c if isinstance(p, dict)) if isinstance(c, list) else "")
    return ""


def _build_task(inp: DecideInput) -> tuple[dict | None, str | None]:
    """(task_dict, error). task text drives the label classifier; metadata drives classify()."""
    text = _last_user_text(inp.messages).strip()
    md = inp.metadata if isinstance(inp.metadata, dict) else None
    if not text and md is None:
        return None, "empty input: provide non-empty messages or metadata"
    return {"task": text, "description": "", "metadata": md or {"scope": text}}, None


def _estimate(entry: CatalogEntry | None, skill: str, benchmarks: Benchmarks) -> dict | None:
    """Routing economics from the live catalog + offline benchmarks. Latency is not modeled
    offline (no column in benchmarks.yaml) — omitted rather than fabricated."""
    if entry is None:
        return None
    return {
        "prompt_usd_per_1k": entry.price_usd_per_1k.prompt,
        "completion_usd_per_1k": entry.price_usd_per_1k.completion,
        "blended_usd_per_1k": round(benchmarks.price(entry), 6),
        "context_window": entry.context_window,
        "benchmark_score": round(benchmarks.score(entry.effective_upstream_model, skill), 4),
    }


def _confidence(reason: str | None) -> str:
    """Coarse, honest — derived from the reason class, not a calibrated probability. `high` when a
    hard rule fixed the pick (privacy, guard, a user pin/label binding, complexity=high); `low` for
    the no-signal default; `medium` otherwise. Upgrade path: surface the benchmark score margin."""
    r = reason or ""
    if (r.startswith("privacy") or "; guard" in r or ":user" in r
            or "pin:user" in r or "complexity=high" in r):
        return "high"
    if "economy: default" in r:
        return "low"
    return "medium"


def _catalog_ref(catalog: Catalog, benchmarks: Benchmarks) -> dict:
    return {"models": [e.id for e in catalog.models], "benchmarks_asof": benchmarks.asof or None}


def _serialize_decision(rd, *, catalog: Catalog, benchmarks: Benchmarks) -> dict:
    ref = _catalog_ref(catalog, benchmarks)
    if rd.blocked:
        return {
            "object": "routing.decision", "blocked": True, "model": None, "lane": None,
            "residency_class": None, "skill": None, "label": None,
            "route_reason": "; ".join(rd.block_reasons), "reasons": list(rd.block_reasons),
            "confidence": "high", "estimate": None, "alternatives": [], "catalog_ref": ref,
        }
    dec = rd.dec
    entry = catalog.get(dec.model_id)
    alternatives = []
    for r in rd.rejected:
        ae = catalog.get(r.get("model_id"))
        alternatives.append({
            "model": r.get("model_id"), "lane": ae.lane if ae else None,
            "why_not": r.get("reason"), "estimate": _estimate(ae, dec.skill, benchmarks),
        })
    return {
        "object": "routing.decision", "blocked": False, "model": dec.model_id, "lane": dec.lane,
        "residency_class": entry.residency_class if entry else None, "skill": dec.skill,
        "label": rd.label, "route_reason": dec.reason,
        "reasons": [p for p in (dec.reason or "").split("; ") if p],
        "confidence": _confidence(dec.reason), "estimate": _estimate(entry, dec.skill, benchmarks),
        "alternatives": alternatives, "catalog_ref": ref,
    }


# --- endpoints ---------------------------------------------------------------


async def _prefs(driver, user_id: str | None) -> dict:
    """Per-user routing prefs (pins/label_models), scoped to the caller — same read dispatch does."""
    try:
        p = driver._preferences(user_id)
        if inspect.isawaitable(p):
            p = await p
        return p or {}
    except Exception:
        return {}


@router.post("/v1/routing/decide")
async def decide(body: DecideBody, request: Request, identity: Identity = Depends(require_auth)):
    driver = getattr(request.app.state, "driver", None)
    if driver is None:
        return _error(503, "driver disabled — start the gateway with TOTO_GW_DRIVER=1", "config_error")

    batch = body.inputs is not None
    inputs = body.inputs if batch else [DecideInput(messages=body.messages,
                                                    optimize=body.optimize, metadata=body.metadata)]
    if batch:
        if not inputs:
            return _error(400, "inputs must be a non-empty array", "invalid_request_error")
        if len(inputs) > MAX_BATCH:
            return _error(400, f"batch too large: {len(inputs)} > {MAX_BATCH}", "invalid_request_error")

    prefs = await _prefs(driver, identity.user_id)
    pins, label_pins = prefs.get("pins") or {}, prefs.get("label_models") or {}

    decisions = []
    for inp in inputs:
        optimize = inp.optimize or prefs.get("optimize")
        if optimize is not None and optimize not in OPTIMIZE:
            return _error(400, f"optimize must be one of {OPTIMIZE}", "invalid_request_error")
        task, err = _build_task(inp)
        if err:
            return _error(400, err, "invalid_request_error")
        rd = await driver._decide_one(task, optimize=optimize, pins=pins,
                                      run_pinned=False, label_pins=label_pins)
        # Serialize through the same effective catalog the decision was made with — an adopted
        # model that wins must carry its residency/estimate instead of nulling out.
        decisions.append(_serialize_decision(rd, catalog=driver._effective_catalog(),
                                             benchmarks=driver.benchmarks))

    return {"decisions": decisions} if batch else decisions[0]


@router.get("/v1/routing/catalog")
async def catalog(request: Request, _identity: Identity = Depends(require_auth)):
    driver = getattr(request.app.state, "driver", None)
    if driver is None:
        return _error(503, "driver disabled — start the gateway with TOTO_GW_DRIVER=1", "config_error")
    cat: Catalog = driver._effective_catalog()  # the caller's view: base + their adoptions
    bench: Benchmarks = driver.benchmarks
    labels = dict(getattr(driver._labels, "labels", {}) or {}) if driver._labels else {}
    binding = {label: (row or {}).get("model") for label, row in labels.items()}  # label -> model|None
    served: dict[str, list[str]] = {}
    for label, model in binding.items():
        if model:
            served.setdefault(model, []).append(label)

    models = []
    for e in cat.models:
        um = e.effective_upstream_model
        models.append({
            "id": e.id, "lane": e.lane, "residency_class": e.residency_class,
            "real": e.endpoint != "fake", "endpoint": e.endpoint, "context_window": e.context_window,
            "upstream_model": um,
            "price_usd_per_1k": {"prompt": e.price_usd_per_1k.prompt,
                                 "completion": e.price_usd_per_1k.completion},
            "blended_usd_per_1k": round(bench.price(e), 6),
            "benchmarks": {s: round(bench.score(um, s), 4) for s in ("code", "reasoning", "general")},
            "labels_served": sorted(served.get(e.id, [])),
        })
    return {"object": "routing.catalog", "benchmarks_asof": bench.asof or None,
            "models": models, "labels": binding}


def _explain_task(t: dict) -> dict:
    ex = t.get("execution") or {}
    reason = ex.get("route_reason") or ""
    return {
        "task": t.get("task"), "model": t.get("model_id") or ex.get("model"),
        "lane": t.get("lane") or ex.get("lane"), "route_reason": reason,
        "reasons": [p for p in reason.split("; ") if p],
        "alternatives": [{"model": r.get("model_id"), "why_not": r.get("reason")}
                         for r in (ex.get("rejected") or [])],
        "outcome": ex.get("outcome"), "blocked": bool(t.get("blocked")),
    }


@router.post("/v1/routing/explain")
async def explain(body: ExplainBody, request: Request, identity: Identity = Depends(require_auth)):
    store = getattr(request.app.state, "runs", None)
    if store is None:
        return _error(503, "driver disabled — start the gateway with TOTO_GW_DRIVER=1", "config_error")
    if not body.run_id.strip():
        return _error(400, "run_id required", "invalid_request_error")
    # Scoped to the caller's own runs (fail-closed IDOR): another user's run_id → 404, never a leak.
    session = await store.get_session(body.run_id, user_id=identity.user_id)
    if session is None:
        return _error(404, f"unknown run {body.run_id!r}", "not_found")
    return {
        "object": "routing.explanation", "run_id": body.run_id,
        "kind": session.get("kind") or session.get("lane"),
        "optimize": (session.get("optimize")),
        "tasks": [_explain_task(t) for t in (session.get("tasks") or [])],
    }
