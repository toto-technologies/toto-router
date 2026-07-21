// Benchmarks view-model — pure functions behind routes/benchmarks (unit-tested via `node --test`).
// Two jobs: (1) the model-differentiation system — a deterministic per-provider visual identity
// (hue + monogram) used identically across leaderboard, drill-down, and compare, so models are
// never interchangeable gray rows; (2) ranking/freshness/format transforms over the ratified
// /v1/admin/benchmarks/* shapes.

// ---- Provider identity ------------------------------------------------------------------------
// Named hues echo each provider's own brand where one exists and are spread around the wheel for
// perceptual distance. Unknown providers hash to a stable hue (FNV-1a → 0..359) so a new provider
// is still distinct and CONSISTENT across renders — never gray, never reshuffled.
const PROVIDER_HUE = {
  anthropic: 21,      // clay
  openai: 168,        // teal
  google: 214,        // blue
  meta: 248, 'meta-llama': 248,
  mistral: 40, mistralai: 40,
  qwen: 282, alibaba: 282,
  deepseek: 200,
  xai: 335, 'x-ai': 335,
  cohere: 310,
  amazon: 48,
  microsoft: 205,
  nvidia: 100,
  moonshot: 322, moonshotai: 322,
  zhipu: 188, 'z-ai': 188,
  ai21: 260,
  fireworks: 12,
  cloudflare: 32,     // orange — Cloudflare's brand
  local: 130,         // sage — the house accent family, reserved for in-perimeter
};

export function providerHue(provider) {
  const p = (provider || '').toLowerCase();
  if (p in PROVIDER_HUE) return PROVIDER_HUE[p];
  let h = 0x811c9dc5; // FNV-1a
  for (let i = 0; i < p.length; i++) {
    h ^= p.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0) % 360;
}

/** Two-letter monogram for the identity mark. */
export function providerMark(provider) {
  const p = (provider || '?').replace(/[^a-zA-Z0-9]/g, '');
  return (p.slice(0, 2) || '?').replace(/^./, (c) => c.toUpperCase());
}

