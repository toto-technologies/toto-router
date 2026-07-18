"""Egress allowlist — one chokepoint over every outbound host the gateway may reach (W2-C6).

The gateway's whole external surface (provider inference, catalog/benchmark discovery, IdP OIDC,
Toto cloud, SMTP, OTLP, S3, LangSmith/Sentry) is funnelled through ONE check keyed by hostname.

Chokepoint: httpx routes EVERY request — ad-hoc `httpx.AsyncClient(...)` sites AND the OpenAI SDK's
internal client — through its default transport class. We wrap `httpx.AsyncHTTPTransport.handle_
async_request` + `httpx.HTTPTransport.handle_request` ONCE at startup, so no call site changes and
nothing new can slip past by constructing its own client. Test transports (MockTransport/ASGI) are a
DIFFERENT class, so the offline suite is never gated. smtplib can't ride the httpx transport, so
mailer gates inline via `check_host` (same function).

Two modes (`TOTO_GW_EGRESS_ENFORCE`, default OFF — observe-only so the feature can soak):
  observe  → allow + one `egress.observed` audit row (deduped 1/(host,subsystem)/hour).
  enforce  → refuse (raise EgressBlockedError → 502 on serving paths) + `egress.blocked` audit row.

The derived host set is computed from configuration at startup (never hand-maintained) plus the
operator extension `TOTO_GW_EGRESS_EXTRA`. SSO issuers are dynamic (loaded at install + `add_issuer`
on config write). GET /v1/admin/egress prints the whole set — the page a customer's network team reads.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from urllib.parse import urlparse

from . import audit

log = logging.getLogger(__name__)

_DEDUPE_SECONDS = 3600.0  # one audit row per (host, subsystem) per hour, per replica

# Coarse subsystem tag for the transport hook — a call site MAY narrow it (`with egress.subsystem(
# "runner"): ...`) but none are required to. ponytail: host is the load-bearing field; per-subsystem
# attribution is a nicety, so the transport default of "httpx" is fine.
_subsystem: ContextVar[str] = ContextVar("egress_subsystem", default="httpx")

# Discovery / benchmarking fetch hosts (admin- or background-triggered). Static: these are wired
# into the gateway's own connectors (catalog_sync, benchmarking/*), not derived from tenant data.
_DISCOVERY_HOSTS = (
    "openrouter.ai", "api.fireworks.ai", "epoch.ai",
    "datasets-server.huggingface.co", "gorilla.cs.berkeley.edu", "artificialanalysis.ai",
)

# Provider API hosts implied by a configured key in the environment (a key present = that host may
# be reached even if no catalog entry names its base_url yet).
_KEY_HOSTS = {
    "OPENAI_API_KEY": "api.openai.com",
    "OPENROUTER_API_KEY": "openrouter.ai",
    "FIREWORKS_API_KEY": "api.fireworks.ai",
    "ANTHROPIC_API_KEY": "api.anthropic.com",
    "ELEVENLABS_API_KEY": "api.elevenlabs.io",
}


class EgressBlockedError(Exception):
    """Enforce mode refused a connection to a non-allowlisted host. Mapped to a 502 on serving paths
    (see app.create_app); background callers log + swallow as they already do for provider I/O."""

    def __init__(self, host: str, subsystem: str) -> None:
        self.host = host
        self.subsystem = subsystem
        super().__init__(f"egress to {host!r} blocked by allowlist (subsystem={subsystem})")


def host_of(url_or_host: str | None) -> str:
    """Bare lowercase hostname from a URL or host[:port] — scheme, userinfo, and port stripped."""
    if not url_or_host:
        return ""
    s = url_or_host.strip()
    netloc = urlparse(s if "//" in s else "//" + s).netloc
    return netloc.split("@")[-1].rsplit(":", 1)[0].lower() if netloc else ""


def derive_hosts(settings, catalog, *, sso_issuers: Iterable[str] = ()) -> dict[str, str]:
    """The allowed host → source map, computed from config (never hand-maintained). Source is a short
    provenance label the printable page shows so a reader knows WHY a host is allowed."""
    hosts: dict[str, str] = {}

    def add(url_or_host: str | None, source: str) -> None:
        h = host_of(url_or_host)
        if h:
            hosts.setdefault(h, source)

    # 1. Catalog base_urls + endpoint-implied provider hosts (the authoritative provider set).
    for e in getattr(catalog, "models", []):
        if e.base_url:
            add(e.base_url, f"catalog:{e.id}")
        elif e.endpoint == "anthropic":
            add("api.anthropic.com", f"catalog:{e.id}")
        elif e.endpoint == "openai":
            add("api.openai.com", f"catalog:{e.id}")
    # 2. Provider hosts implied by a configured key.
    for env, host in _KEY_HOSTS.items():
        if os.environ.get(env):
            add(host, f"key:{env}")
    # 3. Gateway discovery / benchmarking connectors.
    for h in _DISCOVERY_HOSTS:
        add(h, "discovery")
    # 4. Toto cloud, SMTP, S3, OTLP, Sentry, LangSmith, integrations — from config/env.
    add(settings.toto_url, "config:toto_url")
    if settings.smtp_host:
        add(settings.smtp_host, "config:smtp_host")
    if settings.s3_endpoint:
        add(settings.s3_endpoint, "config:s3_endpoint")
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        add(os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"], "config:otlp")
    if settings.sentry_dsn:
        add(settings.sentry_dsn, "config:sentry_dsn")
    if os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGSMITH_TRACING"):
        add(os.environ.get("LANGSMITH_ENDPOINT") or "https://smith.langchain.com", "config:langsmith")
    if getattr(settings, "pipedream", False):
        add("api.pipedream.com", "config:pipedream")
    if getattr(settings, "companion_tts", False):
        add("api.elevenlabs.io", "config:companion_tts")
    # 5. Dynamic org SSO issuers (also grown at runtime via add_issuer).
    for iss in sso_issuers:
        add(iss, "sso:issuer")
    # 6. Operator extension.
    for h in _extra_hosts(settings):
        add(h, "env:TOTO_GW_EGRESS_EXTRA")
    return hosts


def _extra_hosts(settings) -> list[str]:
    raw = getattr(settings, "egress_extra", "") or ""
    return [host_of(h) for h in raw.split(",") if h.strip()]


class _Guard:
    """Holds the derived host set + mode + the audit emit. `check` is the single decision used by the
    httpx chokepoint AND the inline smtplib gate."""

    def __init__(self, hosts: dict[str, str], enforce: bool,
                 emit: Callable[[str, str, str], None] | None = None) -> None:
        self._hosts = hosts
        self.enforce = enforce
        self._emit = emit or (lambda action, host, subsystem: None)
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def hosts(self) -> dict[str, str]:
        return dict(self._hosts)

    def allowed(self, host: str) -> bool:
        return host_of(host) in self._hosts

    def add_issuer(self, issuer: str) -> None:
        """Admit a newly-configured SSO issuer host at runtime (config-write hook)."""
        h = host_of(issuer)
        if h:
            self._hosts.setdefault(h, "sso:issuer")

    def check(self, host: str | None, subsystem: str) -> None:
        """None if allowed. Otherwise audit (deduped) + (enforce) raise EgressBlockedError."""
        h = host_of(host)
        if not h or h in self._hosts:
            return
        action = "egress.blocked" if self.enforce else "egress.observed"
        if self._should_emit(h, subsystem):
            self._emit(action, h, subsystem)
        if self.enforce:
            log.error("egress BLOCKED", extra={"host": h, "subsystem": subsystem})
            raise EgressBlockedError(h, subsystem)
        log.warning("egress observed (would block in enforce mode)",
                    extra={"host": h, "subsystem": subsystem})

    def _should_emit(self, host: str, subsystem: str) -> bool:
        key = (host, subsystem)
        now = time.time()
        with self._lock:
            if now - self._seen.get(key, 0.0) < _DEDUPE_SECONDS:
                return False
            self._seen[key] = now
            return True


# --- module singleton + httpx chokepoint ------------------------------------------------------

_GUARD: _Guard | None = None
_PATCHED = False


def current() -> _Guard | None:
    return _GUARD


def check_host(host: str | None, subsystem: str) -> None:
    """Inline gate for non-httpx egress (smtplib). No-op until install()."""
    if _GUARD is not None:
        _GUARD.check(host, subsystem)


def add_issuer(issuer: str) -> None:
    """Admit an SSO issuer host at runtime (called from the SSO config write path)."""
    if _GUARD is not None:
        _GUARD.add_issuer(issuer)


def install(settings, catalog, store, *, sso_issuers: Iterable[str] = (), loop=None) -> _Guard:
    """Compute the derived set, build the guard, and patch httpx once. Called from lifespan startup
    (store present → audit rows land); re-callable (last install wins the singleton)."""
    global _GUARD, _PATCHED
    hosts = derive_hosts(settings, catalog, sso_issuers=sso_issuers)
    guard = _Guard(hosts, _enforce_enabled(settings), emit=_make_emitter(store, loop))
    _GUARD = guard
    if not _PATCHED:
        _patch_httpx()
        _PATCHED = True
    log.info("egress allowlist installed",
             extra={"enforce": guard.enforce, "host_count": len(hosts)})
    return guard


def _enforce_enabled(settings) -> bool:
    return bool(getattr(settings, "egress_enforce", False))


def _make_emitter(store, loop) -> Callable[[str, str, str], None]:
    """An audit writer safe to call from the loop thread OR a worker thread (sync httpx.Client).
    Best-effort: a closed loop / absent store drops the row, never raises into the request path."""
    if store is None or loop is None:
        return lambda action, host, subsystem: None

    def emit(action: str, host: str, subsystem: str) -> None:
        def _schedule() -> None:
            loop.create_task(audit.record(
                store, action, target_type="egress_host", target_id=host,
                meta={"host": host, "subsystem": subsystem}))
        try:
            loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            pass  # loop closed (teardown) — drop the row

    return emit


def _patch_httpx() -> None:
    """Wrap both default transport classes' request methods. Idempotent via _PATCHED."""
    import httpx

    _async_orig = httpx.AsyncHTTPTransport.handle_async_request
    _sync_orig = httpx.HTTPTransport.handle_request

    async def _async(self, request):  # noqa: ANN001
        _check_request(request)
        return await _async_orig(self, request)

    def _sync(self, request):  # noqa: ANN001
        _check_request(request)
        return _sync_orig(self, request)

    httpx.AsyncHTTPTransport.handle_async_request = _async
    httpx.HTTPTransport.handle_request = _sync


