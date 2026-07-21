// Catalog view-model — pure transforms behind the Catalog page's provider modules and the
// Fireworks-sync panel (unit-tested via `node --test`). Shapes come from /v1/admin/catalog/models
// and /v1/admin/catalog/sync/fireworks. Provider identity (hue + monogram) is lifted from the
// benchmarks system so a provider looks the same on every page.
import { providerHue, providerMark } from './benchmarks.js';
import { providerLabel, prettyModel } from './models.js';
import { relTime } from './time.js';
import { lineageChains, sortEvals, shortRef } from './tuning.js';

/** Last path segment: 'accounts/toto-tech/deployments/b6omdtjm' → 'b6omdtjm'. */
export const lastSeg = (s) => String(s ?? '').split('/').filter(Boolean).pop() ?? '';

// ---- Provider modules ---------------------------------------------------------------------------

// Stable module order: the four first-class providers, then unknowns alphabetically, local/fake last.
const FIRST = ['anthropic', 'openai', 'openrouter', 'fireworks', 'cloudflare'];
const LAST = ['local', 'fake'];
function rank(p) {
  const f = FIRST.indexOf(p);
  if (f !== -1) return f;
  const l = LAST.indexOf(p);
  if (l !== -1) return 100 + l;
  return 50;
}

// Generic two-letter monograms collide across the Open* providers (OpenAI/OpenRouter both "Op") —
// override here rather than in benchmarks.js, which is the Benchmarks page's contract.
const MARK_OVERRIDE = { openrouter: 'OR', fake: 'Te' }; // fake renders as "Test models"
export const catMark = (p) => MARK_OVERRIDE[p] ?? providerMark(p);

/** Group catalog models into ordered provider modules with display identity + module-level facts. */
export function groupByProvider(models) {
  const by = new Map();
  for (const m of models ?? []) {
    const p = (m.provider || m.via || 'unknown').toLowerCase();
    if (!by.has(p)) by.set(p, []);
    by.get(p).push(m);
  }
  return [...by.entries()]
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([provider, list]) => ({
      provider,
      label: providerLabel(provider),
      hue: providerHue(provider),
      mark: catMark(provider),
      models: list,
      keyEnv: keyEnv(list),
      fineTuned: list.filter((m) => m.fine_tuned).length,
    }));
}

/** The module's shared BYOK env var — only counted on openai-shaped endpoints; on other endpoints
 *  api_key_env is a schema default and would read as misinformation. Null when models disagree. */
export function keyEnv(models) {
  const envs = [
    ...new Set(
      (models ?? []).filter((m) => m.endpoint === 'openai' && m.api_key_env).map((m) => m.api_key_env)
    ),
  ];
  return envs.length === 1 ? envs[0] : null;
}

/** Row display name — off the upstream's MODEL segment, never the '#deployment' pin (prettyModel
 *  on the raw ref would title-case the deployment id: "B6omdtjm"). Falls back to the catalog id. */
export const displayName = (m) =>
  prettyModel(String(m?.upstream_model ?? '').split('#')[0] || m?.id || '');

// ---- Per-model derived bits ---------------------------------------------------------------------

/** Split an upstream ref into its base + a short deployment tag:
 *  'accounts/t/models/x#accounts/t/deployments/b6omdtjm' → {base:'accounts/t/models/x',
 *  dep:'b6omdtjm', short:'x #b6omdtjm'}. No '#' pin → dep null, short = last segment. */
export function upstreamParts(ref) {
  if (!ref) return { base: '', dep: null, short: '—' };
  const [base, depRef] = String(ref).split('#');
  const dep = depRef ? lastSeg(depRef) : null;
  const name = lastSeg(base) || base;
  return { base, dep, short: dep ? `${name} #${dep}` : name };
}

