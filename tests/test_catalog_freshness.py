"""Catalog freshness: discovery-snapshot diff, the New / removed-upstream / price-drift surfaces,
auto-adopt (default off + opt-in), and scheduler-tick isolation. Offline — the discovery fetch is
mocked (freshness._fetch_provider), no network."""

from __future__ import annotations

import toto_gateway.freshness as fr
from toto_gateway.freshness import adopted_flags, is_new, price_drift, provider_checked_at, snapshot_diff
from harness.appharness import in_process_app

SECRET = "test-secret-0123456789"


def _or_model(slug, pin=None, pout=None, ctx=8192):
    return {"slug": slug, "name": slug.split("/")[-1], "price_in": pin, "price_out": pout,
            "context_window": ctx, "tools": True, "vision": False, "cataloged": False, "catalog_id": None}


# --- pure diff + flags ---


def test_snapshot_diff_added_removed_priced():
    existing = {"a": {"price_in": 1.0, "price_out": 2.0}, "gone": {"price_in": 5.0, "price_out": 5.0}}
    fetched = [_or_model("a", 1.0, 9.0), _or_model("b", 0.1, 0.2)]  # a re-priced (out), b new, gone removed
    d = snapshot_diff(existing, fetched, now=1000.0)
    assert d.added == ["b"] and d.removed == ["gone"] and d.priced == ["a"]


def test_snapshot_diff_missing_price_is_not_a_change():
    existing = {"a": {"price_in": 1.0, "price_out": 2.0}}
    d = snapshot_diff(existing, [_or_model("a", None, None)], now=1.0)  # fireworks/cf give no price
    assert d.priced == [] and d.added == [] and d.removed == []


def test_is_new_window():
    assert is_new(1000.0, 1000.0 + 5 * 86400, 14) is True
    assert is_new(1000.0, 1000.0 + 20 * 86400, 14) is False
    assert is_new(None, 1.0, 14) is False


def test_price_drift_and_none_paths():
    assert price_drift(1.0, 2.0, {"price_in": 1.0, "price_out": 9.0}) == {
        "old_in": 1.0, "old_out": 2.0, "new_in": 1.0, "new_out": 9.0}
    assert price_drift(1.0, 2.0, {"price_in": 1.0, "price_out": 2.0}) is None  # unchanged
    assert price_drift(1.0, 2.0, {"price_in": None, "price_out": None}) is None  # no upstream price
    assert price_drift(1.0, 2.0, None) is None


def test_adopted_flags_removed_and_drift():
    snap_map = {"x": {"slug": "x", "price_in": 3.0, "price_out": 4.0, "last_seen": 100.0},
                "gone": {"slug": "gone", "price_in": 1.0, "price_out": 1.0, "last_seen": 50.0}}
    checked = provider_checked_at(list(snap_map.values()))  # = 100.0
    assert adopted_flags("x", 1.0, 2.0, snap_map, checked)["price_drift"] is not None
    assert adopted_flags("gone", 1.0, 1.0, snap_map, checked)["upstream_removed"] is True
    assert adopted_flags("never-seen", 1.0, 2.0, snap_map, checked) == {
        "upstream_removed": False, "price_drift": None}  # unknown model → no flag


# --- tick: persist + diff, and provider isolation ---


def _mock_tick(monkeypatch, by_provider):
    """Mock the freshness fetch dispatch: by_provider maps provider → list[model] (or a callable
    returning one). Everything else fetches empty."""
    async def fake_fetch(provider):
        v = by_provider.get(provider)
        models = v() if callable(v) else (v or [])
        return {"models": models, "error": None}
    monkeypatch.setattr(fr, "_fetch_provider", fake_fetch)


async def test_refresh_persists_snapshot_and_new_surfaces_in_discovery(monkeypatch):
    _mock_tick(monkeypatch, {"openrouter": [_or_model("meta/a", 0.001, 0.002)]})
    async with in_process_app(catalog="") as (client, app):
        await fr.run_freshness(app, window_days=14)  # 'meta/a' first-seen
        rows = await app.state.auth.snapshot_rows("openrouter")
        assert [r["slug"] for r in rows] == ["meta/a"]

        # the discovery endpoint's OWN live fetch is separate — mock it (fetch_openrouter returns
        # already-mapped {slug, price_in, ...} rows)
        async def live(_key):
            return {"models": [_or_model("meta/a", 0.001, 0.002)], "error": None}
        monkeypatch.setattr("toto_gateway.routes.admin_catalog_sync.fetch_openrouter", live)
        body = (await client.get("/v1/admin/catalog/discovery/openrouter")).json()
        assert body["new_count"] == 1
        assert next(m for m in body["models"] if m["slug"] == "meta/a")["is_new"] is True


