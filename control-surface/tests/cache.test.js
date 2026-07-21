// node --test — the Caching page's pure view-model math ($lib/cache.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PRESETS, presetFor, strategyWrite, policyPassthrough, toCacheHealth } from '../src/lib/cache.js';

test('presetFor: empty = inherit, known preset id round-trips, anything else is custom', () => {
  assert.equal(presetFor(null), null);
  assert.equal(presetFor({}), null);
  assert.equal(presetFor({ preset: 'balanced', auto_inject: true }), 'balanced');
  assert.equal(presetFor({ auto_inject: false }), 'custom');
  assert.equal(presetFor({ preset: 'made-up' }), 'custom');
});

test('strategyWrite: a preset writes its knob bundle with prewarm split off the cache object', () => {
  const { cache, prewarm } = strategyWrite('max', {});
  assert.equal(cache.preset, 'max');
  assert.equal(cache.auto_inject, true);
  assert.equal(cache.auto_inject_min_messages, 2);
  assert.equal(cache.warmth_routing, true);
  assert.equal(prewarm, true);
  assert.ok(!('prewarm' in cache), 'prewarm rides the policy row, never the cache object');
});

test('strategyWrite: custom clamps min-messages into the backend band [1, 50]', () => {
  const lo = strategyWrite('custom', { autoInject: true, minMessages: 0, warmthRouting: false, prewarm: false });
  assert.equal(lo.cache.auto_inject_min_messages, 1);
  const hi = strategyWrite('custom', { autoInject: true, minMessages: 999, warmthRouting: true, prewarm: true });
  assert.equal(hi.cache.auto_inject_min_messages, 50);
  assert.equal(hi.prewarm, true);
});

test('every preset writes only knobs the PUT accepts', () => {
  const OK = new Set(['preset', 'auto_inject', 'auto_inject_min_messages', 'warmth_routing']);
  for (const p of PRESETS) {
    const { cache } = strategyWrite(p.id, {});
    for (const k of Object.keys(cache)) assert.ok(OK.has(k), `${p.id} writes unknown key ${k}`);
  }
});

test('policyPassthrough: carries the whole routing surface so a full-replace PUT loses nothing', () => {
  const view = {
    labels: [
      { label: 'code_generation', bindable: true, overridden: true, bound_model: 'm1', custom: false },
      { label: 'other', bindable: true, overridden: false, model: 'm2', custom: false },
      { label: 'redact', bindable: false, overridden: false, custom: false },
      { label: 'stale_one', bindable: true, overridden: false, bound_model: 'gone', stale: true, custom: false },
    ],
    optimize: 'cost', optimize_overridden: true,
    custom_labels: [{ name: 'ct', desc: 'd', model: 'm1' }],
    stick_ttls: { code_generation: 3600 },
    prewarm: true,
    fail_policy: 'closed',
    taxonomy: { labels: { pii: { constraint: 'deny', desc: '' } }, default: 'pii' },
    cache: { preset: 'balanced', auto_inject: true },
  };
  const b = policyPassthrough(view);
  assert.deepEqual(b.bindings, { code_generation: 'm1' }); // only real overrides; stale dropped
  assert.equal(b.optimize, 'cost');
  assert.equal(b.prewarm, true);
  assert.equal(b.fail_policy, 'closed');
  assert.deepEqual(b.stick_ttls, { code_generation: 3600 });
  assert.deepEqual(b.taxonomy, view.taxonomy);
  assert.deepEqual(b.cache, view.cache);
  // optimize never pinned when the view says it was the global default
  assert.equal(policyPassthrough({ ...view, optimize_overridden: false }).optimize, null);
});

test('toCacheHealth: token-weighted hit rate and the quiet no-traffic state', () => {
  const h = toCacheHealth({ buckets: [
    { bucket: 'd1', requests: 10, tokens_prompt: 1000, tokens_cached: 900, tokens_cache_write: 50, warm_hold_requests: 2, hit_rate: 0.9 },
    { bucket: 'd2', requests: 1, tokens_prompt: 9000, tokens_cached: 0, tokens_cache_write: 0, warm_hold_requests: 0, hit_rate: 0 },
  ]});
  assert.equal(h.requests, 11);
  assert.equal(h.tokensCached, 900);
  assert.equal(h.hitRate, 0.09); // 900/10000 — a quiet day drags the window figure honestly
  assert.equal(h.warmHolds, 2);
  assert.equal(h.hasTraffic, true);
  assert.equal(toCacheHealth({ buckets: [] }).hasTraffic, false);
  assert.equal(toCacheHealth(null), null);
});
