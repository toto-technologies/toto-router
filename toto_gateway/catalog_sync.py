"""Fireworks account sync: compare the live Fireworks account (its fine-tune models + on-demand
deployments) against what the catalog references, and surface drift for the console.

Two halves, kept apart so the interesting logic is testable without the network:
  - `fetch_fireworks` (async httpx) — resolve account, list account models + deployments, paginate.
  - `reconcile` (pure) — diff catalog entries against the fetched account state → drift/ok/status.

The sync is a LIVE READ, not a stored history. Only Fireworks entries participate (the catalog's
other providers have no equivalent account API here).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from .catalog import CatalogEntry
from .credentials import byok_keys, expand_env_refs
from .routes.models import _provider_of

API = "https://api.fireworks.ai/v1"
INFERENCE_BASE = "https://api.fireworks.ai/inference/v1"


# --- fetch (network) -------------------------------------------------------------------------


async def _get(client: httpx.AsyncClient, path: str, params: dict | None = None) -> dict:
    resp = await client.get(f"{API}{path}", params=params)
    resp.raise_for_status()
    return resp.json()


def _norm_model(m: dict) -> dict:
    return {"name": m.get("name", ""), "display_name": m.get("displayName", ""),
            "state": m.get("state", ""), "base_model": m.get("baseModel", ""),
            "create_time": m.get("createTime", "")}


def _norm_deployment(d: dict) -> dict:
    return {"name": d.get("name", ""), "base_model": d.get("baseModel", ""),
            "state": d.get("state", ""), "create_time": d.get("createTime", "")}


async def fetch_fireworks(api_key: str) -> dict:
    """Resolve the account and list its models + deployments. Returns
    {account, account_models, deployments, error}. Any HTTP failure is caught and returned in
    `error` (with whatever was fetched so far) — the console must never 500 on a provider hiccup."""
    account, account_models, deployments, error = None, [], [], None
    try:
        async with httpx.AsyncClient(timeout=30,
                                     headers={"Authorization": f"Bearer {api_key}"}) as client:
            accts = (await _get(client, "/accounts")).get("accounts", [])
            if not accts:
                return {"account": None, "account_models": [], "deployments": [],
                        "error": "no accounts visible to this API key"}
            account = accts[0]["name"].split("/")[-1]

            token = None
            while True:
                params = {"pageSize": 200}
                if token:
                    params["pageToken"] = token
                data = await _get(client, f"/accounts/{account}/models", params)
                account_models.extend(_norm_model(m) for m in data.get("models", []))
                token = data.get("nextPageToken")
                if not token:
                    break

            deps = await _get(client, f"/accounts/{account}/deployments")
            deployments = [_norm_deployment(d) for d in deps.get("deployments", [])]
    except httpx.HTTPError as e:
        error = f"fireworks API error: {e}"
    return {"account": account, "account_models": account_models,
            "deployments": deployments, "error": error}


# --- reconcile (pure) ------------------------------------------------------------------------


def _slug(model_name: str) -> str:
    """`accounts/toto-tech/models/docx-formatting-editor-v1` → `docx-formatting-editor-v1`."""
    return model_name.rstrip("/").split("/")[-1] or "model"


def _suggested_yaml(model_name: str, deployment: str | None) -> str:
    """A ready-to-paste catalog entry for an un-cataloged account model — mirrors the real shape
    in catalog.fireworks.yaml. Parses back into a valid CatalogEntry (asserted in the tests)."""
    upstream = f"{model_name}#{deployment}" if deployment else model_name
    return (
        f"- id: fw-{_slug(model_name)}\n"
        f"  lane: economy\n"
        f"  endpoint: openai\n"
        f"  base_url: {INFERENCE_BASE}\n"
        f"  api_key_env: FIREWORKS_API_KEY\n"
        f"  residency_class: cloud\n"
        f"  price_usd_per_1k: {{ prompt: 0.0, completion: 0.0 }}  # on-demand GPU billing\n"
        f"  context_window: 131072\n"
        f"  upstream_model: {upstream}\n"
    )


def _ready_for(model_name: str, deployments: list[dict]) -> list[dict]:
    return [d for d in deployments if d["base_model"] == model_name and d["state"] == "READY"]


def reconcile(entries: list[CatalogEntry], account: str | None,
              models: list[dict], deployments: list[dict]) -> dict[str, Any]:
    """Diff Fireworks catalog entries against the live account. Returns {catalog_entries, drift, ok}.
    Pure — no I/O — so every branch is unit-testable. `models`/`deployments` are the normalized
    dicts from `fetch_fireworks` (keys: name, base_model, state, ...)."""
    catalog_entries: list[dict] = []
    drift: list[dict] = []
    ok: list[dict] = []
    account_prefix = f"accounts/{account}/models/" if account else None
    referenced: set[str] = set()  # account model names some catalog entry points at (pre-`#`)

    for e in entries:
        if _provider_of(e) != "fireworks":
            continue
        upstream = e.effective_upstream_model
        pre, _, suffix = upstream.partition("#")
        suffix = suffix or None

        if pre.startswith("accounts/fireworks/"):  # Fireworks serverless platform model
            catalog_entries.append({"id": e.id, "upstream_model": upstream, "status": "serverless"})
            continue
        if not (account_prefix and pre.startswith(account_prefix)):
            # Fireworks-provider entry we can't reconcile against this account (other account, or
            # no account resolved) — report it, but it isn't drift against a model we can see.
            catalog_entries.append({"id": e.id, "upstream_model": upstream, "status": "unknown"})
            continue

        referenced.add(pre)
        ready = _ready_for(pre, deployments)
        if not ready:
            catalog_entries.append({"id": e.id, "upstream_model": upstream,
                                    "status": "cataloged_not_deployed"})
            drift.append({"kind": "cataloged_not_deployed", "severity": "info",
                          "catalog_id": e.id, "upstream_model": upstream})
            continue
        names = {d["name"] for d in ready}
        if suffix is None or suffix in names:
            live = suffix if suffix in names else ready[0]["name"]
            catalog_entries.append({"id": e.id, "upstream_model": upstream, "status": "ok"})
            ok.append({"catalog_id": e.id, "deployment": live, "deployment_state": "READY"})
        else:  # suffix names a different/dead deployment, but a live one exists
            live = ready[0]["name"]
            catalog_entries.append({"id": e.id, "upstream_model": upstream, "status": "stale_suffix"})
            drift.append({"kind": "stale_suffix", "severity": "warn", "catalog_id": e.id,
                          "cataloged_deployment": suffix, "live_deployment": live,
                          "suggested_upstream": f"{pre}#{live}"})

    for m in models:
        if m["name"] in referenced:
            continue
        ready = _ready_for(m["name"], deployments)
        dep = ready[0]["name"] if ready else None
        drift.append({"kind": "not_cataloged", "severity": "warn", "model": m["name"],
                      "deployment": dep, "suggested_yaml": _suggested_yaml(m["name"], dep)})

    return {"catalog_entries": catalog_entries, "drift": drift, "ok": ok}


# --- OpenRouter discovery --------------------------------------------------------------------

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def _f1k(v: Any) -> float:
    """OpenRouter prices are USD-per-token strings; our convention is per-1k. Non-numeric or
    negative sentinels (e.g. "-1") → 0.0."""
    try:
        f = float(v) * 1000
    except (TypeError, ValueError):
        return 0.0
    return f if f >= 0 else 0.0


def map_openrouter_model(m: dict) -> dict | None:
    """One OpenRouter /models entry → our lean shape (cataloged/catalog_id filled by reconcile).
    Returns None for a non-text-out model (obvious skip); permissive when fields are missing."""
    arch = m.get("architecture") or {}
    out_mods = arch.get("output_modalities")
    if isinstance(out_mods, list) and "text" not in out_mods:
        return None  # not a text-out LLM
    pricing = m.get("pricing") or {}
    return {"slug": m.get("id", ""), "name": m.get("name", ""),
            "context_window": m.get("context_length") or 0,
            "price_in": _f1k(pricing.get("prompt")), "price_out": _f1k(pricing.get("completion")),
            "tools": "tools" in (m.get("supported_parameters") or []),
            "vision": "image" in (arch.get("input_modalities") or []),
            "cataloged": False, "catalog_id": None}


async def fetch_openrouter(api_key: str | None) -> dict:
    """List every OpenRouter model. Public endpoint — no key required; the key is sent when present.
    Returns {models, error}; any HTTP failure → error set, models empty (never raises)."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.get(OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except httpx.HTTPError as e:
        return {"models": [], "error": f"openrouter API error: {e}"}
    mapped = [row for m in data if (row := map_openrouter_model(m)) is not None]
    return {"models": mapped, "error": None}


