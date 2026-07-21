"""OSS operator price overrides govern the operator's own traffic costing: an override written
via the console (stored under the `local` scope) must be applied by the operator identity's
effective catalog at dispatch, and — the no-drift guard — a request WITHOUT any override must
price exactly off the shipped catalog."""

from harness.appharness import in_process_app

# catalog.yaml's echo-cloud row: per-1k prompt/completion — the baseline the no-drift test pins.
BASE_PROMPT, BASE_COMPLETION = 0.003, 0.015


def _chat():
    return {"model": "echo-cloud", "messages": [{"role": "user", "content": "hello there"}]}


async def test_no_override_prices_off_the_shipped_catalog():
    async with in_process_app() as (client, _app):
        r = await client.post("/v1/chat/completions", json=_chat())
        assert r.status_code == 200, r.text
        body = r.json()
        u = body["usage"]
        expected = (u["prompt_tokens"] * BASE_PROMPT + u["completion_tokens"] * BASE_COMPLETION) / 1000
        assert abs(body["x_toto"]["cost_usd"] - expected) < 1e-12


async def test_operator_override_applies_to_operator_traffic():
    async with in_process_app() as (client, _app):
        r = await client.put("/v1/admin/catalog/price-overrides/echo-cloud",
                             json={"prompt_usd_per_mtok": 3000.0, "completion_usd_per_mtok": 15000.0})
        assert r.status_code == 200, r.text  # stored (per-Mtok in, per-1k at rest: 3.0/15.0)

        r = await client.post("/v1/chat/completions", json=_chat())
        assert r.status_code == 200, r.text
        body = r.json()
        u = body["usage"]
        # 1000x the base price — unmistakably the override, not the catalog row.
        expected = (u["prompt_tokens"] * 3.0 + u["completion_tokens"] * 15.0) / 1000
        assert abs(body["x_toto"]["cost_usd"] - expected) < 1e-9

        # Removing the override restores base pricing on the very next request.
        r = await client.delete("/v1/admin/catalog/price-overrides/echo-cloud")
        assert r.status_code == 200, r.text
        r = await client.post("/v1/chat/completions", json=_chat())
        u = r.json()["usage"]
        expected = (u["prompt_tokens"] * BASE_PROMPT + u["completion_tokens"] * BASE_COMPLETION) / 1000
        assert abs(r.json()["x_toto"]["cost_usd"] - expected) < 1e-12
