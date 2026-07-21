"""OSS operator adoption scope: adoptions written by the token-gate operator land under the
single-tenant `local` sentinel, and — the fix under test — resolve back onto the operator's OWN
identity, so an adopted model is visible to its dispatch/models surfaces, not just bindable in the
console. Also covers the DELETE round-trip the console's remove affordance uses."""

from toto_gateway.catalog import CatalogEntry
from harness.appharness import in_process_app


def _entry(id: str, slug: str) -> CatalogEntry:
    return CatalogEntry(
        id=id, lane="economy", endpoint="openai",
        base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
        residency_class="cloud", upstream_model=slug, provider="openrouter")


async def test_oss_operator_adoption_visible_and_removable():
    async with in_process_app() as (client, app):  # oss edition + operator bearer (harness default)
        entry = _entry("or-aion-2.0", "aion-labs/aion-2.0")
        # Stored exactly as the adopt endpoint stores it (its discovery fetch is network-bound, so
        # the write goes straight to the store under the operator's `local` scope).
        await app.state.auth.add_adoption(
            "local", entry.id, entry_json=entry.model_dump_json(),
            upstream_model=entry.upstream_model, provider="openrouter", created_by=None)

        ids = [m["id"] for m in (await client.get("/v1/models")).json()["data"]]
        assert "or-aion-2.0" in ids  # operator's own effective catalog, not just the console's

        r = await client.get("/v1/admin/catalog/effective-models")
        assert "or-aion-2.0" in [m["id"] for m in r.json()["models"]]

        r = await client.delete("/v1/admin/catalog/adoptions/or-aion-2.0")
        assert r.status_code == 200 and r.json()["deleted"] == "or-aion-2.0"

        ids = [m["id"] for m in (await client.get("/v1/models")).json()["data"]]
        assert "or-aion-2.0" not in ids
        r = await client.delete("/v1/admin/catalog/adoptions/or-aion-2.0")
        assert r.status_code == 404  # already gone; scope-pinned 404, never a leak
