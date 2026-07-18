"""Memory extraction — the post-capture distiller (docs/plans/2026-07-05-memory-lifecycle.md, P0).

Raw captures (a chat turn, a session outcome) already land in the recall plane. This turns them
into DURABLE TYPED FACTS in the declared plane (user_memory), written through the SAME path the
companion's memory_write tool uses — caps, eviction, and whole-block injection at wake stay intact
(D1). Episodic detail is NOT re-stored here: it already lives as the raw capture (D4, no double-
storage). The service mirrors lists.enrich_list_items — one gateway.complete() call, cost-tracked
(harness='memory' → receipts/metering), then a dedupe gate before any write.

Dedupe is the load-bearing bit (the plan's words): extraction must CONVERGE with declared memory,
not flood it. Two cheap passes, no extra LLM:
  1. against the ≤100 injected user_memory rows — a re-run of the same conversation produces the
     same candidates, which now match existing rows and are dropped (re-extraction is a no-op).
  2. against one recall() over the source text — catches a fact already distilled-and-captured
     elsewhere. Symmetric token overlap (content.token_sim) so a long raw capture that merely
     CONTAINS the fact's words does not falsely reject a genuinely-new fact.

Fire-and-forget off the hot path: MemoryExtractor.maybe_extract gates cadence + per-user daily
budget and create_task's the run, holding a strong ref (asyncio keeps only a weak one — the same
GC footgun content.ContentIndexer just fixed). Everything degrades to a no-op: a garbage model
reply parses to [], a recall outage drops pass 2, a full memory refuses the write. Never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from . import tool_scopes
from .content import token_sim
from .runs import MEMORY_KINDS
from .schemas import ChatCompletionRequest, Message

log = logging.getLogger("toto_gateway.memory_extract")

_MAX_FACTS = 5        # cap writes per pass — bounds churn on a chatty turn
_SOURCE_CHARS = 4000  # trim the source text fed to the model (a turn/outcome, not a transcript)

EXTRACT_SYSTEM = """You distil DURABLE FACTS about a user from a snippet of their conversation or
a work-session outcome, for a long-term memory. Return ONLY a JSON array — no prose.

Each element is {"kind": "preference|fact|context|instruction", "content": "<one durable fact>"}:
- preference: a standing taste or choice ("prefers oat milk", "likes terse answers")
- fact: a stable truth about them ("name is Alex", "runs an art business called ArtFunk")
- context: lasting situational background ("is planning a Tahoe offsite in Q3")
- instruction: an explicit standing directive to you ("always cc her assistant")

Rules:
- ONE fact per element, phrased so it stands alone without the conversation.
- Only what is worth remembering for months: identity, standing preferences, lasting context,
  explicit instructions. NOT transcript summaries, NOT one-off task details, NOT your own actions.
- Do NOT store sensitive categories (health, politics, religion, finances) unless the user states
  one as a standing instruction.
- Nothing durable in the snippet → return []. Prefer [] over a weak guess.

