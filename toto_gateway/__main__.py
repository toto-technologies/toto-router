"""`python -m toto_gateway` / `toto-gateway` — the PRODUCTION launcher (also fine for dev).

This is the Docker ENTRYPOINT, so its uvicorn args are the production posture, not a dev
convenience: a bounded graceful-shutdown window, an explicit bind host, and a single worker that
is intentional (not an oversight) — see the inline notes.
"""

from __future__ import annotations

import uvicorn

from .config import get_settings
from .obs import setup_logging


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
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
