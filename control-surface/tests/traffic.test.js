// node --test — the picker compare-mode math ($lib/traffic.js) + the newer·cheaper tag.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { trafficStats, monthlyCost, fmtMonthly } from '../src/lib/traffic.js';
import { newerCheaper } from '../src/lib/models.js';

const NOW = Date.parse('2026-07-21T12:00:00Z');
const iso = (hoursAgo) => new Date(NOW - hoursAgo * 3600e3).toISOString();

const rows = [
  { ts: iso(1), model: 'a', classified_as: 'summarization', tokens_prompt: 1000, tokens_completion: 200, latency_ms: 100 },
  { ts: iso(2), model: 'a', classified_as: 'summarization', tokens_prompt: 3000, tokens_completion: 400, latency_ms: 300 },
  { ts: iso(3), model: 'b', classified_as: 'chatbot', tokens_prompt: 50, tokens_completion: 50, latency_ms: 900 },
  { ts: iso(4), model: 'a', classified_as: null, tokens_prompt: 10, tokens_completion: 10, latency_ms: 200 },
];

test('trafficStats: per-model p50 is the median of observed latencies', () => {
  const { perModel } = trafficStats(rows, NOW);
  assert.equal(perModel.get('a').p50_ms, 200); // [100, 300, 200] → 200
  assert.equal(perModel.get('a').requests, 3);
  assert.equal(perModel.get('b').p50_ms, 900);
});

test('trafficStats: per-label token averages; unclassified rows never form a label bucket', () => {
  const { perLabel } = trafficStats(rows, NOW);
  const s = perLabel.get('summarization');
  assert.equal(s.requests, 2);
  assert.equal(s.avgTokensIn, 2000);
  assert.equal(s.avgTokensOut, 300);
  assert.equal(perLabel.has(null), false);
  assert.equal(perLabel.size, 2);
});

test('trafficStats: an hour of fresh traffic extrapolates over a 1-day floor, not the hour', () => {
  const { perLabel } = trafficStats(rows, NOW); // oldest row is 4h ago → span floors at 1 day
  assert.equal(perLabel.get('summarization').perMonth, 60); // 2 req/day × 30
});

test('trafficStats: a longer observed span divides the rate honestly', () => {
  const old = [{ ts: iso(10 * 24), model: 'a', classified_as: 'x', tokens_prompt: 1, tokens_completion: 1 }];
  const { perLabel } = trafficStats([...rows, ...old], NOW);
  assert.equal(Math.round(perLabel.get('summarization').perMonth), 6); // 2 req / 10 days × 30
});

test('monthlyCost: avg tokens × monthly rate priced per-1k; cold start and unpriced are null', () => {
  const stat = { avgTokensIn: 2000, avgTokensOut: 300, perMonth: 60 };
  const model = { price_in: 0.003, price_out: 0.015 }; // per-1k → $0.0105/req
  assert.ok(Math.abs(monthlyCost(stat, model) - 0.63) < 1e-9);
  assert.equal(monthlyCost(null, model), null); // no traffic → NEVER fabricate
  assert.equal(monthlyCost(stat, { price_in: null, price_out: null }), null);
});

test('fmtMonthly keeps sub-cent estimates visible', () => {
  assert.equal(fmtMonthly(2.4), '≈ $2.40/mo');
  assert.equal(fmtMonthly(0.0031), '≈ $0.0031/mo');
  assert.equal(fmtMonthly(null), '—');
});

test('newerCheaper: same provider + family, higher version, strictly lower blended price', () => {
  const models = [
    { id: 'or-sonnet-4.6', provider: 'openrouter', price_in: 0.003, price_out: 0.015 },
    { id: 'or-sonnet-5', provider: 'openrouter', price_in: 0.002, price_out: 0.01 },
    { id: 'or-gemini-2.5-flash', provider: 'openrouter', price_in: 0.0003, price_out: 0.0025 },
    { id: 'echo-local', provider: 'fake', price_in: 0, price_out: 0 }, // no trailing version → ignored
  ];
  const tagged = newerCheaper(models);
  assert.deepEqual([...tagged], ['or-sonnet-5']);
});

test('newerCheaper: never tags across providers or when the newer model costs more', () => {
  const models = [
    { id: 'a-thing-1', provider: 'p1', price_in: 0.01, price_out: 0.01 },
    { id: 'b-thing-2', provider: 'p2', price_in: 0.001, price_out: 0.001 },
    { id: 'a-thing-3', provider: 'p1', price_in: 0.02, price_out: 0.02 },
  ];
  assert.equal(newerCheaper(models).size, 0);
});
