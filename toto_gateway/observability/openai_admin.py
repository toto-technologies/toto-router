"""OpenAI Platform Admin API connector — normalizes org usage/cost/members to the shared schema.

Talks to `api.openai.com/v1/organization/*` with an `sk-admin-` key (Authorization: Bearer).
Two pagination dialects live here on purpose (research doc §2-5): the reporting endpoints
(usage/completions, costs) page through opaque `page`/`next_page` cursors, while the
management endpoints (users, projects, audit_logs) walk `limit`+`after` with `last_id`.
Every method flattens OpenAI's bucket/results envelope into the frozen dataclasses from
`schema.py` at this boundary — nothing downstream sees a raw OpenAI payload.

Key material only ever rides the Authorization header on the client; AdminAPIError messages
come from response bodies (see http._detail), never from the request.
"""

from __future__ import annotations

import datetime

import httpx

from .http import get_json
from .schema import CostBucket, OrgMember, UsageBucket

_BASE = "https://api.openai.com"


class OpenAIAdminClient:
    provider = "openai"

    def __init__(self, api_key: str, *, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=_BASE,
            timeout=timeout,
            transport=transport,  # tests inject a MockTransport; None => real network
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def verify(self) -> dict:
        # Cheapest authenticated call: one project. Raises AdminAPIError(401/403) on a
        # bad or wrong-type key. OpenAI exposes no org name here, so org_name is None.
        await get_json(self._http, "/v1/organization/projects",
                       params={"limit": 1}, provider=self.provider)
        return {"org_id": None, "org_name": None}

    async def usage(self, starting_at: str, ending_at: str) -> list[UsageBucket]:
        params = {
            "start_time": _unix(starting_at),
            "end_time": _unix(ending_at),
            "bucket_width": "1d",
            "group_by": ["model", "project_id", "user_id"],
        }
        out: list[UsageBucket] = []
        async for bucket in self._report("/v1/organization/usage/completions", params):
            start, end = _iso(bucket.get("start_time")), _iso(bucket.get("end_time"))
            for r in bucket.get("results") or []:
                out.append(UsageBucket(
                    provider=self.provider,
                    starting_at=start,
                    ending_at=end,
                    model=r.get("model"),
                    scope_id=r.get("project_id"),
                    scope_name=None,
                    actor_id=r.get("user_id"),
                    actor_name=None,
                    # OpenAI's input_tokens INCLUDES cache reads (input_cached_tokens is a
                    # subset); the schema wants uncached-only, like Anthropic reports natively.
                    input_tokens=max((r.get("input_tokens") or 0) - (r.get("input_cached_tokens") or 0), 0),
                    cached_input_tokens=r.get("input_cached_tokens") or 0,
                    cache_creation_tokens=0,  # OpenAI has no cache-write metric
                    output_tokens=r.get("output_tokens") or 0,
                    requests=r.get("num_model_requests"),
                ))
        return out

    async def costs(self, starting_at: str, ending_at: str) -> list[CostBucket]:
        params = {
            "start_time": _unix(starting_at),
            "end_time": _unix(ending_at),
            "bucket_width": "1d",  # costs supports 1d only
            "group_by": ["line_item", "project_id"],
        }
        out: list[CostBucket] = []
        async for bucket in self._report("/v1/organization/costs", params):
            start, end = _iso(bucket.get("start_time")), _iso(bucket.get("end_time"))
            for r in bucket.get("results") or []:
                line_item = r.get("line_item")
                out.append(CostBucket(
                    provider=self.provider,
                    starting_at=start,
                    ending_at=end,
                    model=_model_from_line_item(line_item),
                    line_item=line_item,
                    scope_id=r.get("project_id"),
                    amount_usd=float((r.get("amount") or {}).get("value") or 0.0),
                ))
        return out

    async def members(self) -> list[OrgMember]:
        out: list[OrgMember] = []
        async for u in self._list("/v1/organization/users"):
            out.append(OrgMember(
                provider=self.provider,
                id=u.get("id"),
                email=u.get("email"),
                name=u.get("name"),
                role=u.get("role"),
                added_at=_iso(u.get("added_at")),
            ))
        return out

    async def projects(self) -> list[dict]:
        # Inventory for insights name-resolution: {id, name, archived}.
        out: list[dict] = []
        async for p in self._list("/v1/organization/projects", params={"include_archived": "true"}):
            out.append({"id": p.get("id"), "name": p.get("name"),
                        "archived": p.get("status") == "archived"})
        return out

    async def audit_events(self, effective_at_gte: int, limit: int = 100) -> list[dict]:
        # Raw passthrough of audit_log records at/after effective_at_gte (Unix s).
        out: list[dict] = []
        async for ev in self._list("/v1/organization/audit_logs",
                                    params={"effective_at[gte]": effective_at_gte, "limit": limit}):
            out.append(ev)
        return out

    # --- pagination dialects (research doc §2-5) ---------------------------------

    async def _report(self, url: str, params: dict):
        """Reporting endpoints: opaque page/next_page cursor, `data` is a list of buckets."""
        params = dict(params)
        while True:
            page = await get_json(self._http, url, params=params, provider=self.provider)
            for bucket in page.get("data") or []:
                yield bucket
            if not page.get("has_more") or not page.get("next_page"):
                return
            params["page"] = page["next_page"]

    async def _list(self, url: str, params: dict | None = None):
        """Management endpoints: limit/after cursor walk, `data` is a list of items."""
        params = dict(params or {})
        while True:
            page = await get_json(self._http, url, params=params, provider=self.provider)
            data = page.get("data") or []
            for item in data:
                yield item
            if not page.get("has_more") or not page.get("last_id"):
                return
            params["after"] = page["last_id"]


def _model_from_line_item(line_item: str | None) -> str | None:
    """OpenAI cost line items read '<model>, input' / '<model>, output' (or ft- variants).
    Return the model prefix when it parses, else None — never guess from a bare string."""
    if not line_item:
        return None
    for sep in (", input", ", output"):
        if sep in line_item:
            return line_item.split(sep, 1)[0]
    return None


def _unix(iso: str) -> int:
    """ISO-8601 UTC string -> inclusive Unix seconds for the reporting endpoints' start/end_time."""
    return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _iso(ts: int | None) -> str | None:
    """Unix seconds -> ISO-8601 UTC ('...Z'). None passes through (missing timestamp)."""
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
