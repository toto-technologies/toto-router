"""Audit trail (append-only, metadata-only) and the per-org hash-chained audit export:
config + the batch ledger that doubles as the chain and the listing. See audit_export.py for
the export engine."""

from __future__ import annotations

import secrets
import time


class AuditMixin:
    async def write_audit(self, action: str, *, user_id: str | None = None, ip: str | None = None,
                    request_id: str | None = None, org_id: str | None = None,
                    target_type: str | None = None, target_id: str | None = None,
                    metadata: str | None = None) -> None:
        """Append one audit event. Metadata only — never content. INSERT-only: there is no
        UPDATE/DELETE path for audit_events anywhere in the codebase — immutability is structural
        (prod also GRANTs the app role INSERT-but-not-UPDATE/DELETE). Best-effort at call sites
        (wrap in audit.record for the fire-and-forget-safe emit). `org_id` is what makes a row
        org-scoped-readable at GET /v1/admin/audit; `metadata` is a pre-serialized JSON string."""
        await self._exec(
            "INSERT INTO audit_events (id, ts, action, user_id, ip, request_id, "
            "org_id, target_type, target_id, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (secrets.token_hex(12), time.time(), action, user_id, ip, request_id,
             org_id, target_type, target_id, metadata),
        )

    async def list_audit_events(self, org_id: str | None, *, is_operator: bool = False,
                          action: str | None = None, actor: str | None = None,
                          since: float | None = None, until: float | None = None,
                          limit: int = 50, offset: int = 0) -> list[dict]:
        """Org-scoped audit read (IDOR-critical): a normal admin sees ONLY their own org's rows.
        The operator (platform super-credential, org_id None) sees all — matching Identity's
        unscoped-operator semantics. Filterable by action / actor / time window, paginated. Pure
        SELECT: no mutation counterpart exists (append-only)."""
        where: list[str] = []
        params: list = []
        if not is_operator:  # normal admin: hard org filter — never another org's rows
            where.append("org_id = ?")
            params.append(org_id)
        if action:
            where.append("action = ?")
            params.append(action)
        if actor:
            where.append("user_id = ?")
            params.append(actor)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        if until is not None:
            where.append("ts <= ?")
            params.append(until)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        rows = await self._all(
            "SELECT id, ts, action, user_id, org_id, target_type, target_id, metadata, "
            f"ip, request_id FROM audit_events{clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [dict(r) for r in rows]

    async def count_audit_events(self, org_id: str, *, action: str,
                                 since: float | None = None) -> int:
        """Cheap COUNT(*) over one org's rows for a single action (the console Governance panel's
        denied-attempts counter — read without paging the feed). Always org-scoped (the caller is
        a scoped admin, resolved via _scope_org). ponytail: idx_audit_org_ts covers the org+ts scan."""
        where = ["org_id = ?", "action = ?"]
        params: list = [org_id, action]
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        row = await self._one(
            f"SELECT COUNT(*) AS n FROM audit_events WHERE {' AND '.join(where)}", tuple(params))
        return int((dict(row) if row else {}).get("n", 0))

    async def count_audit_events_global(self, action: str, *, since: float | None = None) -> int:
        """Platform-wide COUNT(*) for one action (NOT org-scoped) — the egress printable page's
        7-day observed/blocked counters. Egress rows are deploy-level (org_id NULL), so this
        deliberately counts across all rows; the metadata is host/subsystem only, never tenant data."""
        where = ["action = ?"]
        params: list = [action]
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        row = await self._one(
            f"SELECT COUNT(*) AS n FROM audit_events WHERE {' AND '.join(where)}", tuple(params))
        return int((dict(row) if row else {}).get("n", 0))

    async def list_audit_since(self, org_id: str, after_ts: float, limit: int) -> list[dict]:
        """audit_events rows for one org with ts > after_ts, oldest first (the export cursor walks
        forward). Metadata only, by construction of the table. org-scoped — never another org's rows."""
        rows = await self._all(
            "SELECT id, ts, action, user_id, ip, request_id, org_id, target_type, target_id, "
            "metadata FROM audit_events WHERE org_id = ? AND ts > ? ORDER BY ts ASC LIMIT ?",
            (org_id, after_ts, max(1, int(limit))))
        return [dict(r) for r in rows]

    # --- audit export ----------------------------------------------------------

    async def set_audit_export_config(self, org_id: str, *, enabled: bool, cadence_hours: float,
                                      retention_days: int, destination: str, s3_endpoint: str,
                                      s3_bucket: str, s3_region: str, s3_access_key: str,
                                      s3_secret_enc: str, s3_prefix: str) -> None:
        """Upsert an org's audit-export config. s3_secret_enc is ciphertext (the route encrypts and
        keeps the stored value on a metadata-only edit); last_run/last_error are owned by the
        scheduler (set_audit_export_run) and untouched here."""
        now = time.time()
        await self._exec(
            "INSERT INTO audit_export_configs (org_id, enabled, cadence_hours, retention_days, "
            "destination, s3_endpoint, s3_bucket, s3_region, s3_access_key, s3_secret_enc, "
            "s3_prefix, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (org_id) DO UPDATE SET enabled=excluded.enabled, "
            "cadence_hours=excluded.cadence_hours, retention_days=excluded.retention_days, "
            "destination=excluded.destination, s3_endpoint=excluded.s3_endpoint, "
            "s3_bucket=excluded.s3_bucket, s3_region=excluded.s3_region, "
            "s3_access_key=excluded.s3_access_key, s3_secret_enc=excluded.s3_secret_enc, "
            "s3_prefix=excluded.s3_prefix, updated_at=excluded.updated_at",
            (org_id, 1 if enabled else 0, cadence_hours, retention_days, destination, s3_endpoint,
             s3_bucket, s3_region, s3_access_key, s3_secret_enc, s3_prefix, now, now))

    async def get_audit_export_config(self, org_id: str) -> dict | None:
        """The org's audit-export config, or None. Carries s3_secret_enc (ciphertext) — the route
        decrypts it to build the customer-S3 client and NEVER echoes it to the admin API."""
        row = await self._one("SELECT * FROM audit_export_configs WHERE org_id = ?", (org_id,))
        if row is None:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        return d

    async def list_audit_export_orgs(self) -> list[dict]:
        """Every org with export enabled (the scheduler's work list). Carries ciphertext; the
        scheduler decrypts per org to reach the customer bucket."""
        rows = await self._all(
            "SELECT * FROM audit_export_configs WHERE enabled = 1 ORDER BY org_id", ())
        out = []
        for r in rows:
            d = dict(r)
            d["enabled"] = True
            out.append(d)
        return out

    async def set_audit_export_run(self, org_id: str, *, last_run: float,
                                   last_error: str | None) -> None:
        """Stamp the scheduler's last-run outcome (surfaced in the config GET). Best-effort caller."""
        await self._exec(
            "UPDATE audit_export_configs SET last_run = ?, last_error = ? WHERE org_id = ?",
            (last_run, last_error, org_id))

    async def audit_export_tip(self, org_id: str, stream: str) -> dict | None:
        """The chain tip for (org, stream): the highest-batch ledger row, or None if never exported.
        The next batch is tip.batch + 1 and its prev_sha256 is tip.sha256."""
        row = await self._one(
            "SELECT * FROM audit_export_batches WHERE org_id = ? AND stream = ? "
            "ORDER BY batch DESC LIMIT 1", (org_id, stream))
        return dict(row) if row else None

    async def record_audit_export_batch(self, org_id: str, stream: str, *, batch: int,
                                        object_key: str, sha256: str, prev_sha256: str,
                                        rows: int, cursor: str, created_at: float) -> None:
        """Append one batch to the ledger (the hash-chain link + the listing entry)."""
        await self._exec(
            "INSERT INTO audit_export_batches (org_id, stream, batch, object_key, sha256, "
            "prev_sha256, rows, cursor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (org_id, stream, batch, object_key, sha256, prev_sha256, rows, cursor, created_at))

    async def list_audit_export_batches(self, org_id: str, stream: str | None = None) -> list[dict]:
        """The org's export batches (the auditor-readable listing), newest first. Filter by stream."""
        where = ["org_id = ?"]
        params: list = [org_id]
        if stream is not None:
            where.append("stream = ?")
            params.append(stream)
        rows = await self._all(
            f"SELECT org_id, stream, batch, object_key, sha256, prev_sha256, rows, created_at "
            f"FROM audit_export_batches WHERE {' AND '.join(where)} "
            f"ORDER BY stream, batch DESC", tuple(params))
        return [dict(r) for r in rows]

    async def get_audit_export_batch(self, org_id: str, stream: str, batch: int) -> dict | None:
        """One batch ledger row (the download route resolves object_key through it, org-scoped)."""
        row = await self._one(
            "SELECT * FROM audit_export_batches WHERE org_id = ? AND stream = ? AND batch = ?",
            (org_id, stream, batch))
        return dict(row) if row else None

    async def prune_audit_export_batches(self, org_id: str, before_ts: float) -> list[dict]:
        """Delete gateway-stored batch rows older than before_ts and return them (so the caller can
        delete the objects). Retention trims the OLDEST batches — a contiguous head prefix — so the
        surviving chain stays internally gapless and verifiable from its new floor."""
        rows = await self._all(
            "SELECT org_id, stream, batch, object_key FROM audit_export_batches "
            "WHERE org_id = ? AND created_at < ?", (org_id, before_ts))
        pruned = [dict(r) for r in rows]
        if pruned:
            await self._exec(
                "DELETE FROM audit_export_batches WHERE org_id = ? AND created_at < ?",
                (org_id, before_ts))
        return pruned
