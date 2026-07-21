"""HTTP round-trip of the caching surface on the org routing policy.

The console's Caching page persists prewarm + stick_ttls + cache over PUT
/v1/admin/org/routing-policy (full-replace). The OSS operator token resolves to the `local`
sentinel org, so no ?org_id= is needed — exactly the console's path. Covers: values echo on the
PUT response and survive a GET, the version bumps per write (the console's re-seed key), a later
PUT omitting a field full-replaces it away, and the fail-closed validators reject junk.
"""

from __future__ import annotations

from harness.appharness import in_process_app

POLICY = "/v1/admin/org/routing-policy"


async def test_cache_prewarm_stick_ttls_round_trip():
    async with in_process_app() as (client, _):
        r = await client.get(POLICY)
        assert r.status_code == 200, r.text
        base = r.json()
        assert base["prewarm"] is False and base["cache"] == {} and base["stick_ttls"] == {}

        body = {
            "prewarm": True,
            "stick_ttls": {"code_generation": 3600, "chatbot": 300},
            "cache": {"preset": "balanced", "auto_inject": True,
                      "auto_inject_min_messages": 3, "warmth_routing": True},
        }
        r = await client.put(POLICY, json=body)
        assert r.status_code == 200, r.text
        put = r.json()
        assert put["prewarm"] is True
        assert put["stick_ttls"] == {"code_generation": 3600.0, "chatbot": 300.0}
        assert put["cache"] == body["cache"]
        assert put["version"] == base["version"] + 1  # the console re-seeds edit state off this

        r = await client.get(POLICY)
        got = r.json()
        assert got["prewarm"] is True and got["cache"] == body["cache"]
        assert got["stick_ttls"] == {"code_generation": 3600.0, "chatbot": 300.0}

        # full-replace: omitting the caching fields clears them (the reason the console passes the
        # whole surface through on every save)
        r = await client.put(POLICY, json={})
        cleared = r.json()
        assert cleared["prewarm"] is False and cleared["cache"] == {} and cleared["stick_ttls"] == {}
        assert cleared["version"] == put["version"] + 1


async def test_cache_and_stick_ttls_validate_fail_closed():
    async with in_process_app() as (client, _):
        # unknown cache key
        r = await client.put(POLICY, json={"cache": {"nope": 1}})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cache"
        # min-messages outside [1, 50]
        r = await client.put(POLICY, json={"cache": {"auto_inject_min_messages": 0}})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cache"
        # unknown label in stick_ttls
        r = await client.put(POLICY, json={"stick_ttls": {"ghost_label": 60}})
        assert r.status_code == 400 and r.json()["error"]["code"] == "unknown_label"
        # hold beyond the one-day cap
        r = await client.put(POLICY, json={"stick_ttls": {"code_generation": 999999}})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_stick_ttls"
        # nothing stuck from the rejected writes
        r = await client.get(POLICY)
        assert r.json()["cache"] == {} and r.json()["stick_ttls"] == {}
