"""Shared route dependencies: identity resolution + the app gate.

`require_auth` resolves the caller to an Identity and returns it (routes that ignore the return
keep working — the old `_auth: None = Depends(require_auth)` shape is unchanged). Three
credentials: the operator bearer token (a permanent service credential, timing-safe compare),
a per-user API bearer (minted at POST /v1/tokens, sha256-at-rest, resolves to a normal user
Identity), and the `toto_session` cookie (a verified user). See docs/api.md.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from ..benchmarking.domain import CredentialScopeRef

SESSION_COOKIE = "toto_session"
# Operator token in cookie form: set by the dev-dashboard credential helper (dev_auth.html) so
# requests that can't carry an Authorization header — iframe/document loads, EventSource SSE,
# open-in-new-tab links — authenticate too. Same credential, same timing-safe compare; unsafe
# methods stay guarded by _origin_ok + SameSite=Lax like the session cookie.
OPERATOR_COOKIE = "toto_operator"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Single-tenant sentinel scope for the OSS edition. The operator IS the only tenant, so its
# routing/governance lives under one well-known key: the console (authed as operator) reads/writes
# the org-default routing policy here, and the operator's own bearer traffic resolves policy from
# the same key — so a console binding actually governs `Bearer <token>` requests. Enterprise keeps
# the unscoped operator (no implicit org; a multi-tenant super-credential must name an org).
OSS_LOCAL_ORG = "local"


@dataclass(frozen=True)
class Identity:
    """Who is calling. `user_id` is the scoping key: set → a logged-in user (reads/writes filter
    STRICTLY to their own rows — never another user's, never legacy NULL-owner rows; creates stamp
    it); None → unscoped operator (sees all, stamps NULL). `authenticated` gates the app: only an
    unauthenticated caller is turned away with 401 (a session user and the operator are both
    authenticated=True)."""

    user_id: str | None = None
    email: str | None = None
    is_operator: bool = False
    authenticated: bool = False
    # Content-plane routing key, resolved SERVER-SIDE from the verified user — never from a
    # header/param/slug. None for the operator: the content plane refuses unscoped access.
    tenant_id: str | None = None
    # Control-plane tenancy: the org this caller belongs to, their role in it (owner/admin/
    # member), and their team (None when teamless). Resolved SERVER-SIDE from `memberships` off the
    # verified user — never a header/param. None for the operator and anonymous. tenant_id stays
    # == user_id (the content-plane routing key keeps STRICT per-user isolation, IDOR discipline);
    # org_id is the NEW group dimension above it. require_role() reads `role`.
    org_id: str | None = None
    team_id: str | None = None
    role: str | None = None
    # Zero-retention mode: True => this caller's org opted out of ALL durable payload
    # persistence, so every telemetry sink (request_content, response cache, experience corpus,
    # driver spans, LangSmith mirror) must skip the payload regardless of env flags. Resolved
    # SERVER-SIDE from `organizations.zero_retention` at auth time (fail-closed: an org present but
    # unreadable resolves True — when in doubt, don't persist). None org (operator/anon/thin) => False
    # => env flags apply, exactly as before. Read at the sinks via getattr(identity, "zero_retention").
    zero_retention: bool = False
    # Catalog-scoped RBAC: the caller's team catalog policy blob (allow/deny + residency +
    # default_model), resolved SERVER-SIDE from `catalog_policies` at auth time — never a header.
    # None when the caller has no team / no policy (the common case) → effective_policy returns
    # None → routing is unchanged. `effective_policy` reads this field; carrying it on Identity
    # keeps that seam synchronous (no store handle in the gateway) and makes it flow to the driver
    # plane via the request contextvar.
    catalog_policy: dict | None = None
    # Per-team routing overlay: the caller's team tag->model bindings + optimize preset,
    # resolved SERVER-SIDE from `routing_policies` at auth time — never a header. None when the
    # caller has no team / no policy (the common case) → effective_policy carries no overlay →
    # routing is byte-identical global. Threaded to the driver plane via effective_policy(identity),
    # reached through the request contextvar (same seam as catalog_policy).
    routing_policy: dict | None = None
    # Server-side catalog adoptions: the caller's adopted provider-library models
    # as materialized CatalogEntry dicts, resolved SERVER-SIDE from `catalog_adoptions` at auth time
    # — never a header/param. () when the caller adopted nothing (the common case) → effective_catalog
    # returns the base catalog unchanged. Read by catalog.effective_catalog, threaded to the driver
    # plane via the request contextvar (same seam as catalog_policy/routing_policy).
    catalog_adoptions: tuple[dict, ...] = field(default_factory=tuple)
    # Merged price overrides for this caller (model_id → override row), team>org>platform
    # precedence already applied by _resolve_price_overrides. Empty for operator-scope callers
    # only when no platform overrides exist. Consumed by catalog.effective_catalog.
    price_overrides: dict = field(default_factory=dict)
    # Org deny-by-default gate: when the caller's org is in allowlist mode, this is the frozen
    # set of catalog ids the org has approved (its allow list + org catalog adoptions), resolved
    # SERVER-SIDE from the org-default `catalog_policies` row at auth time — never a header. None
    # when the org is allow_all / has no policy (the common case) → effective_policy adds no gate →
    # routing is unchanged. Read by effective_policy → Policy.org_allowlist; also read directly in
    # Gateway._plan for the clean model_not_permitted 403. Threaded to the driver plane via the same
    # request-contextvar seam as catalog_policy.
    org_allowlist: frozenset[str] | None = None
    # Credential resolution is authoritative per provider. CandidateCatalog must never infer a
    # platform fallback from whichever snapshot rows happen to exist because a valid BYOK scope
    # may legitimately have zero offers.
    provider_credential_scopes: Mapping[str, CredentialScopeRef] = field(default_factory=dict)
    # Configured or indeterminate BYOK that could not be read is distinct from absence: only the
    # affected provider must reject platform-key fallback; local and other providers stay usable.
    unavailable_provider_credentials: frozenset[str] = field(default_factory=frozenset)
    # Provenance of a write: the credential class the caller
    # used, recorded on object/position writes so the frontend can render "who did this". Derived
    # from HOW the caller authenticated, not who they are: a browser session cookie is the user's
    # own HAND ("user"); a per-user API token is a programmatic/agent caller (MCP, pi, scripts →
    # "agent"); the operator bearer is ops ("operator"). The in-process companion stamps "agent"
    # directly (it never carries an HTTP Identity — it IS Toto).
    actor: str = "user"


# The unauthenticated sentinel: no credential resolved. Not a user — the require_auth gate turns
# it away with 401. (Login is required for every route that depends on require_auth.)
ANONYMOUS = Identity()
OPERATOR = Identity(user_id=None, email=None, is_operator=True, authenticated=True, actor="operator")

# The resolved caller for the current request, set by require_auth. Its purpose is the DRIVER
# plane (catalog-scope enforcement at dispatch): the driver's complete_fn is a boot-time closure
# that never receives the per-request Identity as a param, but it runs inside the request's
# context (the /v1/route handler awaits driver.run in-line; a spawned run copies the context at
# create_task), so it can read the caller here and pass identity=... to gateway.complete.
# ponytail: a contextvar is the minimal per-request thread — a full identity field on the driver
# graph state is the deferred routing-principal seam, unneeded for catalog scope.
current_identity_var: ContextVar[Identity | None] = ContextVar("toto_gw_identity", default=None)


def current_identity() -> Identity | None:
    """The caller resolved for this request/run context, or None (unset, or an internal call)."""
    return current_identity_var.get()


def _resolve_tenant(user_id: str | None) -> str | None:
    """Content-plane tenant key: tenant_id == user_id (STRICT per-user isolation, IDOR discipline).
    The control-plane org lives separately on `Identity.org_id` (see _resolve_org) — kept distinct
    so populating the group layer never widens content-plane visibility. Operator (None) → no tenant."""
    return user_id


async def _resolve_org(auth, user_id: str | None,
                       preferred_org_id: str | None = None) -> tuple[str | None, str | None, str | None]:
    """(org_id, team_id, role) for a verified user, resolved from `memberships` (lazily
    provisioning a personal owner-org on first sight — the backfill seam). `preferred_org_id` is the
    caller's credential org binding: a switched session's active org or an org-scoped API
    token's org; when the user still holds a membership there it wins over oldest-row, else it falls
    through safely. Fail-open: if the auth store is absent or the lookup fails, return the permissive
    (None, None, None) so a resolution hiccup degrades to identity-thin rather than 500-ing the
    request. Operator/anon → all None."""
    if auth is None or user_id is None:
        return (None, None, None)
    try:
        m = await auth.resolve_membership(user_id, preferred_org_id=preferred_org_id)
    except Exception:  # tenancy resolution must never break an authenticated request
        return (None, None, None)
    return (m.get("org_id"), m.get("team_id"), m.get("role"))


async def _resolve_zero_retention(auth, org_id: str | None) -> bool:
    """The org's zero-retention switch, resolved once at auth time so every telemetry sink
    gates synchronously off the Identity (no store handle in the gateway/driver). No org (operator/
    anon/thin caller) -> False -> env flags apply. Fail-CLOSED on a read error for a KNOWN org: a
    privacy opt-out we can't confirm-negative must default to not-persisting (unlike the fail-open
    tenancy resolvers, because here the safe direction is retention-off, not retention-on)."""
    if auth is None or org_id is None:
        return False
    try:
        row = await auth.get_org(org_id)
    except Exception:  # can't read the org's setting -> assume opted out (don't persist payload)
        return True
    if row is None:
        return False  # no such org row -> nothing opted out; env flags apply
    return bool(row.get("zero_retention"))


async def _resolve_catalog_policy(auth, team_id: str | None) -> dict | None:
    """The caller's team catalog policy, or None. Only queried when the caller has a team, so
    a personal-org user (team_id None — the common case) pays ZERO extra DB work. Fail-open: any
    lookup hiccup degrades to None (permissive), never a 500 on the auth path. ponytail: one
    indexed SELECT (team_id PK) per authed team request; cache per-request if it shows in p95."""
    if auth is None or not team_id:
        return None
    try:
        return await auth.get_catalog_policy(team_id)
    except Exception:
        return None


async def _resolve_org_allowlist(auth, org_id: str | None) -> frozenset[str] | None:
    """The org's approved-model set, or None when the org isn't in allowlist mode. Reads the
    ORG-DEFAULT catalog policy (the row keyed by org_id — the sentinel, same pattern as routing);
    only when its mode is 'allowlist' do we build the set = its models list + the org's catalog
    adoptions (adopted models are implicitly approved). allow_all / no row / any other mode → None
    (permissive, today's behavior). Fail-open: any lookup hiccup degrades to None, never a 500 on
    the auth path. ponytail: one extra indexed SELECT (org_id PK) per authed request, matching the
    routing overlay's cost; the adoptions SELECT runs ONLY in allowlist mode (the enterprise case)."""
    if auth is None or not org_id:
        return None
    try:
        org_policy = await auth.get_catalog_policy(org_id)  # org-default sentinel row
        if not org_policy or org_policy.get("mode") != "allowlist":
            return None
        approved = set(org_policy.get("models") or [])
        approved |= {row["id"] for row in await auth.list_adoptions(org_id)}
        return frozenset(approved)
    except Exception:  # org-gate resolution must never break an authenticated request
        return None


async def _resolve_routing_policy(auth, org_id: str | None, team_id: str | None) -> dict | None:
    """The caller's routing overlay. A team member gets their TEAM policy, falling back to the
    ORG-DEFAULT policy (stored under the org_id key) when their team has none; a teamless caller —
    a personal-org OWNER, the pi / API-token common case — reads the org-default directly. The
    fallback is on the ROW, not the key: a team with no routing row must still honor the console's
    org-default (an "org default" that skipped every team member wasn't a default at all). No key
    at all (operator/anon) → None (pure global routing). Fail-open: any lookup hiccup degrades to
    None, never a 500 on the auth path."""
    if auth is None or not (team_id or org_id):
        return None
    try:
        if team_id:
            team_policy = await auth.get_routing_policy(team_id)
            if team_policy is not None:
                return team_policy  # team overlay wins outright when present
        return await auth.get_routing_policy(org_id) if org_id else None
    except Exception:
        return None


async def _resolve_adoptions(auth, org_id: str | None, team_id: str | None) -> tuple[dict, ...]:
    """The caller's adopted catalog entries as materialized CatalogEntry dicts.
    Scope key = team_id or org_id — the SAME fallback as _resolve_routing_policy, so a personal-org
    OWNER's own adoptions apply to their traffic (the pi / API-token common case). No key at all
    (operator/anon) → () (base catalog only). Fail-open: any lookup hiccup degrades to () (base
    only), never a 500 on the auth path. ponytail: one indexed SELECT (scope_key is the PK prefix)
    per authed request; cache per-request if it shows in p95."""
    key = team_id or org_id
    if auth is None or not key:
        return ()
    try:
        return tuple(row["entry"] for row in await auth.list_adoptions(key))
    except Exception:
        return ()


async def _resolve_price_overrides(auth, org_id: str | None, team_id: str | None) -> dict:
    """Merged price overrides for the caller: one indexed SELECT over (platform, org, team) scope
    keys, merged narrowest-last so team beats org beats platform per model_id. Fail-open to {}
    (base prices), same degradation discipline as _resolve_adoptions."""
    if auth is None:
        return {}
    try:
        rows = await auth.list_price_overrides("platform", org_id or "", team_id or "")
    except Exception:
        return {}
    merged: dict[str, dict] = {}
    for scope in ("platform", org_id, team_id):  # later (narrower) wins
        if not scope:
            continue
        for row in rows:
            if row["scope_key"] == scope:
                merged[row["model_id"]] = row
    return merged


async def _operator_identity(settings, auth) -> Identity:
    """The resolved operator identity. Enterprise → the pure unscoped OPERATOR. OSS → the operator
    bound to the single-tenant `local` scope, carrying the routing overlay stored there, so the
    governance the operator sets in the console governs its OWN bearer traffic (effective_policy
    reads routing_policy off the Identity). Resolved fresh per request → console edits apply live."""
    if settings.edition.strip().lower() != "oss":
        return OPERATOR
    routing_policy = await _resolve_routing_policy(auth, OSS_LOCAL_ORG, None)
    return replace(OPERATOR, org_id=OSS_LOCAL_ORG, routing_policy=routing_policy)


async def _resolve_identity(request: Request) -> Identity:
    """Operator bearer (timing-safe) OR user API bearer (sha256 lookup) OR toto_session
    cookie → verified user, else unauthenticated."""
    settings = request.app.state.settings
    header = request.headers.get("authorization", "")
    if not header:
        # Anthropic-SDK clients (Claude Code, anthropic-python) authenticate with x-api-key
        # instead of a bearer. Same credentials, different header name — alias it here so
        # every downstream lookup (operator compare, user-token sha256) works unchanged.
        key = request.headers.get("x-api-key", "")
        if key:
            header = f"Bearer {key}"
    auth = getattr(request.app.state, "auth", None)
    if settings.auth_token:
        if hmac.compare_digest(header, f"Bearer {settings.auth_token}"):
            return await _operator_identity(settings, auth)
        cookie = request.cookies.get(OPERATOR_COOKIE, "")
        if cookie and hmac.compare_digest(cookie.encode(), settings.auth_token.encode()):
            return await _operator_identity(settings, auth)
    # A bearer that isn't the operator token: a per-user API token OR an org-owned service token
    # (both sha256-at-rest). ONE indexed read resolves either (resolve_bearer), plus a distinct
    # 401 `token_expired` for a lapsed token so a client can tell expiry from revocation.
    if auth is not None and header.startswith("Bearer "):
        bearer = header[len("Bearer "):]
        res = await auth.resolve_bearer(bearer)
        if res is not None:
            if res["expired"]:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={"error": {"message": "token expired",
                                      "type": "authentication_error", "code": "token_expired"}},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if res["purpose"] == "service":
                # Org-owned service credential: org-scoped, role member, actor 'service'.
                # Never tied to a person — user_id/tenant_id carry the owning org id (strict scoping,
                # never operator). No membership lookup: the org comes straight off the token.
                org_id = res["org_id"]
                routing_policy = await _resolve_routing_policy(auth, org_id, None)
                adoptions = await _resolve_adoptions(auth, org_id, None)
                price_overrides = await _resolve_price_overrides(auth, org_id, None)
                org_allowlist = await _resolve_org_allowlist(auth, org_id)
                zero_retention = await _resolve_zero_retention(auth, org_id)
                return Identity(user_id=org_id, email=None, authenticated=True,
                                tenant_id=org_id, org_id=org_id, team_id=None, role="member",
                                routing_policy=routing_policy, catalog_adoptions=adoptions,
                                price_overrides=price_overrides,
                                org_allowlist=org_allowlist, zero_retention=zero_retention,
                                actor="service")
            user = await auth.get_user(res["user_id"])
            if user is not None and not (settings.require_email_verify
                                         and not user["email_verified"]):
                bound_org = res["org_id"]  # the token's org binding (read in the same row above)
                org_id, team_id, role = await _resolve_org(auth, user["user_id"], bound_org)
                catalog_policy = await _resolve_catalog_policy(auth, team_id)
                routing_policy = await _resolve_routing_policy(auth, org_id, team_id)
                adoptions = await _resolve_adoptions(auth, org_id, team_id)
                price_overrides = await _resolve_price_overrides(auth, org_id, team_id)
                org_allowlist = await _resolve_org_allowlist(auth, org_id)
                zero_retention = await _resolve_zero_retention(auth, org_id)
                return Identity(user_id=user["user_id"], email=user["email"],
                                authenticated=True, tenant_id=_resolve_tenant(user["user_id"]),
                                org_id=org_id, team_id=team_id, role=role,
                                catalog_policy=catalog_policy, routing_policy=routing_policy,
                                catalog_adoptions=adoptions, price_overrides=price_overrides,
                                org_allowlist=org_allowlist,
                                zero_retention=zero_retention,
                                actor="agent")  # API token → programmatic caller (MCP/pi/scripts)
    raw = request.cookies.get(SESSION_COOKIE)
    if raw and auth is not None:
        user = await auth.session_user(raw, require_verified=settings.require_email_verify)
        if user is not None:
            active_org = await auth.token_org(raw, "session")  # per-session active org
            org_id, team_id, role = await _resolve_org(auth, user["user_id"], active_org)
            catalog_policy = await _resolve_catalog_policy(auth, team_id)
            routing_policy = await _resolve_routing_policy(auth, org_id, team_id)
            adoptions = await _resolve_adoptions(auth, org_id, team_id)
            price_overrides = await _resolve_price_overrides(auth, org_id, team_id)
            org_allowlist = await _resolve_org_allowlist(auth, org_id)
            zero_retention = await _resolve_zero_retention(auth, org_id)
            return Identity(user_id=user["user_id"], email=user["email"], authenticated=True,
                            tenant_id=_resolve_tenant(user["user_id"]),
                            org_id=org_id, team_id=team_id, role=role,
                            catalog_policy=catalog_policy, routing_policy=routing_policy,
                            catalog_adoptions=adoptions, price_overrides=price_overrides,
                            org_allowlist=org_allowlist,
                            zero_retention=zero_retention,
                            actor="user")  # browser session → the user's own hand
    return ANONYMOUS


def _origin_ok(request: Request) -> bool:
    """Reject unsafe cross-site requests: when an Origin header is present on a state-changing
    method, its host:port must match the request's Host. Absent Origin (curl/scripts) passes —
    SameSite=Lax already blocks the browser cross-site case; this is belt-and-suspenders."""
    if request.method not in UNSAFE_METHODS:
        return True
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlparse(origin).netloc == request.headers.get("host", "")


async def require_auth(request: Request) -> Identity:
    """Resolve identity and enforce the app gate. Login is required: a caller with no valid
    credential (neither the operator bearer nor a session cookie) gets 401. Routes that ignore
    the return value still work — the gate raises before they run."""
    if not _origin_ok(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": {"message": "cross-origin request rejected",
                              "type": "authentication_error", "code": "origin_mismatch"}},
        )
    identity = await _resolve_identity(request)
    # Both a session user and the operator are authenticated=True; only the unauthenticated
    # sentinel is turned away.
    if not identity.authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": {"message": "missing or invalid credentials",
                              "type": "authentication_error", "code": "invalid_token"}},
            headers={"WWW-Authenticate": "Bearer"},
        )
    from ..credentials import PROVIDERS, byok_unavailable_envs, load_byok

    provider_scopes = await load_byok(
        request.app.state.settings, request.app.state.auth, identity.user_id, identity.org_id
    )
    unavailable_envs = byok_unavailable_envs.get()
    identity = replace(
        identity,
        provider_credential_scopes=provider_scopes,
        unavailable_provider_credentials=frozenset(
            provider for provider, definition in PROVIDERS.items()
            if definition.api_key_env in unavailable_envs),
    )
    request.state.user_id = identity.user_id  # picked up by the request log line (obs.py)
    from ..obs import user_id_var  # also expose to Sentry event tags

    user_id_var.set(identity.user_id)
    current_identity_var.set(identity)  # driver plane reads this to enforce catalog scope
    return identity