Example → [{"kind": "fact", "content": "name is Alex"}, {"kind": "preference", "content": "wants concise replies"}]"""


def _parse_facts(text: str) -> list[dict]:
    """First bracketed JSON array of {kind, content} objects → validated list, else []. Tolerant:
    finds the array inside any surrounding prose/fences, drops malformed or unknown-kind entries."""
    m = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for d in data:
        if not isinstance(d, dict):
            continue
        kind, content = d.get("kind"), d.get("content")
        if kind in MEMORY_KINDS and isinstance(content, str) and content.strip():
            out.append({"kind": kind, "content": " ".join(content.split())})
    return out


def _is_dup(content: str, existing: list[str], sim: float) -> bool:
    """A candidate duplicates something we already know. Symmetric token overlap ≥ sim (the main
    signal, length-robust). PLUS substring-either-direction, but ONLY between comparable-length
    strings — a short fact IS a substring of a long raw capture that merely mentions it, so an
    unguarded substring test would falsely reject a genuinely-new fact against a recall hit."""
    low = content.lower()
    for e in existing:
        el = (e or "").lower()
        if not el:
            continue
        if token_sim(content, e) >= sim:
            return True
        if (low in el or el in low) and max(len(low), len(el)) <= 3 * min(len(low), len(el)):
            return True
    return False


async def extract_memories(text: str, *, user_id: str | None, gateway, runs, memory,
                           model: str, dedupe_sim: float = 0.85,
                           source_run: str = "extract") -> dict:
    """Distil `text` into durable typed facts and write the survivors to user_memory. Returns a
    small receipt {written, skipped, cost_usd, candidates} for metering/tests. Pure service (the
    trigger + task hygiene live in MemoryExtractor); never raises — degrades to a no-op."""
    text = (text or "").strip()
    if not text or not user_id:
        return {"written": [], "skipped": 0, "cost_usd": 0.0, "candidates": 0}
    # Scope enforcement (stamped decision 4): extract may ONLY write memory. If an operator narrows
    # the extract scope (removes memory_write), extraction becomes a no-op — before any LLM spend.
    if "memory_write" not in tool_scopes.effective_scope("extract"):
        return {"written": [], "skipped": 0, "cost_usd": 0.0, "candidates": 0}
    req = ChatCompletionRequest(model=model, messages=[
        Message(role="system", content=EXTRACT_SYSTEM),
        Message(role="user", content=text[:_SOURCE_CHARS]),
    ])
    try:
        res = await gateway.complete(req, harness="memory")
        raw = res.response.choices[0].message.content if res.response.choices else ""
        cost = res.trace.cost_usd or 0.0
    except Exception:
        log.debug("extract: gateway.complete failed (degrade-to-off)", exc_info=True)
        return {"written": [], "skipped": 0, "cost_usd": 0.0, "candidates": 0}

    cands = _parse_facts(raw)[:_MAX_FACTS]
    if not cands:
        return {"written": [], "skipped": 0, "cost_usd": cost, "candidates": 0}

    # Dedupe corpus: the declared rows (pass 1) + one recall over the source (pass 2, degradable).
    try:
        seen = [r["content"] for r in await runs.memory_rows(user_id)]
    except Exception:
        seen = []
    if memory is not None:
        try:
            seen += [h.get("text") or "" for h in await memory.recall(user_id, text[:500])]
        except Exception:
            log.debug("extract: recall dedupe unavailable — exact-match only", exc_info=True)

    written, skipped = [], 0
    for c in cands:
        if _is_dup(c["content"], seen, dedupe_sim):
            skipped += 1
            continue
        r = await runs.memory_write(user_id, c["kind"], c["content"], source_run=source_run)
        if "error" in r:  # caps refused it (e.g. only preference/instruction left) — surface nothing
            skipped += 1
            continue
        written.append(r["memory_id"])
        seen.append(c["content"])  # within-batch dedupe: two candidates that collapse to one fact
    return {"written": written, "skipped": skipped, "cost_usd": cost, "candidates": len(cands)}


def _utc_date() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class MemoryExtractor:
    """The trigger seam wired at app boot when TOTO_GW_MEMORY_EXTRACT=1 (attached to the companion
    and the driver). maybe_extract() gates cadence + a per-user daily budget, then fires the
    service fire-and-forget with a strong task ref. Present == enabled, so callers just null-check."""

    def __init__(self, *, gateway, runs, memory, model: str, every: int = 6,
                 dedupe_sim: float = 0.85, daily_usd: float = 0.25) -> None:
        self._gateway = gateway
        self._runs = runs
        self._memory = memory
        self._model = model
        self._every = max(int(every), 1)
        self._dedupe_sim = dedupe_sim
        self._daily_usd = daily_usd
        self._tasks: set = set()          # strong refs — asyncio holds only weak ones
        self._spend: dict[tuple, float] = {}  # (user_id, utc_date) → usd spent extracting today
        # ponytail: in-proc per-replica meter, resets on restart. Bounds a runaway at current
        # scale (Alex + QA, sub-cent/day). Move to a PG meter if extraction volume ever grows.

    def _spent_today(self, user_id: str) -> float:
        return self._spend.get((user_id, _utc_date()), 0.0)

    def maybe_extract(self, user_id: str | None, text: str, *, turn: int | None = None,
                      session_end: bool = False, source_run: str = "extract") -> bool:
        """Gate + fire. Returns True if a task was scheduled. Cadence: session-end always;
        conversation on every Nth turn (D1). No user, empty text, off-cadence, or over the daily
        cap → skip. Never awaits, never raises."""
        if not user_id or not (text or "").strip():
            return False
        # Zero-retention (W1-C4, widened 2026-07-13): distillation both persists conversation-
        # derived facts into user_memory AND ships turn text to a model — skip entirely, before
        # cadence or spend. Same contextvar read as Memory.capture (the sibling gate).
        from .routes.deps import current_identity

        if getattr(current_identity(), "zero_retention", False):
            return False
        if not (session_end or (turn is not None and turn % self._every == 0)):
            return False
        if self._daily_usd > 0 and self._spent_today(user_id) >= self._daily_usd:
            return False
        task = asyncio.create_task(self._run(user_id, text, source_run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(self, user_id: str, text: str, source_run: str) -> None:
        try:
            out = await extract_memories(text, user_id=user_id, gateway=self._gateway,
                                         runs=self._runs, memory=self._memory, model=self._model,
                                         dedupe_sim=self._dedupe_sim, source_run=source_run)
            key = (user_id, _utc_date())
            self._spend[key] = self._spend.get(key, 0.0) + (out.get("cost_usd") or 0.0)
        except Exception:
            log.debug("extract task failed (degrade-to-off)", exc_info=True)


if __name__ == "__main__":  # ponytail: one runnable self-check of the pure logic (no DB, no model)
    assert _parse_facts("") == []
    assert _parse_facts("no json here") == []
    assert _parse_facts('garbage {"tool":"x"} more') == []          # object, not the array we want
    assert _parse_facts('[{"kind":"fact","content":"name is Alex"}]') == \
        [{"kind": "fact", "content": "name is Alex"}]
    assert _parse_facts('here: [{"kind":"nope","content":"x"},{"kind":"fact","content":" a b "}]') \
        == [{"kind": "fact", "content": "a b"}]                       # unknown kind dropped, ws collapsed

    # dedupe: substring, symmetric overlap, and the long-capture non-match that makes pass 2 sound
    assert _is_dup("name is Alex", ["her name is Alex"], 0.85)                 # comparable substring
    assert _is_dup("prefers concise replies", ["wants concise replies"], 0.6)  # overlap paraphrase
    assert not _is_dup("name is Alex", [], 0.85)
    long_turn = ("hey so my name is Alex and today I was thinking a lot about the weather and "
                 "the offsite and a dozen other unrelated things entirely")
    assert not _is_dup("name is Alex", [long_turn], 0.85)  # buried in a long turn → NOT a dup
    print("memory_extract self-check ok")
