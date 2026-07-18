"""Toto API client — the driver's ONLY conduit to Toto cloud.

Toto doesn't want your data: ONLY task METADATA (title/description/intent/scope/keywords/
requires/routing) and execution PROVENANCE ever cross this boundary — never prompt text,
model answers, code, or file contents. `write_execution` enforces this architecturally with
a provenance allowlist; any other key is dropped before it leaves the process.

Grounded in the real Toto API (repo `toto`, app/api/todos.py):
  create_list      POST /api/lists                          -> {"id": ...}
  batch_items      POST /api/lists/{id}/items/batch         -> {"succeeded":[item], "failed":[]}
  set_status       POST /api/items/{id}/status  {"status"}  -> item
  write_execution  POST /api/items/{id}/edit    {"metadata"} -> item  (metadata is REPLACED,
                   so we read-merge-write to preserve sibling keys)
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque

import httpx

log = logging.getLogger(__name__)


async def provision_toto_user(settings, email: str, name: str) -> dict | None:
    """Provision a Toto-app identity + API key for a gateway user via Toto's internal endpoint.

    Returns the response dict ({"user_id", "created", "api_key": "toto_...", "key_name"}) or None on
    ANY failure — a missing secret, a non-200 (403 bad secret / 503 disabled / anything), a network
    error, or a missing api_key. NEVER raises and NEVER logs the key: provisioning is best-effort and
    must never block signup or a request; the caller degrades to the shared token. Only the outcome
    (status code / exception class) is logged.
    """
    secret = getattr(settings, "toto_provision_secret", "")
    if not secret:
        return None
    url = f"{settings.toto_url.rstrip('/')}/api/internal/provision"
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, headers={"Authorization": f"Bearer {secret}"},
                             json={"email": email, "name": name})
        if r.status_code != 200:
            log.warning("toto provision failed: HTTP %s", r.status_code)
            return None
        data = r.json()
        if not data.get("api_key"):
            log.warning("toto provision returned no api_key")
            return None
        return data
    except Exception as exc:  # noqa: BLE001 — best-effort; degrade to the shared token, never raise
        log.warning("toto provision error: %s", type(exc).__name__)
        return None


# Provenance keys allowed to reach Toto cloud. Everything else is dropped.
_EXECUTION_ALLOWLIST = frozenset({
    "runner", "executor", "model", "lane",
    "tokens_prompt", "tokens_completion", "cost_usd",
    "outcome", "latency_ms", "fallback_used", "route_reason",
    "artifact", "rejected",  # typed receipt (hash only) + routing-rejection alternatives
})


class TotoClient:
    """Async client persisting task metadata + execution provenance to Toto."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0,
                 transport: httpx.BaseTransport | None = None):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = timeout
        self._transport = transport  # DI seam for tests (respx MockTransport); None = real network.

    def _client(self) -> httpx.AsyncClient:
        # ponytail: fresh client per call — no lifecycle to manage; pool it if call rate matters.
        return httpx.AsyncClient(base_url=self._base, headers=self._headers,
                                 timeout=self._timeout, transport=self._transport)

    async def _post(self, path: str, json: dict) -> dict:
        async with self._client() as c:
            r = await c.post(path, json=json)
            r.raise_for_status()
            return r.json()

    async def _get(self, path: str) -> dict:
        async with self._client() as c:
            r = await c.get(path)
            r.raise_for_status()
            return r.json()

    async def create_list(self, name: str, metadata: dict) -> str:
        """Create a list; return its id."""
        data = await self._post("/api/lists", {"name": name, "metadata": metadata})
        return data["id"]

    async def batch_items(self, list_id: str, items: list[dict]) -> list[str]:
        """Batch-create items; return ids order-aligned to `items`.

        The batch response's `succeeded` array preserves input order (minus any failures).
        We map ids back by matching task title in order (robust to failures/reordering); if
        the batch envelope can't cover every input, fall back to GET the list and map there.
        """
        resp = await self._post(f"/api/lists/{list_id}/items/batch", {"items": items})
        ids = _align_ids(items, resp.get("succeeded", []))
        if ids is not None:
            return ids
        page = await self._get(f"/api/lists/{list_id}/items?limit=200")
        ids = _align_ids(items, page.get("items", []))
        if ids is None:
            raise RuntimeError("batch_items: could not map created items back to input by title")
        return ids

    async def set_status(self, item_id: str, status: str) -> None:
        """Set item status: "pending" | "in_progress" | "done"."""
        await self._post(f"/api/items/{item_id}/status", {"status": status})

    async def list_lists(self) -> list[dict]:
        """This token's lists (GET /api/lists). Tolerates {"lists": [...]} or a bare array."""
        data = await self._get("/api/lists")
        return (data.get("lists", []) if isinstance(data, dict) else data) or []

    async def list_items(self, list_id: str) -> list[dict]:
        """A list's items (GET /api/lists/{id}/items) — the endpoint batch_items already falls back to."""
        data = await self._get(f"/api/lists/{list_id}/items?limit=200")
        return (data.get("items", []) if isinstance(data, dict) else data) or []

    async def edit_item(self, item_id: str, *, description: str | None = None,
                        metadata: dict | None = None) -> None:
        """Replace an item's description/metadata (POST /api/items/{id}/edit). /edit REPLACES
        metadata wholesale; for a generated work-map item the metadata IS authoritative, so a full
        replace is intended (unlike write_execution's read-merge-write)."""
        body: dict = {}
        if description is not None:
            body["description"] = description
        if metadata is not None:
            body["metadata"] = metadata
        await self._post(f"/api/items/{item_id}/edit", body)

    async def write_execution(self, item_id: str, execution: dict,
                              classified: dict | None = None) -> None:
        """Merge filtered execution provenance into metadata.execution.

        DATA BOUNDARY: drop every key not in the provenance allowlist BEFORE it leaves the
        process. Toto's /edit replaces item metadata wholesale, so read-merge-write to keep
        the task's other metadata intact.

        `classified` (the request's totoshape metadata: component/files/keywords/scope/intent — all
        task METADATA, within the boundary) is merged into the item's TOP-LEVEL metadata
        non-destructively: fields the driver already set (decompose) win, so shape parity enriches
        without clobbering. Folded into this same GET→merge→POST round-trip (no extra call).
        """
        filtered = {k: v for k, v in execution.items() if k in _EXECUTION_ALLOWLIST}
        item = await self._get(f"/api/items/{item_id}")
        metadata = dict(item.get("metadata") or {})
        if classified:  # existing keys win — never overwrite what the driver already set
            metadata = {**classified, **metadata}
        metadata["execution"] = filtered
        await self._post(f"/api/items/{item_id}/edit", {"metadata": metadata})


def _align_ids(items: list[dict], created: list[dict]) -> list[str] | None:
    """Map each input item to a created id by matching task title in order.

    Returns None if any input title has no unconsumed match in `created` (caller falls back).
    """
    by_title: dict[str, deque[str]] = defaultdict(deque)
    for it in created:
        by_title[it.get("task")].append(it["id"])
    ids: list[str] = []
    for it in items:
        q = by_title.get(it.get("task"))
        if not q:
            return None
        ids.append(q.popleft())
    return ids
