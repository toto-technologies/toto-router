"""Anthropic Organization Admin API connector — normalizes into the shared schema.

An org owner pastes a `sk-ant-admin01-...` admin key; this pulls what the Admin API sees
(usage, cost, members, workspaces, api keys, Claude Code analytics) and flattens each
provider envelope into `schema.py` shapes at the fetch boundary. Two facts drive the code:

  1. Two pagination schemes that must NOT share a loop. Management endpoints (users,
     workspaces, api_keys) cursor on object ids (`after_id` <- `last_id`); reporting
     endpoints (usage/cost/claude_code) use opaque `page`/`next_page` tokens.
  2. Cost `amount` is a decimal string of CENTS ("123.45" == $1.23) — normalized to float
     USD dollars via Decimal so nothing downstream ever divides money again.

Auth (`x-api-key` + `anthropic-version`) lives on the client; keys never reach a log or an
error message (get_json builds AdminAPIError from response bodies only).
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from .http import get_json
from .schema import CostBucket, OrgMember, UsageBucket

_BASE_URL = "https://api.anthropic.com"
_VERSION = "2023-06-01"
_LIMIT = 1000  # management cursor page size (endpoint max); reporting uses next_page tokens


class AnthropicAdminClient:
    provider = "anthropic"

    def __init__(self, api_key: str, *, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        # transport is the DI seam for respx tests (mirrors TotoClient); None = real network.
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={"x-api-key": api_key, "anthropic-version": _VERSION},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def verify(self) -> dict:
        """Cheapest live auth check. Raises AdminAPIError on a bad/wrong-type key."""
        body = await get_json(self._client, "/v1/organizations/me", provider=self.provider)
        return {"org_id": body.get("id"), "org_name": body.get("name")}

    async def usage(self, starting_at: str, ending_at: str) -> list[UsageBucket]:
        params = {
            "starting_at": starting_at,
            "ending_at": ending_at,
            "bucket_width": "1d",
            "group_by[]": ["model", "workspace_id", "api_key_id"],
            "limit": 31,  # 1d bucket max — default is 7, which makes a 90-day pull ~13 round-trips
        }
        out: list[UsageBucket] = []
        async for bucket in self._walk_reporting("/v1/organizations/usage_report/messages", params):
            for r in bucket.get("results") or []:
                cache_creation = r.get("cache_creation") or {}
                out.append(UsageBucket(
                    provider=self.provider,
                    starting_at=bucket.get("starting_at"),
                    ending_at=bucket.get("ending_at"),
                    model=r.get("model"),
                    scope_id=r.get("workspace_id"),
                    scope_name=None,
                    actor_id=r.get("api_key_id"),
                    actor_name=None,
                    input_tokens=r.get("uncached_input_tokens") or 0,
                    cached_input_tokens=r.get("cache_read_input_tokens") or 0,
                    cache_creation_tokens=sum(v or 0 for v in cache_creation.values()),
                    output_tokens=r.get("output_tokens") or 0,
                    requests=None,
                ))
        return out

    async def costs(self, starting_at: str, ending_at: str) -> list[CostBucket]:
        params = {
            "starting_at": starting_at,
            "ending_at": ending_at,
            "bucket_width": "1d",
            "group_by[]": ["description", "workspace_id"],
        }
        out: list[CostBucket] = []
        async for bucket in self._walk_reporting("/v1/organizations/cost_report", params):
            for r in bucket.get("results") or []:
                amount = r.get("amount")
                # amount is decimal-string CENTS; Decimal keeps the round exact before float.
                usd = float(Decimal(str(amount)) / 100) if amount is not None else 0.0
                out.append(CostBucket(
                    provider=self.provider,
                    starting_at=bucket.get("starting_at"),
                    ending_at=bucket.get("ending_at"),
                    model=r.get("model"),
                    line_item=r.get("description"),
                    scope_id=r.get("workspace_id"),
                    amount_usd=usd,
                ))
        return out

    async def members(self) -> list[OrgMember]:
        out: list[OrgMember] = []
        async for u in self._walk_management("/v1/organizations/users"):
            out.append(OrgMember(
                provider=self.provider,
                id=u.get("id"),
                email=u.get("email"),
                name=u.get("name"),
                role=u.get("role"),
                added_at=u.get("added_at"),
            ))
        return out

    async def workspaces(self) -> list[dict]:
        out: list[dict] = []
        # include_archived so insights can still resolve the name of an archived scope.
        async for w in self._walk_management("/v1/organizations/workspaces",
                                             include_archived="true"):
            out.append({
                "id": w.get("id"),
                "name": w.get("name"),
                "archived": w.get("archived_at") is not None,
            })
        return out

    async def api_keys(self) -> list[dict]:
        out: list[dict] = []
        async for k in self._walk_management("/v1/organizations/api_keys"):
            out.append({
                "id": k.get("id"),
                "name": k.get("name"),
                "scope_id": k.get("workspace_id"),
                "status": k.get("status"),
                "hint": k.get("partial_key_hint"),
            })
        return out

    async def claude_code_daily(self, date: str) -> list[dict]:
        """Raw per-user Claude Code records for a single UTC day ("YYYY-MM-DD"), passed through."""
        records: list[dict] = []
        async for rec in self._walk_reporting("/v1/organizations/usage_report/claude_code",
                                              {"starting_at": date}):
            records.append(rec)
        return records

    async def _walk_reporting(self, url: str, params: dict):
        """Reporting endpoints: opaque page/next_page tokens. Yields each item of data[]."""
        page: dict = dict(params)
        while True:
            body = await get_json(self._client, url, params=page, provider=self.provider)
            for item in body.get("data") or []:
                yield item
            next_page = body.get("next_page")
            if not body.get("has_more") or not next_page:
                return
            page["page"] = next_page

    async def _walk_management(self, url: str, **params):
        """Management endpoints: after_id/last_id id cursor. Yields each object of data[]."""
        cursor: dict = {"limit": _LIMIT, **params}
        while True:
            body = await get_json(self._client, url, params=cursor, provider=self.provider)
            for obj in body.get("data") or []:
                yield obj
            last_id = body.get("last_id")
            if not body.get("has_more") or not last_id:
                return
            cursor["after_id"] = last_id
