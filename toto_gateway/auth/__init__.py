"""Auth plane: AuthStore (users, tenancy, tokens, policies, SSO/SCIM, audit) plus the password
and vocabulary helpers. Split by concern into sibling modules; this package root re-exports the
full public surface so `from toto_gateway.auth import X` keeps working unchanged."""

from .crypto import (
    _DUMMY_HASH,
    _token_hash,
    burn_dummy_hash,
    hash_password,
    verify_password,
)
from .schema import _SCHEMA
from .store import AuthStore
from .tenancy import _parse_retention
from .tokens import _LAST_USED_THROTTLE, API_TOKEN_TTL, VERIFY_TTL
from .vocab import (
    _SCIM_ROLE_RANK,
    CATALOG_MODES,
    CATALOG_ORG_MODES,
    CATALOG_TEAM_MODES,
    ROLES,
    ROUTING_OPTIMIZE,
    resolve_scim_role,
)

__all__ = [
    "API_TOKEN_TTL",
    "AuthStore",
    "CATALOG_MODES",
    "CATALOG_ORG_MODES",
    "CATALOG_TEAM_MODES",
    "ROLES",
    "ROUTING_OPTIMIZE",
    "VERIFY_TTL",
    "burn_dummy_hash",
    "hash_password",
    "resolve_scim_role",
    "verify_password",
]