# --- Role gate --------------------------------------------------------------------------------
# Org RBAC: owner > admin > member, a simple total order. The operator bearer (the platform
# super-credential, user_id=None) sits ABOVE org RBAC and always passes. Fail-closed: no role /
# too low → 403.
_ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}
# The lateral read-only role: DELIBERATELY absent from _ROLE_RANK. Because rank lookup
# defaults to 0, an auditor is BELOW `member` and so fails EVERY plain require_role gate — every
# mutation route excludes it by construction, no per-route edit. It is admitted only where a read
# route opts in via require_read_role / require_role(..., allow=("auditor",)).
AUDITOR = "auditor"


def require_role(min_role: str, *, allow: tuple[str, ...] = ()):
    """FastAPI dependency factory: gate a route on a minimum org role. Returns the Identity on
    success (so a handler can `identity: Identity = Depends(require_role("admin"))`). Layers on
    require_auth, so an unauthenticated caller is already turned away with 401 before this runs;
    an authenticated caller whose role is below `min_role` gets 403.

    `allow` lists LATERAL roles (outside the rank ladder) that also pass regardless of rank — the
    seam for the read-only auditor on GET routes (see require_read_role). Everyone not in `allow`
    must still meet the `min_role` floor."""
    if min_role not in _ROLE_RANK:
        raise ValueError(f"unknown role {min_role!r}")
    floor = _ROLE_RANK[min_role]
    allow_set = frozenset(allow)

    async def _require(identity: Identity = Depends(require_auth)) -> Identity:
        if identity.is_operator:  # platform super-credential — above org RBAC
            return identity
        if identity.role in allow_set:  # lateral opt-in (auditor on reads), rank-independent
            return identity
        if _ROLE_RANK.get(identity.role or "", 0) < floor:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error": {"message": f"requires role {min_role} or higher",
                                  "type": "authorization_error", "code": "insufficient_role"}},
            )
        return identity

    return _require


