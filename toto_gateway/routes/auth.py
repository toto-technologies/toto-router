"""Auth API — email/password accounts with verification, cookie sessions.

Enumeration-safe: register/resend always return the same generic 200 whether or not the email
is new, and login returns one generic 401 for both unknown-email and wrong-password (burning a
dummy scrypt on the unknown-email path so timing doesn't leak existence). Rate-limited per client
IP. Google OAuth + real SMTP are unbuilt — mailer.py and the settings are shaped for them to drop in.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from ..auth import VERIFY_TTL, AuthStore, burn_dummy_hash, hash_password, verify_password
from ..credentials import provision_and_store
from ..mailer import send_verification
from .deps import OPERATOR_COOKIE, SESSION_COOKIE, Identity, require_auth

router = APIRouter()

MIN_PASSWORD_LEN = 8
# Fixed-window per-IP limiter on the three unauthenticated write paths (each can trigger email /
# a scrypt). ponytail: single-process dict of deques — resets on redeploy; a shared store only if
# this ever runs multi-process.
RATE_LIMIT, RATE_WINDOW = 10, 60.0
_hits: dict[str, deque[float]] = defaultdict(deque)

# The generic response every register/resend returns — never reveals whether the email exists.
_GENERIC = {"ok": True, "message": "If that email can register, a verification link is on its way."}

# Strong refs to in-flight provision-on-signup tasks (asyncio holds only a weak one — a fire-and-
# forget task can otherwise be GC'd mid-run; same pattern as memory_extract/content).
_provision_tasks: set[asyncio.Task] = set()


async def _provision_toto_identity(request: Request, user_id: str) -> None:
    """Fire-and-forget: give a newly-verified user a vaulted Toto identity + API key. Silent no-op
    when provisioning is off (no secret); idempotent (provision_and_store skips when a key is
    already vaulted). NEVER surfaces failure — verify/signup must not depend on Toto being reachable
    (fail-open)."""
    settings = request.app.state.settings
    if not settings.toto_provision_secret:
        return
    try:
        auth = _auth_store(request)
        user = await auth.get_user(user_id)
        if user is None:
            return
        email = user["email"]
        name = email.split("@", 1)[0] or email  # local part as a display name; Toto owns the rest
        await provision_and_store(settings, auth, user_id, email, name)
    except Exception:  # noqa: BLE001 — best-effort background task; never let it escape
        pass


def _fire_provision(request: Request, user_id: str) -> None:
    """Spawn the provision hook, holding a strong ref so it isn't GC'd before it runs."""
    if not request.app.state.settings.toto_provision_secret:
        return  # provisioning off → don't even spawn a task
    task = asyncio.create_task(_provision_toto_identity(request, user_id))
    _provision_tasks.add(task)
    task.add_done_callback(_provision_tasks.discard)


def _error(status_code: int, message: str, err_type: str, code: str | None = None) -> JSONResponse:
    err = {"message": message, "type": err_type}
    if code:
        err["code"] = code
    return JSONResponse(status_code=status_code, content={"error": err})


def _client_ip(request: Request) -> str:
    """Rightmost X-Forwarded-For hop (the one our trusted proxy appends — the real client on
    single-proxy Railway; leftmost hops are client-forgeable), else the socket peer."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


async def _audit(request: Request, action: str, user_id: str | None = None) -> None:
    """Append an auth audit event (metadata only), org-tagged so it surfaces in the org-scoped
    admin read (GET /v1/admin/audit). Best-effort — never breaks the request (audit.record
    swallows write failures; the org resolve is separately guarded)."""
    from .. import audit
    from ..obs import request_id_var

    store = getattr(request.app.state, "auth", None)
    if store is None:
        return
    org_id = None
    if user_id is not None:
        try:  # org tag is best-effort — a resolve hiccup must not drop the audit row
            org_id = (await store.resolve_membership(user_id)).get("org_id")
        except Exception:
            pass
    await audit.record(store, action, actor_user_id=user_id, org_id=org_id,
                       ip=_client_ip(request), request_id=request_id_var.get())


async def _rate_limited(request: Request) -> bool:
    """Per-IP limiter on the unauthenticated write paths. PG mode → the shared rate_limits table
    (correct across replicas); SQLite mode → the in-proc deque."""
    settings = request.app.state.settings
    ip = _client_ip(request)
    if settings.database_url:
        store = getattr(request.app.state, "auth", None)
        if store is not None:
            return not await store.check_rate_limit(f"auth:{ip}", RATE_LIMIT, int(RATE_WINDOW))
    now = time.monotonic()
    dq = _hits[ip]
    while dq and now - dq[0] > RATE_WINDOW:
        dq.popleft()
    if len(dq) >= RATE_LIMIT:
        return True
    dq.append(now)
    return False