// ---- Categories -------------------------------------------------------------------------------
// Plain-language labels for the wire category slugs (house rule: nontechnical-intuitive first).
const CAT_LABEL = {
  coding: 'Coding',
  reasoning: 'Reasoning',
  math: 'Math',
  agentic: 'Agentic',
  writing: 'Writing',
  long_context: 'Long context',
  multilingual: 'Multilingual',
  preference: 'Preference',
};
export function catLabel(slug) {
  if (CAT_LABEL[slug]) return CAT_LABEL[slug];
  const s = String(slug || '').replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// ---- Sources ----------------------------------------------------------------------------------
// Display label + redistribution license for the attribution line (good citizenship, not clutter).
const SOURCE_META = {
  epoch: { label: 'Epoch AI', license: 'CC-BY' },
  lmarena: { label: 'LMArena', license: 'attribution' },
  bfcl: { label: 'BFCL', license: 'apache-2.0' },
  openrouter: { label: 'OpenRouter', license: 'attribution' },
};
export function sourceLabel(source) {
  return SOURCE_META[source]?.label ?? String(source || '');
}
export function sourceLicense(source) {
  return SOURCE_META[source]?.license ?? '';
}

// ---- Freshness --------------------------------------------------------------------------------
export const STALE_AFTER_S = 7 * 86400;

/** True when a source's latest fact is older than 7 days (or missing entirely). */
export function isStale(epochS, nowMs = Date.now()) {
  if (!epochS) return true;
  return nowMs / 1000 - epochS > STALE_AFTER_S;
}

// ---- Console scope + provider inventory ------------------------------------------------------
export const BENCHMARK_SCOPE_OPTIONS = [
  { value: 'all_evidence', label: 'All evidence' },
  { value: 'provider_available', label: 'Provider available' },
  { value: 'smart_route_eligible', label: 'Smart-route eligible' },
];
export const DEFAULT_BENCHMARK_SCOPE = 'provider_available';

/** Append one server page without duplicating identities; later rows refresh earlier metadata. */
export function mergeBenchmarkPage(current, incoming, reset = false) {
  if (reset || !current) return incoming;
  const rows = new Map((current.models ?? []).map((model) => [model.id, model]));
  for (const model of incoming?.models ?? []) rows.set(model.id, model);
  return { ...current, ...incoming, models: [...rows.values()] };
}

/** Platform identity ids are opaque; only the backend's display name is human-facing. */
export function benchmarkModelLabel(model) {
  return String(model?.display_name || model?.id || '');
}

/** Evidence missing is unknown, never zero. */
export function hasBenchmarkEvidence(model) {
  return (model?.fact_count ?? 0) > 0 || Object.keys(model?.scores ?? {}).length > 0;
}

/** Apply only the three approved console scopes; catalog membership is deliberately irrelevant. */
export function modelsForScope(models, scope = DEFAULT_BENCHMARK_SCOPE) {
  const rows = models ?? [];
  if (scope === 'all_evidence') return rows.filter(hasBenchmarkEvidence);
  if (scope === 'smart_route_eligible') return rows.filter((model) => model?.smart_route_eligible === true);
  return rows.filter((model) => model?.provider_available === true);
}

function offerIsAvailable(offer) {
  if (offer?.available === false) return false;
  if (offer?.available === true) return true;
  const state = String(offer?.availability ?? offer?.status ?? '').toLowerCase();
  if (state) return state === 'available';
  const snapshot = String(offer?.snapshot_status ?? offer?.inventory_status?.snapshot_status ?? '').toLowerCase();
  const freshness = String(offer?.freshness ?? '').toLowerCase();
  const current = offer?.stale === false || freshness === 'current' || freshness === 'fresh';
  return snapshot === 'success' && current && offer?.partial !== true;
}

/** Collapse the two network marketplaces into a single "both" chip when they share an identity. */
export function providerOfferChips(model) {
  const kinds = new Set();
  for (const offer of model?.offers ?? []) {
    if (!offerIsAvailable(offer)) continue;
    const provider = String(offer?.provider ?? '').toLowerCase();
    const routeKind = String(offer?.route_kind ?? offer?.via ?? '').toLowerCase();
    if (provider === 'openrouter' || provider === 'fireworks' || provider === 'local') {
      kinds.add(provider);
    } else if (provider === 'direct' || routeKind === 'direct' || provider) {
      kinds.add('direct');
    }
  }
  const chips = [];
  if (kinds.has('openrouter') && kinds.has('fireworks')) {
    chips.push({ kind: 'both', label: 'OpenRouter + Fireworks' });
  } else if (kinds.has('openrouter')) {
    chips.push({ kind: 'openrouter', label: 'OpenRouter' });
  } else if (kinds.has('fireworks')) {
    chips.push({ kind: 'fireworks', label: 'Fireworks' });
  }
  if (kinds.has('local')) chips.push({ kind: 'local', label: 'Local' });
  if (kinds.has('direct')) chips.push({ kind: 'direct', label: 'Direct' });
  return chips;
}

function scopeKind(value) {
  if (typeof value === 'string') return value.split(':', 1)[0].toLowerCase();
  return String(value?.kind ?? value?.scope ?? '').toLowerCase();
}

export function credentialScopeLabel(model) {
  const kinds = new Set();
  for (const offer of model?.offers ?? []) {
    const kind = scopeKind(offer?.credential_scope ?? offer?.scope);
    if (kind) kinds.add(kind);
  }
  if (!kinds.size) {
    const kind = scopeKind(model?.credential_scope ?? model?.scope);
    if (kind) kinds.add(kind);
  }
  if (kinds.size > 1) return 'Mixed credential scopes';
  const kind = [...kinds][0];
  return {
    user: 'User scope',
    organization: 'Organization scope',
    org: 'Organization scope',
    platform: 'Platform scope',
  }[kind] ?? (kind ? `${catLabel(kind)} scope` : '');
}

/** Normalize row-level or per-provider inventory state into the two actionable warnings. */
export function inventoryWarnings(model, nowMs = Date.now()) {
  const inventory = model?.inventory;
  const states = Array.isArray(inventory)
    ? [...inventory]
    : inventory && typeof inventory === 'object'
      ? Object.values(inventory)
      : [];
  if (model?.inventory_status != null) states.push(model.inventory_status);
  if (model?.freshness != null) states.push(model.freshness);
  const statuses = states.flatMap((state) =>
    typeof state === 'string'
      ? [state.toLowerCase()]
      : [state?.status, state?.freshness, state?.snapshot_status, state?.last_attempt_status]
          .filter(Boolean)
          .map((value) => String(value).toLowerCase())
  );
  const stale = states.length
    ? states.some((state) => {
        if (typeof state === 'string') return state.toLowerCase() === 'stale';
        const expiresAt = Number(state?.expires_at);
        if (Number.isFinite(expiresAt) && expiresAt > 0) return nowMs / 1000 >= expiresAt;
        return state?.stale === true ||
          [state?.status, state?.freshness, state?.snapshot_status]
            .some((value) => String(value ?? '').toLowerCase() === 'stale');
      })
    : model?.inventory_stale === true || model?.stale === true || statuses.includes('stale');
  const failed = statuses.includes('failed');
  const partial = model?.inventory_partial === true || model?.partial === true ||
    states.some((state) => state?.partial === true) || statuses.includes('partial');
  return [
    stale && 'Stale inventory',
    failed ? 'Inventory refresh failed' : partial && 'Partial inventory',
  ].filter(Boolean);
}

export function eligibilityReasonLabels(model) {
  const labels = (model?.eligibility_reasons ?? [])
    .map((reason) => typeof reason === 'string' ? reason : reason?.message ?? reason?.reason ?? reason?.code)
    .filter(Boolean);
  return [...new Set(labels)];
}

// ---- Ranking ----------------------------------------------------------------------------------
/**
 * Rank models for one category: covered models only (uncovered are an explicit separate state),
 * score desc, then evidence count desc (n=4 beats n=1 on a tie), then id for stability.
 * Returns [{model, score, n, rank}] with rank 1-based across ALL covered models.
 */
export function rankModels(models, category) {
  const covered = [];
  for (const m of models ?? []) {
    const s = m?.scores?.[category];
    if (s && typeof s.score === 'number') covered.push({ model: m, score: s.score, n: s.n ?? 1 });
  }
  covered.sort((a, b) => b.score - a.score || b.n - a.n || (a.model.id < b.model.id ? -1 : 1));
  covered.forEach((r, i) => (r.rank = i + 1));
  return covered;
}

// ---- Value formatting -------------------------------------------------------------------------
/** Native display value for a fact. `value_raw` is the truth when the backend sends it; the
 *  fallback derives from unit so a missing value_raw never renders as a bare normalized float. */
export function fmtFactValue(fact) {
  if (fact?.value_raw) return fact.value_raw;
  const v = fact?.value;
  if (v == null) return '—';
  switch (fact?.unit) {
    case 'pct':
      return `${(v <= 1 ? v * 100 : v).toFixed(1)}%`;
    case 'fraction':
      return `${(v * 100).toFixed(1)}%`;
    case 'elo':
      return `${Math.round(v <= 1 ? v * 2000 : v)} Elo`; // defensive: elo should arrive raw
    case 'arena':
      return `${v.toFixed(3)} arena strength`; // cohort-relative; rank 1 ≈ 0.14, NOT a percentage
    default:
      return String(Math.round(v * 1000) / 1000);
  }
}

/** Drill-down bar width (0..100) for a fact, or null when the value has no meaningful 0..1
 *  position: arena strength is cohort-relative (0.141 ≠ 14% quality) and raw units (elo) overflow. */
export function factBarWidth(fact) {
  const v = fact?.value;
  if (v == null || fact?.unit === 'arena') return null;
  if (v < 0 || v > 1) return null;
  return Math.round(v * 100);
}

/** Aggregate 0..1 → the 0–100 index shown next to bars ('62'). null-safe. */
export function score100(s) {
  return s == null ? null : Math.round(s * 100);
}

/** Group a model's facts by category, categories in the store's canonical order (then any extras),
 *  facts inside a category strongest-first. */
export function groupFacts(facts, categoryOrder = []) {
  const by = new Map();
  for (const f of facts ?? []) {
    const c = f.category ?? 'other';
    if (!by.has(c)) by.set(c, []);
    by.get(c).push(f);
  }
  for (const list of by.values()) list.sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
  const ordered = [];
  for (const c of categoryOrder) if (by.has(c)) ordered.push([c, by.get(c)]), by.delete(c);
  for (const [c, list] of by) ordered.push([c, list]);
  return ordered;
}
