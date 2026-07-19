"""BYOK provider keys, per-user and org-wide. Ciphertext in, ciphertext out (Fernet — see
credentials.py); last4 is a plaintext UI hint only."""

from __future__ import annotations

import time


class ProviderKeysMixin:
    # --- per-user keys ---------------------------------------------------------
    # STRICT per-user scoping: every method keys on user_id, no NULL grandfathering. Anonymous
    # (user_id None) has no keys — `WHERE user_id = ?` with None matches nothing (fail closed).

    async def set_provider_key(self, user_id: str, provider: str, encrypted: str,
                               last4: str) -> None:
        """Upsert this user's encrypted key for a provider (re-PUT replaces it)."""
        await self._exec(
            "INSERT INTO provider_keys (user_id, provider, encrypted_key, last4, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, provider) DO UPDATE SET "
            "encrypted_key = excluded.encrypted_key, last4 = excluded.last4, "
            "created_at = excluded.created_at",
            (user_id, provider, encrypted, last4, time.time()),
        )

    async def list_provider_keys(self, user_id: str) -> list[dict]:
        """This user's providers + last4 — NO key material. For the Settings status list."""
        rows = await self._all(
            "SELECT provider, last4 FROM provider_keys WHERE user_id = ? ORDER BY provider",
            (user_id,),
        )
        return [dict(r) for r in rows]

    async def get_provider_key_map(self, user_id: str) -> dict[str, str]:
        """{provider: encrypted_key} for the auth-time BYOK load. Ciphertext only (decrypt in
        credentials.py)."""
        rows = await self._all(
            "SELECT provider, encrypted_key FROM provider_keys WHERE user_id = ?",
            (user_id,),
        )
        return {r["provider"]: r["encrypted_key"] for r in rows}

    async def delete_provider_key(self, user_id: str, provider: str) -> None:
        await self._exec(
            "DELETE FROM provider_keys WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        )

    # --- org-wide keys (routes/org_credentials.py) -----------------------------
    # Same posture as the per-user methods, keyed on org_id. `WHERE org_id = ?` with None
    # matches nothing (fail closed for the org-less caller).

    async def set_org_provider_key(self, org_id: str, provider: str, encrypted: str,
                                   last4: str) -> None:
        await self._exec(
            "INSERT INTO org_provider_keys (org_id, provider, encrypted_key, last4, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(org_id, provider) DO UPDATE SET "
            "encrypted_key = excluded.encrypted_key, last4 = excluded.last4, "
            "created_at = excluded.created_at",
            (org_id, provider, encrypted, last4, time.time()),
        )

    async def list_org_provider_keys(self, org_id: str) -> list[dict]:
        rows = await self._all(
            "SELECT provider, last4, created_at FROM org_provider_keys WHERE org_id = ? "
            "ORDER BY provider",
            (org_id,),
        )
        return [dict(r) for r in rows]

    async def get_org_provider_key_map(self, org_id: str) -> dict[str, str]:
        rows = await self._all(
            "SELECT provider, encrypted_key FROM org_provider_keys WHERE org_id = ?",
            (org_id,),
        )
        return {r["provider"]: r["encrypted_key"] for r in rows}

    async def delete_org_provider_key(self, org_id: str, provider: str) -> None:
        await self._exec(
            "DELETE FROM org_provider_keys WHERE org_id = ? AND provider = ?",
            (org_id, provider),
        )
