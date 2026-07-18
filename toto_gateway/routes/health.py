"""Liveness (/healthz) + readiness (/readyz).

/healthz is pure liveness — status + version only (it used to leak the whole catalog
unauthenticated). /readyz checks the process can actually serve: DB reachable + catalog loaded,
so a healthcheck-gated rollout only cuts over to replicas that work (plan D6).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import __version__

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> dict:
    # License state rides the liveness payload (W3-C7) so an operator can spot an expired/blocked
    # deploy without an operator token. Org/entitlements are withheld here (public path); the full
    # detail is on /statusz + /v1/admin/license. `unlicensed` (dev/OSS) is omitted to keep the
    # OSS payload byte-identical.
    out = {"status": "ok", "version": __version__}
    lic = getattr(request.app.state, "license_status", None)
    if lic is not None and lic.state() != "unlicensed":
        out["license"] = lic.snapshot(include_org=False)
    return out


@router.get("/readyz")
async def readyz(request: Request):
    # DB through the always-present AuthStore connection (works for SQLite now, Postgres later).
    try:
        await request.app.state.auth.ping()
    except Exception as exc:
        return JSONResponse(status_code=503,
                            content={"status": "not_ready", "reason": "db", "detail": str(exc)})
    if not request.app.state.gateway.catalog.models:  # base-catalog-ok: startup readiness of the shipped catalog
        return JSONResponse(status_code=503,
                            content={"status": "not_ready", "reason": "catalog"})
    # On a Postgres deploy the driver plane depends on two more planes a replica needs to serve
    # correctly (a >1-replica deploy silently drops live fan-out / recall otherwise). Gate on them
    # fail-closed; SQLite/single-replica skips this (runs._pg is False → the in-proc bus is trivially
    # armed and there's no shared content plane to miss).
    runs = getattr(request.app.state, "runs", None)
    if runs is not None and getattr(runs, "_pg", False):
        # (a) SSE wake-bus listener armed — else this replica gets no cross-replica wakes.
        if not runs.wake_armed():
            return JSONResponse(status_code=503,
                                content={"status": "not_ready", "reason": "fanout"})
        # (b) content plane resolves — notes/recall reads route through it.
        content = getattr(request.app.state, "content", None)
        if content is not None:
            try:
                await content.ping()
            except Exception as exc:
                return JSONResponse(status_code=503,
                    content={"status": "not_ready", "reason": "content", "detail": str(exc)})
    # memory recall plane readout (never gates readiness — it degrades, the app serves):
    #   "on"           = pgvector cosine + keyword (or SQLite Python scoring in dev)
    #   "keyword-only" = plane on but pgvector unavailable → tsvector keyword only
    #   "off"          = TOTO_GW_MEMORY unset (or driver plane off)
    mem = getattr(request.app.state, "memory", None)
    memory = mem.mode if mem is not None else "off"
    return {"status": "ready", "version": __version__, "memory": memory}
