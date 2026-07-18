"""Sessions launcher service — runs a driver run end-to-end for any caller.

Extracted from routes/sessions.py so the companion can import it at module top: this module
never imports routes (or companion), so the old "lazy import inside the tool handler" dance
that kept routes → companion one-way is gone. routes/sessions.py re-exports everything here
under its historical underscore names for existing callers and tests.

execute_run drives one run to a terminal event (run_done or run_failed, never silence) and
writes the session card + recall capture; track_run keeps the strong task refs / run_ids that
graceful drain and the cooperative-cancel plane work off.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import secrets
import time

from .runs import CURRENT_RUN_ID, RunStore

# In-flight driver runs: strong task refs (so runs aren't GC'd mid-flight) + their run_ids (so
# graceful drain can fail the stragglers by id). Both are cleared when the task completes.
# Mutated IN PLACE only — routes/sessions.py (drain/statusz) and tests import these very objects.
_live_runs: set[asyncio.Task] = set()
_live_run_ids: set[str] = set()
# Cooperative interrupt (voice-agent plan): run_id → spoken_chars the client actually PLAYED.
# Presence = a cancel was requested; the companion loop reads it at each node boundary
# (cancel_boundary) and finishes the turn truncated-to-spoken. In-proc, this-replica only — a
# companion chat turn always runs on the replica that took its POST (same as _live_runs). Cleared
# when the run's task completes (done-callback below), so a stale flag never leaks to a reused id.
_cancels: dict[str, int] = {}


def track_run(task: asyncio.Task, run_id: str) -> None:
    _live_runs.add(task)
    _live_run_ids.add(run_id)
    task.add_done_callback(_live_runs.discard)
    task.add_done_callback(lambda _t: _live_run_ids.discard(run_id))
    task.add_done_callback(lambda _t: _cancels.pop(run_id, None))


def request_cancel(run_id: str, spoken_chars: int) -> None:
    """Flag a live run for cooperative cancellation at its next node boundary. Idempotent —
    re-flagging just updates the spoken boundary. No-op safety: a run that never checks the flag
    (already terminal) simply ignores it, and track_run clears it when the task ends."""
    _cancels[run_id] = max(0, spoken_chars)


def cancel_boundary(run_id: str) -> int | None:
    """The spoken_chars boundary if this run was flagged for cancel, else None. Read at the top of
    the companion's agent/tools nodes — does NOT clear (run() clears once, after the graph)."""
    return _cancels.get(run_id)


def clear_cancel(run_id: str) -> None:
    _cancels.pop(run_id, None)


async def _write_card(store: RunStore, driver, row: dict, query: str, answer: str) -> None:
    """Write/revise the conversation's persistent card. Keyed by conv_id so every turn upserts the
    SAME canvas_objects row (created_at preserved, updated_at bumped, put_object) — the board never
    flickers. Turn 1 stores the answer verbatim (0 LLM calls); a continued turn pays ONE revise
    call that merges the prior card summary with the new turn's result."""
    conv_id = row.get("conv_id") or row["run_id"]
    turn = row.get("turn") or 1
    user_id = row.get("user_id")
    title = " ".join(query.split())[:80] or "toto session"
    summary = answer
    if turn > 1:
        prior = next((o for o in await store.get_objects("card", user_id=user_id)
                      if o["object_id"] == conv_id), None)
        if prior is not None:  # revise the draft; preserve the conversation's title
            summary = await driver.revise_card(prior["payload"].get("summary", ""), answer)
            title = prior["payload"].get("title") or title
    await store.put_object("card", conv_id, {
        "title": title, "summary": summary, "run_id": conv_id,
        "turn": turn, "updated_at": time.time()}, user_id=user_id)


async def write_document(store: RunStore, *, user_id: str, title: str, body: str,
                         provenance: str, run_id: str) -> dict:
    """Write one markdown document: bytes into the residency-flexible ObjectStore (filesystem
    in-perimeter, S3-compatible when TOTO_GW_S3_* is set) + the index row that makes it listable.
    The ONE writer both producers share — the session-completion sink below and the companion's
    save_document tool. Returns {doc_id, title, bytes} for receipts/chips. The store instance
    comes from the resolver (per-save, not a hot path): the user's org connector (BYOS) when one
    is enabled, else the platform store — reads (routes/documents.py) resolve identically."""
    from .storage import resolve_store_for_user

    title = " ".join(title.split())[:80] or "toto document"
    day = datetime.date.today().isoformat()
    markdown = (f"# {title}\n\n"
                f"*{provenance} · {day} · session {run_id}*\n\n"
                f"{body}")
    data = markdown.encode("utf-8")
    doc_id = secrets.token_hex(6)  # 12 hex chars
    key = f"documents/{doc_id}.md"
    (await resolve_store_for_user(user_id)).put(user_id, key, data, "text/markdown")
    await store.document_create(doc_id, user_id, run_id, title, key,
                                hashlib.sha256(data).hexdigest(), len(data))
    return {"doc_id": doc_id, "title": title, "bytes": len(data)}


async def _save_document(store: RunStore, run_id: str, query: str, answer: str,
                         user_id: str) -> None:
    """The session-completion sink — a THIRD post-completion sink beside the card / recall ones;
    the caller wraps it in try/except so a storage failure never fails the session."""
    await write_document(store, user_id=user_id, title=query, body=answer,
                         provenance="Generated by a Toto work session", run_id=run_id)


