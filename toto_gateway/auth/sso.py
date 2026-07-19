"""OIDC SSO and SCIM provisioning.

SSO: org relying-party config (client secret is Fernet ciphertext — the store never sees
plaintext), the domain->org map (uniqueness enforced by the sso_domains PK), server-side
single-use login state, and JIT provisioning matched on (issuer, sub) then verified email.

SCIM: per-org config in org_scim_configs — the SCIM bearer's sha256 digest (only the hash, like
auth_tokens), the group->role map (JSON), and the enabled flag. The SCIM endpoints authenticate
ONLY via org_by_scim_token — no session/user credential is ever accepted there.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time

from .crypto import _token_hash
from .vocab import ROLES


class SsoScimMixin:
    # --- OIDC SSO --------------------------------------------------------------

    async def set_sso_config(self, org_id: str, *, issuer: str, client_id: str,
                             client_secret_enc: str, domains: list[str],
                             sso_required: bool) -> None:
        """Upsert an org's SSO config and REPLACE its domain set. Raises ValueError('domain_taken')
        if any domain is already claimed by a different org (the sso_domains PK is global). Domains
        are lowercased/stripped; empties dropped."""
        clean = sorted({d.strip().lower() for d in domains if d.strip()})
        for d in clean:  # a domain owned by another org must not be silently re-pointed
            row = await self._one("SELECT org_id FROM sso_domains WHERE domain = ?", (d,))
            if row is not None and row["org_id"] != org_id:
                raise ValueError("domain_taken")
        now = time.time()
        await self._exec(
            "INSERT INTO org_sso_configs (org_id, issuer, client_id, client_secret_enc, "
            "sso_required, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (org_id) DO UPDATE SET issuer=excluded.issuer, client_id=excluded.client_id, "
            "client_secret_enc=excluded.client_secret_enc, sso_required=excluded.sso_required, "
            "updated_at=excluded.updated_at",
            (org_id, issuer, client_id, client_secret_enc, int(sso_required), now, now))
        # Replace this org's domain rows (full-replace semantics, like the routing overlay).
        await self._exec("DELETE FROM sso_domains WHERE org_id = ?", (org_id,))
        for d in clean:
            await self._exec("INSERT INTO sso_domains (domain, org_id) VALUES (?, ?)", (d, org_id))

    async def _sso_domains(self, org_id: str) -> list[str]:
        rows = await self._all(
            "SELECT domain FROM sso_domains WHERE org_id = ? ORDER BY domain", (org_id,))
        return [r["domain"] for r in rows]

    async def get_sso_config(self, org_id: str) -> dict | None:
        """The org's SSO config with its domains, or None. Carries client_secret_enc (ciphertext) —
        the route decrypts it for the token exchange and NEVER echoes it to the admin API."""
        row = await self._one("SELECT * FROM org_sso_configs WHERE org_id = ?", (org_id,))
        if row is None:
            return None
        d = dict(row)
        d["sso_required"] = bool(d["sso_required"])
        d["domains"] = await self._sso_domains(org_id)
        return d

    async def get_sso_config_by_domain(self, domain: str) -> dict | None:
        """Resolve an email domain to its org's SSO config (the start endpoint + the login
        sso_required check). One indexed join. None when the domain has no SSO org."""
        if not domain:
            return None
        row = await self._one(
            "SELECT c.* FROM org_sso_configs c JOIN sso_domains d ON d.org_id = c.org_id "
            "WHERE d.domain = ?", (domain.strip().lower(),))
        if row is None:
            return None
        d = dict(row)
        d["sso_required"] = bool(d["sso_required"])
        return d

    async def all_sso_issuers(self) -> list[str]:
        """Every configured org SSO issuer (for the egress allowlist's dynamic issuer admission)."""
        rows = await self._all("SELECT DISTINCT issuer FROM org_sso_configs")
        return [r["issuer"] for r in (dict(x) for x in rows) if r.get("issuer")]

    async def create_login_state(self, *, org_id: str, nonce: str, code_verifier: str,
                                 redirect_to: str, ttl_seconds: float) -> str:
        """Store a single-use OIDC login state, return the state token (rides the authorize redirect)."""
        state = secrets.token_urlsafe(24)
        now = time.time()
        await self._exec(
            "INSERT INTO sso_login_states (state, org_id, nonce, code_verifier, redirect_to, "
            "expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (state, org_id, nonce, code_verifier, redirect_to, now + ttl_seconds, now))
        return state

    async def consume_login_state(self, state: str) -> dict | None:
        """Single-use: return the state row and delete it atomically. None if unknown/expired. Same
        DELETE ... RETURNING (PG) / non-yielding SELECT+DELETE (SQLite) idiom as consume_token."""
        if self._pg:
            row = await self._one(
                "DELETE FROM sso_login_states WHERE state = ? "
                "RETURNING org_id, nonce, code_verifier, redirect_to, expires_at", (state,))
        else:
            row = await self._one(
                "SELECT org_id, nonce, code_verifier, redirect_to, expires_at "
                "FROM sso_login_states WHERE state = ?", (state,))
            if row is not None:
                await self._exec("DELETE FROM sso_login_states WHERE state = ?", (state,))
        if row is None or row["expires_at"] < time.time():
            return None
        return dict(row)

    async def get_user_by_oidc(self, issuer: str, sub: str) -> dict | None:
        row = await self._one(
            "SELECT * FROM users WHERE oidc_issuer = ? AND oidc_sub = ?", (issuer, sub))
        return dict(row) if row else None

    async def _set_user_oidc(self, user_id: str, issuer: str, sub: str) -> None:
        await self._exec(
            "UPDATE users SET oidc_issuer = ?, oidc_sub = ? WHERE user_id = ?",
            (issuer, sub, user_id))

    async def _create_sso_user(self, email: str, issuer: str, sub: str, org_id: str,
                               role: str = "member") -> str:
        """Insert a JIT-provisioned SSO user (email_verified, oidc identity) and attach the CONFIGURED
        org membership only — no personal org — so resolve_membership returns the enterprise org as
        their home (that's what 'lands in the right org' means). Raises sqlite3.IntegrityError on a
        racing duplicate."""
        user_id = secrets.token_hex(8)
        try:
            await self._exec(
                "INSERT INTO users (user_id, email, password_hash, email_verified, "
                "oidc_issuer, oidc_sub, created_at) VALUES (?, ?, NULL, 1, ?, ?, ?)",
                (user_id, email.strip().lower(), issuer, sub, time.time()))
        except Exception as exc:  # normalize PG's UniqueViolation to the sqlite contract
            if self._pg and type(exc).__name__ == "UniqueViolation":
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise
        await self.add_membership(org_id, user_id, role)
        return user_id

    async def provision_sso_login(self, *, issuer: str, sub: str, email: str,
                                  org_id: str) -> tuple[str, bool]:
        """Resolve an SSO login to a user_id, JIT-provisioning if new. Returns (user_id, provisioned).
        Match order: (issuer, sub) first, then verified email (link the oidc identity + attach the org
        membership), else create the user in the org as `member`. Never widens an existing user's
        role — an email match keeps whatever role they already hold (add_membership is idempotent)."""
        found = await self.get_user_by_oidc(issuer, sub)
        if found is not None:
            return found["user_id"], False
        existing = await self.get_user_by_email(email)
        if existing is not None:
            await self._set_user_oidc(existing["user_id"], issuer, sub)
            await self.add_membership(org_id, existing["user_id"], "member")
            return existing["user_id"], False
        return await self._create_sso_user(email, issuer, sub, org_id), True

    # --- SCIM 2.0 provisioning -------------------------------------------------

    async def get_scim_config(self, org_id: str) -> dict:
        """The org's SCIM config: {enabled, group_role_map (dict), has_token}. Always returns a dict
        (defaults when no row yet) so the admin surface can render before first save."""
        row = await self._one("SELECT * FROM org_scim_configs WHERE org_id = ?", (org_id,))
        if row is None:
            return {"enabled": False, "group_role_map": {}, "has_token": False}
        try:
            gm = json.loads(row["group_role_map"] or "{}")
        except (ValueError, TypeError):
            gm = {}
        return {"enabled": bool(row["enabled"]), "group_role_map": gm,
                "has_token": bool(row["token_hash"])}

    async def set_scim_config(self, org_id: str, *, group_role_map: dict, enabled: bool) -> None:
        """Upsert the group->role map + enabled flag WITHOUT touching the token (generate/revoke own
        that column). Owner is stripped from the map at rest -- SCIM can never grant ownership."""
        clean = {str(g): r for g, r in group_role_map.items() if r in ROLES and r != "owner"}
        now = time.time()
        await self._exec(
            "INSERT INTO org_scim_configs (org_id, group_role_map, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (org_id) DO UPDATE SET "
            "group_role_map=excluded.group_role_map, enabled=excluded.enabled, "
            "updated_at=excluded.updated_at",
            (org_id, json.dumps(clean), int(enabled), now, now))

    async def generate_scim_token(self, org_id: str) -> str:
        """Mint (or rotate) the org's SCIM bearer -- returns the raw token ONCE, stores only its
        sha256 digest. Rotation is implicit: writing a new hash invalidates the prior token. Enables
        SCIM as a side effect (a token with the switch off would be dead on arrival)."""
        raw = "scim_" + secrets.token_urlsafe(32)
        now = time.time()
        await self._exec(
            "INSERT INTO org_scim_configs (org_id, token_hash, enabled, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?) ON CONFLICT (org_id) DO UPDATE SET "
            "token_hash=excluded.token_hash, enabled=1, updated_at=excluded.updated_at",
            (org_id, _token_hash(raw), now, now))
        return raw

    async def revoke_scim_token(self, org_id: str) -> None:
        """Clear the org's SCIM bearer (IdP can no longer provision). Leaves the group map intact."""
        await self._exec(
            "UPDATE org_scim_configs SET token_hash = NULL, updated_at = ? WHERE org_id = ?",
            (time.time(), org_id))

    async def org_by_scim_token(self, raw: str) -> str | None:
        """The org a live, ENABLED SCIM bearer authenticates -- else None. The ONLY auth path for the
        SCIM endpoints; hard-scopes every SCIM request to exactly one org (org A's token can never
        touch org B). No expiry (revocation is the lever, same as API tokens)."""
        if not raw:
            return None
        row = await self._one(
            "SELECT org_id FROM org_scim_configs WHERE token_hash = ? AND enabled = 1",
            (_token_hash(raw),))
        return row["org_id"] if row else None

    async def scim_provision(self, *, org_id: str, email: str, role: str,
                             external_id: str | None, issuer: str | None) -> tuple[str, bool]:
        """SCIM create: resolve the email to a user (creating one, email_verified, if new), attach a
        membership in THIS org at `role`, and set the oidc identity when the payload carried it.
        Returns (user_id, created_now). Idempotent: a repeat with the same email returns the existing
        user with created_now=False (the route turns that into the RFC 409 uniqueness response).
        Never grants owner (the route resolves role via resolve_scim_role, which excludes owner)."""
        existing = await self.get_user_by_email(email)
        if existing is not None:
            uid = existing["user_id"]
            if external_id and issuer and not existing.get("oidc_sub"):
                await self._set_user_oidc(uid, issuer, external_id)
            await self.add_membership(org_id, uid, role if role != "owner" else "member")
            return uid, False
        uid = secrets.token_hex(8)
        await self._exec(
            "INSERT INTO users (user_id, email, password_hash, email_verified, "
            "oidc_issuer, oidc_sub, created_at) VALUES (?, ?, NULL, 1, ?, ?, ?)",
            (uid, email.strip().lower(), issuer, external_id, time.time()))
        await self.add_membership(org_id, uid, role if role != "owner" else "member")
        return uid, True

    async def scim_deactivate(self, org_id: str, user_id: str) -> dict:
        """The deprovision money path. Kill this user's access to THIS org within the request:
        drop their org membership and revoke every credential BOUND to this org (sessions AND api
        tokens where auth_tokens.org_id == this org). Multi-org safe: their other-org-bound and
        their still-resolving unbound credentials survive UNLESS this was their last org -- when no
        membership remains anywhere, we also revoke their unbound credentials (a personal/unbound
        session would otherwise keep resolving), i.e. a global credential kill. Returns counts for
        the audit row. ponytail: two scoped DELETEs + a conditional third; no per-token loop."""
        removed = await self.remove_membership(org_id, user_id)
        killed = await self._exec_count(
            "DELETE FROM auth_tokens WHERE user_id = ? AND org_id = ?", (user_id, org_id))
        remaining = await self.list_user_memberships(user_id)
        if not remaining:  # last org -> also kill unbound (personal) credentials: total lockout
            killed += await self._exec_count(
                "DELETE FROM auth_tokens WHERE user_id = ? AND org_id IS NULL", (user_id,))
        return {"membership_removed": removed, "credentials_revoked": killed,
                "last_org": not remaining}
