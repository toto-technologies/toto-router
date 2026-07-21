"""Observability primitives: JSON logging, per-request IDs, memory watermark (plan D6).

stdlib only. `setup_logging` installs one stdout JSON handler on the root logger (called from
__main__ for the real server; tests keep pytest's logging). `RequestContextMiddleware` is a pure
ASGI middleware — NOT BaseHTTPMiddleware, which would buffer the SSE stream — that mints a
request_id, echoes X-Request-ID, and logs one line per request. The request_id rides a contextvar
so every log record and every driver span (created inside the request's task) correlates.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("toto_gw_request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("toto_gw_user_id", default=None)
# W3-C3: the request_id a "retry on frontier" was escalated from, set from the x-toto-escalated-from
# header in the middleware (validated) and read at the gateway trace chokepoint. None for a normal
# first-attempt request.
escalated_from_var: ContextVar[str | None] = ContextVar("toto_gw_escalated_from", default=None)

# A trace/run id shape: optional `req-` prefix + hex (uuid hex, run_id[:12], or hyphenated uuid).
# Junk (quotes, spaces, `;`, `<`) never matches, so a spoofed header is silently dropped, not stored.
_ESCALATED_FROM_RE = re.compile(r"^(req-)?[0-9a-f-]{8,64}$")


def valid_escalated_from(value: str | None) -> str | None:
    """The header value if it looks like a request/run id, else None (ignore junk, never raise)."""
    return value if value and _ESCALATED_FROM_RE.match(value) else None

# LogRecord's built-in attrs — anything else on a record came from logger.*(..., extra={}).
_STD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            out["request_id"] = rid
        for k, v in record.__dict__.items():  # merge structured extras
            if k not in _STD and k not in out:
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str = "info") -> None:
    """Root logger → one stdout JSON handler. Idempotent. Routes uvicorn through the same handler
    and silences its access log (we emit our own request line)."""
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    acc = logging.getLogger("uvicorn.access")
    acc.handlers.clear()
    acc.disabled = True  # our request log line replaces it


def _scrub_event(event, hint):
    """Sentry before_send: strip anything that could carry prompt/answer content (toto never
    sends message bodies), then tag with the request/user for triage. Belt-and-suspenders on top
    of send_default_pii=False."""
    req = event.get("request")
    if req:
        req.pop("data", None)  # request body (would hold prompts)
        req.pop("cookies", None)
        req.pop("query_string", None)  # carries ?token= (operator/email-verify creds)
        req.pop("url", None)  # may embed the same query string
        for h in ("cookie", "authorization"):
            (req.get("headers") or {}).pop(h, None)
    rid = request_id_var.get()
    uid = user_id_var.get()
    if rid or uid:
        tags = event.setdefault("tags", {})
        if rid:
            tags["request_id"] = rid
        if uid:
            tags["user_id"] = uid
    return event


def init_sentry(settings) -> bool:
    """Initialize Sentry iff a DSN is configured. Returns whether it activated. Import is deferred
    so an unconfigured deploy pays zero cost. Errors-only by default (traces_sample_rate 0)."""
    if not settings.sentry_dsn:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    from . import __version__

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        release=f"toto-gateway@{__version__}",
        environment=settings.sentry_environment or "unknown",
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,  # never attach IPs, cookies, or request bodies
        include_local_variables=False,  # never serialize frame locals (plaintext passwords, API keys)
        integrations=[StarletteIntegration(), FastApiIntegration()],
        before_send=_scrub_event,
    )
    return True


def redact_settings(settings) -> dict:
    """Settings dump with secret-ish fields masked. Suffix-match (not substring) so `max_tokens_*`
    and similar non-secrets aren't caught by 'token'. URLs with embedded credentials
    (postgresql://user:PASS@host) get the credential section masked — a DB password leaked
    into retained logs once (2026-07-02); never again. invite_code is an access credential too."""
    out = {}
    for k, v in settings.model_dump().items():
        if k.lower().endswith(("token", "pass", "secret", "key", "dsn", "invite_code")):
            out[k] = "***" if v else ""
        elif isinstance(v, str) and "://" in v and "@" in v.split("://", 1)[1].split("/", 1)[0]:
            scheme, rest = v.split("://", 1)
            hostpart = rest.split("@", 1)[1]
            out[k] = f"{scheme}://***@{hostpart}"
        else:
            out[k] = v
    return out


def last4(value: str) -> str:
    """Last 4 chars of a secret, for eyeballing WHICH key is set — never more, and '' for
    values too short to safely truncate. Companion to redact_settings for the credentials
    status panel."""
    return value[-4:] if value and len(value) >= 8 else ""


def rss_bytes() -> int | None:
    """Current resident set size. Linux /proc only (prod is Linux); None on macOS dev."""
    try:
        import os

        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None


def peak_rss_bytes() -> int:
    """Peak RSS via getrusage (ru_maxrss: kilobytes on Linux, bytes on macOS)."""
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def _mb(n: int | None) -> float | None:
    return round(n / 1_048_576, 1) if n else None


class RequestContextMiddleware:
    """Pure-ASGI: mint/propagate request_id, echo X-Request-ID, log one line per request."""

    def __init__(self, app) -> None:
        self.app = app
        self.log = logging.getLogger("toto_gateway.request")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = _req_header(scope, b"x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        # W3-C3: the escalated-from signal rides its own header; validated here (junk → None) so the
        # gateway trace chokepoint stamps only a real id. Set for the whole request so the companion's
        # background task (which copies this context at create_task) inherits it too.
        esc_token = escalated_from_var.set(
            valid_escalated_from(_req_header(scope, b"x-toto-escalated-from")))
        start = time.perf_counter()
        status = {"code": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        from .metrics import METRICS
        # in-flight needs a label BEFORE dispatch (the matched template isn't known yet), so it
        # uses the top-level prefix — bounded and real concurrency; requests/duration get the
        # precise matched template post-dispatch.
        prefix = _route_prefix(scope)
        try:
            METRICS.in_flight.labels(prefix).inc()
        except Exception:
            pass
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            try:
                METRICS.in_flight.labels(prefix).dec()
            except Exception:
                pass
            METRICS.observe_request(
                _route_label(scope), scope.get("method", ""), status["code"] or 0,
                time.perf_counter() - start)
            # scope["state"] backs request.state; require_auth stamps user_id there when known.
            user_id = (scope.get("state") or {}).get("user_id")
            self.log.info("request", extra={
                "method": scope.get("method"), "path": scope.get("path"),
                "status": status["code"],
                "latency_ms": round((time.perf_counter() - start) * 1000, 1),
                "user_id": user_id,
            })
            request_id_var.reset(token)
            escalated_from_var.reset(esc_token)


def _route_label(scope) -> str:
    """Matched route template (bounded cardinality), resolved post-dispatch. Starlette stamps the
    matched Route on scope['route']; unmatched (404) → 'unmatched', never the raw path."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    return "unmatched"


def _route_prefix(scope) -> str:
    """Top-level path segment — known before dispatch, so the in-flight gauge stays bounded
    (/, /v1, /readyz, …) instead of exploding on per-run paths."""
    p = scope.get("path", "/") or "/"
    seg = p.lstrip("/").split("/", 1)[0]
    return f"/{seg}" if seg else "/"


def _req_header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k == name:
            return v.decode()
    return None
