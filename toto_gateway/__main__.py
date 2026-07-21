"""`python -m toto_gateway` / `toto-gateway` — the PRODUCTION launcher (also fine for dev).

This is the Docker ENTRYPOINT, so its uvicorn args are the production posture, not a dev
convenience: a bounded graceful-shutdown window, an explicit bind host, and a single worker that
is intentional (not an oversight) — see the inline notes.
"""

from __future__ import annotations

import logging

import uvicorn

from .config import get_settings
from .obs import setup_logging


def _print_console_url(settings) -> None:
    """One-click console launch for the OSS/local edition: log a ready-to-open URL carrying the
    operator token in the URL FRAGMENT (never a query param, so it stays out of server logs and
    Referer headers). The console reads it on load, authenticates, and strips it from the address
    bar. Only when token auth is on and this is the oss edition — enterprise uses account login."""
    if settings.edition.strip().lower() != "oss" or not settings.auth_token:
        return
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    # Point at the real app page, not the /console/ redirect stub — that stub navigates without the
    # fragment, dropping the token before the SPA can read it.
    url = f"http://{host}:{settings.port}/console/overview#token={settings.auth_token}"
    logging.getLogger("toto_gateway").info("console ready — open %s", url)


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    _print_console_url(settings)
    uvicorn.run(
        "toto_gateway.app:create_app",
        factory=True,
        host=settings.host,   # TOTO_GW_HOST; the Dockerfile sets 0.0.0.0 so we never silently bind loopback
        port=settings.port,
        # ONE worker per replica, on purpose: a single event loop + the in-process wake bus
        # (LISTEN/NOTIFY fan-out) assume one process. Horizontal scale is REPLICAS, not workers —
        # forking N workers would split the in-proc fan-out and buy nothing. Do not "fix" to >1.
        workers=1,
        # Bound the graceful drain so an idle SSE subscriber (a stream that owns no run, so drain()
        # never sends it a terminal event) can't hold the process open until the platform SIGKILL —
        # which would truncate in-flight runs mid-drain and defeat the graceful-shutdown guarantee.
        # On expiry uvicorn cancels the remaining connections; the SSE generator's `finally` closes
        # its subscription cleanly. Tied to drain_seconds so lifespan drain finishes first.
        timeout_graceful_shutdown=settings.drain_seconds,
        # log_config=None → uvicorn's loggers propagate to our root JSON handler; our own
        # per-request log line replaces uvicorn's access log.
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
