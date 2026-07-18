"""Per-source benchmark fetchers (chunk B2, wave 1).

Each source splits into a pure `<source>_rows(...)` normalizer (fed recorded fixtures in tests)
and a thin `fetch_<source>(client)` that does the I/O and calls it. Normalizers emit NEAR-schema
rows: the store's score-fact shape but keyed by `source_model_id` (+ `organization`/`hugging_face_id`
hints) instead of `canonical_id` — ingest.py resolves the canonical id and fills provenance.

Values are SOURCE-NATIVE (pct as 88.7, elo as 1423, fraction as 0.95); `value_raw` keeps the
original string. Normalization to a comparable scale is a later chunk's job. Licenses/redistributable
are per the source survey: Epoch CC-BY, LMArena attribution, BFCL Apache-2.0 all redistributable;
Artificial-Analysis data (direct AND the copy OpenRouter embeds) is redistributable=0 — internal
routing only, never customer-facing display.

ponytail: zip/csv sources use a plain client.get (raise_for_status) rather than the JSON-only
observability http helper — refresh()'s per-source try/except is the retry/isolation boundary.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile

from ..observability.http import get_json

# Same-model variants get their own benchmark_version so they're DISTINCT facts (PK carries
# version), not a last-write-wins collapse — B3's aggregation picks among them. Epoch encodes a
# reasoning-effort tier as a trailing '_max'/'_high'; BFCL a '(FC)'/'(Prompt)' harness suffix.
_EFFORT_RE = re.compile(r"_(max|high|medium|low|xhigh|xlow|min|minimal|none|unknown|thinking)$", re.I)
_PAREN_RE = re.compile(r"\(([^)]*)\)\s*$")


def _epoch_version(model: str) -> str:
    m = _EFFORT_RE.search(model)
    return f"effort={m.group(1).lower()}" if m else ""


_CTX_RE = re.compile(r"[-_ ](\d+[km])$", re.I)


def _paren_version(model: str) -> str:
    """Trailing '(...)' variant tag → benchmark_version (BFCL 'FC'/'Prompt', LMArena 'High'/'xHigh')."""
    m = _PAREN_RE.search(model)
    return m.group(1).strip() if m else ""


def _variant_version(model: str) -> str:
    """benchmark_version from a name's variant tag: parenthetical harness, else a trailing context-
    window suffix ('claude-3-7-sonnet_16K' → 'ctx=16k') — a serving config, not a distinct model."""
    paren = _paren_version(model)
    if paren:
        return paren
    m = _CTX_RE.search(model)
    return f"ctx={m.group(1).lower()}" if m else ""

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
EPOCH_URL = "https://epoch.ai/data/benchmark_data.zip"
LMARENA_URL = "https://datasets-server.huggingface.co/rows"
LMARENA_FILTER_URL = "https://datasets-server.huggingface.co/filter"
LMARENA_DATASET = "lmarena-ai/leaderboard-dataset"
BFCL_URL = "https://gorilla.cs.berkeley.edu/data_overall.csv"
AA_URL = "https://artificialanalysis.ai/api/v2/language/models/free"

_AA_NO_REDIST = "AA-no-redistribute"


# --- OpenRouter (public) — pricing + AA-embedded indices; also the canonical id authority ------

def openrouter_rows(models: list[dict]) -> list[dict]:
    """Pure. OpenRouter /models → cost facts (public pricing, redistributable) + the AA indices
    OR embeds (redistributable=0). One blended $/Mtok cost fact per priced model (3:1 in:out, the
    AA convention), plus aa_*_index facts where present."""
    out: list[dict] = []
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        org = mid.split("/", 1)[0] if "/" in mid else ""
        hf = m.get("hugging_face_id") or ""
        base = {"source_model_id": mid, "organization": org, "hugging_face_id": hf,
                "source_url": OPENROUTER_URL}
        p = m.get("pricing") or {}
        pin, pout = _f(p.get("prompt")), _f(p.get("completion"))
        if pin or pout:
            blended = round((3 * pin + pout) / 4 * 1e6, 4)  # $/Mtok, 3:1 input:output blend
            out.append({**base, "benchmark_id": "usd_per_mtok_blended", "value": blended,
                        "value_raw": f"${pin * 1e6:.2f}/{pout * 1e6:.2f} per Mtok in/out",
                        "unit": "usd_per_mtok", "license": "", "redistributable": 1})
        aa = (m.get("benchmarks") or {}).get("artificial_analysis") or {}
        for key, bid in (("intelligence_index", "aa_intelligence_index"),
                         ("coding_index", "aa_coding_index"),
                         ("agentic_index", "aa_agentic_index")):
            v = aa.get(key)
            if v is None:
                continue
            out.append({**base, "benchmark_id": bid, "value": float(v), "value_raw": str(v),
                        "unit": "index", "license": _AA_NO_REDIST, "redistributable": 0})
    return out


async def fetch_openrouter_models(client) -> list[dict]:
    """The raw OR model list — the canonical-id universe ingest maps every other source against."""
    return (await get_json(client, OPENROUTER_URL, provider="openrouter")).get("data") or []


# --- Epoch AI (CC-BY, redistributable) — per-benchmark run CSVs in one zip --------------------

# zip member → (benchmark_id, value column, native unit[, fixed_version]). mean_score/Accuracy/
# Score/EM are 0..1 fractions; aider's "Percent correct" and LiveBench's average are 0..100 pct;
# lech_mazur (0..10 quality) and ECI (0..160) have no fixed 0..100 scale, so they ride 'arena' —
# aggregate percentile-ranks arena/elo within (benchmark_id, source), the same rationale as Elo;
# an 'index' /100 would compress ECI to a 1.0 tie and shrink lech_mazur to ≤0.10, skewing category
# means wherever coverage is uneven. An optional 4th element pins a benchmark_version — used to
# split one benchmark's variant CSVs (frontiermath tier_4 vs the base file) into distinct facts.
# Columns whose native scale is named misleadingly (gdpval "Win Rate (%)", the_agent_company
# "% Score") verified 0..1 → fraction.
EPOCH_MEMBERS = {
    "gpqa_diamond.csv": ("gpqa_diamond", "mean_score", "fraction"),
    "frontiermath.csv": ("frontiermath", "mean_score", "fraction"),
    "frontiermath_tier_4.csv": ("frontiermath", "mean_score", "fraction", "tier_4"),
    "math_level_5.csv": ("math_hard", "mean_score", "fraction"),
    "swe_bench_verified.csv": ("swe_bench_verified", "mean_score", "fraction"),
    "aider_polyglot_external.csv": ("aider_polyglot", "Percent correct", "pct"),
    "hle_external.csv": ("hle", "Accuracy", "fraction"),
    "arc_agi_external.csv": ("arc_agi", "Score", "fraction"),
    "bbh_external.csv": ("bbh", "Average", "fraction"),
    "terminalbench_external.csv": ("terminal_bench", "Accuracy mean", "fraction"),
    "scicode_external.csv": ("scicode", "Score", "fraction"),
    # knowledge / factuality
    "mmlu_external.csv": ("mmlu", "EM", "fraction"),
    "simpleqa_verified.csv": ("simpleqa_verified", "mean_score", "fraction"),
    "trivia_qa_external.csv": ("trivia_qa", "EM", "fraction"),
    "open_book_qa_external.csv": ("openbookqa", "Accuracy", "fraction"),
    "bool_q_external.csv": ("boolq", "Score", "fraction"),
    # long context
    "fictionlivebench_external.csv": ("fictionlivebench", "120k token score", "fraction"),
    # writing
    "lech_mazur_writing_external.csv": ("lech_mazur_writing", "Mean score", "arena"),
    # agentic
    "gdpval_external.csv": ("gdpval", "Win Rate (%)", "fraction"),
    "osworld_2_external.csv": ("osworld_2", "Binary accuracy", "fraction"),
    "the_agent_company_external.csv": ("the_agent_company", "% Score", "fraction"),
    # math
    "otis_mock_aime_2024_2025.csv": ("aime", "mean_score", "fraction"),
    # reasoning
    "live_bench_external.csv": ("livebench", "Global average", "pct"),
    "simplebench_external.csv": ("simplebench", "Score (AVG@5)", "fraction"),
    "epoch_capabilities_index.csv": ("epoch_capabilities_index", "ECI Score", "arena"),
}


def _epoch_member_version(fixed: str, model: str) -> str:
    """Combine a member's fixed version tag (e.g. 'tier_4') with the model's effort tier so both
    dimensions stay distinct facts — a fixed tag alone would collapse a model's effort tiers."""
    ev = _epoch_version(model)
    return f"{fixed};{ev}" if fixed and ev else (fixed or ev)