def _auth_store(request: Request) -> AuthStore:
    return request.app.state.auth


def _set_session_cookie(response: Response, request: Request, raw: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        SESSION_COOKIE, raw,
        max_age=settings.session_ttl_days * 86400,
        httponly=True, samesite="lax", secure=settings.cookie_secure, path="/",
    )


def _verify_url(request: Request, raw: str) -> str:
    """Absolute verification link back to this API (same origin as the SPA). Configured
    public_url wins; only when it's unset do we derive from the request (dev/local Just Works).
    The attacker-controllable Host header is never trusted for a security-sensitive link."""
    base = request.app.state.settings.public_url or str(request.base_url).rstrip("/")
    return f"{base}/v1/auth/verify?token={raw}"


async def _issue_verification(request: Request, user_id: str, email: str) -> None:
    """Mint a fresh verify token and email its link. Shared by register + resend so an
    already-registered (still-unverified) email does the exact same work as a brand-new
    signup — the register/resend paths stay enumeration-safe by construction."""
    settings = request.app.state.settings
    raw = await _auth_store(request).mint_token(user_id, "verify", VERIFY_TTL, supersede=True)
    await send_verification(settings, email, _verify_url(request, raw))


# --- OIDC SSO flow (W1-C6) --------------------------------------------------------------------
# Two public endpoints next to login: /v1/auth/sso/start (resolve org by email domain -> authorize
# redirect with state+nonce+PKCE) and /v1/auth/sso/callback (validate state, exchange code, verify
# ID token, JIT-provision, issue the SAME session cookie as password login). The OIDC crypto/HTTP
# lives in ..oidc; the org config + single-use state live in AuthStore.
SSO_STATE_TTL = 600.0  # 10 min: the window between the authorize redirect and the callback
_DEFAULT_NEXT = "/console/"  # enterprise SSO users land in the console; overridable via ?next=


def _sso_transport(request: Request):
    """httpx transport DI seam (None in prod = real network; a MockTransport in tests). Same seam
    TotoClient uses; read off app.state so a test can inject a stub IdP with no monkeypatching."""
    return getattr(request.app.state, "sso_transport", None)