def reconcile_openrouter(entries: list[CatalogEntry], models: list[dict]) -> list[dict]:
    """Mark each discovered model cataloged/catalog_id (a catalog openrouter entry's
    effective_upstream_model == slug), sorted by slug for stable output. Mutates in place."""
    by_slug = {e.effective_upstream_model: e.id
               for e in entries if _provider_of(e) == "openrouter"}
    for m in models:
        m["catalog_id"] = by_slug.get(m["slug"])
        m["cataloged"] = m["catalog_id"] is not None
    return sorted(models, key=lambda m: m["slug"])


# --- Fireworks library discovery -------------------------------------------------------------

FIREWORKS_LIBRARY_ACCOUNT = "fireworks"  # the platform's own account holds the public model library


def map_fireworks_library_model(m: dict) -> dict | None:
    """One Fireworks platform-library entry → our lean shape (cataloged/catalog_id filled by
    reconcile). Returns None for a model to hide: non-READY, deprecated, or embedding-kind."""
    if m.get("state", "READY") != "READY" or m.get("deprecationDate"):
        return None
    if "EMBEDDING" in (m.get("kind") or "").upper():
        return None
    name = m.get("name", "")
    return {"slug": name, "name": m.get("displayName") or name.rstrip("/").split("/")[-1],
            "context_window": m.get("contextLength") or 0,
            "tunable": bool(m.get("tunable")), "tools": bool(m.get("supportsTools")),
            "vision": bool(m.get("supportsImageInput")),
            "cataloged": False, "catalog_id": None}


