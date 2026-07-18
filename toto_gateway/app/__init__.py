"""FastAPI application package: builders, background loops, HTTP helpers, and the app factory.

`create_app()` (factory.py) builds the catalog, runner registry, trace writer, and gateway, and
wires them into `app.state`. Tests can inject a pre-built `gateway` (e.g. with a fake runner
factory) to exercise the full HTTP surface offline.

Modules:
    build.py        `build_gateway` / `build_driver` and their shared helpers
    background.py   background loops the lifespan spawns (reaper, dreamer, calsync, refreshers)
    http.py         security headers/CSP + SPA static-file mounts
    factory.py      `create_app`: lifespan, middleware, state wiring, router mounting
"""

from __future__ import annotations

from .background import _benchmark_refresher, calsync_tick
from .build import audit_driver_denial, build_driver, build_gateway
from .factory import create_app

__all__ = [
    "audit_driver_denial", "build_driver", "build_gateway", "calsync_tick", "create_app",
    "_benchmark_refresher",
]
