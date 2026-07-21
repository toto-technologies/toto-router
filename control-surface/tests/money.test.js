// node --test — shared currency honesty ($lib/usage.js): sub-cent spend must render as a
// real figure, never a dishonest "$0".
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fmtUsd, fmtUsdCell } from '../src/lib/usage.js';
import { usd } from '../src/lib/overview/telemetry.js';

test('fmtUsd: 2 significant figures below $1, cents to $1k, whole dollars above', () => {
  assert.equal(fmtUsd(0.000075), '$0.000075');
  assert.equal(fmtUsd(0.0123), '$0.012');
  assert.equal(fmtUsd(0.35), '$0.35');
  assert.equal(fmtUsd(4.6), '$4.60');
  assert.equal(fmtUsd(1234.5), '$1,235');
  assert.equal(fmtUsd(0), '$0');
  assert.equal(fmtUsd(null), '$0');
  assert.equal(fmtUsd(-0.004), '−$0.004');
});

test('fmtUsdCell: rollup cells compact tiny-but-real amounts to <$0.01 with the exact figure on hover', () => {
  assert.deepEqual(fmtUsdCell(0.000075), { text: '<$0.01', title: '$0.000075' });
  assert.deepEqual(fmtUsdCell(0), { text: '$0', title: null });
  assert.equal(fmtUsdCell(2.5).text, '$2.50');
});

test('overview usd: keeps $12.4k compaction above $10k, honest below', () => {
  assert.equal(usd(12400), '$12.4k');
  assert.equal(usd(0.000068), '$0.000068');
  assert.equal(usd(0), '$0');
});