def _check_request(request) -> None:  # noqa: ANN001
    if _GUARD is not None:
        _GUARD.check(request.url.host, _subsystem.get())


def _demo() -> None:
    """Self-check (no network): derive, allow/block decision, dedupe, enforce vs observe."""
    from types import SimpleNamespace

    settings = SimpleNamespace(toto_url="https://toto.tech", smtp_host="smtp.acme.com",
                               s3_endpoint="", sentry_dsn="", pipedream=False, companion_tts=False,
                               egress_extra="extra.example, https://second.example:8443",
                               egress_enforce=False)
    catalog = SimpleNamespace(models=[
        SimpleNamespace(id="or-x", base_url="https://openrouter.ai/api/v1", endpoint="openai"),
        SimpleNamespace(id="cl-x", base_url=None, endpoint="anthropic"),
        SimpleNamespace(id="oa-x", base_url=None, endpoint="openai"),
    ])
    hosts = derive_hosts(settings, catalog)
    for h in ("openrouter.ai", "api.anthropic.com", "api.openai.com", "toto.tech",
              "smtp.acme.com", "extra.example", "second.example", "epoch.ai"):
        assert h in hosts, (h, sorted(hosts))
    assert hosts["toto.tech"] == "config:toto_url"

    emitted: list[tuple] = []
    g = _Guard(hosts, enforce=False, emit=lambda *a: emitted.append(a))
    assert g.check("openrouter.ai", "runner") is None       # allowed → silent
    g.check("evil.example", "runner")                        # observe → audit, no raise
    g.check("evil.example", "runner")                        # deduped within the hour
    assert emitted == [("egress.observed", "evil.example", "runner")], emitted

    blocked: list[tuple] = []
    ge = _Guard(hosts, enforce=True, emit=lambda *a: blocked.append(a))
    try:
        ge.check("https://evil.example/path", "catalog_sync")
    except EgressBlockedError as exc:
        assert exc.host == "evil.example"
    else:
        raise AssertionError("enforce mode must raise")
    assert blocked == [("egress.blocked", "evil.example", "catalog_sync")], blocked
    print("egress self-check OK")


if __name__ == "__main__":
    _demo()
