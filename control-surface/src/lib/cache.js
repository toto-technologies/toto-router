// Pure view-model transforms for the Cache page. No Svelte, no DOM — `node --test`
// (tests/cache.test.js) exercises the preset/body math directly. Input shapes are exactly what
// GET /v1/admin/usage/cache-health and the routing-policy GET return
// (toto_gateway/routes/admin_usage.py, admin_routing.py).

/**
 * The three strategy presets — knob bundles the console writes into the policy's `cache` object
 * (plus the top-level `prewarm` bool, which rides the same policy row). `blurb` is the card copy;
 * `knobs` is what Save persists. Custom is not here: it exposes the knobs directly.
 */
export const PRESETS = [
  {
    id: 'off',
    name: 'Off',
    blurb: 'Caching stays exactly as your clients send it.',
    knobs: { auto_inject: false, warmth_routing: false, prewarm: false },
  },
  {
    id: 'balanced',
    name: 'Balanced',
    recommended: true,
    blurb: 'Cache continuing conversations automatically; never pay to cache a one-off.',
    knobs: { auto_inject: true, auto_inject_min_messages: 3, warmth_routing: true, prewarm: false },
  },
  {
    id: 'max',
    name: 'Max savings',
    blurb: 'Cache as early and hold as long as possible; snappier session starts.',
    knobs: { auto_inject: true, auto_inject_min_messages: 2, warmth_routing: true, prewarm: true },
  },
];

/**
 * Which strategy the stored policy expresses: a known preset id, 'custom' for any other explicit
 * cache config, or null when the cache object is empty/absent — the "inherited from global
 * defaults" state.
 * @param {object|null|undefined} cache  the policy's cache object
 */
export function presetFor(cache) {
  const c = cache ?? {};
  if (Object.keys(c).length === 0) return null;
  if (PRESETS.some((p) => p.id === c.preset)) return c.preset;
  return 'custom';
}

/**
 * The `cache` object + `prewarm` bool a Save writes for a strategy selection.
 * @param {string} id  'off'|'balanced'|'max'|'custom'
 * @param {{autoInject:boolean, minMessages:number, warmthRouting:boolean, prewarm:boolean}} knobs
 *   the draft knob values — read only for 'custom'
 * @returns {{cache: object, prewarm: boolean}}
 */
export function strategyWrite(id, knobs) {
  const p = PRESETS.find((x) => x.id === id);
  if (p) {
    const { prewarm, ...cacheKnobs } = p.knobs;
    return { cache: { preset: p.id, ...cacheKnobs }, prewarm };
  }
  return {
    cache: {
      preset: 'custom',
      auto_inject: !!knobs.autoInject,
      auto_inject_min_messages: Math.min(50, Math.max(1, Math.round(Number(knobs.minMessages) || 1))),
      warmth_routing: !!knobs.warmthRouting,
    },
    prewarm: !!knobs.prewarm,
  };
}

/**
 * Everything ALREADY on the routing-policy row, rebuilt as a PUT body — the PUT full-replaces, so
 * a cache-strategy Save must send bindings/optimize/custom_labels/stick_ttls through unchanged.
 * Mirrors the catalog page's routingBody: only real overrides ride the overlay (an overridden row's
 * stored key is `bound_model`; stale bindings are dropped, exactly as a catalog Save drops them),
 * and optimize goes back as null when the view says it was never overridden — sending the displayed
 * global default would pin it.
 * @param {import('./api/types').RoutingPolicyView|null|undefined} view  the GET response
 */
export function policyPassthrough(view) {
  const bindings = {};
  for (const row of view?.labels ?? []) {
    if (row.custom || !row.bindable || !row.overridden) continue;
    const key = row.bound_model ?? row.model;
    if (key) bindings[row.label] = key;
  }
  return {
    bindings,
    optimize: view?.optimize_overridden ? view.optimize : null,
    custom_labels: (view?.custom_labels ?? []).map((c) => ({ ...c })),
    stick_ttls: { ...(view?.stick_ttls ?? {}) },
    prewarm: !!view?.prewarm,
    // full-replace: carry the org's fail_policy through or a Cache save silently resets it to 'open'
    fail_policy: view?.fail_policy ?? 'open',
    // full-replace: carry the org's data-classification taxonomy (W2-C7) through, or a Cache save
    // silently wipes it (same trap as fail_policy).
    taxonomy: view?.taxonomy ?? {},
    cache: { ...(view?.cache ?? {}) },
  };
}

/**
 * Render model for GET /v1/admin/usage/cache-health: coerced per-bucket numbers plus window
 * totals. `hitRate` is token-weighted (total cached / total prompt), not a mean of daily rates —
 * a quiet day must not drag the window figure. hasTraffic false = the quiet empty state.
 * @param {{buckets?: any[]}} [data]
 */
export function toCacheHealth(data) {
  if (!data) return null;
  const num = (x) => Number(x) || 0;
  const buckets = (data.buckets ?? []).map((b) => ({
    bucket: String(b.bucket ?? ''),
    requests: num(b.requests),
    prompt: num(b.tokens_prompt),
    cached: num(b.tokens_cached),
    written: num(b.tokens_cache_write),
    warmHolds: num(b.warm_hold_requests),
    hitRate: Math.min(1, Math.max(0, num(b.hit_rate))),
  }));
  const sum = (k) => buckets.reduce((a, b) => a + b[k], 0);
  const prompt = sum('prompt');
  return {
    buckets,
    requests: sum('requests'),
    warmHolds: sum('warmHolds'),
    tokensCached: sum('cached'),
    hitRate: prompt ? Math.min(1, sum('cached') / prompt) : 0,
    hasTraffic: sum('requests') > 0,
  };
}