def _safe_next(raw: str | None) -> str:
    """A same-origin return path only — an absolute path starting with a single '/'. Anything else
    (scheme, host, protocol-relative '//', backslash trick) falls back to the default. Closes the
    open-redirect hole on both start and callback."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or raw.startswith("/\\"):
        return _DEFAULT_NEXT
    return raw


def _sso_redirect_uri(request: Request) -> str:
    """The callback URL handed to the IdP — must exactly match what the org registered. Built from
    the configured public_url (never the attacker-controllable Host header) when set."""
    base = request.app.state.settings.public_url or str(request.base_url).rstrip("/")
    return f"{base}/v1/auth/sso/callback"


def _sso_error(next_path: str, reason: str) -> RedirectResponse:
    """Bounce back to the app login with a non-leaky error code (never IdP internals)."""
    sep = "&" if "?" in next_path else "?"
    return RedirectResponse(f"{next_path}{sep}sso_error={reason}", status_code=302)


def _require_sso_edition(request: Request) -> None:
    """SSO login is enterprise-only; the oss edition ships email/password auth and 404s these two
    endpoints — same plain-404 posture as the excluded enterprise routes, and it lets the export
    drop the oidc module wholesale."""
    if request.app.state.settings.edition.strip().lower() == "oss":
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/v1/auth/sso/start")
async def sso_start(request: Request, email: str = "", next: str = ""):
    """Resolve the caller's org by email domain and redirect to its IdP (authorization-code + PKCE).
    Unknown domain / SSO not configured → bounce to login with sso_error=no_sso (the domain's SSO
    posture is public, so this reveals nothing about any account)."""
    _require_sso_edition(request)
    from .. import oidc

    next_path = _safe_next(next)
    domain = email.strip().lower().rpartition("@")[2]
    store = _auth_store(request)
    cfg = await store.get_sso_config_by_domain(domain) if domain else None
    if cfg is None:
        return _sso_error(next_path, "no_sso")
    try:
        doc = await oidc.discover(cfg["issuer"], _sso_transport(request))
        authorize = doc["authorization_endpoint"]
    except Exception:  # noqa: BLE001 — unreachable/malformed IdP config → user-facing bounce, not 500
        return _sso_error(next_path, "discovery")
    verifier, challenge = oidc.new_pkce()
    nonce = oidc.new_nonce()
    state = await store.create_login_state(
        org_id=cfg["org_id"], nonce=nonce, code_verifier=verifier,
        redirect_to=next_path, ttl_seconds=SSO_STATE_TTL)
    url = oidc.build_authorize_url(
        authorize, client_id=cfg["client_id"], redirect_uri=_sso_redirect_uri(request),
        state=state, nonce=nonce, code_challenge=challenge)
    return RedirectResponse(url, status_code=302)


@router.get("/v1/auth/sso/callback")
async def sso_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """The IdP redirect target: validate state (single-use, TTL), exchange the code (client_secret +
    PKCE verifier), verify the ID token (signature/iss/aud/exp/nonce), enforce email_verified + that
    the email's domain belongs to this org, JIT-provision, and issue a session — identical to password
    login. Every failure bounces to login with a generic sso_error code."""
    _require_sso_edition(request)
    from .. import oidc
    from ..credentials import credentials_secret, credentials_secret_old, decrypt

    store = _auth_store(request)
    settings = request.app.state.settings
    st = await store.consume_login_state(state)
    next_path = _safe_next(st["redirect_to"]) if st else _DEFAULT_NEXT
    if error or not code or st is None:
        return _sso_error(next_path, "state" if st is None else "idp")

    cfg = await store.get_sso_config(st["org_id"])
    secret_key = credentials_secret(settings)
    if cfg is None or not secret_key:
        return _sso_error(next_path, "config")
    try:
        client_secret = decrypt(secret_key, cfg["client_secret_enc"], credentials_secret_old(settings))
    except Exception:  # noqa: BLE001 — a rotated/unreadable secret can't complete the exchange
        return _sso_error(next_path, "config")

    transport = _sso_transport(request)
    try:
        doc = await oidc.discover(cfg["issuer"], transport)
        tokens = await oidc.exchange_code(
            doc["token_endpoint"], code=code, redirect_uri=_sso_redirect_uri(request),
            client_id=cfg["client_id"], client_secret=client_secret,
            code_verifier=st["code_verifier"], transport=transport)
        claims = await oidc.verify_id_token(
            tokens["id_token"], issuer=cfg["issuer"], client_id=cfg["client_id"],
            nonce=st["nonce"], jwks_uri=doc["jwks_uri"], transport=transport)
    except oidc.OIDCError:
        return _sso_error(next_path, "verify")
    except Exception:  # noqa: BLE001 — any IdP/transport hiccup is a user-facing bounce, not a 500
        return _sso_error(next_path, "verify")

    email = (claims.get("email") or "").strip().lower()
    if not email or not claims.get("email_verified") or not claims.get("sub"):
        return _sso_error(next_path, "email")
    # Org isolation: the IdP can assert any email, so only provision when the email's domain is one
    # THIS org configured — org A's IdP can't mint a member of org B by asserting B's domain.
    if email.rpartition("@")[2] not in cfg["domains"]:
        return _sso_error(next_path, "domain")

    user_id, provisioned = await store.provision_sso_login(
        issuer=cfg["issuer"], sub=str(claims["sub"]), email=email, org_id=st["org_id"])
    if provisioned:
        await _audit(request, "sso_provision", user_id)
    await _audit(request, "sso_login", user_id)
    # W2-C1 belt-and-braces: land the SSO user in the org they were provisioned into, not whatever
    # oldest membership they might also hold (a consultant with a personal org + this enterprise one).
    raw = await store.create_session(user_id, settings.session_ttl_days * 86400,
                                     org_id=st["org_id"])
    response = RedirectResponse(next_path, status_code=302)
    _set_session_cookie(response, request, raw)
    return response


class Register(BaseModel):
    email: str
    password: str
    invite_code: str | None = None


class Login(BaseModel):
    email: str
    password: str


class Resend(BaseModel):
    email: str


@router.post("/v1/auth/register")
async def register(body: Register, request: Request):
    settings = request.app.state.settings
    if await _rate_limited(request):
        return _error(429, "too many requests — try again shortly", "rate_limit_error")
    invite = settings.invite_code
    if invite and (body.invite_code or "") != invite:
        return _error(403, "a valid invite code is required", "authentication_error",
                      "invite_required")
    email = body.email.strip().lower()
    if "@" not in email or len(body.password) < MIN_PASSWORD_LEN:
        return _error(400, f"provide a valid email and a password of at least "
                      f"{MIN_PASSWORD_LEN} characters", "invalid_request_error")

    store = _auth_store(request)
    try:
        user_id = await store.create_user(email, hash_password(body.password))
    except sqlite3.IntegrityError:
        # Email already registered — never reveal that. Do the same mint+send work a fresh signup
        # does (against the existing account) while it's still unverified, so the response time
        # stays flat instead of short-circuiting here. ponytail: a verified/absent account still
        # skips the send — a one-email residual timing tell we accept (low sev); closing it fully
        # would mean either mailing already-verified users (spam) or a dummy send (crypto theater).
        existing = await store.get_user_by_email(email)
        if existing is not None and not existing["email_verified"]:
            await _issue_verification(request, existing["user_id"], email)
        return _GENERIC
    await _audit(request, "register", user_id)
    await _issue_verification(request, user_id, email)
    return _GENERIC


@router.get("/v1/auth/verify")
async def verify(token: str, request: Request):
    """Single-use, 24h. Consumes the token and marks the user verified, then bounces to the SPA.
    Mail-scanner prefetch is harmless: the token only ever existed in that inbox."""
    store = _auth_store(request)
    user_id = await store.consume_token(token, "verify")
    if user_id is None:
        return RedirectResponse("/svelte/?verify_error=1", status_code=302)
    await store.mark_verified(user_id)
    await _audit(request, "verify", user_id)
    _fire_provision(request, user_id)  # provision-on-signup: the moment the user becomes real
    return RedirectResponse("/svelte/?verified=1", status_code=302)


@router.post("/v1/auth/resend")
async def resend(body: Resend, request: Request):
    """Re-send a verification link. Silent no-op for unknown or already-verified emails."""
    if await _rate_limited(request):
        return _error(429, "too many requests — try again shortly", "rate_limit_error")
    store = _auth_store(request)
    user = await store.get_user_by_email(body.email)
    if user is not None and not user["email_verified"]:
        await _issue_verification(request, user["user_id"], user["email"])
    return _GENERIC


@router.post("/v1/auth/login")
async def login(body: Login, request: Request):
    settings = request.app.state.settings
    if await _rate_limited(request):
        return _error(429, "too many requests — try again shortly", "rate_limit_error")
    store = _auth_store(request)
    # SSO-required domains disable password login (W1-C6). Keyed on the email DOMAIN, whose SSO
    # posture is already public (sso/start reveals it), so this stays enumeration-safe — every
    # address on the domain gets the identical response whether or not the account exists, and the
    # check runs BEFORE any user lookup so timing can't distinguish them either.
    domain = body.email.strip().lower().rpartition("@")[2]
    sso = await store.get_sso_config_by_domain(domain) if domain else None
    if sso is not None and sso["sso_required"]:
        return _error(403, "this organization requires single sign-on — continue with SSO",
                      "authentication_error", "sso_required")
    user = await store.get_user_by_email(body.email)
    if user is None or not user["password_hash"]:
        burn_dummy_hash()  # equalize timing so an unknown email isn't faster than a wrong password
        await _audit(request, "login_failed")
        return _error(401, "invalid email or password", "authentication_error", "invalid_credentials")
    if not verify_password(body.password, user["password_hash"]):
        await _audit(request, "login_failed", user["user_id"])
        return _error(401, "invalid email or password", "authentication_error", "invalid_credentials")
    if settings.require_email_verify and not user["email_verified"]:
        return _error(403, "please verify your email before signing in",
                      "authentication_error", "email_unverified")
    raw = await store.create_session(user["user_id"], settings.session_ttl_days * 86400)
    await _audit(request, "login", user["user_id"])
    response = JSONResponse({"user_id": user["user_id"], "email": user["email"]})
    _set_session_cookie(response, request, raw)
    return response


@router.post("/v1/auth/logout", status_code=204)
async def logout(request: Request):
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        store = _auth_store(request)
        settings = request.app.state.settings
        user = await store.session_user(raw, require_verified=settings.require_email_verify)
        await store.revoke_session(raw)
        await _audit(request, "logout", user["user_id"] if user else None)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    # The OSS console authenticates via the operator cookie (deps.OPERATOR_COOKIE), not a
    # server session — expire it too, or Sign out leaves the operator silently signed in.
    response.delete_cookie(OPERATOR_COOKIE, path="/")
    return response


# --- Multi-org (W2-C1): introspect memberships + switch the session's active org ----------------

class ActiveOrg(BaseModel):
    org_id: str


@router.get("/v1/auth/memberships")
async def list_memberships(request: Request, identity: Identity = Depends(require_auth)):
    """The caller's orgs — [{org_id, org_name, role}] — and which one is currently active. The
    console renders a switcher from this; a single-membership user needs no switch UI."""
    store = _auth_store(request)
    memberships = await store.list_user_memberships(identity.user_id) if identity.user_id else []
    return {"memberships": memberships, "active_org_id": identity.org_id}


@router.post("/v1/auth/active-org")
async def set_active_org(body: ActiveOrg, request: Request,
                         identity: Identity = Depends(require_auth)):
    """Switch the CURRENT session's active org (W2-C1). Validates the caller holds a membership in
    the target (403 on a foreign org — no cross-org leak), binds it to the session row, audits. A
    switch is session-scoped: an API-token caller (no session cookie) gets 400 (tokens bind at
    mint)."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return _error(400, "active-org switch requires a browser session",
                      "invalid_request_error", "session_required")
    store = _auth_store(request)
    if identity.user_id is None or await store.get_membership_in(identity.user_id, body.org_id) is None:
        return _error(403, "not a member of that org", "authorization_error", "not_a_member")
    await store.set_session_org(raw, body.org_id)
    from .. import audit
    from ..obs import request_id_var
    await audit.record(store, "auth:active_org", actor_user_id=identity.user_id,
                       org_id=body.org_id, target_type="org", target_id=body.org_id,
                       ip=_client_ip(request), request_id=request_id_var.get())
    return {"ok": True, "active_org_id": body.org_id}