async def fetch_fireworks_library(api_key: str) -> dict:
    """List the public Fireworks model library (the `fireworks` account). Requires the key.
    Returns {models, filtered_out, error}; hidden entries (deprecated/embedding/non-READY) are
    counted in filtered_out. Never raises — HTTP failure → error set, models empty."""
    models, filtered_out, error = [], 0, None
    try:
        async with httpx.AsyncClient(timeout=30,
                                     headers={"Authorization": f"Bearer {api_key}"}) as client:
            token = None
            while True:
                params = {"pageSize": 200}
                if token:
                    params["pageToken"] = token
                data = await _get(client, f"/accounts/{FIREWORKS_LIBRARY_ACCOUNT}/models", params)
                for raw in data.get("models", []):
                    row = map_fireworks_library_model(raw)
                    if row is None:
                        filtered_out += 1
                    else:
                        models.append(row)
                token = data.get("nextPageToken")
                if not token:
                    break
    except httpx.HTTPError as e:
        return {"models": [], "filtered_out": 0, "error": f"fireworks API error: {e}"}
    return {"models": models, "filtered_out": filtered_out, "error": error}


def reconcile_fireworks_library(entries: list[CatalogEntry], models: list[dict]) -> list[dict]:
    """Mark each library model cataloged/catalog_id — a fireworks catalog entry whose
    effective_upstream_model pre-`#` == slug. Sorted by slug. Mutates in place."""
    by_slug = {e.effective_upstream_model.partition("#")[0]: e.id
               for e in entries if _provider_of(e) == "fireworks"}
    for m in models:
        m["catalog_id"] = by_slug.get(m["slug"])
        m["cataloged"] = m["catalog_id"] is not None
    return sorted(models, key=lambda m: m["slug"])


# --- Cloudflare Workers AI library discovery -------------------------------------------------

CF_API = "https://api.cloudflare.com/client/v4"


def _cf_prop(props: Any, key: str) -> str | None:
    """Value of the {property_id, value} entry named `key` in a Cloudflare model's `properties`
    list (Cloudflare's model-catalog shape), or None. Tolerates a missing/oddly-shaped list."""
    if not isinstance(props, list):
        return None
    for p in props:
        if isinstance(p, dict) and p.get("property_id") == key:
            return p.get("value")
    return None


def map_cloudflare_model(m: dict) -> dict | None:
    """One Cloudflare Workers AI models/search entry → our lean shape (cataloged/catalog_id filled by
    reconcile). Returns None for a non-text-generation entry. Cloudflare's models API exposes no
    per-token price (pricing lives on a separate page), so price_in/out are 0 — same as the Fireworks
    library; an admin refines the price after adopting.
    ponytail: the properties shape (a list of {property_id, value}) follows Cloudflare's model-catalog
    docs but is not verified against a live account in this build — a shape drift just yields ctx 0 /
    tools False, never a crash (the fields default)."""
    task = ((m.get("task") or {}).get("name") or "").lower()
    if task and task != "text generation":
        return None
    slug = m.get("name", "")
    if not slug:
        return None
    props = m.get("properties")
    try:
        ctx = int(_cf_prop(props, "context_window") or 0)
    except (TypeError, ValueError):
        ctx = 0
    return {"slug": slug, "name": slug.rstrip("/").split("/")[-1],
            "context_window": ctx, "price_in": 0.0, "price_out": 0.0,
            "tools": str(_cf_prop(props, "function_calling") or "").lower() == "true",
            "vision": str(_cf_prop(props, "vision") or "").lower() == "true",
            "cataloged": False, "catalog_id": None}


