#!/usr/bin/env python
"""Read-only calibration report over shadow-mode trajectory scores on the trace store.

Answers Phase 1's gating question: how often would a trajectory escalation have fired, and
on which turns? Scans `gateway_events` (the same store the analytics plane reads) and prints:
share of agentic turns, a confidence histogram, the would-have-escalated count at a threshold
(score >= threshold while the served model was an efficient/economy-lane tier), and the
top-contribution frequency table. Routes on nothing — this only reads what the gateway stamped.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

_BUCKETS = 10  # confidence histogram resolution over [0, 1]


def _since_iso(spec: str | None) -> str | None:
    """`--since 7d`/`24h`/`30m` -> ISO cutoff string (compared lexically against ts_start), or None."""
    if not spec:
        return None
    m = re.fullmatch(r"(\d+)([dhm])", spec.strip())
    if not m:
        raise SystemExit(f"--since must look like 7d / 24h / 30m, got {spec!r}")
    n, unit = int(m[1]), m[2]
    delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
    return (datetime.now(timezone.utc) - delta).isoformat()


def _bucket(confidence: float) -> int:
    """Confidence in [0, 1] -> histogram bucket index in [0, _BUCKETS)."""
    return min(_BUCKETS - 1, int(confidence * _BUCKETS))


def summarize(rows: list[dict], threshold: float) -> dict:
    """Pure reduction over trace dicts (keys: trajectory_score, trajectory_confidence,
    trajectory_top, lane). Kept separate from I/O so demo() can exercise it offline."""
    total = len(rows)
    agentic = [r for r in rows if r.get("trajectory_score") is not None]
    hist = [0] * _BUCKETS
    for r in agentic:
        hist[_bucket(r["trajectory_confidence"])] += 1
    would_escalate = sum(
        1 for r in agentic
        if r["trajectory_score"] >= threshold and r.get("lane") == "economy"
    )
    tops = Counter(r["trajectory_top"] for r in agentic if r.get("trajectory_top"))
    return {
        "total": total,
        "agentic": len(agentic),
        "hist": hist,
        "would_escalate": would_escalate,
        "tops": tops,
    }


def _print_report(rep: dict, threshold: float) -> None:
    total, agentic = rep["total"], rep["agentic"]
    share = (agentic / total * 100) if total else 0.0
    print(f"traces scanned:        {total}")
    print(f"agentic (scored) turns:{agentic:>6}  ({share:.1f}% of total)")
    if agentic:
        esc_share = rep["would_escalate"] / agentic * 100
        print(f"would-have-escalated:  {rep['would_escalate']:>6}  "
              f"({esc_share:.1f}% of agentic, at score>={threshold} on economy lane)")
        print("\nconfidence histogram (each bucket = width 0.1):")
        for i, count in enumerate(rep["hist"]):
            lo = i / _BUCKETS
            print(f"  [{lo:.1f}-{lo + 1 / _BUCKETS:.1f})  {count:>5}  {'#' * min(count, 50)}")
        print("\ntop-contribution frequency:")
        for name, count in rep["tops"].most_common():
            print(f"  {name:<28} {count:>5}")


def _load_rows(db_url: str, since: str | None) -> list[dict]:
    # Reuse the writer's connection bootstrap verbatim: it opens the engine AND runs the additive
    # migration, so the trajectory columns exist even on a DB written before they were added.
    from sqlmodel import Session, select

    from toto_gateway.trace import SqlModelTraceWriter, TraceRow

    engine = SqlModelTraceWriter(db_url).engine
    stmt = select(TraceRow.trajectory_score, TraceRow.trajectory_confidence,
                  TraceRow.trajectory_top, TraceRow.lane, TraceRow.ts_start)
    if since is not None:
        stmt = stmt.where(TraceRow.ts_start >= since)
    with Session(engine) as s:
        return [dict(zip(("trajectory_score", "trajectory_confidence", "trajectory_top",
                          "lane", "ts_start"), row)) for row in s.execute(stmt)]


def demo() -> None:
    """Self-check: three synthetic trace dicts through the bucketing logic."""
    rows = [
        {"trajectory_score": 0.8, "trajectory_confidence": 0.8, "trajectory_top": "error_intensity",
         "lane": "economy"},                                   # would escalate (>=0.5, economy)
        {"trajectory_score": 0.9, "trajectory_confidence": 0.9, "trajectory_top": "error_intensity",
         "lane": "frontier"},                                  # confident but already capable
        {"trajectory_score": None, "trajectory_confidence": None, "trajectory_top": None,
         "lane": "economy"},                                   # plain chat, not counted
    ]
    rep = summarize(rows, threshold=0.5)
    assert rep["total"] == 3 and rep["agentic"] == 2, rep
    assert rep["would_escalate"] == 1, rep                     # only the economy-lane confident one
    assert rep["hist"][8] == 1 and rep["hist"][9] == 1, rep     # 0.8 -> bucket 8, 0.9 -> bucket 9
    assert rep["tops"]["error_intensity"] == 2, rep
    print("demo ok")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="lookback window, e.g. 7d / 24h / 30m (default: all)")
    ap.add_argument("--threshold", type=float, default=0.5, help="escalation score threshold")
    ap.add_argument("--db", default=os.environ.get("TOTO_GW_TRACE_DB", ""),
                    help="trace DB URL (default: $TOTO_GW_TRACE_DB)")
    ap.add_argument("--demo", action="store_true", help="run the offline self-check and exit")
    args = ap.parse_args()
    if args.demo:
        demo()
        return
    if not args.db:
        raise SystemExit("no trace DB: pass --db or set TOTO_GW_TRACE_DB")
    rows = _load_rows(args.db, _since_iso(args.since))
    _print_report(summarize(rows, args.threshold), args.threshold)


if __name__ == "__main__":
    main()