@router.get("/v1/auth/me")
async def me(request: Request):
    """SPA boot probe. 401 when nobody is signed in — the frontend reads that as 'show auth'
    (only when the API is actually gating). The operator token reports a synthetic identity."""
    # Resolve directly (not via require_auth's gate) so `me` is a clean probe: it answers
    # 200/401 about the caller, never the app-gate 401 that require_auth would raise.
    from .deps import _resolve_identity

    identity: Identity = await _resolve_identity(request)
    if not identity.authenticated:
        return _error(401, "not signed in", "authentication_error", "not_authenticated")
    if identity.is_operator:
        return {"user_id": "operator", "email": None, "email_verified": True,
                "has_google": False, "is_operator": True}
    user = await _auth_store(request).get_user(identity.user_id)
    if user is None:  # a service token (W2-C3): org-owned, no user row — report the org-scoped view
        return {"user_id": identity.user_id, "email": None, "email_verified": True,
                "has_google": False, "is_operator": False, "org_id": identity.org_id,
                "actor": identity.actor}
    return {
        "user_id": user["user_id"], "email": user["email"],
        "email_verified": bool(user["email_verified"]), "has_google": bool(user["google_sub"]),
        "is_operator": False,  # `toto whoami` and API clients read this to confirm scoping
    }