/** 262144 → '262k' · 1048576 → '1.0M' · 800 → '800' · null → '—'. */
export function ctxShort(n) {
  if (!n) return '—'; // null/0 both mean "unknown" in provider feeds
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

/** Plain-language serving mode for a fine-tuned model: pinned to a deployment (on-demand GPU) or
 *  serverless. Stock catalog models get no label. */
export function servingLabel(m) {
  if (!m?.fine_tuned) return null;
  return upstreamParts(m.upstream_model).dep
    ? 'Fine-tuned · on-demand deployment'
    : 'Fine-tuned · serverless';
}

// ---- Fireworks sync -----------------------------------------------------------------------------

/** {warn, info} counts over the drift list (unknown severities count as info). */
export function driftCounts(drift) {
  const c = { warn: 0, info: 0 };
  for (const d of drift ?? []) c[d.severity === 'warn' ? 'warn' : 'info']++;
  return c;
}

/** One-line panel summary: 'In sync' / '2 need attention · 1 note'. */
export function driftSummary(drift) {
  const { warn, info } = driftCounts(drift);
  if (!warn && !info) return 'In sync';
  const parts = [];
  if (warn) parts.push(`${warn} need${warn === 1 ? 's' : ''} attention`);
  if (info) parts.push(`${info} note${info === 1 ? '' : 's'}`);
  return parts.join(' · ');
}

/** Plain-language sentence for one drift row — what happened, in words a nontechnical admin reads
 *  at a glance. Unknown kinds fall back to the raw kind string, never throw. */
export function driftSentence(d) {
  switch (d?.kind) {
    case 'not_cataloged':
      return `${lastSeg(d.model)} is deployed in Fireworks but not in the catalog yet.`;
    case 'cataloged_not_deployed':
      return `${d.catalog_id} is in the catalog; no GPU is currently deployed (normal for on-demand).`;
    case 'stale_suffix':
      return `${d.catalog_id} points at an old deployment — the live one is #${lastSeg(d.live_deployment)}.`;
    default:
      return d?.kind ?? '';
  }
}

/** The fix affordance for a drift row: {label, title, yaml, instruction} for the YAML modal, or
 *  null when the row is informational only. */
export function driftAction(d) {
  if (d?.kind === 'not_cataloged' && d.suggested_yaml) {
    return {
      label: 'Adopt',
      title: `Adopt ${lastSeg(d.model)}`,
      yaml: d.suggested_yaml,
      instruction: 'Paste into catalog.fireworks.yaml and redeploy the gateway.',
    };
  }
  if (d?.kind === 'stale_suffix' && d.suggested_upstream) {
    return {
      label: 'Fix ref',
      title: `Fix ${d.catalog_id}`,
      yaml: `upstream_model: ${d.suggested_upstream}`,
      instruction: `Replace the upstream_model line for ${d.catalog_id} in catalog.fireworks.yaml and redeploy the gateway.`,
    };
  }
  return null;
}

/** 'checked 4m ago' off the sync payload's epoch-seconds checked_at ('' when absent). */
export function syncFreshness(s) {
  if (!s?.checked_at) return '';
  const r = relTime(s.checked_at);
  return r ? `checked ${r}` : '';
}

// ---- Provider discovery (OpenRouter + Fireworks libraries) ---------------------------------------
// Shapes from /v1/admin/catalog/discovery/*: {slug, name, context_window, tools, vision,
// cataloged, catalog_id} + OpenRouter price_in/out (USD per 1k) / Fireworks tunable + filtered_out.

/** Model family from the slug. OpenRouter shape: the vendor prefix ('moonshotai/kimi-k2.5' →
 *  'moonshotai'). Fireworks shape ('accounts/…/models/glm-5p2'): the last segment's leading
 *  alpha run ('glm'). Feed to vendorHue for the family tint — stable, never gray. */
export function vendorFromSlug(slug) {
  const s = String(slug ?? '');
  if (s.startsWith('accounts/')) return (lastSeg(s).match(/^[a-z]+/i)?.[0] ?? '').toLowerCase();
  if (s.startsWith('@cf/')) return (s.split('/')[1] ?? '').toLowerCase(); // @cf/meta/llama → 'meta'
  return s.split('/')[0].toLowerCase();
}

// Families whose hue lives under another name in the benchmarks PROVIDER_HUE map.
const VENDOR_HUE_ALIAS = { glm: 'z-ai', llama: 'meta' };
export const vendorHue = (v) => providerHue(VENDOR_HUE_ALIAS[v] ?? v);

/** Per-1k wire price → per-MILLION display (the unit people quote): 0.00055 → '$0.55'. */
export function perM(perK) {
  if (perK == null) return '—';
  const v = perK * 1000;
  if (v === 0) return '$0';
  return '$' + (v >= 0.01 ? v.toFixed(2) : parseFloat(v.toPrecision(2)));
}

/** Catalog id for an adopted slug: '<prefix>-' + last segment; bumps a numeric suffix on
 *  collision ('or-x' taken → 'or-x-2'). */
export function suggestedId(slug, existingIds, prefix = 'or') {
  const base = `${prefix}-${lastSeg(slug)}`;
  const taken = new Set(existingIds ?? []);
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base}-${n}`)) n++;
  return `${base}-${n}`;
}

/** Paste-ready catalog.openrouter.yaml entry — exact field shape of the real fragment. */
export function orYaml(m, id) {
  return [
    `- id: ${id}`,
    `  lane: economy`,
    `  endpoint: openai`,
    `  base_url: https://openrouter.ai/api/v1`,
    `  api_key_env: OPENROUTER_API_KEY`,
    `  residency_class: cloud`,
    `  price_usd_per_1k: { prompt: ${m.price_in ?? 0}, completion: ${m.price_out ?? 0} }`,
    `  context_window: ${m.context_window ?? 0}`,
    `  upstream_model: ${m.slug}`,
  ].join('\n');
}

