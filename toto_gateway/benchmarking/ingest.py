"""Shared ingestion pipeline: alias-map every source's rows onto canonical ids, fill provenance,
upsert into the BenchmarkStore.

The canonical id is the OpenRouter model slug (vendor/model). OpenRouter is fetched first and IS
the alias authority — its full model list builds the mapping universe, and every OR model gets an
identity alias. Other sources map their foreign names down a confidence ladder:
  exact slug match            → 1.0
  normalized match (norm())   → 0.9   ('Claude Sonnet 5' ~ 'anthropic/claude-sonnet-5')
  cleaned+normalized match    → 0.8   (after stripping ' (FC)' / date / reasoning-effort suffix)
  unmappable                  → 0.4   provisional vendor/model slug, low-confidence review row
Unmappable models are STILL ingested (comprehensive-list goal); the 0.4 alias surfaces them in the
needs-review queue (store.aliases(max_confidence=0.8)).

refresh() isolates each source in its own try/except — one source down never fails the others; the
report carries per-source facts/aliases/status. No delete_source: the PK upsert is latest-only, and
retrieved_at dates each fact, so stale rows age visibly rather than vanishing mid-refresh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .store import BenchmarkStore

_ALL_SOURCES = ("openrouter", "epoch", "lmarena", "bfcl", "artificial_analysis")


def norm(name: str) -> str:
    """Match key: org prefix dropped, lowercase alnum only (scripts/fetch_benchmarks.norm)."""
    return re.sub(r"[^a-z0-9]", "", name.split("/")[-1].lower())


# Vendor spellings that name the SAME lab across sources → the OpenRouter prefix. Only affects the
# provisional slug's vendor segment (norm() already drops the prefix for matching), so unmapped
# variants of one lab dedupe instead of scattering (x-ai/… and xai/… → one slug).
_VENDOR_SYNONYMS = {"x-ai": "xai", "google-deepmind": "google", "meta-llama": "meta",
                    "meta-ai": "meta", "alibaba": "qwen", "mistral-ai": "mistral",
                    "moonshotai": "moonshot", "openai-inc": "openai"}


def _clean(name: str) -> str:
    """Strip the decorations that block a normalized match: a trailing '(FC)'/'(High)' parenthetical,
    an embedded release date, a trailing context-window suffix ('_16K'/'-1m' — a serving config, not
    a different model), and a trailing reasoning-effort token (epoch/lmarena variants)."""
    s = re.sub(r"\s*\([^)]*\)\s*$", "", name)
    s = re.sub(r"[-_ ]20\d{2}[-_]?\d{2}[-_]?\d{2}", "", s)  # embedded release date (may precede _effort)
    s = re.sub(r"[-_ ]\d+[km]$", "", s, flags=re.I)         # trailing context-window suffix
    s = re.sub(r"_(max|high|medium|low|xhigh|xlow|min|minimal|none|unknown|thinking)$", "", s, flags=re.I)
    return s.strip(" -_")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _provisional_slug(org: str, model: str) -> str:
    """A stable vendor/model canonical id for a model no source maps onto an OR slug."""
    vendor = _slug(org) or "unknown"
    return f"{_VENDOR_SYNONYMS.get(vendor, vendor)}/{_slug(_clean(model)) or 'model'}"


@dataclass
class Universe:
    ids: set = field(default_factory=set)
    by_norm: dict = field(default_factory=dict)
    by_hf: dict = field(default_factory=dict)   # hugging_face_id → canonical id


def build_universe(or_models: list[dict]) -> Universe:
    """The canonical-id universe from OpenRouter's model list: exact ids + a norm→id index (id and
    canonical_slug both feed it; first spelling wins) + a hugging_face_id→id index for HF-named
    sources."""
    u = Universe()
    for m in or_models:
        mid = m.get("id")
        if not mid:
            continue
        u.ids.add(mid)
        u.by_norm.setdefault(norm(mid), mid)
        cs = m.get("canonical_slug")
        if cs:
            u.by_norm.setdefault(norm(cs), mid)
        hf = m.get("hugging_face_id")
        if hf:
            u.by_hf.setdefault(hf, mid)
    return u


def map_model(source_model_id: str, organization: str, universe: Universe) -> tuple[str, float]:
    """Resolve a foreign model name to (canonical_id, confidence) down the ladder above."""
    if source_model_id in universe.ids:
        return source_model_id, 1.0
    if source_model_id in universe.by_hf:          # a source name that IS an HF id → strong match
        return universe.by_hf[source_model_id], 0.95
    if (n := norm(source_model_id)) in universe.by_norm:
        return universe.by_norm[n], 0.9
    if (cn := norm(_clean(source_model_id))) and cn in universe.by_norm:
        return universe.by_norm[cn], 0.8
    return _provisional_slug(organization, source_model_id), 0.4


def _map_rows(source: str, rows: list[dict], universe: Universe) -> tuple[list[dict], list[dict]]:
    """Near-schema source rows → (store-ready score rows, alias rows). Pure."""
    score_rows: list[dict] = []
    aliases: dict[tuple, dict] = {}
    for r in rows:
        smid = r["source_model_id"]
        org = r.get("organization", "")
        hf = r.get("hugging_face_id", "")
        canonical, confidence = map_model(smid, org, universe)
        sr = {k: v for k, v in r.items()
              if k not in ("source_model_id", "organization", "hugging_face_id")}
        sr["canonical_id"] = canonical
        sr["source"] = source
        score_rows.append(sr)
        aliases.setdefault((source, smid), {
            "source": source, "source_model_id": smid, "canonical_id": canonical,
            "hugging_face_id": hf, "confidence": confidence})
    return score_rows, list(aliases.values())


async def _upsert(store: BenchmarkStore, source: str, rows: list[dict], universe: Universe,
                  aliases: list[dict] | None = None) -> dict:
    score_rows, derived = _map_rows(source, rows, universe)
    # RESET-ON-SUCCESS: the fetch (this `rows` arg) already succeeded, so wipe the source's old
    # facts before re-upserting — clears stale PKs latest-only can't (schema/unit/name changes,
    # delisted entries). A failed fetch never reaches here (isolation). Aliases are crosswalk state,
    # not facts, so delete_source leaves them.
    await store.delete_source(source)
    n_scores = await store.upsert_scores(score_rows)
    n_aliases = await store.upsert_aliases(aliases if aliases is not None else derived)
    return {"facts": n_scores, "aliases": n_aliases, "status": "ok"}


async def refresh(store: BenchmarkStore, sources: list[str] | None = None,
                  aa_api_key: str = "") -> dict:
    """Fetch → alias-map → upsert every requested source, isolated. Returns
    {source: {facts, aliases, status, error?, skipped?}}."""
    import httpx

    # The external benchmark-provider fetchers reach upstream over the observability http helper —
    # imported here, not at module top, so the pure mapping helpers (norm/map_model) stay importable
    # without pulling that plane, and the refresh path is the only place that needs it.
    from . import connectors as C

    sources = list(sources) if sources else list(_ALL_SOURCES)
    report: dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        # OpenRouter is the canonical-id authority — fetch once, up front, before the universe.
        or_models: list[dict] = []
        or_err: str | None = None
        if "openrouter" in sources:
            try:
                or_models = await C.fetch_openrouter_models(client)
            except Exception as e:  # noqa: BLE001 — one source's failure is isolated, not fatal
                or_err = str(e)
        universe = build_universe(or_models)

        for source in sources:
            try:
                if source == "openrouter":
                    if or_err:
                        raise RuntimeError(or_err)
                    # every OR model → identity alias (the full canonical universe), not just the
                    # subset that produced facts
                    uni = [{"source": "openrouter", "source_model_id": m["id"],
                            "canonical_id": m["id"], "hugging_face_id": m.get("hugging_face_id") or "",
                            "confidence": 1.0} for m in or_models if m.get("id")]
                    report[source] = await _upsert(store, source, C.openrouter_rows(or_models),
                                                   universe, aliases=uni)
                elif source == "artificial_analysis":
                    if not aa_api_key:
                        report[source] = {"facts": 0, "aliases": 0, "status": "skipped",
                                          "skipped": "no key"}
                        continue
                    report[source] = await _upsert(store, source, await C.fetch_aa(aa_api_key), universe)
                elif source == "epoch":
                    report[source] = await _upsert(store, source, await C.fetch_epoch(client), universe)
                elif source == "lmarena":
                    report[source] = await _upsert(store, source, await C.fetch_lmarena(client), universe)
                elif source == "bfcl":
                    report[source] = await _upsert(store, source, await C.fetch_bfcl(client), universe)
                else:
                    report[source] = {"facts": 0, "aliases": 0, "status": "error",
                                      "error": f"unknown source {source}"}
            except Exception as e:  # noqa: BLE001 — per-source isolation IS the contract
                report[source] = {"facts": 0, "aliases": 0, "status": "error", "error": str(e)}
    return report


async def _main() -> None:
    import json
    import sys

    from ..config import get_settings

    s = get_settings()
    db_path = sys.argv[1] if len(sys.argv) > 1 else s.db
    store = BenchmarkStore(db_path, s.database_url if len(sys.argv) <= 1 else "")
    try:
        report = await refresh(store, aa_api_key=s.aa_api_key)
        print(json.dumps({"report": report, "coverage": await store.coverage()}))
    finally:
        await store.close_pool()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
