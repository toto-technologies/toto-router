"""AuthStore — users + opaque server-side sessions, same SQLite file as RunStore.

Separate from RunStore because auth must gate the app regardless of the driver flag (RunStore
is only built when the driver is on). Same idioms: stdlib sqlite3, WAL, single connection
guarded by a lock, CREATE TABLE IF NOT EXISTS + guarded ALTERs. No auth/crypto library:
`hashlib.scrypt` for passwords, `secrets` for tokens, `hmac.compare_digest` for every compare.

One class, one concern per mixin: the mixins share the AsyncStoreMixin DB surface
(self._one/_all/_exec/_exec_count) and each other's public methods, nothing else.
"""

from __future__ import annotations

import threading

from .. import db as _db_mod
from .audit import AuditMixin
from .policies import PoliciesMixin
from .provider_keys import ProviderKeysMixin
from .schema import _SCHEMA, apply_migrations
from .sso import SsoScimMixin
from .tenancy import TenancyMixin
from .tokens import TokensMixin
from .users import UsersMixin


class AuthStore(UsersMixin, ProviderKeysMixin, TenancyMixin, PoliciesMixin, SsoScimMixin,
                AuditMixin, TokensMixin, _db_mod.AsyncStoreMixin):
    def __init__(self, path: str = ":memory:", database_url: str = "",
                 pool: dict | None = None) -> None:
        from .. import db as _db

        self._db, self._pg = _db.connect(database_url, path)  # sync conn: init DDL
        self._pool = _db.make_async_pool(database_url, **(pool or {}))  # async pool: runtime queries
        self._db.executescript(_SCHEMA)
        apply_migrations(self._db, self._pg)
        self._db.commit()
        self._lock = threading.Lock()

    async def ping(self) -> None:
        """Readiness probe — raises if the DB connection is unusable. /readyz goes through this."""
        await self._one("SELECT 1")