/** Paste-ready catalog.cloudflare.yaml entry — exact field shape of the real fragment. base_url keeps
 *  the ${CLOUDFLARE_ACCOUNT_ID} template (the runner expands it); Cloudflare's models API exposes no
 *  per-token price, so the entry ships zeros with a fix-me comment. */
export function cfYaml(m, id) {
  return [
    `- id: ${id}`,
    `  lane: economy`,
    `  endpoint: openai`,
    `  base_url: https://api.cloudflare.com/client/v4/accounts/\${CLOUDFLARE_ACCOUNT_ID}/ai/v1`,
    `  api_key_env: CLOUDFLARE_API_TOKEN`,
    `  residency_class: cloud`,
    `  price_usd_per_1k: { prompt: 0.0, completion: 0.0 } # set the real per-token price (Cloudflare's models API has none)`,
    `  context_window: ${m.context_window ?? 0}`,
    `  upstream_model: "${m.slug}"`,
  ].join('\n');
}

/** Paste-ready catalog.fireworks.yaml entry — exact field shape of the real fragment. The
 *  platform API exposes no per-token price, so the entry ships zeros with a fix-me comment. */
export function fwYaml(m, id) {
  return [
    `- id: ${id}`,
    `  lane: economy`,
    `  endpoint: openai`,
    `  base_url: https://api.fireworks.ai/inference/v1`,
    `  api_key_env: FIREWORKS_API_KEY`,
    `  residency_class: cloud`,
    `  price_usd_per_1k: { prompt: 0.0, completion: 0.0 } # serverless — set the real per-token price`,
    `  context_window: ${m.context_window ?? 0}`,
    `  upstream_model: ${m.slug}`,
  ].join('\n');
}

/** Paste-ready catalog.anthropic.yaml entry — exact field shape of the real fragment: the native
 *  Messages endpoint (no base_url — the adapter knows the host). Anthropic's models API exposes no
 *  per-token price or context, so the entry ships zeros with fix-me comments. */
export function anYaml(m, id) {
  return [
    `- id: ${id}`,
    `  lane: economy`,
    `  endpoint: anthropic`,
    `  api_key_env: ANTHROPIC_API_KEY`,
    `  residency_class: cloud`,
    `  price_usd_per_1k: { prompt: 0.0, completion: 0.0 } # set the real per-token price (the models API has none)`,
    `  context_window: ${m.context_window || 0} # set the real context window`,
    `  upstream_model: ${m.slug}`,
  ].join('\n');
}

