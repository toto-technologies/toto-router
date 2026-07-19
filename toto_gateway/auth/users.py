"""User accounts: lookup, creation, verification, and per-user companion preferences."""

from __future__ import annotations

import secrets
import sqlite3
import time


class UsersMixin:
    async def get_user_by_email(self, email: str) -> dict | None:
        row = await self._one("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
        return dict(row) if row else None

    async def get_user(self, user_id: str) -> dict | None:
        row = await self._one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(row) if row else None

    async def create_user(self, email: str, password_hash: str | None, *,
                    email_verified: bool = False, google_sub: str | None = None) -> str:
        """Insert a user, return its user_id. Raises sqlite3.IntegrityError on duplicate email."""
        user_id = secrets.token_hex(8)
        try:
            await self._exec(
                "INSERT INTO users (user_id, email, password_hash, email_verified, "
                "google_sub, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, email.strip().lower(), password_hash, int(email_verified),
                 google_sub, time.time()),
            )
        except Exception as exc:  # normalize PG's UniqueViolation to the sqlite contract
            if self._pg and type(exc).__name__ == "UniqueViolation":
                raise sqlite3.IntegrityError(str(exc)) from exc
            raise
        # Provision the personal org (owner) at creation so every new user is tenanted from
        # request #1; existing users get theirs lazily via resolve_membership.
        await self.resolve_membership(user_id)
        return user_id

    async def mark_verified(self, user_id: str) -> None:
        await self._exec("UPDATE users SET email_verified = 1 WHERE user_id = ?", (user_id,))

    async def has_users(self) -> bool:
        return await self._one("SELECT 1 FROM users LIMIT 1") is not None

    async def set_companion_conv(self, user_id: str, conv_id: str) -> None:
        """Pin the user's eternal companion conversation (set once, on the first message)."""
        await self._exec(
            "UPDATE users SET companion_conv_id = ? WHERE user_id = ?", (conv_id, user_id),
        )

    async def companion_conv(self, user_id: str) -> str | None:
        row = await self._one(
            "SELECT companion_conv_id FROM users WHERE user_id = ?", (user_id,),
        )
        return row["companion_conv_id"] if row else None

    async def set_companion_model(self, user_id: str, model: str | None) -> None:
        """The user's chat-model lever; None clears back to the configured default."""
        await self._exec(
            "UPDATE users SET companion_model = ? WHERE user_id = ?", (model, user_id),
        )

    async def companion_model(self, user_id: str) -> str | None:
        row = await self._one(
            "SELECT companion_model FROM users WHERE user_id = ?", (user_id,),
        )
        return row["companion_model"] if row else None
