"""Org audit-export pipeline (W2-C4): hash-chained JSONL batches to the gateway's own object
store and/or a customer S3 bucket.

Two streams per org, both metadata-only by construction: `gateway_events` (trace rows) and
`audit_events`. NEVER request_content / payloads. One batch file per (org, stream) per export
run: a JSON header line carrying {batch, prev_sha256, rows} followed by one JSON line per row.

Integrity is a per-stream sha256 hash chain. Each batch file's sha256 is recorded in the ledger
(auth store's audit_export_batches, which doubles as the listing), and the NEXT batch's header
carries it as prev_sha256. An auditor downloads the batches + the listing and runs verify_chain:
a single altered byte changes a file's sha (recompute mismatch); a removed or reordered batch
breaks the prev_sha256 linkage or the contiguous numbering. Retention prunes the OLDEST batches
(a contiguous head prefix) — legitimate, so a valid chain may start at any batch but never has an
internal gap.

The engine (run_export_for_org) is shared by the scheduled task (app._audit_exporter) and the
manual POST .../audit-export/run — one code path, no drift.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from .storage import FilesystemBackend, ObjectStore, S3Backend

log = logging.getLogger("toto_gateway.audit_export")

STREAMS = ("gateway_events", "audit_events")
# One batch per run caps at this many rows; the next run picks up where the cursor left off. A
# ponytail ceiling: a firehose org lags one cycle behind rather than writing an unbounded file.
_BATCH_LIMIT = 50_000


def _canonical_line(obj: dict) -> str:
    """Stable one-line JSON: sorted keys, no spaces, non-ASCII escaped. The canonical bytes the
    sha256 is taken over — so the same rows always hash the same regardless of dict order."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def build_batch_bytes(*, batch: int, stream: str, org_id: str, prev_sha256: str,
                      rows: list[dict], created_at: float) -> bytes:
    """The canonical file bytes for one batch: header line + one line per row, '\\n'-joined with a
    trailing newline. sha256 is taken over exactly these bytes (header included)."""
    header = {"batch": batch, "stream": stream, "org_id": org_id, "prev_sha256": prev_sha256,
              "rows": len(rows), "created_at": created_at}
    lines = [_canonical_line(header)] + [_canonical_line(r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _customer_store(cfg: dict, secret_key: str) -> ObjectStore | None:
    """A per-org S3Backend built from the org's stored (decrypted) customer-bucket creds, or None
    if the customer S3 destination isn't fully configured. Reuses the storage substrate's S3
    client — no new dependency."""
    from .credentials import decrypt

    if not (cfg.get("s3_endpoint") and cfg.get("s3_bucket")):
        return None
    enc = cfg.get("s3_secret_enc") or ""
    secret = decrypt(secret_key, enc) if (enc and secret_key) else ""
    return S3Backend(
        endpoint=cfg["s3_endpoint"], bucket=cfg["s3_bucket"], region=cfg.get("s3_region") or "us-east-1",
        access_key=cfg.get("s3_access_key") or "", secret_key=secret, force_path_style=True,
    )


def _object_key(stream: str, batch: int, prefix: str = "") -> str:
    """The batch's object key. The ObjectStore prefixes it with the org id, so both the gateway
    store and a customer bucket are org-scoped. An optional org prefix nests it further."""
    base = f"audit-export/{stream}/{batch:06d}.jsonl"
    return f"{prefix.strip('/')}/{base}" if prefix.strip("/") else base


def _read_gateway_events(engine: Any, org_id: str, after_id: int, limit: int) -> list[dict]:
    """gateway_events rows for one org with id > after_id (the cursor), oldest first. Serialized via
    the SQLModel row's own fields — the trace table is metadata by construction (no payload columns)."""
    from sqlmodel import Session, select

    from .trace import TraceRow

    with Session(engine) as s:
        rows = s.exec(
            select(TraceRow).where(TraceRow.org_id == org_id, TraceRow.id > after_id)
            .order_by(TraceRow.id).limit(limit)
        ).all()
    return [dict(r.model_dump()) for r in rows]


async def _export_stream(auth, object_store: ObjectStore, customer: ObjectStore | None,
                         *, org_id: str, stream: str, rows: list[dict], cursor: str,
                         destination: str, prefix: str) -> dict:
    """Write one batch for one stream if it has new rows. Returns {batch, rows, sha256} or
    {rows: 0} when nothing new. Idempotent-ish: the ledger's PK(org, stream, batch) rejects a
    duplicate batch number, so a double-run can't fork the chain."""
    if not rows:
        return {"stream": stream, "rows": 0}
    tip = await auth.audit_export_tip(org_id, stream)
    batch = (tip["batch"] + 1) if tip else 0
    prev_sha = tip["sha256"] if tip else ""
    created = time.time()
    payload = build_batch_bytes(batch=batch, stream=stream, org_id=org_id, prev_sha256=prev_sha,
                                rows=rows, created_at=created)
    sha = hashlib.sha256(payload).hexdigest()
    key = _object_key(stream, batch, prefix)

    if destination in ("gateway", "both"):
        object_store.put(org_id, key, payload, "application/x-ndjson")
    if destination in ("s3", "both") and customer is not None:
        customer.put(org_id, key, payload, "application/x-ndjson")

    # The ledger row is written only after the object lands — a failed put leaves no dangling
    # ledger entry, so the next run retries the same batch number cleanly.
    await auth.record_audit_export_batch(
        org_id, stream, batch=batch, object_key=key, sha256=sha, prev_sha256=prev_sha,
        rows=len(rows), cursor=cursor, created_at=created)
    return {"stream": stream, "batch": batch, "rows": len(rows), "sha256": sha}


async def run_export_for_org(auth, trace_engine, object_store: ObjectStore, *, org_id: str,
                             cfg: dict, secret_key: str) -> dict:
    """One export cycle for one org: both streams, then retention pruning. Returns a summary. Reads
    are hard-org-scoped (gateway_events.org_id / audit_events.org_id / the object-store org prefix),
    so org A's batches can never contain org B's rows. Raises on a real failure so the caller records
    last_error and retries next cycle — the serving path never calls this."""
    destination = cfg.get("destination") or "gateway"
    prefix = cfg.get("s3_prefix") or ""
    customer = _customer_store(cfg, secret_key) if destination in ("s3", "both") else None
    if destination in ("s3", "both") and customer is None:
        raise ValueError("customer S3 destination selected but s3_endpoint/s3_bucket not configured")

    results = []

    # gateway_events: cursor is the last exported row id (monotonic int PK).
    tip_ge = await auth.audit_export_tip(org_id, "gateway_events")
    after_id = int(tip_ge["cursor"]) if (tip_ge and tip_ge["cursor"]) else 0
    ge_rows = _read_gateway_events(trace_engine, org_id, after_id, _BATCH_LIMIT) if trace_engine else []
    ge_cursor = str(ge_rows[-1]["id"]) if ge_rows else str(after_id)
    results.append(await _export_stream(
        auth, object_store, customer, org_id=org_id, stream="gateway_events", rows=ge_rows,
        cursor=ge_cursor, destination=destination, prefix=prefix))

    # audit_events: cursor is the last exported row ts (float, walked with ts > cursor).
    tip_ae = await auth.audit_export_tip(org_id, "audit_events")
    after_ts = float(tip_ae["cursor"]) if (tip_ae and tip_ae["cursor"]) else 0.0
    ae_rows = await auth.list_audit_since(org_id, after_ts, _BATCH_LIMIT)
    ae_cursor = repr(ae_rows[-1]["ts"]) if ae_rows else repr(after_ts)
    results.append(await _export_stream(
        auth, object_store, customer, org_id=org_id, stream="audit_events", rows=ae_rows,
        cursor=ae_cursor, destination=destination, prefix=prefix))

    pruned = await _prune_retention(auth, object_store, org_id, int(cfg.get("retention_days") or 0))
    return {"org_id": org_id, "destination": destination, "streams": results, "pruned": pruned}


async def _prune_retention(auth, object_store: ObjectStore, org_id: str, retention_days: int) -> int:
    """Delete gateway-stored batches older than retention_days (0 = keep forever) and their ledger
    rows. Customer-bucket retention is the customer's lifecycle policy, not ours (ceiling)."""
    if retention_days <= 0:
        return 0
    before = time.time() - retention_days * 86400
    dropped = await auth.prune_audit_export_batches(org_id, before)
    for b in dropped:
        try:
            object_store.delete(org_id, b["object_key"])
        except Exception:  # noqa: BLE001 — a missing object is fine; the ledger row is already gone
            pass
    return len(dropped)


def verify_chain(items: list[dict]) -> bool:
    """Auditor-side verification. `items` is an ordered list of {batch, bytes, sha256, prev_sha256}
    (bytes = the downloaded file; sha256/prev_sha256 = the listing's recorded values). Returns True
    iff: every file's recomputed sha matches its recorded sha (no tampering); batch numbers are
    contiguous (no removed/reordered middle batch); each batch's prev_sha256 links to the previous
    batch's sha (and a batch-0 head has prev_sha256 == ''); and each header's rows == its data-line
    count. A trimmed head is fine — the list may start at any batch, but must be gapless from there."""
    if not items:
        return True
    prev = None
    for i, it in enumerate(items):
        data = it["bytes"]
        if hashlib.sha256(data).hexdigest() != it["sha256"]:
            return False
        lines = data.decode("utf-8").rstrip("\n").split("\n")
        try:
            header = json.loads(lines[0])
        except (ValueError, IndexError):
            return False
        if header.get("batch") != it["batch"]:
            return False
        if header.get("rows") != len(lines) - 1:
            return False
        if header.get("prev_sha256") != it["prev_sha256"]:
            return False
        if prev is None:
            if it["batch"] == 0 and it["prev_sha256"] != "":
                return False
        else:
            if it["batch"] != prev["batch"] + 1:  # gap => a batch was removed/reordered
                return False
            if it["prev_sha256"] != prev["sha256"]:  # chain break
                return False
        prev = it
    return True


if __name__ == "__main__":  # pragma: no cover — runnable self-check of the chain math
    # Build a 3-batch chain by hand, then prove verify_chain accepts it and rejects tamper/removal.
    def _batch(n, prev_sha, rows):
        b = build_batch_bytes(batch=n, stream="s", org_id="o", prev_sha256=prev_sha, rows=rows,
                              created_at=1.0)
        return {"batch": n, "bytes": b, "sha256": hashlib.sha256(b).hexdigest(),
                "prev_sha256": prev_sha}

    b0 = _batch(0, "", [{"a": 1}])
    b1 = _batch(1, b0["sha256"], [{"a": 2}, {"a": 3}])
    b2 = _batch(2, b1["sha256"], [{"a": 4}])
    assert verify_chain([b0, b1, b2]) is True, "clean chain must verify"
    assert verify_chain([b0, b2]) is False, "removed middle batch must fail (gap)"

    tampered = dict(b1, bytes=b1["bytes"].replace(b'"a":2', b'"a":9'))
    assert verify_chain([b0, tampered, b2]) is False, "altered bytes must fail (sha mismatch)"

    relinked = _batch(1, "deadbeef", [{"a": 2}, {"a": 3}])
    assert verify_chain([b0, relinked, b2]) is False, "broken prev linkage must fail"

    trimmed = _batch(5, b1["sha256"], [{"a": 4}])  # head pruned: chain starts mid-way
    b6 = _batch(6, trimmed["sha256"], [{"a": 5}])
    assert verify_chain([trimmed, b6]) is True, "retention-trimmed head is still a valid chain"
    print("audit_export chain self-check OK")