async def fetch_cloudflare_library(api_token: str, account_id: str) -> dict:
    """List the Cloudflare Workers AI text-generation model catalog for `account_id`. Needs BOTH the
    token and the account id (the two-part credential). Returns {models, filtered_out, error}; hidden
    (non-text-generation) entries are counted in filtered_out. Never raises — HTTP failure → error
    set, models empty."""
    models, filtered_out, error = [], 0, None
    url = f"{CF_API}/accounts/{account_id}/ai/models/search"
    try:
        async with httpx.AsyncClient(timeout=30,
                                     headers={"Authorization": f"Bearer {api_token}"}) as client:
            page = 1
            while True:
                resp = await client.get(url, params={"task": "Text Generation",
                                                     "page": page, "per_page": 100})
                resp.raise_for_status()
                body = resp.json()
                for raw in body.get("result") or []:
                    row = map_cloudflare_model(raw)
                    if row is None:
                        filtered_out += 1
                    else:
                        models.append(row)
                info = body.get("result_info") or {}
                total_pages = info.get("total_pages")
                if not total_pages or page >= total_pages:
                    break
                page += 1
    except httpx.HTTPError as e:
        return {"models": [], "filtered_out": 0, "error": f"cloudflare API error: {e}"}
    return {"models": models, "filtered_out": filtered_out, "error": error}


def reconcile_cloudflare_library(entries: list[CatalogEntry], models: list[dict]) -> list[dict]:
    """Mark each library model cataloged/catalog_id — a cloudflare catalog entry whose
    effective_upstream_model == slug (the `@cf/...` id). Sorted by slug. Mutates in place."""
    by_slug = {e.effective_upstream_model: e.id
               for e in entries if _provider_of(e) == "cloudflare"}
    for m in models:
        m["catalog_id"] = by_slug.get(m["slug"])
        m["cataloged"] = m["catalog_id"] is not None
    return sorted(models, key=lambda m: m["slug"])


# --- Anthropic direct-API library discovery ---------------------------------------------------

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"  # the stable version the anthropic SDK pins for /v1/messages


def map_anthropic_model(m: dict) -> dict | None:
    """One Anthropic /v1/models entry → our lean shape (cataloged/catalog_id filled by reconcile).
    The endpoint returns only id/display_name/created_at — no context, price, or capability
    fields — so those stay 0/False (unknown, not claims); an admin refines after adopting, same
    as the Fireworks library."""
    slug = m.get("id", "")
    if not slug:
        return None
    return {"slug": slug, "name": m.get("display_name") or slug,
            "context_window": 0, "price_in": 0.0, "price_out": 0.0,
            "tools": False, "vision": False, "cataloged": False, "catalog_id": None}


async def fetch_anthropic_library(api_key: str) -> dict:
    """List Anthropic's model catalog (GET /v1/models). Anthropic's native auth is x-api-key +
    anthropic-version — not Bearer, which is why the generic availability probe can't cover this
    provider. Paginates via has_more/last_id. Returns {models, filtered_out, error}; never raises
    — HTTP failure → error set, models empty."""
    models, error = [], None
    headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            after = None
            while True:
                params: dict[str, Any] = {"limit": 100}
                if after:
                    params["after_id"] = after
                resp = await client.get(ANTHROPIC_MODELS_URL, params=params)
                resp.raise_for_status()
                body = resp.json()
                for raw in body.get("data") or []:
                    row = map_anthropic_model(raw)
                    if row is not None:
                        models.append(row)
                after = body.get("last_id")
                if not body.get("has_more") or not after:
                    break
    except httpx.HTTPError as e:
        return {"models": [], "filtered_out": 0, "error": f"anthropic API error: {e}"}
    return {"models": models, "filtered_out": 0, "error": error}


def reconcile_anthropic_library(entries: list[CatalogEntry], models: list[dict]) -> list[dict]:
    """Mark each model cataloged/catalog_id — an anthropic catalog entry whose
    effective_upstream_model == slug. Sorted by slug. Mutates in place."""
    by_slug = {e.effective_upstream_model: e.id
               for e in entries if _provider_of(e) == "anthropic"}
    for m in models:
        m["catalog_id"] = by_slug.get(m["slug"])
        m["cataloged"] = m["catalog_id"] is not None
    return sorted(models, key=lambda m: m["slug"])


