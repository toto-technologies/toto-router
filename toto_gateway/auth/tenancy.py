"""Tenancy: orgs, teams, memberships, invitations, and per-org account settings (zero-retention,
content retention policy, BYOS storage connector).

Admin methods are ORG-SCOPED: the org_id is passed in (resolved server-side from the caller's
Identity, never a param) and every WHERE pins it, so a caller can only ever touch their own org's
rows (IDOR discipline). Mutations that target a specific row SELECT-to-verify-ownership then act
and return False when the row isn't in this org.
"""

from __future__ import annotations

import json
import secrets
import time

from .vocab import ROLES


def _parse_retention(raw) -> dict:
    """A stored retention_policy value → {sink: days} with only positive-int day values kept.
    Tolerant: bad JSON or non-positive/non-int entries are dropped (keep-forever). Key validation
    lives at the write boundary (the admin route); this just reads defensively."""
    if not raw:
        return {}
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return {k: int(v) for k, v in obj.items()
            if isinstance(v, int) and not isinstance(v, bool) and v > 0}


class TenancyMixin:
    # org -> team -> member. Roles owner/admin/member/auditor. The personal-org id is DERIVED from
    # the user_id ("o_" + user_id) so provisioning is idempotent and race-safe with zero locking:
    # two concurrent first-requests for the same user target the same rows and ON CONFLICT DO
    # NOTHING.

    def _ignore(self, cols: str) -> str:
        """`ON CONFLICT (...) DO NOTHING` — valid on both SQLite (3.24+) and Postgres."""
        return f"ON CONFLICT ({cols}) DO NOTHING"

    async def create_org(self, name: str, *, org_id: str | None = None) -> str:
        """Insert an org (random id unless one is supplied), return its org_id. Idempotent on id."""
        org_id = org_id or ("o_" + secrets.token_hex(8))
        await self._exec(
            f"INSERT INTO organizations (org_id, name, created_at, status) VALUES (?, ?, ?, 'active') "
            f"{self._ignore('org_id')}",
            (org_id, name, time.time()),
        )
        return org_id

    async def get_org(self, org_id: str) -> dict | None:
        row = await self._one("SELECT * FROM organizations WHERE org_id = ?", (org_id,))
        return dict(row) if row else None

    async def get_team(self, team_id: str) -> dict | None:
        row = await self._one("SELECT * FROM teams WHERE team_id = ?", (team_id,))
        return dict(row) if row else None

    async def create_team(self, org_id: str, name: str, *, team_id: str | None = None) -> str:
        team_id = team_id or ("t_" + secrets.token_hex(8))
        await self._exec(
            f"INSERT INTO teams (team_id, org_id, name, created_at, status) "
            f"VALUES (?, ?, ?, ?, 'active') {self._ignore('team_id')}",
            (team_id, org_id, name, time.time()),
        )
        return team_id

    async def add_membership(self, org_id: str, user_id: str, role: str,
                             *, team_id: str | None = None) -> None:
        """Attach a user to an org with a role. Idempotent on (org_id, user_id) — a re-add is a
        no-op (use set_role to change a role). Role is validated fail-closed."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        await self._exec(
            f"INSERT INTO memberships (org_id, user_id, team_id, role, created_at) "
            f"VALUES (?, ?, ?, ?, ?) {self._ignore('org_id, user_id')}",
            (org_id, user_id, team_id, role, time.time()),
        )

    async def get_membership(self, user_id: str) -> dict | None:
        """Read the user's first membership without provisioning one."""
        row = await self._one(
            "SELECT org_id, team_id, role FROM memberships WHERE user_id = ? "
            "ORDER BY created_at LIMIT 1",
            (user_id,),
        )
        return dict(row) if row is not None else None

    async def get_membership_in(self, user_id: str, org_id: str) -> dict | None:
        """The user's membership in ONE specific org, else None. The multi-org selector: used to
        honor a credential's org binding at resolve time and to validate a switch/mint request (a
        foreign org_id returns None -> 403 or safe fallback, never a cross-org leak)."""
        row = await self._one(
            "SELECT org_id, team_id, role FROM memberships WHERE user_id = ? AND org_id = ?",
            (user_id, org_id),
        )
        return dict(row) if row is not None else None

    async def list_user_memberships(self, user_id: str) -> list[dict]:
        """Every org this user belongs to — {org_id, org_name, role} — for the switch UI and
        GET /v1/auth/memberships. Ordered oldest-first (matches the default-resolution order)."""
        rows = await self._all(
            "SELECT m.org_id, o.name AS org_name, m.role FROM memberships m "
            "JOIN organizations o ON o.org_id = m.org_id WHERE m.user_id = ? ORDER BY m.created_at",
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def resolve_membership(self, user_id: str, *, preferred_org_id: str | None = None) -> dict:
        """The user's {org_id, team_id, role}. Lazily provisions a personal org (owner) the first
        time a pre-tenancy user is seen — this IS the backfill for existing users (no boot scan).

        `preferred_org_id`: when the caller's credential is bound to an org (a switched session /
        an org-scoped API token) and the user STILL holds a membership there, resolve THAT org
        instead of the oldest row. A stale/foreign binding (membership since removed, or never
        held) falls through to the deterministic default (oldest), never 500s and never leaks
        another org (fail-safe).
        ponytail: one SELECT on the hot auth path per authed request, one INSERT once-ever per
        user; add a request-scoped cache only if it shows in p95."""
        if preferred_org_id:
            bound = await self.get_membership_in(user_id, preferred_org_id)
            if bound is not None:
                return bound
        row = await self.get_membership(user_id)
        if row is not None:
            return row
        # No membership yet: provision the personal org (owner). Derived org_id → idempotent.
        user = await self.get_user(user_id)
        name = user["email"].split("@")[0] if user and user.get("email") else "Personal"
        org_id = await self.create_org(f"{name}'s org", org_id="o_" + user_id)
        await self.add_membership(org_id, user_id, "owner")
        return {"org_id": org_id, "team_id": None, "role": "owner"}

    async def backfill_personal_orgs(self) -> int:
        """Provision a personal org for every user lacking one. Optional batch companion to the
        lazy resolve path (call at boot to warm it); returns the count provisioned. Idempotent."""
        rows = await self._all(
            "SELECT user_id FROM users WHERE user_id NOT IN (SELECT user_id FROM memberships)")
        for r in rows:
            await self.resolve_membership(r["user_id"])
        return len(rows)

    # --- tenancy admin ---------------------------------------------------------

    async def list_teams(self, org_id: str) -> list[dict]:
        rows = await self._all(
            "SELECT team_id, org_id, name, created_at, status FROM teams "
            "WHERE org_id = ? AND status = 'active' ORDER BY created_at", (org_id,))
        return [dict(r) for r in rows]

    async def rename_team(self, org_id: str, team_id: str, name: str) -> bool:
        """Rename a team, org-scoped. False if the team isn't in this org (→ 404, no cross-org edit)."""
        row = await self._one(
            "SELECT team_id FROM teams WHERE team_id = ? AND org_id = ? AND status = 'active'",
            (team_id, org_id))
        if row is None:
            return False
        await self._exec("UPDATE teams SET name = ? WHERE team_id = ?", (name, team_id))
        return True

    async def delete_team(self, org_id: str, team_id: str) -> bool:
        """Soft-delete a team (status='deleted'), org-scoped. False if not in this org. Soft so a
        stray membership.team_id doesn't dangle to a hard-gone row."""
        row = await self._one(
            "SELECT team_id FROM teams WHERE team_id = ? AND org_id = ? AND status = 'active'",
            (team_id, org_id))
        if row is None:
            return False
        await self._exec("UPDATE teams SET status = 'deleted' WHERE team_id = ?", (team_id,))
        return True

    async def list_members(self, org_id: str) -> list[dict]:
        """Org members with their role + email (joined from users). Ordered by join time."""
        rows = await self._all(
            "SELECT m.user_id, m.role, m.team_id, m.created_at, u.email "
            "FROM memberships m JOIN users u ON u.user_id = m.user_id "
            "WHERE m.org_id = ? ORDER BY m.created_at", (org_id,))
        return [dict(r) for r in rows]

    async def _owner_count(self, org_id: str) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS n FROM memberships WHERE org_id = ? AND role = 'owner'", (org_id,))
        return row["n"] if row else 0

    async def set_role(self, org_id: str, user_id: str, role: str) -> bool:
        """Change a member's role, org-scoped. Returns False if the user isn't a member of this org.
        Raises ValueError on an unknown role or on demoting the org's LAST owner (that would orphan
        the org — nobody could ever administer it again). The route maps False→404, ValueError→409."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        row = await self._one(
            "SELECT role FROM memberships WHERE org_id = ? AND user_id = ?", (org_id, user_id))
        if row is None:
            return False
        if row["role"] == "owner" and role != "owner" and await self._owner_count(org_id) <= 1:
            raise ValueError("cannot demote the last owner")
        await self._exec(
            "UPDATE memberships SET role = ? WHERE org_id = ? AND user_id = ?",
            (role, org_id, user_id))
        return True

    async def remove_membership(self, org_id: str, user_id: str) -> bool:
        """Remove a user from an org, org-scoped. False if they aren't a member. Raises ValueError
        on removing the LAST owner (would orphan the org). Route maps False→404, ValueError→409."""
        row = await self._one(
            "SELECT role FROM memberships WHERE org_id = ? AND user_id = ?", (org_id, user_id))
        if row is None:
            return False
        if row["role"] == "owner" and await self._owner_count(org_id) <= 1:
            raise ValueError("cannot remove the last owner")
        await self._exec(
            "DELETE FROM memberships WHERE org_id = ? AND user_id = ?", (org_id, user_id))
        return True

    async def rename_org(self, org_id: str, name: str) -> None:
        await self._exec("UPDATE organizations SET name = ? WHERE org_id = ?", (name, org_id))

    async def set_zero_retention(self, org_id: str, on: bool) -> None:
        """Flip the org's zero-retention switch. Stored as 0/1; the identity resolver reads it at
        auth time so every downstream telemetry sink can gate on it."""
        await self._exec("UPDATE organizations SET zero_retention = ? WHERE org_id = ?",
                         (1 if on else 0, org_id))

    # --- content-plane retention policy ---------------------------------------
    # Per-org retention DAYS per product-storage sink, stored as JSON on the org row (sibling to
    # zero_retention). The prune sweep (retention.py) reads it; absent/0 for a sink = keep forever.

    async def set_retention_policy(self, org_id: str, policy: dict) -> None:
        """Persist the org's per-sink retention policy (validated by the route). Empty dict clears
        it (back to keep-forever). Stored as a JSON string in organizations.retention_policy."""
        await self._exec("UPDATE organizations SET retention_policy = ? WHERE org_id = ?",
                         (json.dumps(policy) if policy else '', org_id))

    async def get_retention_policy(self, org_id: str) -> dict:
        """The org's per-sink retention policy as a dict ({} = nothing set = keep everything)."""
        row = await self._one(
            "SELECT retention_policy FROM organizations WHERE org_id = ?", (org_id,))
        return _parse_retention(dict(row).get("retention_policy") if row else None)

    async def list_retention_orgs(self) -> list[dict]:
        """Every org with a non-empty retention policy — the sweep's work list. [{org_id, policy}]."""
        rows = await self._all(
            "SELECT org_id, retention_policy FROM organizations "
            "WHERE retention_policy <> '' AND retention_policy IS NOT NULL")
        out = []
        for r in rows:
            d = dict(r)
            policy = _parse_retention(d.get("retention_policy"))
            if policy:
                out.append({"org_id": d["org_id"], "policy": policy})
        return out

    async def list_org_user_ids(self, org_id: str) -> list[str]:
        """Every member user_id of an org (the per-user scope the content plane prunes by)."""
        rows = await self._all(
            "SELECT user_id FROM memberships WHERE org_id = ?", (org_id,))
        return [r["user_id"] for r in rows]

    # --- BYOS storage connector -----------------------------------------------

    async def set_org_storage_config(self, org_id: str, *, enabled: bool, s3_endpoint: str,
                                     s3_bucket: str, s3_region: str, s3_access_key: str,
                                     s3_secret_enc: str, s3_force_path_style: bool) -> None:
        """Upsert an org's BYOS storage connector. s3_secret_enc is ciphertext (the route encrypts
        and keeps the stored value on a metadata-only edit); last_test/last_error are owned by the
        test endpoint (set_org_storage_test) and untouched here."""
        now = time.time()
        await self._exec(
            "INSERT INTO org_storage_configs (org_id, enabled, s3_endpoint, s3_bucket, s3_region, "
            "s3_access_key, s3_secret_enc, s3_force_path_style, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (org_id) DO UPDATE SET enabled=excluded.enabled, "
            "s3_endpoint=excluded.s3_endpoint, s3_bucket=excluded.s3_bucket, "
            "s3_region=excluded.s3_region, s3_access_key=excluded.s3_access_key, "
            "s3_secret_enc=excluded.s3_secret_enc, "
            "s3_force_path_style=excluded.s3_force_path_style, updated_at=excluded.updated_at",
            (org_id, 1 if enabled else 0, s3_endpoint, s3_bucket, s3_region, s3_access_key,
             s3_secret_enc, 1 if s3_force_path_style else 0, now, now))

    async def get_org_storage_config(self, org_id: str) -> dict | None:
        """The org's storage connector config, or None. Carries s3_secret_enc (ciphertext) — the
        resolver decrypts it to build the org's S3 client and it is NEVER echoed over the API."""
        row = await self._one("SELECT * FROM org_storage_configs WHERE org_id = ?", (org_id,))
        if row is None:
            return None
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        d["s3_force_path_style"] = bool(d["s3_force_path_style"])
        return d

    async def set_org_storage_test(self, org_id: str, *, last_test: float,
                                   last_error: str | None) -> None:
        """Stamp the most recent connection-test outcome (surfaced in the config GET)."""
        await self._exec(
            "UPDATE org_storage_configs SET last_test = ?, last_error = ? WHERE org_id = ?",
            (last_test, last_error, org_id))

    # --- invitations -----------------------------------------------------------

    async def create_invitation(self, org_id: str, email: str, role: str) -> dict:
        """Create a pending invitation, return its row (token included — shown to the inviter so
        they can hand it to the invitee). Role validated fail-closed."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        inv_id = "inv_" + secrets.token_hex(8)
        token = secrets.token_urlsafe(24)
        now = time.time()
        await self._exec(
            "INSERT INTO invitations (id, org_id, email, role, token, created_ts, accepted_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (inv_id, org_id, email.strip().lower(), role, token, now))
        return {"id": inv_id, "org_id": org_id, "email": email.strip().lower(), "role": role,
                "token": token, "created_ts": now, "accepted_ts": None}

    async def list_invitations(self, org_id: str, *, pending_only: bool = True) -> list[dict]:
        """This org's invitations. pending_only → only those not yet accepted."""
        clause = " AND accepted_ts IS NULL" if pending_only else ""
        rows = await self._all(
            "SELECT id, org_id, email, role, token, created_ts, accepted_ts FROM invitations "
            f"WHERE org_id = ?{clause} ORDER BY created_ts DESC", (org_id,))
        return [dict(r) for r in rows]

    async def accept_invitation(self, token: str, user_id: str) -> dict | None:
        """Bind the accepting user to the invited org with the invited role, then stamp the invite
        accepted. Returns the invitation row on success, None if the token is unknown or already
        accepted. Idempotent membership add (a user already in the org keeps their existing role —
        add_membership is ON CONFLICT DO NOTHING; use set_role to change it)."""
        row = await self._one(
            "SELECT id, org_id, email, role FROM invitations "
            "WHERE token = ? AND accepted_ts IS NULL", (token,))
        if row is None:
            return None
        await self.add_membership(row["org_id"], user_id, row["role"])
        await self._exec(
            "UPDATE invitations SET accepted_ts = ? WHERE id = ?", (time.time(), row["id"]))
        return {"id": row["id"], "org_id": row["org_id"], "email": row["email"],
                "role": row["role"]}
