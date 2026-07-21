"""Custom task-type description rules: the desc IS the routing behavior (the classifier matches
prompts against it), so a too-thin one is rejected at the PUT with the writing guidance in the
error body — that message is where an agent creating task types programmatically learns the shape."""

from harness.appharness import in_process_app


def _body(desc):
    return {"custom_labels": [{"name": "sql_authoring", "desc": desc, "model": "echo-local"}]}


async def test_thin_desc_rejected_with_guidance():
    async with in_process_app() as (client, _app):
        for thin in ("", "sql", "database stuff"):
            r = await client.put("/v1/admin/org/routing-policy", json=_body(thin))
            assert r.status_code == 400, (thin, r.text)
            err = r.json()["error"]
            assert err["code"] == "invalid_custom_label_desc"
            # The guidance rides the error: behavioral one-sentence rule + the good/bad pair.
            assert "one focused sentence" in err["message"]
            assert "database stuff" in err["message"]


async def test_real_desc_accepted():
    async with in_process_app() as (client, _app):
        r = await client.put(
            "/v1/admin/org/routing-policy",
            json=_body("writing or explaining SQL queries against a relational database"))
        assert r.status_code == 200, r.text
        assert [c["name"] for c in r.json()["custom_labels"]] == ["sql_authoring"]