# --- availability probe (static provider entries) --------------------------------------------
#
# Every OpenAI-compatible provider serves GET {base_url}/models (ids only, no pricing). The
# hand-maintained catalog rows rot when an upstream retires an id. This probe fetches each keyed
# provider's live id list and diffs it against what we declare, so drift (a broken row / an
# undeclared model) surfaces on the console. Fetch half (async httpx) and reconcile half (pure)
# are kept apart, like the Fireworks sync above.


async def fetch_provider_models(client: httpx.AsyncClient, base_url: str, api_key: str) -> list[str]:
    """GET {base_url}/models (OpenAI list shape `{"data": [{"id": ...}]}`) → live model ids.
    Tolerates a missing `data` and non-dict rows; a single un-paginated page is assumed (these
    /models listings return the full set). Raises httpx errors — the probe catches them per-provider."""
    resp = await client.get(f"{base_url.rstrip('/')}/models",
                            headers={"Authorization": f"Bearer {api_key}"})
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


def reconcile_availability(entries: list[CatalogEntry],
                           live_ids_by_base_url: dict[str, list[str]]) -> dict[str, dict]:
    """Per provider base_url present in `live_ids_by_base_url`, diff declared upstream ids against
    live ids → {vanished, undeclared}:
      - vanished: declared upstream_model ids absent upstream (the row is broken).
      - undeclared: live ids we don't declare (adoption candidates).
    Only openai-endpoint entries with a non-None base_url are declared. A base_url absent from the
    map — its api_key_env wasn't set, so the probe skipped it — yields nothing (no key, no drift).
    Pure: no I/O, so every branch is unit-testable."""
    declared_by_base: dict[str, set[str]] = {}
    for e in entries:
        if e.endpoint != "openai" or not e.base_url:
            continue
        declared_by_base.setdefault(e.base_url, set()).add(e.effective_upstream_model)

    out: dict[str, dict] = {}
    for base_url, live_ids in live_ids_by_base_url.items():
        declared = declared_by_base.get(base_url, set())
        live = set(live_ids)
        out[base_url] = {"vanished": sorted(declared - live),
                         "undeclared": sorted(live - declared)}
    return out


async def probe_availability(entries: list[CatalogEntry]) -> dict[str, Any]:
    """Walk the unique (base_url, api_key_env) pairs among openai-endpoint entries whose api_key_env
    is set in the environment, GET each provider's /models concurrently (per-provider try/except so
    one dead provider never blanks the others — its httpx error is recorded as `error`), and
    reconcile declared-vs-live. Returns {checked_at, providers: {base_url: {checked_at, vanished,
    undeclared, error}}}. A LIVE READ, not stored history — the caller pins the latest on app.state."""
    providers: dict[str, str] = {}  # base_url -> api key (first key env seen wins; same host, same key)
    for e in entries:
        if e.endpoint != "openai" or not e.base_url:
            continue
        # Stored key first (request-scoped byok overlay — a key pasted in Settings counts as
        # configured), env fallback: the same precedence dispatch uses.
        key = byok_keys.get().get(e.api_key_env) or os.environ.get(e.api_key_env)
        if key:
            providers.setdefault(e.base_url, key)

    now = time.time()

    async def _one(base_url: str, key: str) -> tuple[str, list[str] | None, str | None]:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Expand ${ENV} in the URL (Cloudflare embeds the account id) before the live GET —
                # stored credentials first, env fallback (expand_env_refs); the returned key stays
                # the raw base_url so declared/live reconcile by the same key.
                target = expand_env_refs(base_url)
                return base_url, await fetch_provider_models(client, target, key), None
        except httpx.HTTPError as e:
            return base_url, None, f"{type(e).__name__}: {e}"

    results = await asyncio.gather(*(_one(b, k) for b, k in providers.items()))
    live_by_base = {b: ids for b, ids, err in results if err is None}
    errors = {b: err for b, ids, err in results if err is not None}

    diff = reconcile_availability(entries, live_by_base)
    out = {}
    for base_url in providers:
        d = diff.get(base_url, {"vanished": [], "undeclared": []})
        out[base_url] = {"checked_at": now, "vanished": d["vanished"],
                         "undeclared": d["undeclared"], "error": errors.get(base_url)}
    return {"checked_at": now, "providers": out}
