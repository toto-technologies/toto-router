"""Append-only audit emit — the fire-and-forget-safe wrapper over AuthStore.write_audit.

ONE emit path for the SOC2 audit floor (`audit_events`, widened in C3 with org_id + target +
metadata so admin/policy events are org-scoped-readable at GET /v1/admin/audit). NEVER raises:
observability must never break a request, exactly like the trace writer — a failing sink swallows
its exception rather than propagating into the caller's response path.

Contract for siblings (C2 tenancy, C5 policy) — call at the mutation point, best-effort:

    from .. import audit
    await audit.record(store, "admin:policy.update",
                       actor_user_id=identity.user_id, org_id=identity.org_id,
                       target_type="policy", target_id=team_id,
                       meta={"before": old, "after": new},
                       ip=ip, request_id=rid)

`store` is `request.app.state.auth` (the AuthStore). `action` uses the `admin:*` namespace for
control-plane mutations (policy/role/tenancy changes); auth events keep their bare verbs
(login/logout/token_mint/…). `meta` is any JSON-serializable object — serialized here, never
content, metadata only.
"""

from __future__ import annotations

import json


async def record(store, action: str, *, actor_user_id: str | None = None,
                 org_id: str | None = None, target_type: str | None = None,
                 target_id: str | None = None, meta: object | None = None,
                 ip: str | None = None, request_id: str | None = None) -> None:
    """Append one immutable audit row. Best-effort: swallows every failure (store absent, DB
    down, unserializable meta) so a failing sink can't break the caller's request."""
    if store is None:
        return
    try:
        await store.write_audit(
            action, user_id=actor_user_id, org_id=org_id, ip=ip, request_id=request_id,
            target_type=target_type, target_id=target_id,
            metadata=json.dumps(meta) if meta is not None else None,
        )
    except Exception:  # observability never breaks a request (quickstart principle 5)
        pass