async def test_tick_isolation_one_provider_error(monkeypatch):
    async def fake_fetch(provider):
        if provider == "openrouter":
            return {"models": [], "error": "openrouter boom"}
        return {"models": [], "error": None}
    monkeypatch.setattr(fr, "_fetch_provider", fake_fetch)
    async with in_process_app(catalog="") as (client, app):
        result = await fr.run_freshness(app, window_days=14)
        assert result["providers"]["openrouter"]["error"] == "openrouter boom"
        assert result["providers"]["cloudflare"]["error"] is None  # others still ran


# --- auto-adopt: default off, opt-in on ---


async def test_auto_adopt_default_off(monkeypatch):
    _mock_tick(monkeypatch, {"openrouter": [_or_model("meta/new-thing", 0.001, 0.002)]})
    async with in_process_app(catalog="") as (client, app):
        await fr.run_freshness(app, window_days=14)
        adoptions = (await client.get("/v1/admin/catalog/adoptions")).json()["adoptions"]
        assert not any(a["upstream_model"] == "meta/new-thing" for a in adoptions)  # nothing auto-adopted


async def test_auto_adopt_opt_in(monkeypatch):
    _mock_tick(monkeypatch, {"openrouter": [_or_model("meta/new-thing", 0.001, 0.002)]})
    async with in_process_app(catalog="") as (client, app):
        r = await client.put("/v1/admin/catalog/freshness/auto-adopt/openrouter", json={"enabled": True})
        assert r.status_code == 200 and r.json()["auto_adopt"] is True
        await fr.run_freshness(app, window_days=14)  # _auto_adopt materializes from the tick row — no network
        adoptions = (await client.get("/v1/admin/catalog/adoptions")).json()["adoptions"]
        auto = next(a for a in adoptions if a["upstream_model"] == "meta/new-thing")
        assert auto["created_by"] == "auto:freshness"  # distinct provenance


# --- price drift + removed-upstream on an adopted row + accept (adoption seeded directly) ---


async def _seed_adoption(app, id, slug, provider, price_in, price_out):
    from toto_gateway.catalog import CatalogEntry, Price
    from toto_gateway.routes.deps import OSS_LOCAL_ORG
    entry = CatalogEntry(id=id, lane="economy", endpoint="openai",
                         base_url="https://openrouter.ai/api/v1", api_key_env="OPENROUTER_API_KEY",
                         residency_class="cloud", upstream_model=slug, provider=provider,
                         price_usd_per_1k=Price(prompt=price_in, completion=price_out))
    await app.state.auth.add_adoption(OSS_LOCAL_ORG, id, entry_json=entry.model_dump_json(),
                                      upstream_model=slug, provider=provider, created_by="tester")


async def test_price_drift_flag_and_accept(monkeypatch):
    prices = {"n": 1}
    _mock_tick(monkeypatch, {"openrouter": lambda: [
        _or_model("meta/adoptme", 0.001, 0.002 if prices["n"] == 1 else 0.009)]})
    async with in_process_app(catalog="") as (client, app):
        await fr.run_freshness(app, window_days=14)  # snapshot @ 0.002
        await _seed_adoption(app, "or-adoptme", "meta/adoptme", "openrouter", 0.001, 0.002)

        prices["n"] = 2
        await fr.run_freshness(app, window_days=14)  # snapshot @ 0.009 — drift

        row = next(a for a in (await client.get("/v1/admin/catalog/adoptions")).json()["adoptions"]
                   if a["id"] == "or-adoptme")
        assert row["price_drift"] == {"old_in": 0.001, "old_out": 0.002, "new_in": 0.001, "new_out": 0.009}

        r = await client.post("/v1/admin/catalog/adoptions/or-adoptme/accept-price")
        assert r.status_code == 200 and r.json()["entry"]["price_out"] == 0.009
        cleared = next(a for a in (await client.get("/v1/admin/catalog/adoptions")).json()["adoptions"]
                       if a["id"] == "or-adoptme")
        assert cleared["price_drift"] is None  # accepted → no drift


async def test_removed_upstream_flag(monkeypatch):
    present = {"yes": True}
    _mock_tick(monkeypatch, {"openrouter": lambda: (
        [_or_model("meta/staying", 0.001, 0.002)]
        + ([_or_model("meta/leaving", 0.001, 0.002)] if present["yes"] else []))})
    async with in_process_app(catalog="") as (client, app):
        await fr.run_freshness(app, window_days=14)  # both present
        await _seed_adoption(app, "or-leaving", "meta/leaving", "openrouter", 0.001, 0.002)
        present["yes"] = False
        await fr.run_freshness(app, window_days=14)  # 'meta/leaving' no longer returned upstream

        row = next(a for a in (await client.get("/v1/admin/catalog/adoptions")).json()["adoptions"]
                   if a["id"] == "or-leaving")
        assert row["upstream_removed"] is True