async def execute_run(store: RunStore, driver, run_id: str, query: str, optimize: str | None,
                      history: list[dict] | None = None, kind: str | None = None,
                      user_id: str | None = None, tasks: list[dict] | None = None) -> None:
    """Run the driver with spans addressed to this session. Always terminates the event
    stream — run_done or run_failed, never silence. `kind` pre-triages the run (companion
    spawns force 'multistep' — the spawn decision IS triage). `tasks` (optional, already
    validated) are orchestrator-authored: the driver skips decompose and dispatches them."""
    token = CURRENT_RUN_ID.set(run_id)
    try:
        result = await driver.run(query, optimize=optimize, history=history, kind=kind,
                                  user_id=user_id, tasks=tasks)
        cost = 0.0
        for t in result.tasks:
            c = (t.get("execution") or {}).get("cost_usd")
            if isinstance(c, (int, float)):
                cost += c
        catalog = getattr(driver, "catalog", None)
        await store.finish(run_id, status="done", answer=result.answer,
                     tasks=_public_tasks(result.tasks, catalog),
                     cost_total=None if not cost else cost)
        row = await store.get_session(run_id)  # conv_id + turn + owner, for the card and memory
        # Revisable session card: reuse put_object's atomic upsert on canvas_objects, keyed by
        # conv_id (stable across a conversation's turns — run_id changes each turn). Turn 1 stores
        # the answer verbatim (no extra LLM); a continued turn REVISES the prior card in place.
        if result.answer and row is not None:
            await _write_card(store, driver, row, query, result.answer)
        # Save the synthesized result as a downloadable markdown document (residency-flexible store
        # + index row). Third post-completion sink; a storage failure must never fail the session.
        doc_user = (row or {}).get("user_id")
        if result.answer and doc_user:
            try:
                await _save_document(store, run_id, query, result.answer, doc_user)
            except Exception:
                # Swallowed on purpose (a storage failure never fails the session) but LOUD:
                # a silent sink made a prod permission bug invisible for three deploy cycles.
                import logging

                logging.getLogger("toto_gateway").warning(
                    "document save failed for run %s", run_id, exc_info=True)
        # Feed the RECALL plane: a completed work session's {query, answer summary} becomes
        # long-term memory the companion can recall later. No-op unless memory is on; scoped to
        # the run's owner. Fire-and-forget (the adapter swallows all failure).
        memory = getattr(driver, "memory", None)
        if memory is not None and result.answer:
            summary = f"Work session: {query}\n\nOutcome:\n{result.answer[:2000]}"
            asyncio.create_task(memory.capture((row or {}).get("user_id"), summary,
                                               {"type": "session"}))
            # Post-capture distiller (memory-lifecycle P0): session-end always distils (D1).
            # Gated + fire-and-forget in maybe_extract; no-op when extraction is off.
            extractor = getattr(driver, "extractor", None)
            if extractor is not None:
                extractor.maybe_extract((row or {}).get("user_id"), summary,
                                        session_end=True, source_run=run_id)
    except Exception as exc:
        await store.finish(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        CURRENT_RUN_ID.reset(token)


def _provider_label(entry) -> str:
    """Human name for where the task actually ran — 'OpenRouter' for the OR base_url, the
    host for any other OpenAI-compatible upstream, the provider keyword otherwise."""
    if entry.endpoint == "fake":
        return "fake (offline)"
    # base_url when set (OpenAI-compatible frontier); for local entries the base URL IS endpoint.
    base = entry.base_url or (entry.endpoint if entry.endpoint.startswith("http") else "")
    if "openrouter.ai" in base:
        return "OpenRouter"
    if base:
        from urllib.parse import urlparse

        return urlparse(base).hostname or base
    return {"anthropic": "Anthropic", "openai": "OpenAI"}.get(entry.endpoint, entry.endpoint)


def _model_detail(catalog, model_id: str | None) -> dict:
    """Catalog-derived provenance for a routed model_id: the upstream model actually called,
    the provider/gateway, and the $/1k rates the run was priced at. Empty when unresolvable
    (blocked tasks have no model_id; older stored sessions predate these fields)."""
    entry = catalog.get(model_id) if (catalog and model_id) else None
    if entry is None:
        return {"upstream_model": None, "provider": None,
                "rate_prompt_per_1k": None, "rate_completion_per_1k": None}
    return {
        "upstream_model": entry.effective_upstream_model,
        "provider": _provider_label(entry),
        "rate_prompt_per_1k": entry.price_usd_per_1k.prompt,
        "rate_completion_per_1k": entry.price_usd_per_1k.completion,
    }


def _public_tasks(tasks: list[dict], catalog=None) -> list[dict]:
    """The snapshot shape the UI renders — provenance + result, no internal fields."""
    out = []
    for i, t in enumerate(tasks):
        ex = t.get("execution") or {}
        out.append({
            "task_id": str(i), "task": t.get("task", ""), "blocked": bool(t.get("blocked")),
            "lane": t.get("lane"), "model_id": t.get("model_id"), "result": t.get("result"),
            "skill": t.get("skill", "general"),
            "residency": t.get("residency", "frontier"),
            "cost_usd": ex.get("cost_usd"), "latency_ms": ex.get("latency_ms"),
            "tokens_prompt": ex.get("tokens_prompt"), "tokens_completion": ex.get("tokens_completion"),
            "tokens_cached": ex.get("tokens_cached"),
            "route_reason": ex.get("route_reason"), "outcome": ex.get("outcome"),
            "artifact": ex.get("artifact"), "rejected": ex.get("rejected") or [],
            **_model_detail(catalog, t.get("model_id")),
        })
    return out
