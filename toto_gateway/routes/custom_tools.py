"""Custom tools + template instantiation — the UI-parity + portability REST surface (TTC v1 §3/§4).

All require_auth, all flag-gated: with TOTO_GW_CUSTOM_TOOLS off every path is a plain 404 (the
fail-closed boundary every surface shares). PUT is import (full validation, same rules as the
companion's create_tool); GET is export (spec round-trips byte-stable modulo server-stamped
fields). POST /v1/templates/{id}/instantiate backs the UI Instantiate button through the SAME
expansion code path the companion tool uses (toolspec.expand_template).
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import toolspec
from ..toolspec import SpecError
from .deps import Identity, require_auth
from ..toolspec import OBJECT_KINDS

router = APIRouter()


def _error(status: int, message: str, err_type: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": err_type}})


def _guard(request: Request):
    """(store, None) when the flag is on and the store exists, else (None, JSONResponse). Flag off
    → 404 (the path simply does not exist), driver off → 503."""
    settings = request.app.state.settings
    if not settings.custom_tools:
        return None, _error(404, "not found", "not_found")
    store = getattr(request.app.state, "runs", None)
    if store is None:
        return None, _error(503, "driver disabled — start the gateway with TOTO_GW_DRIVER=1",
                            "config_error")
    return store, None


@router.get("/v1/tools/custom")
async def list_tools(request: Request, identity: Identity = Depends(require_auth)):
    store, err = _guard(request)
    if err:
        return err
    tools = await store.list_custom_tools(identity.user_id)
    return {"tools": [{"name": t["name"], "description": t["description"], "version": t["version"],
                       "created_at": t["created_at"], "updated_at": t["updated_at"]} for t in tools]}


@router.get("/v1/tools/custom/{name}")
async def get_tool(name: str, request: Request, identity: Identity = Depends(require_auth)):
    store, err = _guard(request)
    if err:
        return err
    tool = await store.get_custom_tool(identity.user_id, name)
    if tool is None:
        return _error(404, f"unknown custom tool {name!r}", "not_found")
    return {"name": tool["name"], "version": tool["version"], "spec": tool["spec"],
            "created_at": tool["created_at"], "updated_at": tool["updated_at"]}


@router.put("/v1/tools/custom/{name}")
async def put_tool(name: str, request: Request, identity: Identity = Depends(require_auth)):
    store, err = _guard(request)
    if err:
        return err
    raw = await request.body()
    if len(raw) > toolspec.MAX_SPEC_BYTES:
        return _error(422, f"spec exceeds {toolspec.MAX_SPEC_BYTES} bytes", "invalid_request_error")
    try:
        spec = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return _error(422, "body must be a JSON tool spec", "invalid_request_error")
    if isinstance(spec, dict) and spec.get("tool") != name:
        return _error(422, f"spec tool name must match the URL ({name!r})", "invalid_request_error")
    try:
        toolspec.validate_spec(spec)
    except SpecError as e:
        return _error(422, str(e), "invalid_request_error")
    res = await store.create_custom_tool(identity.user_id, spec["tool"],
                                         spec["description"], spec, spec["version"],
                                         max_tools=toolspec.MAX_TOOLS_PER_USER)
    if "error" in res:
        return _error(409, res["error"], "conflict")
    return {"ok": True, "created": res["created"], "version": spec["version"]}


@router.delete("/v1/tools/custom/{name}")
async def delete_tool(name: str, request: Request, identity: Identity = Depends(require_auth)):
    store, err = _guard(request)
    if err:
        return err
    if not await store.delete_custom_tool(identity.user_id, name):
        return _error(404, f"unknown custom tool {name!r}", "not_found")
    return {"ok": True}


@router.post("/v1/templates/{object_id}/instantiate")
async def instantiate_template(object_id: str, request: Request,
                               identity: Identity = Depends(require_auth)):
    store, err = _guard(request)
    if err:
        return err
    try:
        body = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, ValueError):
        return _error(422, "body must be a JSON object", "invalid_request_error")
    if not isinstance(body, dict):
        body = {}
    res = await toolspec.expand_template(
        store, identity.user_id, object_id, body.get("params") or {},
        object_kinds=OBJECT_KINDS, x=body.get("x"), y=body.get("y"), actor=identity.actor)
    if "error" in res:
        code = 404 if "No template" in res["error"] else 422
        return _error(code, res["error"], "not_found" if code == 404 else "invalid_request_error")
    return {"ok": True, **res}