def epoch_rows(zip_bytes: bytes) -> list[dict]:
    """Pure. Epoch benchmark_data.zip → fact rows for the mapped members. Skips rows with no model
    version or an unparseable value."""
    out: list[dict] = []
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = set(z.namelist())
    for member, spec in EPOCH_MEMBERS.items():
        bid, col, unit = spec[0], spec[1], spec[2]
        fixed = spec[3] if len(spec) > 3 else ""
        if member not in names:
            continue
        with z.open(member) as fh:
            reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))
            for r in reader:
                model = (r.get("Model version") or "").strip()
                v = _f(r.get(col), none=True)
                if not model or v is None:
                    continue
                out.append({"source_model_id": model, "organization": (r.get("Organization") or "").strip(),
                            "benchmark_id": bid, "benchmark_version": _epoch_member_version(fixed, model),
                            "value": v, "value_raw": (r.get(col) or "").strip(),
                            "unit": unit, "source_url": EPOCH_URL, "license": "CC-BY", "redistributable": 1})
    return out


async def fetch_epoch(client) -> list[dict]:
    resp = await client.get(EPOCH_URL)
    resp.raise_for_status()
    return epoch_rows(resp.content)


# --- LMArena (attribution, redistributable) — HF datasets-server rows -------------------------

# arena config → benchmark_id. `text`/`text_style_control`/`webdev` are Elo; `agent` reports a
# 0..1 score (unit picked per-row from which field is present).
LMARENA_CONFIGS = {
    "text": "lmarena_elo",
    "text_style_control": "lmarena_style_control",
    "webdev": "lmarena_coding",
    "agent": "lmarena_agent",
}
# `text`-config per-category arenas (Elo, same schema as overall). Each is a distinct routing fact:
# lmarena_coding_text is the text-arena coding vote (distinct measurement from webdev's lmarena_coding
# — both kept, named honestly). creative_writing/hard_prompts_english reuse ids the registry already
# defines. Verified live: these categories exist; 'if'/'summarization'/'extraction' do NOT.
LMARENA_TEXT_CATEGORIES = {
    "instruction_following": "lmarena_if",
    "math": "lmarena_math",
    "coding": "lmarena_coding_text",
    "creative_writing": "lmarena_creative_writing",
    "hard_prompts_english": "lmarena_hard_prompts",
    "multi_turn": "lmarena_multiturn",
    "expert": "lmarena_expert",
}
# Per-language text arenas collapse to one benchmark_id, the language carried as benchmark_version
# (the established same-fact-many-variants pattern).
LMARENA_LANGUAGES = (
    "chinese", "korean", "russian", "spanish", "japanese",
    "german", "french", "portuguese", "vietnamese",
)
# ponytail: page a bounded top-N per config (rows come rank-ordered). Top ~500 is ample for a
# routing/comparison surface; raise if the long tail ever matters.
LMARENA_MAX_ROWS = 500


