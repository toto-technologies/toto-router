"""Opaque credentials, one table (auth_tokens), split by purpose: single-use verify tokens,
session cookies, per-user API tokens, and org-owned service tokens. Only sha256 digests are
stored — the raw secret is returned exactly once at mint time."""

from __future__ import annotations

import secrets
import time

from .crypto import _token_hash

VERIFY_TTL = 24 * 3600  # verification tokens: 24h, single-use
# ponytail: no expiry v1 — revocation (DELETE /v1/tokens/{id}) is the lever; a real TTL knob
# slots into mint_api_token if a customer asks. auth_tokens.expires_at is NOT NULL, so "no
# expiry" is a far-future stamp.
API_TOKEN_TTL = 100 * 365 * 86400
# last_used write-throttle: the auth hot path stamps last_used at most once per this window
# per token (compared against the stored value), so a busy CI token adds no per-request write.
_LAST_USED_THROTTLE = 15 * 60


class TokensMixin:
    # --- tokens (verify / session, one table) ---------------------------------

    async def mint_token(self, user_id: str, purpose: str, ttl_seconds: float,
                   *, supersede: bool = False, label: str | None = None,
                   org_id: str | None = None) -> str:
        """Create an opaque token; store only its sha256 digest. Returns the raw token (shown
        once). `supersede` deletes the user's prior tokens of this purpose first (re-issue).
        `org_id` binds the credential to one org — the active org for a session, the
        minted-against org for an API token; NULL leaves resolution on the oldest-membership default."""
        raw = secrets.token_urlsafe(32)
        now = time.time()
        if supersede:
            await self._exec(
                "DELETE FROM auth_tokens WHERE user_id = ? AND purpose = ?", (user_id, purpose),
            )
        await self._exec(
            "INSERT INTO auth_tokens (token_hash, user_id, purpose, expires_at, created_at, label, "
            "org_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_token_hash(raw), user_id, purpose, now + ttl_seconds, now, label, org_id),
        )
        return raw

    async def token_org(self, raw: str, purpose: str) -> str | None:
        """The org this live token/session is bound to, else None. One PK-indexed read on the auth
        hot path — the caller already resolved the user via lookup_token; this reads the sibling
        binding column so identity resolution can prefer it.
        ponytail: a separate read rather than folding into lookup_token keeps that method's
        single-column contract; collapse them only if this shows in p95."""
        row = await self._one(
            "SELECT org_id FROM auth_tokens WHERE token_hash = ? AND purpose = ?",
            (_token_hash(raw), purpose),
        )
        return row["org_id"] if row and row["org_id"] else None

    async def set_session_org(self, raw: str, org_id: str | None) -> None:
        """Bind (or clear) a live session's active org (the switch endpoint + SSO login). The
        route validates membership before calling; this just writes the binding on the session row."""
        await self._exec(
            "UPDATE auth_tokens SET org_id = ? WHERE token_hash = ? AND purpose = 'session'",
            (org_id, _token_hash(raw)),
        )

    async def lookup_token(self, raw: str, purpose: str) -> str | None:
        """user_id for a live token of this purpose, else None. Read-only (no consume)."""
        row = await self._one(
            "SELECT user_id, expires_at FROM auth_tokens WHERE token_hash = ? AND purpose = ?",
            (_token_hash(raw), purpose),
        )
        if row is None or row["expires_at"] < time.time():
            return None
        return row["user_id"]

    async def consume_token(self, raw: str, purpose: str) -> str | None:
        """Single-use: return user_id and delete the row, atomically. None if absent/expired."""
        if self._pg:
            # DELETE ... RETURNING is a single atomic statement — two replicas racing the
            # same one-time token can't both win (only one DELETE affects the row).
            row = await self._one(
                "DELETE FROM auth_tokens WHERE token_hash = ? AND purpose = ? "
                "RETURNING user_id, expires_at", (_token_hash(raw), purpose),
            )
        else:
            # SQLite: the SELECT and DELETE run without an await-suspension between them (the
            # inline path never yields), so no other coroutine can slip in — still single-use.
            row = await self._one(
                "SELECT user_id, expires_at FROM auth_tokens WHERE token_hash = ? AND purpose = ?",
                (_token_hash(raw), purpose),
            )
            if row is not None:
                await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(raw),))
        if row is None:
            return None
        return row["user_id"] if row["expires_at"] >= time.time() else None

    async def revoke_token(self, raw: str) -> None:
        await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (_token_hash(raw),))

    # --- per-user API tokens (purpose 'api') -----------------------------------
    # token_id is the sha256 digest's first 12 hex chars — derivable, collision-safe at this
    # scale, and never invertible to the secret. No extra id column needed.

    async def _org_token_policy(self, org_id: str | None) -> tuple[int, int]:
        """(max_token_lifetime_days, token_rotation_grace_minutes) for an org, defaults on miss."""
        if not org_id:
            return 0, 60
        org = await self.get_org(org_id)
        if org is None:
            return 0, 60
        return int(org.get("max_token_lifetime_days") or 0), int(
            org.get("token_rotation_grace_minutes") if org.get("token_rotation_grace_minutes")
            is not None else 60)

    async def mint_api_token(self, user_id: str, label: str, *, org_id: str | None = None,
                             expires_in_days: float | None = None) -> tuple[str, str]:
        """(raw_token, token_id). The raw token is returned exactly once — only its sha256 digest is
        stored, same at-rest posture as sessions/verify tokens. `org_id` binds the token to one of
        the caller's orgs; the route validates membership before minting. `expires_in_days` is the
        requested lifetime — CLAMPED DOWN to the org's max_token_lifetime_days cap;
        None + no cap = no expiry (far-future stamp), None + a cap = the cap (the ceiling wins)."""
        eff_org = org_id or (await self.get_membership(user_id) or {}).get("org_id")
        cap, _grace = await self._org_token_policy(eff_org)
        days = expires_in_days
        if cap > 0:
            days = cap if days is None else min(days, cap)
        ttl = days * 86400 if days else API_TOKEN_TTL
        raw = await self.mint_token(user_id, "api", ttl, label=label, org_id=org_id)
        return raw, _token_hash(raw)[:12]

    async def list_api_tokens(self, user_id: str) -> list[dict]:
        """This user's API tokens — metadata only, never a hash or secret. `org_id` is the token's
        org binding, NULL when unbound; `org_name` is joined for display. expires_at is the
        hygiene ceiling (a far-future stamp reads as no-expiry to the UI)."""
        rows = await self._all(
            "SELECT substr(t.token_hash, 1, 12) AS token_id, t.label, t.created_at, t.last_used, "
            "t.expires_at, t.rotated_at, t.org_id, o.name AS org_name FROM auth_tokens t "
            "LEFT JOIN organizations o ON o.org_id = t.org_id "
            "WHERE t.user_id = ? AND t.purpose = 'api' ORDER BY t.created_at",
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def revoke_api_token(self, user_id: str, token_id: str) -> bool:
        """Delete one of THIS user's API tokens by id. False if it isn't theirs / doesn't
        exist — the route turns that into 404 (fail-closed, no cross-user revocation)."""
        row = await self._one(
            "SELECT token_hash FROM auth_tokens "
            "WHERE user_id = ? AND purpose = 'api' AND substr(token_hash, 1, 12) = ?",
            (user_id, token_id),
        )
        if row is None:
            return False
        await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (row["token_hash"],))
        return True

    async def rotate_api_token(self, user_id: str, token_id: str,
                               *, grace_minutes: int | None = None) -> tuple[str, str, float | None] | None:
        """Rotate one of THIS user's API tokens: mint a NEW secret (returned once) carrying the same
        label + org binding, and let the OLD secret keep working for a grace window then die. Returns
        (new_raw, new_token_id, old_expires_at) or None when the id isn't theirs (route → 404).

        ponytail: the grace is expressed as the OLD row's expires_at (now + grace) — the existing
        auth-path expiry check (lookup/resolve_bearer) then kills it for free, so the hot path stays
        a single PK-indexed read (no OR-lookup, no stored prev-hash). grace<=0 deletes the old row
        now. Two rows exist only during the grace window; the new token_id differs (new secret)."""
        row = await self._one(
            "SELECT token_hash, label, org_id FROM auth_tokens "
            "WHERE user_id = ? AND purpose = 'api' AND substr(token_hash, 1, 12) = ?",
            (user_id, token_id),
        )
        if row is None:
            return None
        if grace_minutes is None:
            _cap, grace_minutes = await self._org_token_policy(
                row["org_id"] or (await self.get_membership(user_id) or {}).get("org_id"))
        new_raw, new_id = await self.mint_api_token(user_id, row["label"], org_id=row["org_id"])
        now = time.time()
        await self._exec("UPDATE auth_tokens SET rotated_at = ? WHERE token_hash = ?",
                         (now, _token_hash(new_raw)))
        if grace_minutes <= 0:
            await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (row["token_hash"],))
            return new_raw, new_id, None
        old_expires = now + grace_minutes * 60
        await self._exec("UPDATE auth_tokens SET expires_at = ? WHERE token_hash = ?",
                         (old_expires, row["token_hash"]))
        return new_raw, new_id, old_expires

    async def resolve_bearer(self, raw: str) -> dict | None:
        """The auth HOT PATH for API + service bearers: ONE PK-indexed read. Returns
        {user_id, purpose, org_id, expired} for a live api/service token, {..., expired: True} for an
        expired one (so the route can 401 `token_expired` distinctly), or None when the bearer is
        neither (→ fall through to the session cookie / anon). Touches last_used AT MOST once per
        _LAST_USED_THROTTLE window (compared against the value just read) — no write per request."""
        h = _token_hash(raw)
        row = await self._one(
            "SELECT user_id, purpose, org_id, expires_at, last_used FROM auth_tokens "
            "WHERE token_hash = ? AND purpose IN ('api', 'service')",
            (h,),
        )
        if row is None:
            return None
        now = time.time()
        base = {"user_id": row["user_id"], "purpose": row["purpose"], "org_id": row["org_id"]}
        if row["expires_at"] < now:
            return {**base, "expired": True}
        if row["last_used"] is None or now - row["last_used"] >= _LAST_USED_THROTTLE:
            await self._exec("UPDATE auth_tokens SET last_used = ? WHERE token_hash = ?", (now, h))
        return {**base, "expired": False}

    # --- service-account tokens (purpose 'service', ORG-owned) -----------------
    # Not tied to a person: user_id carries the OWNING ORG id (never a real user_id, which is a
    # 16-hex token — no collision), so a SCIM deprovision of any USER (DELETE ... WHERE
    # user_id=<uid>) can never touch them. Minted/listed/revoked at the org level; identity
    # resolves org-scoped with role 'member' + actor 'service'. REQUIRED to be org-bound
    # (org_id == the owning org).

    async def mint_service_token(self, org_id: str, label: str,
                                 *, expires_in_days: float | None = None) -> tuple[str, str]:
        """(raw, token_id). Org-owned CI credential. Lifetime clamped to the org cap like api tokens."""
        cap, _grace = await self._org_token_policy(org_id)
        days = expires_in_days
        if cap > 0:
            days = cap if days is None else min(days, cap)
        ttl = days * 86400 if days else API_TOKEN_TTL
        raw = await self.mint_token(org_id, "service", ttl, label=label, org_id=org_id)
        return raw, _token_hash(raw)[:12]

    async def list_service_tokens(self, org_id: str) -> list[dict]:
        """The org's service tokens — metadata only. Keyed on the owning org (user_id == org_id)."""
        rows = await self._all(
            "SELECT substr(token_hash, 1, 12) AS token_id, label, created_at, last_used, "
            "expires_at, rotated_at FROM auth_tokens "
            "WHERE org_id = ? AND purpose = 'service' ORDER BY created_at",
            (org_id,),
        )
        return [dict(r) for r in rows]

    async def revoke_service_token(self, org_id: str, token_id: str) -> bool:
        """Delete one of the org's service tokens by id. False if not this org's (route → 404)."""
        row = await self._one(
            "SELECT token_hash FROM auth_tokens "
            "WHERE org_id = ? AND purpose = 'service' AND substr(token_hash, 1, 12) = ?",
            (org_id, token_id),
        )
        if row is None:
            return False
        await self._exec("DELETE FROM auth_tokens WHERE token_hash = ?", (row["token_hash"],))
        return True

    # --- org-level bulk revoke + compliance list -------------------------------

    async def revoke_org_credentials(self, org_id: str, *, user_id: str | None = None,
                                     include_sessions: bool = False,
                                     include_service: bool = False) -> dict:
        """Bulk-revoke, org-scoped. `user_id` set → just that member's org-bound credentials (admin
        act); else org-wide (owner act). purposes = api always, + session when include_sessions, +
        service when include_service (org-wide only; a per-user revoke never owns service tokens).
        Returns counts. ponytail: one DELETE per purpose class, no per-token loop; org-BOUND only
        (org_id column == this org) — an unbound personal token that merely resolves here is untouched."""
        purposes = ["api"]
        if include_sessions:
            purposes.append("session")
        if include_service and user_id is None:
            purposes.append("service")
        placeholders = ", ".join("?" for _ in purposes)
        counts: dict[str, int] = {}
        for p in purposes:
            if user_id is not None:
                n = await self._exec_count(
                    "DELETE FROM auth_tokens WHERE org_id = ? AND user_id = ? AND purpose = ?",
                    (org_id, user_id, p))
            else:
                n = await self._exec_count(
                    "DELETE FROM auth_tokens WHERE org_id = ? AND purpose = ?", (org_id, p))
            counts[p] = n
        counts["total"] = sum(counts.values())
        return counts

    async def list_org_credentials(self, org_id: str) -> list[dict]:
        """Every live api + service credential attributable to this org (the compliance screen): the
        org's service tokens (org_id == org) AND every member's api tokens (org-bound OR personal).
        Owner email joined for api tokens; service tokens carry a label, no owner. Sessions excluded
        (ephemeral cookie creds). Admin/auditor-readable at GET /v1/admin/tokens."""
        rows = await self._all(
            "SELECT substr(t.token_hash, 1, 12) AS token_id, t.purpose, t.user_id, t.org_id, "
            "t.label, t.created_at, t.expires_at, t.last_used, t.rotated_at, u.email AS owner_email "
            "FROM auth_tokens t LEFT JOIN users u ON u.user_id = t.user_id "
            "WHERE t.purpose IN ('api', 'service') AND ("
            "  t.org_id = ? OR t.user_id IN (SELECT user_id FROM memberships WHERE org_id = ?)) "
            "ORDER BY t.created_at DESC",
            (org_id, org_id),
        )
        return [dict(r) for r in rows]

    async def set_token_policy(self, org_id: str, *, max_token_lifetime_days: int,
                               token_rotation_grace_minutes: int) -> None:
        """Write the org's token-hygiene policy. Clamps to sane bounds; the mint/rotate paths
        read it via _org_token_policy. Owner/admin-gated at the route."""
        cap = max(0, int(max_token_lifetime_days))
        grace = max(0, int(token_rotation_grace_minutes))
        await self._exec(
            "UPDATE organizations SET max_token_lifetime_days = ?, "
            "token_rotation_grace_minutes = ? WHERE org_id = ?", (cap, grace, org_id))

    # --- sessions (a session is a token with purpose 'session') ---------------

    async def create_session(self, user_id: str, ttl_seconds: float,
                             *, org_id: str | None = None) -> str:
        """A session is a token with purpose 'session'. `org_id` seeds the active-org binding —
        SSO login passes the provisioned org so the user lands there."""
        return await self.mint_token(user_id, "session", ttl_seconds, org_id=org_id)

    async def session_user(self, raw: str, *, require_verified: bool = True) -> dict | None:
        """The verified user behind a live session cookie, else None. `require_verified` must
        match the same `settings.require_email_verify` flag `login()` already gates new sessions
        on — callers own threading it through, this method has no settings access. Defaults True
        (the production behavior) so any caller that doesn't pass it explicitly keeps the safe
        assumption. The mismatch failure mode: with require_email_verify off (open registration,
        no verification step), login() issues a real session for an unverified user — an
        unconditional check here would then reject that exact session on every subsequent
        request, a dead-end "missing or invalid credentials" seconds after a successful login."""
        user_id = await self.lookup_token(raw, "session")
        if user_id is None:
            return None
        user = await self.get_user(user_id)
        if user is None or (require_verified and not user["email_verified"]):
            return None
        return user

    async def revoke_session(self, raw: str) -> None:
        await self.revoke_token(raw)
