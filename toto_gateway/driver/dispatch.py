"""Per-task routing + execution — the decide/execute pair behind the dispatch node.

`decide_one` is the PURE routing decision (guard → residency → classify → label → kNN →
pin → residency re-check): no dispatch, no Toto writes. `dispatch_one` executes that
decision through the adapter seam and writes provenance back. `/v1/routing/decide`
serializes `decide_one`'s result — the same function on both paths, so a decision preview
can never diverge from what dispatch actually does.

Both take the Driver as their first argument (they read its wiring: guard, labels, kNN,
adapters, Toto client); `Driver._decide_one` / `Driver._dispatch_one` are the thin methods
callers and tests use.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..artifacts import make_artifact
from ..catalog import Catalog, effective_catalog
from ..pipeline import BLOCK, DOWNGRADE_LOCAL, Signal
from ..schemas import ChatCompletionRequest, Message
from . import prompts
from .classify import TaskDecision, classify
from .contracts import RouteDecision, _first_in_perimeter_model, _privacy_pinned, _safe_corpus

if TYPE_CHECKING:
    from .core import Driver


async def classify_label(driver: "Driver", text: str,
                         custom: list[dict] | None = None) -> tuple[str | None, dict | None]:
    """One haiku-class call → (verbatim vocab label, totoshape metadata), or (None, None) on ANY
    failure. None label means the fallback ladder decides — the classifier being down is never
    routing being down. Metadata is the totoshape classify block (None unless that variant is
    live); it enriches the Toto task, never the routing decision.
    Hard wall-clock cap: a HUNG provider must degrade to the fallback too, not stall the
    sub-task for the SDK's default timeout while holding an _llm slot.

    `custom` is the caller's team's invented task types [{name, desc, model}]. Their
    {name: desc} entries are appended to the classifier vocabulary FOR THIS REQUEST — both the
    LABEL_PROMPT enumeration and parse_label's accepted set — so the classifier can emit a
    custom label. No custom labels -> byte-identical to the global-vocab call."""
    labels = driver._labels.labels
    if custom:  # append team-invented {name: desc} onto the global vocab for this request only
        labels = {**labels,
                  **{c["name"]: {"desc": c.get("desc", "")} for c in custom if c.get("name")}}
    try:
        ex = await asyncio.wait_for(
            driver._llm(driver._label_model,
                        prompts.build_label_messages(text, labels),
                        name="label.classify", temperature=0.0,
                        max_tokens=driver._max_tokens.get("triage")),
            timeout=driver._label_timeout_ms / 1000.0)
        return prompts.parse_label(ex.text, sorted(labels)), prompts.parse_label_metadata(ex.text)
    except Exception:
        return None, None


def effective_catalog_for(driver: "Driver") -> Catalog:
    """Base catalog + the current caller's adoptions. The driver runs inside the request
    context, so current_identity() carries the caller resolved at auth (None for internal/test
    runs → base unchanged). This lets the driver's OWN selection — classify, label bindings,
    user pins — pick an adopted model, matching the gateway dispatch choke point.
    Cheap: no adoptions → returns the base catalog itself."""
    from ..routes.deps import current_identity
    return effective_catalog(driver.catalog, current_identity())


async def emit_task_block(driver: "Driver", t: dict, reasons: list[str]) -> list[dict]:
    """Finalize a task the residency gate refused (MNPI egress, or local-required-but-no-local-lane):
    mark it blocked, emit the guard_block span, and write Toto status/exec. Never dispatches."""
    spans: list[dict] = []
    t.update(
        blocked=True, lane=None, model_id=None, result=None,
        execution={"outcome": "blocked_constraints", "route_reason": "; ".join(reasons)},
    )
    spans.append(await driver._emit("guard_block", task=t.get("task"), reasons=reasons))
    if t.get("item_id"):
        _, s = await driver._toto(lambda: driver.toto.set_status(t["item_id"], "done"), "status")
        spans += s
        _, s = await driver._toto(
            lambda: driver.toto.write_execution(t["item_id"], t["execution"]), "exec")
        spans += s
    return spans


async def decide_one(driver: "Driver", t: dict, *, optimize: str | None = None,
                     pins: dict[str, str] | None = None, run_pinned: bool = False,
                     label_pins: dict[str, str] | None = None,
                     team_bindings: dict[str, str] | None = None,
                     team_custom: list[dict] | None = None) -> RouteDecision:
    """The pure routing decision for one task — guard → residency → classify → label → kNN →
    pin → residency re-check. May emit the label span (observability only)."""
    spans: list[dict] = []
    md = t.get("metadata") or {}
    catalog = effective_catalog_for(driver)  # selection resolves adopted models too
    prompt_text = f"{t.get('task', '')}\n\n{t.get('description', '')}".strip()
    probe = ChatCompletionRequest(
        model=driver.driver_model, messages=[Message(role="user", content=prompt_text)]
    )

    # GUARD (fail-closed) — per task, before any executor OR embedding sees it.
    verdict = driver.guard.check(probe, Signal())
    if verdict.action == BLOCK:
        return RouteDecision(None, [], None, blocked=True,
                             block_reasons=verdict.reasons, spans=spans)

    # RESIDENCY decided on the RAW task BEFORE any external call: data_policy (cheap,
    # side-effect-free) or a guard downgrade both pin the work local. Nothing sensitive may
    # leave the perimeter as a side effect of deciding how to route it.
    data_policy = (md.get("requires") or {}).get("data_policy")
    local_pinned = (data_policy in {"local_only", "local"}
                    or verdict.action == DOWNGRADE_LOCAL or run_pinned)
    # FAIL CLOSED: pinned in-perimeter but no in-perimeter model exists → block, never fall to frontier.
    if local_pinned and _first_in_perimeter_model(driver.catalog) is None:
        return RouteDecision(
            None, [], None, local_pinned=True, blocked=True,
            block_reasons=["privacy: in-perimeter handling required, no in-perimeter model available"],
            spans=spans)
    # Surface residency so the dispatch node can raise the run-level pin: otherwise synthesize
    # aggregates this local-only OUTPUT and egresses it to the frontier driver_model. Set AFTER
    # the gate above → a flagged task provably has a local lane, so the raised state flag can
    # never make _reasoning_model fall open to frontier.
    t["local_pinned"] = local_pinned

    # SKILL: embedding nearest-centroid when enabled AND egress is permitted, else keyword.
    # Local-pinned text is never POSTed to the external embedder — degrade to the keyword
    # classifier (the exact embedder-is-None path).
    skill_override = None
    if driver._embed_routing and driver._embedder is not None and not local_pinned:
        skill_override = await driver._embedder.infer_skill(prompt_text)
    # CLASSIFY metadata → executor; guard downgrade_local overrides toward local.
    dec = classify(md, catalog, driver.benchmarks, optimize, skill=skill_override)
    rejected = list(dec.rejected)  # benchmark losers; override paths append the displaced pick
    if driver._embed_routing:  # only annotate when the flag is on → flag-off is byte-identical
        dec = TaskDecision(dec.lane, dec.tools_required, dec.model_id,
                           dec.reason + f"; skill:{'embed' if skill_override else 'keyword'}",
                           dec.skill)
    # LABEL ROUTING: haiku classifier → closed-set label → labels.yaml binding displaces the
    # benchmark pick (lane AND model — the binding sets the tier). Pinned text never egresses
    # (same gate as the embedder above). Any miss — no/unknown/unbound label — leaves the
    # classify() pick standing; kNN, pins, and the residency re-check below still apply on top.
    label = None
    label_metadata = None
    user_bound = None
    team_bound = None
    if driver._labels is not None and not local_pinned:
        # team_custom expands the classifier vocab for this request (its bound models were
        # folded into team_bindings in dispatch, so a custom classification routes below).
        label, label_metadata = await classify_label(driver, prompt_text, team_custom)
        # Binding precedence for a classified label: a user's Settings override wins, then the
        # TEAM overlay (admin config), then the shipped labels.yaml default. A label the team
        # didn't set falls through to the global default; another team gets the global default.
        # All ids PUT-validated against the catalog; a stale id (since left the catalog) falls
        # through to the next tier — .get() on an unknown id returns None.
        user_bound = catalog.get((label_pins or {}).get(label) or "") if label else None
        team_bound = catalog.get((team_bindings or {}).get(label) or "") if label else None
        bound = (user_bound or team_bound
                 or (catalog.get(driver._labels.model_for(label) or "") if label else None))
        if bound is not None:
            rejected.append({"model_id": dec.model_id, "reason": "label binding outbid benchmarks"})
            origin = ":user" if user_bound else (":team" if team_bound else "")
            dec = TaskDecision(bound.lane, dec.tools_required, bound.id,
                               f"label:{label}{origin}", dec.skill)
        else:
            dec = TaskDecision(dec.lane, dec.tools_required, dec.model_id,
                               dec.reason + f"; label:{label or 'none'}:fallback", dec.skill)
        spans.append(await driver._emit("label", task=t.get("task"), label=label,
                                        model=driver._label_model,
                                        bound=bound.id if bound is not None else None))
    # EXPERIENCE-kNN: similar past tasks propose a model, overriding the benchmark pick within
    # the decided lane. Skipped for privacy lanes (privacy > kNN), for an EXPLICIT label binding
    # — a user's OR the team's — since that is deliberate intent (same authority as pins), and
    # yields to the pin below (pins > kNN). None when flag off / sparse neighbors → prior stays.
    if driver._knn is not None and not local_pinned and user_bound is None and team_bound is None:
        prop = await driver._knn.propose(prompt_text, dec.lane)
        if prop is not None:
            rejected.append({"model_id": dec.model_id, "reason": "knn outvoted"})
            dec = TaskDecision(dec.lane, dec.tools_required, prop.model_id,
                               dec.reason + f"; {prop.reason}", dec.skill)
    # User pin for this skill overrides the benchmark pick — but never a privacy lane
    # (data_policy pins beat user pins), and the guard downgrade below still beats both.
    pin = (pins or {}).get(dec.skill)
    if pin and not local_pinned:
        entry = catalog.get(pin)
        if entry is not None:
            rejected.append({"model_id": dec.model_id, "reason": "pin override"})
            dec = TaskDecision(entry.lane, dec.tools_required, entry.id,
                               dec.reason + "; pin:user", dec.skill)
    # Egress/privacy guard keys off RESIDENCY, not tier: when the run is pinned local
    # (local_pinned — propagated from the raw-query DOWNGRADE_LOCAL guard), keep this sub-task
    # in-perimeter unless it already is, landing on an in-perimeter model (never a cheap CLOUD one).
    cur = catalog.get(dec.model_id)
    if local_pinned and (cur is None or cur.residency_class != "in_perimeter"):
        perim = _first_in_perimeter_model(driver.catalog)  # local models only — base catalog
        if perim:
            entry = catalog.get(perim)
            rejected.append({"model_id": dec.model_id, "reason": "privacy guard: downgrade_local"})
            dec = TaskDecision(entry.lane, dec.tools_required, perim,
                               dec.reason + "; guard: downgrade_local", dec.skill)

    return RouteDecision(dec, rejected, label, local_pinned=local_pinned, spans=spans,
                         label_metadata=label_metadata)


async def dispatch_one(driver: "Driver", t: dict, optimize: str | None = None,
                       pins: dict[str, str] | None = None, idx: int = 0,
                       run_pinned: bool = False,
                       label_pins: dict[str, str] | None = None,
                       team_bindings: dict[str, str] | None = None,
                       team_custom: list[dict] | None = None) -> list[dict]:
    # DECIDE (pure) then EXECUTE — the decision is the same function /v1/routing/decide exposes.
    rd = await decide_one(driver, t, optimize=optimize, pins=pins, run_pinned=run_pinned,
                          label_pins=label_pins, team_bindings=team_bindings,
                          team_custom=team_custom)
    spans: list[dict] = list(rd.spans)
    if rd.blocked:
        return spans + await emit_task_block(driver, t, rd.block_reasons)
    dec, rejected, label, local_pinned = rd.dec, rd.rejected, rd.label, rd.local_pinned
    if t.get("authored"):  # orchestrator-authored task: provenance stamp. APPENDED, never
        # prefixed — _privacy_pinned keys off the head of the reason string.
        dec.reason += "; orchestrator:authored"
    classified = rd.label_metadata  # totoshape metadata → stamped onto the Toto item at write-back
    md = t.get("metadata") or {}
    # RESIDENCY × RUNNER (fail-closed, BEFORE any spawn): an in-perimeter decision — local
    # pin, data_policy, guard downgrade — must never execute on claude_code, which egresses
    # to Anthropic directly, outside the gateway's residency enforcement. The decision
    # object knows the pin; the adapter alone never would. pi is exempt: its completions
    # come back THROUGH this gateway, which enforces residency itself.
    if local_pinned and (md.get("requires") or {}).get("runner") == "claude_code":
        return spans + await emit_task_block(driver, t, [
            "privacy: in-perimeter handling required; runner claude_code egresses to Anthropic"])
    prompt_text = f"{t.get('task', '')}\n\n{t.get('description', '')}".strip()

    if t.get("item_id"):
        _, s = await driver._toto(
            lambda: driver.toto.set_status(t["item_id"], "in_progress"), "status")
        spans += s

    exreq = ChatCompletionRequest(
        model=dec.model_id,
        messages=[Message(role="system", content=prompts.EXECUTOR_PROMPT),
                  Message(role="user", content=prompt_text)],
        max_tokens=driver._max_tokens.get("dispatch"),
    )
    # The classifier chose the model; the adapter registry chooses the harness (default:
    # gateway; a task can pin claude_code/pi via metadata.requires.runner). _call adds
    # provider retry + fallback (honoring the residency/privacy boundary) and traces each try.
    try:
        ex, final_model, note = await driver._call(
            exreq, lambda r: driver._adapters.run(r, md),
            name=f"dispatch:{dec.model_id}",
            route_meta={"route_reason": dec.reason, "skill": dec.skill, "lane": dec.lane,
                        "task": t.get("task"),
                        **({"label": label} if driver._labels is not None else {})},
            privacy=_privacy_pinned(dec.reason),
        )
    except Exception as exc:  # executor died after retries+fallback (provider, stub, timeout)
        err = f"{type(exc).__name__}: {exc}"
        t.update(
            lane=dec.lane, model_id=dec.model_id, tools_required=dec.tools_required,
            result=None,
            execution={"outcome": "failed", "lane": dec.lane, "model": dec.model_id,
                       "route_reason": dec.reason, "error": err},
        )
        spans.append(await driver._emit("dispatch_error", task=t.get("task"),
                                        model=dec.model_id, error=err))
        if t.get("item_id"):
            _, s = await driver._toto(lambda: driver.toto.set_status(t["item_id"], "done"), "status")
            spans += s
            _, s = await driver._toto(
                lambda: driver.toto.write_execution(t["item_id"], t["execution"], classified), "exec")
            spans += s
        return spans

    # Record the model that ACTUALLY ran (fallback may have switched it), with the reason noted.
    final_entry = effective_catalog_for(driver).get(final_model)  # adopted models carry their lane too
    final_lane = final_entry.lane if final_entry else dec.lane
    reason = dec.reason + (f"; {note}" if note else "")
    execution = {
        "runner": ex.adapter or "gateway", "executor": ex.model, "lane": final_lane,
        "model": final_model, "tokens_prompt": ex.tokens_prompt,
        "tokens_completion": ex.tokens_completion, "tokens_cached": ex.tokens_cached,
        "cost_usd": ex.cost_usd,
        "outcome": "completed", "latency_ms": ex.latency_ms, "route_reason": reason,
        # Typed receipt (hash only, never a copy of the answer) + who lost the routing bid.
        "artifact": make_artifact("task_result", ex.text, produced_by=final_model,
                                  evidence=[final_lane, dec.skill], confidence=None),
        "rejected": rejected,
    }
    t.update(
        lane=final_lane, model_id=final_model, tools_required=dec.tools_required, result=ex.text,
        skill=dec.skill, execution=execution,
    )
    residency = final_entry.residency_class if final_entry else "frontier"
    t["residency"] = residency
    spans.append(await driver._emit("dispatch", task=t.get("task"), lane=final_lane,
                                    model=final_model, cost=ex.cost_usd, reason=reason,
                                    skill=dec.skill, residency=residency,
                                    tokens_prompt=ex.tokens_prompt,
                                    tokens_cached=ex.tokens_cached,
                                    sha256=execution["artifact"]["sha256"],
                                    n_rejected=len(rejected)))
    if t.get("item_id"):
        _, s = await driver._toto(lambda: driver.toto.set_status(t["item_id"], "done"), "status")
        spans += s
        _, s = await driver._toto(
            lambda: driver.toto.write_execution(t["item_id"], t["execution"], classified), "exec")
        spans += s
    # Experience corpus (groundwork) — fire-and-forget, never blocks or breaks the run.
    # Skipped for local-pinned tasks: their prompt_text must not be embedded externally
    # (the app-wired sink POSTs it) nor persisted to task_embeddings.
    if driver._corpus_sink is not None and not local_pinned:
        asyncio.create_task(_safe_corpus(
            driver._corpus_sink, str(idx), prompt_text, dec.skill, final_model,
            "completed", ex.cost_usd, ex.latency_ms))
    return spans