def lmarena_rows(config: str, rows: list[dict]) -> list[dict]:
    """Pure. Unwrapped datasets-server rows for one arena config → fact rows (category='overall'
    only). Schema is per-config (verified live): text/text_style_control/webdev carry `rating`
    (Elo); `agent` carries `score` — a COHORT-RELATIVE strength (rank-1 ≈ 0.14, not a 0..1 quality
    fraction), stored unit='arena' so aggregate() percentile-normalizes it like Elo. A config that
    surfaces neither field is dropped, not guessed. Variant tags ('(High)'/'(xHigh)') → version."""
    bid = LMARENA_CONFIGS[config]
    out: list[dict] = []
    for r in rows:
        if (r.get("category") or "overall") != "overall":
            continue
        model = r.get("model_name")
        if not model:
            continue
        base = {"source_model_id": model, "organization": r.get("organization") or "",
                "benchmark_id": bid, "benchmark_version": _paren_version(model),
                "source_url": f"{LMARENA_URL}?dataset={LMARENA_DATASET}&config={config}",
                "license": "attribution", "redistributable": 1}
        if r.get("rating") is not None:
            elo = float(r["rating"])
            out.append({**base, "value": round(elo, 1), "value_raw": f"{elo:.1f} Elo", "unit": "elo"})
        elif r.get("score") is not None:
            sc = float(r["score"])
            out.append({**base, "value": sc, "value_raw": str(r["score"]), "unit": "arena"})
    return out


def lmarena_category_rows(bid: str, rows: list[dict], *, version: str = "") -> list[dict]:
    """Pure. /filter rows for one `text`-config category → Elo fact rows for `bid`. Same Elo/license
    shape as lmarena_rows; `version` pins a benchmark_version (the language for the multilingual
    arena), else the model's own variant tag ('(High)'/'(xHigh)')."""
    out: list[dict] = []
    for r in rows:
        model = r.get("model_name")
        if not model or r.get("rating") is None:
            continue
        elo = float(r["rating"])
        out.append({"source_model_id": model, "organization": r.get("organization") or "",
                    "benchmark_id": bid, "benchmark_version": version or _paren_version(model),
                    "value": round(elo, 1), "value_raw": f"{elo:.1f} Elo", "unit": "elo",
                    "source_url": f"{LMARENA_FILTER_URL}?dataset={LMARENA_DATASET}&config=text",
                    "license": "attribution", "redistributable": 1})
    return out


