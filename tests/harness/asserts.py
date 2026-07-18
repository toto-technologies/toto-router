"""Span / log assertion helpers (Chunk H2, reused by the observability chunk).

Driver spans reach tests two ways: a `spans` list from `observe=spans.append`, or the run's
event log (`store.events_after` → rows with `kind` = the span's node). These helpers stop every
domain hand-rolling `[s for s in spans if s["node"] == ...]` (as test_driver_resilience.py does).
"""

from __future__ import annotations

from typing import Any, Iterable


def _node_of(item: dict) -> Any:
    # spans carry {"node": ...}; stored events carry {"kind": ...} (publish() renames it).
    return item.get("node", item.get("kind"))


def find_spans(spans: Iterable[dict], node: str) -> list[dict]:
    return [s for s in spans if _node_of(s) == node]


def assert_span(spans: Iterable[dict], node: str, **fields: Any) -> dict:
    """Assert exactly one span/event with this node exists (and matches any given fields);
    return it. `fields` match against the span dict OR its nested `data` (event rows nest the
    payload under `data`)."""
    spans = list(spans)
    hits = find_spans(spans, node)
    assert hits, f"no span with node={node!r}; saw {sorted({str(_node_of(s)) for s in spans})}"
    assert len(hits) == 1, f"expected 1 span node={node!r}, got {len(hits)}"
    span = hits[0]
    for key, want in fields.items():
        got = span.get(key, span.get("data", {}).get(key) if isinstance(span.get("data"), dict) else None)
        assert got == want, f"span[{node}].{key}: expected {want!r}, got {got!r}"
    return span


def assert_log_field(record: dict, **fields: Any) -> None:
    """Assert a structured record carries each field=value (checks the record and a nested
    `data`/`extra` payload)."""
    for key, want in fields.items():
        got = record.get(key)
        if got is None:
            for nest in ("data", "extra"):
                sub = record.get(nest)
                if isinstance(sub, dict) and key in sub:
                    got = sub[key]
                    break
        assert got == want, f"log field {key}: expected {want!r}, got {got!r}"