if __name__ == "__main__":  # ponytail: self-check for the enumeration-safe register/resend paths
    import asyncio
    import contextlib
    import io
    from types import SimpleNamespace

    from ..auth import AuthStore

    def _fake_request(store: AuthStore, *, public_url: str = "") -> Request:
        settings = SimpleNamespace(database_url="", invite_code="", public_url=public_url,
                                   smtp_enabled=False, require_email_verify=True)
        app = SimpleNamespace(state=SimpleNamespace(settings=settings, auth=store))
        return SimpleNamespace(app=app, headers={}, base_url="http://host-header.example/",
                               client=SimpleNamespace(host="1.2.3.4"))  # type: ignore[return-value]

    async def _capture(coro) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):  # mailer prints the link in dev mode
            await coro
        return buf.getvalue()

    async def _demo() -> None:
        store = AuthStore(":memory:")
        req = _fake_request(store)
        pw = {"password": "password123"}

        # 1) fresh signup emails a verification link
        out = await _capture(register(Register(email="a@x.com", **pw), req))
        assert "VERIFY LINK for a@x.com" in out, out

        # 2) re-register the SAME still-unverified email: must do the same mint+send work (no
        #    early-return short-circuit), so an attacker can't tell it already exists.
        out = await _capture(register(Register(email="a@x.com", **pw), req))
        assert "VERIFY LINK for a@x.com" in out, "re-register of unverified must resend"

        # 3) links come from configured public_url, never the (attacker-controllable) Host header.
        req2 = _fake_request(store, public_url="https://real.example")
        out = await _capture(resend(Resend(email="a@x.com"), req2))
        assert "https://real.example/v1/auth/verify?token=" in out, out
        assert "host-header.example" not in out, "must not trust the Host header"

        print("self-check ok")

    asyncio.run(_demo())