async def _lmarena_page(client, url: str, params: dict) -> list[dict]:
    """Page bounded rank-ordered rows off datasets-server (/rows or /filter), unwrapping row envelopes."""
    rows: list[dict] = []
    offset = 0
    while offset < LMARENA_MAX_ROWS:
        data = await get_json(client, url, provider="lmarena",
                              params={**params, "offset": offset, "length": 100})
        page = data.get("rows") or []
        rows += [pr["row"] for pr in page]
        if len(page) < 100:
            break
        offset += 100
    return rows


async def fetch_lmarena(client) -> list[dict]:
    out: list[dict] = []
    for config in LMARENA_CONFIGS:
        rows = await _lmarena_page(client, LMARENA_URL,
                                   {"dataset": LMARENA_DATASET, "config": config, "split": "latest"})
        out += lmarena_rows(config, rows)
    # per-category text arenas sit DEEP in the rank-ordered feed; pull each directly via /filter
    base = {"dataset": LMARENA_DATASET, "config": "text", "split": "latest"}
    for cat, bid in LMARENA_TEXT_CATEGORIES.items():
        rows = await _lmarena_page(client, LMARENA_FILTER_URL, {**base, "where": f'"category"=\'{cat}\''})
        out += lmarena_category_rows(bid, rows)
    for lang in LMARENA_LANGUAGES:
        rows = await _lmarena_page(client, LMARENA_FILTER_URL, {**base, "where": f'"category"=\'{lang}\''})
        out += lmarena_category_rows("lmarena_multilingual", rows, version=lang)
    return out


# --- BFCL / Berkeley Function-Calling Leaderboard (Apache-2.0, redistributable) ---------------

def bfcl_rows(csv_text: str) -> list[dict]:
    """Pure. data_overall.csv → bfcl facts from 'Overall Acc' ('77.47%' → 77.47, raw kept)."""
    out: list[dict] = []
    for r in csv.DictReader(io.StringIO(csv_text)):
        acc = (r.get("Overall Acc") or "").strip()
        model = (r.get("Model") or "").strip()
        v = _f(acc.rstrip("%"), none=True)
        if not model or v is None:
            continue
        out.append({"source_model_id": model, "organization": (r.get("Organization") or "").strip(),
                    "benchmark_id": "bfcl", "benchmark_version": _variant_version(model),
                    "value": v, "value_raw": acc, "unit": "pct",
                    "source_url": BFCL_URL, "license": "Apache-2.0", "redistributable": 1})
    return out


async def fetch_bfcl(client) -> list[dict]:
    resp = await client.get(BFCL_URL)
    resp.raise_for_status()
    return bfcl_rows(resp.text)


# --- Artificial Analysis (INTERNAL-ONLY, redistributable=0) — optional, key-gated -------------

def aa_rows(data: dict) -> list[dict]:
    """Pure. AA /models/free JSON → aa_intelligence_index facts (all redistributable=0). Other AA
    fields are added when the shape's pinned down; the intelligence index is the stable one."""
    out: list[dict] = []
    for m in data.get("data") or []:
        slug = m.get("slug") or m.get("id") or m.get("name")
        if not slug:
            continue
        ev = m.get("evaluations") or {}
        v = ev.get("artificial_analysis_intelligence_index") or m.get("intelligence_index")
        if v is None:
            continue
        out.append({"source_model_id": slug, "organization": (m.get("model_creator") or {}).get("slug", "")
                    if isinstance(m.get("model_creator"), dict) else "",
                    "benchmark_id": "aa_intelligence_index", "value": float(v), "value_raw": str(v),
                    "unit": "index", "source_url": AA_URL, "license": _AA_NO_REDIST, "redistributable": 0})
    return out


async def fetch_aa(api_key: str) -> list[dict]:
    """Key on a dedicated client's header (never logged — get_json's error path never echoes the
    request). Caller skips this entirely when no key is set."""
    import httpx

    async with httpx.AsyncClient(timeout=30, headers={"x-api-key": api_key}) as c:
        data = await get_json(c, AA_URL, provider="artificial_analysis")
    return aa_rows(data)


# Ids emitted outside the static source maps (OpenRouter cost/AA indices, BFCL, AA). The truth set
# the registry invariant test asserts against — every fed id must be a real connector output.
_STATIC_FED_IDS = frozenset({
    "usd_per_mtok_blended", "aa_intelligence_index", "aa_coding_index", "aa_agentic_index",
    "bfcl", "lmarena_multilingual",
})


def fed_benchmark_ids() -> frozenset[str]:
    """Every benchmark_id a connector actually emits, derived from the source maps + fixed ids."""
    return frozenset(LMARENA_CONFIGS.values()) | frozenset(LMARENA_TEXT_CATEGORIES.values()) \
        | {spec[0] for spec in EPOCH_MEMBERS.values()} | _STATIC_FED_IDS


def _f(v, none: bool = False):
    """Best-effort float. `none=True` returns None on failure (skip the row); else 0.0."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None if none else 0.0