/** The quiet capability line on a discovery card (chips are gone — Alex ruling): 'Tools ·
 *  Vision · Cloud'. Fireworks swaps Cloud for the factory angle, 'Tunable (LoRA)'; on
 *  OpenRouter, Cloud is the cataloged entry's residency fact and only shows there. */
export function capsLine(m, provider) {
  const parts = [];
  if (m?.tools) parts.push('Tools');
  if (m?.vision) parts.push('Vision');
  if (provider === 'fireworks') {
    if (m?.tunable) parts.push('Tunable (LoRA)');
  } else if (m?.cataloged) {
    parts.push('Cloud');
  }
  return parts.join(' · ');
}

/** The library filter chips — keys match FILTER_PRED; labels are the UI text. */
export const DISCOVERY_FILTERS = [
  { key: 'new', label: 'New' },
  { key: 'cataloged', label: 'In catalog' },
  { key: 'not_cataloged', label: 'Not cataloged' },
  { key: 'tools', label: 'Tools' },
  { key: 'vision', label: 'Vision' },
  { key: 'cheap', label: '≤ $1 /M in' },
  { key: 'bigctx', label: '128k+ context' },
];
export const FW_DISCOVERY_FILTERS = [
  { key: 'new', label: 'New' },
  { key: 'cataloged', label: 'In catalog' },
  { key: 'not_cataloged', label: 'Not cataloged' },
  { key: 'tunable', label: 'Tunable (LoRA)' },
  { key: 'vision', label: 'Vision' },
  { key: 'bigctx', label: '128k+ context' },
];
// Cloudflare exposes tools/vision/context but no per-token price → same chips as OpenRouter minus 'cheap'.
export const CF_DISCOVERY_FILTERS = [
  { key: 'new', label: 'New' },
  { key: 'cataloged', label: 'In catalog' },
  { key: 'not_cataloged', label: 'Not cataloged' },
  { key: 'tools', label: 'Tools' },
  { key: 'vision', label: 'Vision' },
  { key: 'bigctx', label: '128k+ context' },
];
// Anthropic's models API exposes no capability/price/context facts → only the catalog-state chips.
export const AN_DISCOVERY_FILTERS = [
  { key: 'cataloged', label: 'In catalog' },
  { key: 'not_cataloged', label: 'Not cataloged' },
];
const FILTER_PRED = {
  new: (m) => !!m.is_new,
  cataloged: (m) => !!m.cataloged,
  not_cataloged: (m) => !m.cataloged,
  tools: (m) => !!m.tools,
  vision: (m) => !!m.vision,
  tunable: (m) => !!m.tunable,
  cheap: (m) => m.price_in != null && m.price_in <= 0.001, // ≤ $1 per M = ≤ $0.001 per 1k
  bigctx: (m) => (m.context_window ?? 0) >= 128000,
};

/** Join adoption freshness flags (upstream_removed / price_drift, keyed by upstream_model) onto the
 *  discovery rows so a cataloged card can show them. Returns a fresh array. */
export function withFreshnessFlags(models, adoptions) {
  const bySlug = new Map((adoptions ?? []).map((a) => [a.upstream_model, a]));
  return (models ?? []).map((m) => {
    const a = bySlug.get(m.slug);
    return a ? { ...m, upstream_removed: a.upstream_removed, price_drift: a.price_drift } : m;
  });
}

/** 'first seen Jul 21' from an epoch-seconds first_seen, '' when absent. */
export function firstSeenLabel(first_seen) {
  if (!first_seen) return '';
  const d = new Date(first_seen * 1000);
  return `new · ${d.toLocaleString('en-US', { month: 'short', day: 'numeric' })}`;
}

/** Search (name/slug substring — the slug carries the vendor) + AND of the active filter keys. */
export function filterDiscovery(models, q, active) {
  const needle = (q ?? '').trim().toLowerCase();
  return (models ?? []).filter((m) => {
    for (const k of active ?? []) if (FILTER_PRED[k] && !FILTER_PRED[k](m)) return false;
    if (!needle) return true;
    return `${m.slug ?? ''} ${m.name ?? ''}`.toLowerCase().includes(needle);
  });
}

