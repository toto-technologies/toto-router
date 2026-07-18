"""Typed artifact envelope — content-addressable receipts (steal from Puppetmaster).

One shape, two producers: the driver's dispatch node wraps each executor completion, and the
companion's tools node wraps each tool result. The sha256 makes every claim verifiable and lets
follow-ups dedupe/cite without re-reading transcripts — receipts are the brand.

The envelope carries the HASH, never a second copy of the content: the raw text already lives
next to it (a task's `result`, a tool call's `result`), so duplicating it would bloat the store
and, for the driver, risk leaking answer text past the privacy boundary. sha256 is the receipt.
"""

from __future__ import annotations

import hashlib
import time


def make_artifact(kind: str, content: str, *, evidence: list | None = None,
                  confidence: float | None = None, produced_by: str = "",
                  ts: float | None = None) -> dict:
    """The envelope. `content` is hashed (sha256 hex) for integrity; the caller keeps the raw
    text elsewhere. confidence is 0..1 or None (unknown — we have no calibrated signal yet).
    evidence is a list of short provenance refs (model/lane/run ids, source tags) — never a
    transcript. produced_by is the model or tool that made it."""
    return {
        "type": kind,
        "sha256": hashlib.sha256((content or "").encode("utf-8")).hexdigest(),
        "confidence": confidence,
        "evidence": list(evidence or []),
        "produced_by": produced_by,
        "ts": ts if ts is not None else time.time(),
    }


if __name__ == "__main__":  # ponytail: one runnable check for the hashing/shape
    a = make_artifact("task_result", "hello", produced_by="m1", evidence=["local"])
    assert a["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert a["type"] == "task_result" and a["produced_by"] == "m1" and a["evidence"] == ["local"]
    assert a["confidence"] is None
    assert make_artifact("x", "")["sha256"] == hashlib.sha256(b"").hexdigest()  # empty ok
    assert make_artifact("x", "z")["sha256"] == make_artifact("x", "z")["sha256"]  # deterministic
    assert make_artifact("x", "z", ts=1.0)["ts"] == 1.0
    print("artifacts self-check ok")
