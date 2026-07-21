"""Cloudflare Workers AI provider module — offline coverage of the one thing that differs from a
single-key aggregator: the two-part credential (CLOUDFLARE_API_TOKEN for the bearer, and
CLOUDFLARE_ACCOUNT_ID interpolated into base_url). No live Cloudflare call — every assertion is
about how the catalog + runner resolve the URL/headers from env."""

from __future__ import annotations

from toto_gateway.catalog import Catalog
from toto_gateway.routes.models import _provider_of

FRAGMENT = "catalog.cloudflare.yaml"
ACCOUNT = "acc123deadbeef"


def _cf_entries() -> list:
    return Catalog.load(FRAGMENT).models


def test_fragment_loads_and_is_all_cloudflare():
    """Every entry is an openai-compat cloud model keyed on the Cloudflare two-part credential."""
    entries = _cf_entries()
    assert entries, "cloudflare fragment is empty"
    for e in entries:
        assert e.endpoint == "openai"
        assert e.residency_class == "cloud"
        assert e.lane == "economy"  # base_url passthrough, never the provider-keyword frontier lane
        assert e.api_key_env == "CLOUDFLARE_API_TOKEN"
        assert "${CLOUDFLARE_ACCOUNT_ID}" in e.base_url  # account id is templated, not hard-coded
        assert e.upstream_model.startswith("@cf/")  # honest Cloudflare slug


def test_resolved_base_url_interpolates_account_id(monkeypatch):
    """resolved_base_url expands ${CLOUDFLARE_ACCOUNT_ID} from the env at client-construction time —
    the account id lands in the path, the token stays in api_key_env (the bearer)."""
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT)
    e = next(x for x in _cf_entries() if x.id == "cf-gpt-oss-120b")
    assert e.resolved_base_url == (
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/v1"
    )
    assert "${" not in e.resolved_base_url


def test_resolved_base_url_leaves_untemplated_urls_untouched():
    """A base_url with no $ is byte-for-byte unchanged — the seam is inert for every other provider
    (OpenRouter, Fireworks, OpenAI)."""
    e = Catalog.load("catalog.openrouter.yaml").get("or-qwen3-coder-flash")
    assert e.resolved_base_url == e.base_url == "https://openrouter.ai/api/v1"


def test_provider_grouping_reads_cloudflare():
    """The console/models provider-display seam demotes the CLOUDFLARE_API_TOKEN + cloudflare host
    to a clean 'cloudflare' label (so cards group under Cloudflare, not raw 'openai')."""
    for e in _cf_entries():
        assert _provider_of(e) == "cloudflare"