/** Stable override key for a library model — discovery slugs are unique per source. */
export const adoptionKey = (m) => `${m.source}:${m.slug}`;

/** Overlay optimistic adoption state + the "added by you" join onto discovery models.
 *  `overrides` maps adoptionKey → {cataloged, catalog_id} written the instant a click lands
 *  (before the server refetch reconciles); `adoptedIds` is the caller-scope adoptions list.
 *  Every model gains `adopted`: true when it's in the catalog because this scope added it —
 *  base (shipped) entries stay adopted:false and get no remove affordance. */
export function withAdoptions(models, adoptedIds, overrides) {
  const ids = new Set(adoptedIds ?? []);
  return (models ?? []).map((m) => {
    const o = overrides?.[adoptionKey(m)];
    const v = o ? { ...m, ...o } : m;
    return { ...v, adopted: o ? !!o.cataloged : !!(v.cataloged && ids.has(v.catalog_id)) };
  });
}

/** Task-type labels whose effective (bound-or-default) model is this catalog id.
 *  Joins the routing-policy view already loaded on the page — no extra fetch. */
export const routedTasks = (catalogId, routing) =>
  (routing?.labels ?? []).filter((r) => (r.model ?? r.default_model) === catalogId).map((r) => r.label);

// ---- /models page joins ---------------------------------------------------------------------

/** Tag discovery models with their source and concatenate — the Library's "All sources" list. */
export const mergeDiscovery = (orModels, fwModels, cfModels, anModels) => [
  ...(orModels ?? []).map((m) => ({ ...m, source: 'openrouter' })),
  ...(fwModels ?? []).map((m) => ({ ...m, source: 'fireworks' })),
  ...(cfModels ?? []).map((m) => ({ ...m, source: 'cloudflare' })),
  ...(anModels ?? []).map((m) => ({ ...m, source: 'anthropic' })),
];

/** Union of both sources' filter chips (OpenRouter order first) — the "All sources" chip row.
 *  A chip that doesn't apply to a source simply never matches its cards. */
export const ALL_DISCOVERY_FILTERS = [
  ...DISCOVERY_FILTERS,
  ...FW_DISCOVERY_FILTERS.filter((f) => !DISCOVERY_FILTERS.some((d) => d.key === f.key)),
];

/** 'LoRA SFT · qwen3-4b' from a tuning job — method + base model, '-instruct…' tail dropped. */
const KIND = { 'sft-lora': 'LoRA SFT', sft: 'SFT', rft: 'RFT', dpo: 'DPO' };
export function kindLabel(job) {
  if (!job) return '—';
  const method = KIND[job.method] ?? job.method ?? '—';
  const base = shortRef(job.base_model).replace(/-instruct.*$/, '');
  return base && base !== '—' ? `${method} · ${base}` : method;
}

/** Custom Models rows: tuning lineage ⋈ catalog entry ⋈ Fireworks-sync GPU state.
 *  gpu: 'ready' (live deployment), 'off' (cataloged_not_deployed — normal for on-demand),
 *  null (unknown / not cataloged). */
export function customModelRows(tuning, catModels, sync) {
  const catById = new Map((catModels ?? []).map((m) => [m.id, m]));
  const ready = new Set((sync?.ok ?? []).map((r) => r.catalog_id));
  const off = new Set(
    (sync?.drift ?? []).filter((d) => d.kind === 'cataloged_not_deployed').map((d) => d.catalog_id)
  );
  return lineageChains(tuning?.models, tuning?.jobs, tuning?.datasets, tuning?.evals).map((c) => {
    const cid = c.model.catalog_id;
    return {
      ...c,
      cat: (cid && catById.get(cid)) || null,
      bestEval: sortEvals(c.evals)[0] ?? null,
      gpu: cid && ready.has(cid) ? 'ready' : cid && off.has(cid) ? 'off' : null,
    };
  });
}
