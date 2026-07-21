// node --test — the freshness view-model helpers in $lib/catalog.js.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withFreshnessFlags, firstSeenLabel, DISCOVERY_FILTERS, filterDiscovery } from '../src/lib/catalog.js';

test('withFreshnessFlags joins adoption flags onto discovery rows by upstream_model', () => {
  const models = [{ slug: 'meta/a' }, { slug: 'meta/b' }];
  const adoptions = [{ upstream_model: 'meta/a', upstream_removed: true, price_drift: { new_out: 9 } }];
  const out = withFreshnessFlags(models, adoptions);
  assert.equal(out[0].upstream_removed, true);
  assert.deepEqual(out[0].price_drift, { new_out: 9 });
  assert.equal(out[1].upstream_removed, undefined); // unmatched row untouched
});

test('the New filter chip exists and selects is_new rows', () => {
  assert.ok(DISCOVERY_FILTERS.some((f) => f.key === 'new'));
  const pool = [{ slug: 'a', is_new: true }, { slug: 'b', is_new: false }];
  assert.deepEqual(filterDiscovery(pool, '', new Set(['new'])).map((m) => m.slug), ['a']);
});

test('firstSeenLabel formats an epoch, empty when absent', () => {
  assert.equal(firstSeenLabel(0), '');
  assert.equal(firstSeenLabel(null), '');
  assert.match(firstSeenLabel(Date.UTC(2026, 6, 21) / 1000), /^new · Jul \d+$/);
});
