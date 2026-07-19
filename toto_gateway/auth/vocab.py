"""Role and policy-mode vocabularies shared by the store, the admin routes, and SCIM."""

from __future__ import annotations

# Catalog-policy modes (fail-closed: anything else is rejected at write time). Two disjoint
# vocabularies share the `mode` column by row scope: a per-TEAM row carries an allow/deny model
# LIST; the ORG-DEFAULT row (team_id == org_id sentinel) carries the org GOVERNANCE mode —
# allow_all (permissive) or allowlist (deny-by-default: only the org's approved set resolves).
# The admin endpoints validate each path against its own subset; the store guard is the union so
# an unknown string is still rejected at write time.
CATALOG_TEAM_MODES = ("allow", "deny")          # per-team list mode
CATALOG_ORG_MODES = ("allow_all", "allowlist")  # org governance mode
CATALOG_MODES = CATALOG_TEAM_MODES + CATALOG_ORG_MODES

# Routing-overlay optimize presets — mirrors benchmarks.OPTIMIZE, fail-closed at write time.
ROUTING_OPTIMIZE = ("quality", "balanced", "cost")

# The rank ladder (owner > admin > member) plus the lateral read-only `auditor`. `auditor` is a
# VALID assignable role but is NOT in the rank ladder (deps._ROLE_RANK) — it grants read-only
# access to org surfaces and refuses every mutation. Membership/invite validation accepts it
# here; the read-vs-write gating lives in require_role / require_read_role.
ROLES = ("owner", "admin", "member", "auditor")

# SCIM group->role resolution: a user in several mapped IdP groups gets the HIGHEST role.
# `owner` is DELIBERATELY absent — SCIM can never grant ownership (a hard rule: ownership is the
# billing/deletion authority, never IdP-assignable). `auditor` (read-only, lateral) sits below
# `member` here so an admin+auditor user resolves to admin (most capable wins). Unmapped groups
# contribute nothing; no mapped group at all -> the default `member`.
_SCIM_ROLE_RANK = {"auditor": 1, "member": 2, "admin": 3}


def resolve_scim_role(groups: list[str], group_role_map: dict) -> str:
    """Highest role the user's IdP groups map to, else 'member'. Owner is never grantable (filtered
    even if the map names it). `groups` are IdP group display names; `group_role_map` is
    {group_name: role}. ponytail: linear scan over a user's groups -- a handful per user, not a hot
    path (runs on create/PATCH only)."""
    best, best_rank = "member", _SCIM_ROLE_RANK["member"]
    for g in groups:
        role = group_role_map.get(g)
        if role == "owner" or role not in _SCIM_ROLE_RANK:
            continue  # owner never grantable; unmapped/unknown ignored
        if _SCIM_ROLE_RANK[role] > best_rank:
            best, best_rank = role, _SCIM_ROLE_RANK[role]
    return best