def require_read_role(min_role: str = "member"):
    """Read gate for org-scoped GET routes: the require_role floor PLUS the lateral auditor role.
    Use ONLY on read-only routes an auditor may see; every mutation keeps plain require_role, which
    excludes auditor by construction (it is not in the rank ladder)."""
    return require_role(min_role, allow=(AUDITOR,))


# --- Idempotency -------------------------------------------------------------------------------
# Opt-in per request via the Idempotency-Key header: absent -> behaves EXACTLY as if the feature
# didn't exist. Present -> a client retry after a network blip replays the first response instead
# of double-executing a create (double token spend, duplicate lists). Backed by RunStore's
# idempotency_keys table. ponytail ceiling: an in-flight duplicate (claimed but not yet
# stored -- a sub-second double-submit) gets 409 retry rather than blocking on a distributed lock;
# add a lease/poll column only if a real client needs block-and-wait.
IDEMPOTENCY_HEADER = "idempotency-key"


class Idem:
    """Idempotency guard for one create request. Call `replay()` at the mutation point (after
    validation, before the side effect): it returns a JSONResponse to short-circuit-replay, raises
    409 on an in-flight duplicate, or returns None to proceed (we won the claim, or the header is
    absent). Wrap the success return in `store(value, status_code)` to seal the key. Inactive (no
    header or no store) -> both are no-ops, so wiring a route is two lines and changes nothing when
    the client sends no key."""

    def __init__(self, store, user_id: str | None, key: str | None, method: str, path: str) -> None:
        self._store = store
        self._user_id = user_id
        self._key = key
        self._method = method
        self._path = path

    @property
    def active(self) -> bool:
        return self._key is not None and self._store is not None

    async def replay(self) -> JSONResponse | None:
        if not self.active:
            return None
        outcome = await self._store.claim_idempotency(self._user_id, self._key,
                                                       self._method, self._path)
        if outcome == "won":
            return None  # first writer: proceed, then store()
        if outcome is not None and outcome["status_code"] is not None:
            return JSONResponse(status_code=outcome["status_code"],
                                content=json.loads(outcome["response_json"]))
        # Claimed by a concurrent request that hasn't stored its result yet.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"message": "a request with this Idempotency-Key is still in "
                                         "flight — retry shortly",
                              "type": "conflict", "code": "idempotency_in_flight"}},
        )

    async def store(self, value, status_code: int):
        if self.active:
            await self._store.store_idempotency_result(
                self._user_id, self._key, status_code, json.dumps(jsonable_encoder(value)))
        return value


async def idempotency(request: Request, identity: Identity = Depends(require_auth)) -> Idem:
    """The Idempotency-Key dependency. Depends(require_auth) is cached, so it does NOT re-run auth
    for a handler that also depends on it — it just gives us the resolved user_id to scope the key."""
    store = getattr(request.app.state, "runs", None)
    return Idem(store, identity.user_id, request.headers.get(IDEMPOTENCY_HEADER),
                request.method, request.url.path)
