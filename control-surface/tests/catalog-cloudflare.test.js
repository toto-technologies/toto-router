// node --test — the Cloudflare additions to $lib/catalog.js (discovery source view).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mergeDiscovery, cfYaml, vendorFromSlug, CF_DISCOVERY_FILTERS } from '../src/lib/catalog.js';

test('mergeDiscovery tags a third cloudflare source', () => {
  const out = mergeDiscovery([{ slug: 'a/b' }], [{ slug: 'accounts/x/models/c' }], [{ slug: '@cf/meta/d' }]);
  assert.deepEqual(
    out.map((m) => m.source),
    ['openrouter', 'fireworks', 'cloudflare']
  );
  // omitting cfModels stays backwards-compatible (no crash, no cloudflare rows)
  assert.equal(mergeDiscovery([{ slug: 'a/b' }], []).length, 1);
});

test('vendorFromSlug reads the family out of a @cf/ slug', () => {
  assert.equal(vendorFromSlug('@cf/meta/llama-3.1-8b-instruct-fp8-fast'), 'meta');
  assert.equal(vendorFromSlug('@cf/openai/gpt-oss-120b'), 'openai');
  // the other shapes still work
  assert.equal(vendorFromSlug('moonshotai/kimi-k2.5'), 'moonshotai');
  assert.equal(vendorFromSlug('accounts/fireworks/models/glm-5p2'), 'glm');
});

test('cfYaml emits a paste-ready fragment with the templated base_url and pinned slug', () => {
  const y = cfYaml({ slug: '@cf/openai/gpt-oss-120b', context_window: 128000 }, 'cf-gpt-oss-120b');
  assert.match(y, /id: cf-gpt-oss-120b/);
  assert.match(y, /base_url: https:\/\/api\.cloudflare\.com\/client\/v4\/accounts\/\$\{CLOUDFLARE_ACCOUNT_ID\}\/ai\/v1/);
  assert.match(y, /api_key_env: CLOUDFLARE_API_TOKEN/);
  assert.match(y, /upstream_model: "@cf\/openai\/gpt-oss-120b"/);
  assert.match(y, /context_window: 128000/);
});

test('CF_DISCOVERY_FILTERS has tools but no price chip (CF exposes no price)', () => {
  const keys = CF_DISCOVERY_FILTERS.map((f) => f.key);
  assert.ok(keys.includes('tools'));
  assert.ok(!keys.includes('cheap'));
});
