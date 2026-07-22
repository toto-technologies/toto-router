"""Live catalog recompose: a provider key saved/removed via the Settings API recomposes the
DEFAULTED catalog off-request and atomically swaps it on the gateway — models appear/leave in
/v1/models with no restart. An explicit TOTO_GW_CATALOG is never touched. Offline (fake exec)."""

from __future__ import annotations

import asyncio

from toto_gateway.app.build import recompose_catalog
from toto_gateway.credentials import compose_default_catalog
from toto_gateway.config import Settings
from harness.appharness import in_process_app

CF_TOKEN = "cf_tok"
CF_ACCT = "c8c30db3dddc4ad31065d336368c7905"


# --- composition (pure) ---


def test_compose_openrouter_is_self_contained_base():
    assert compose_default_catalog({"openrouter"}) == "catalog.openrouter.yaml"


def test_compose_overlays_ride_on_the_base():
    assert compose_default_catalog({"openrouter", "cloudflare"}) == \
        "catalog.openrouter.yaml,catalog.cloudflare.yaml"
    # no openrouter → catalog.yaml carries the echo/test lanes
    assert compose_default_catalog({"cloudflare"}) == "catalog.yaml,catalog.cloudflare.yaml"
    assert compose_default_catalog(set()) == "catalog.yaml"
    assert compose_default_catalog({"openai"}) == "catalog.yaml"  # no shipped fragment → inert


# --- recompose (swap mechanism, no HTTP) ---


class _FakeGateway:
    def __init__(self, catalog):
        from toto_gateway.catalog import Catalog
        self.catalog = Catalog.load(catalog)
        self._labels = None


def test_recompose_swaps_when_defaulted():
    s = Settings(catalog="", db=":memory:", label_routing=False)  # _default_catalog → catalog.yaml
    assert s._catalog_defaulted is True
    gw = _FakeGateway(s.catalog)
    before = {e.id for e in gw.catalog.models}
    changed, added, removed = recompose_catalog(gw, s, {"cloudflare"})
    assert changed is True
    assert set(added) == {e.id for e in gw.catalog.models} - before
    assert any(i.startswith("cf-") for i in added)
    assert s.catalog == "catalog.yaml,catalog.cloudflare.yaml"
    # removing the key un-composes
    changed, added, removed = recompose_catalog(gw, s, set())
    assert changed is True and any(i.startswith("cf-") for i in removed)
    assert s.catalog == "catalog.yaml"


def test_recompose_noop_on_explicit_catalog():
    s = Settings(catalog="catalog.cloudflare.yaml", db=":memory:", label_routing=False)
    assert s._catalog_defaulted is False
    gw = _FakeGateway(s.catalog)
    changed, added, removed = recompose_catalog(gw, s, {"openrouter", "cloudflare"})
    assert changed is False and added == [] and removed == []
    assert s.catalog == "catalog.cloudflare.yaml"  # operator's pin, untouched


def test_recompose_noop_when_path_unchanged():
    s = Settings(catalog="", db=":memory:", label_routing=False)
    gw = _FakeGateway(s.catalog)
    changed, *_ = recompose_catalog(gw, s, set())  # still catalog.yaml
    assert changed is False


def test_recompose_soft_disables_orphaned_labels():
    # openrouter base carries the or-* ids the shipped labels bind to → labels live.
    s = Settings(catalog="catalog.openrouter.yaml", db=":memory:")
    s._catalog_defaulted = True  # pretend it was defaulted so recompose acts
    gw = _FakeGateway(s.catalog)
    from toto_gateway.app.build import _build_labels
    gw._labels = _build_labels(s, gw.catalog)
    assert gw._labels is not None  # bindings valid against the openrouter catalog
    # drop to catalog.yaml (no or-economy ids the classifier/labels need) → soft-disable, no crash
    recompose_catalog(gw, s, set())
    assert gw._labels is None  # orphaned bindings soft-disabled, gateway still up


def test_recompose_concurrent_reads_never_tear():
    """A reader hammering catalog_for()-style access across a swap always sees a consistent Catalog
    (every entry has an id) — the swap is a single atomic attribute assignment."""
    s = Settings(catalog="", db=":memory:", label_routing=False)
    gw = _FakeGateway(s.catalog)

    async def reader():
        for _ in range(2000):
            ids = [e.id for e in gw.catalog.models]  # snapshot the current catalog
            assert all(ids)  # no torn/empty entry
            await asyncio.sleep(0)

    async def swapper():
        for i in range(50):
            recompose_catalog(gw, s, {"cloudflare"} if i % 2 else set())
            await asyncio.sleep(0)

    async def run():
        await asyncio.gather(reader(), reader(), swapper())

    asyncio.run(run())


# --- boot-time multi-fragment defaulting ---


def test_boot_env_defaulting_composes_all_keyed(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "y")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "z")
    s = Settings(catalog="", db=":memory:")
    assert s.catalog == "catalog.openrouter.yaml,catalog.cloudflare.yaml"
    assert s._catalog_defaulted is True


# --- live through the Settings API (HTTP, fake exec) ---


async def test_put_key_grows_catalog_without_restart(tmp_path):
    # A FILE db (not :memory:) so the SQLite peek stored_org_key_providers sees the just-saved key —
    # the real OSS deploy shape. catalog="" arms recompose; credentials_secret for the at-rest store.
    async with in_process_app(catalog="", db=f"{tmp_path}/gw.db",
                              credentials_secret="test-secret-0123456789") as (client, app):
        assert app.state.settings._catalog_defaulted is True

        before = {m["id"] for m in (await client.get("/v1/models")).json()["data"]}
        assert not any(i.startswith("cf-") for i in before)

        r = await client.put("/v1/admin/provider-keys/cloudflare",
                             json={"key": CF_TOKEN, "account_id": CF_ACCT})
        assert r.status_code == 200, r.text
        assert any(i.startswith("cf-") for i in r.json()["models_added"])

        after = {m["id"] for m in (await client.get("/v1/models")).json()["data"]}
        assert {i for i in after if i.startswith("cf-")}  # cf models live, no restart
        # the DRIVER plane captured its own catalog at build — it must move with the gateway
        assert app.state.driver.catalog.get("cf-gpt-oss-120b") is not None

        r = await client.delete("/v1/admin/provider-keys/cloudflare")
        assert r.status_code == 200
        assert any(i.startswith("cf-") for i in r.json()["models_removed"])
        gone = {m["id"] for m in (await client.get("/v1/models")).json()["data"]}
        assert not any(i.startswith("cf-") for i in gone)
        assert app.state.driver.catalog.get("cf-gpt-oss-120b") is None  # driver un-composed too


async def test_get_reports_catalog_defaulted():
    async with in_process_app() as (client, app):
        body = (await client.get("/v1/admin/provider-keys")).json()
        assert body["catalog_defaulted"] == app.state.settings._catalog_defaulted
