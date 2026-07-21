// Pure view-model helpers for the telemetry widgets (SpendKpis, RequestVolume, ProviderHealth,
// CacheSavings). Rune-free on purpose so `node --test` covers them (tests/overview-telemetry.test.js).
import { providerLabel } from '../models.js';
import { fmtUsd } from '../usage.js';

const RANGE_MS = { '24h': 24 * 3600e3, '7d': 7 * 864e5, '30d': 30 * 864e5 };

/** Page range → the current window, the previous equal-length window, and unix-second bounds. */
export function rangeWindow(range, now = Date.now()) {
  const ms = RANGE_MS[range] ?? RANGE_MS['24h'];
  return {
    start: new Date(now - ms).toISOString(),
    prevStart: new Date(now - 2 * ms).toISOString(),
    prevEnd: new Date(now - ms).toISOString(),
    granularity: range === '24h' ? 'hour' : 'day',
    fromS: Math.floor((now - ms) / 1000),
    toS: Math.floor(now / 1000)
  };
}

/** Humanized previous window, for delta sentences ("up 12% on last week"). */
export const prevLabel = (r) =>
  ({ '24h': 'yesterday', '7d': 'last week', '30d': 'last month' })[r] ?? 'the prior period';

// ---- formatting (brief §4: "$12.4k" above $10k, compact counts; sub-cent honesty below) ----

export function usd(n) {
  n = Number(n) || 0;
  if (n >= 10000) return '$' + (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return fmtUsd(n);
}

export function count(n) {
  n = n || 0;
  if (n >= 1e9) return { big: (n / 1e9).toFixed(2), unit: 'B' };
  if (n >= 1e6) return { big: (n / 1e6).toFixed(2), unit: 'M' };
  if (n >= 1e3) return { big: (n / 1e3).toFixed(1), unit: 'K' };
  return { big: String(n), unit: '' };
}

/** p95 in human units — ms below a second, seconds above; "—" when the deploy has no trace DB. */
export const fmtMs = (ms) =>
  ms == null ? '—' : ms >= 1000 ? (ms / 1000).toFixed(2) + ' s' : Math.round(ms) + ' ms';

// ---- usage-slice folding ----

/** Sum a usage slice's rows into one totals object. */
export function usageTotals(rows = []) {
  const t = { cost: 0, requests: 0, tokens: 0, savings: 0 };
  for (const r of rows) {
    t.cost += +r.cost_usd || 0;
    t.requests += +r.requests || 0;
    t.tokens += +r.tokens || 0;
    t.savings += +r.savings_usd || 0;
  }
  return t;
}

/** Fold residency×bucket rows into chronological {k, cloud, local, requests, tokens, savings}
 *  buckets (ISO bucket keys sort chronologically). Cloud vs in-perimeter drives the stacks. */
export function foldBuckets(rows = []) {
  const by = new Map();
  for (const r of rows) {
    const k = r.bucket ?? '';
    const b = by.get(k) ?? { k, cloud: 0, local: 0, requests: 0, tokens: 0, savings: 0 };
    const req = +r.requests || 0;
    if (r.residency === 'cloud') b.cloud += req;
    else b.local += req;
    b.requests += req;
    b.tokens += +r.tokens || 0;
    b.savings += +r.savings_usd || 0;
    by.set(k, b);
  }
  return [...by.values()].sort((a, b) => a.k.localeCompare(b.k));
}

/** Zero-fill the folded buckets to the full window (24 hourly / 7 or 30 daily slots, UTC —
 *  matching the server's ISO-prefix bucket keys). The rollup only returns buckets WITH traffic;
 *  without the gaps a lone busy hour renders as one full-width bar claiming the whole day. */
export function fillBuckets(folded, range, now = Date.now()) {
  const hourly = (RANGE_MS[range] ?? RANGE_MS['24h']) === RANGE_MS['24h'];
  const step = hourly ? 3600e3 : 864e5;
  const n = range === '7d' ? 7 : range === '30d' ? 30 : 24;
  const by = new Map(folded.map((b) => [b.k, b]));
  return Array.from({ length: n }, (_, i) => {
    const k = new Date(now - (n - 1 - i) * step).toISOString().slice(0, hourly ? 13 : 10);
    return by.get(k) ?? { k, cloud: 0, local: 0, requests: 0, tokens: 0, savings: 0 };
  });
}

/** Whole-percent change vs the previous window; null when there is no baseline to compare. */
export const deltaPct = (cur, prev) =>
  prev > 0 ? Math.round(((cur - prev) / prev) * 100) : null;

/** Sparkline geometry for the 78×30 .kpi spark (area fill + emphasized endpoint). */
export function spark(vals) {
  const w = 78, h = 30, pad = 2, d = vals.length ? vals : [0, 0];
  const max = Math.max(...d), min = Math.min(...d), rng = max - min || 1;
  const pts = d.map((v, i) => [
    pad + (i * (w - 2 * pad)) / (d.length - 1 || 1),
    h - pad - ((v - min) / rng) * (h - 2 * pad)
  ]);
  const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
  const area = line + ' L' + pts.at(-1)[0].toFixed(1) + ' ' + h + ' L' + pts[0][0].toFixed(1) + ' ' + h + ' Z';
  return { line, area, last: pts.at(-1) };
}

// ---- provider health ----

/** Circuit-breaker enum → plain-word state + .state modifier class (brief §4 W4: never show
 *  closed/open/half-open as primary copy). Unknown states pass through un-styled, never invented. */
export const providerState = (state) =>
  ({
    closed: { word: 'healthy', cls: 'ok' },
    'half-open': { word: 'recovering', cls: 'warn' },
    open: { word: 'paused', cls: 'open' }
  })[state] ?? { word: state ?? 'unknown', cls: '' };

/** Friendly provider name from a breaker host key ("openrouter.ai" → "OpenRouter",
 *  "127.0.0.1:8081" → "Local models", bare "anthropic" → "Anthropic"). The raw key stays
 *  available for tooltips — display never shows a naked host/port. */
const KNOWN_PROVIDERS = ['openrouter', 'anthropic', 'openai', 'fireworks', 'google'];
export function providerDisplay(host = '') {
  const h = String(host).toLowerCase();
  if (/^(127\.|localhost|0\.0\.0\.0|\[?::1)/.test(h)) return 'Local models';
  // known vendor anywhere in the host ("api.fireworks.ai" → Fireworks), else first label titlecased
  const known = KNOWN_PROVIDERS.find((k) => h.includes(k));
  return providerLabel(known ?? h.split('.')[0].split(':')[0]);
}

/** Pagehead health clause (brief §3.3): the worst state wins, colored only when not healthy.
 *  Returns { text, cls } — cls '' | 'warn' | 'crit' maps to the statusline tint. */
export function headerHealthClause(providers = []) {
  const names = (list) => list.map((p) => providerDisplay(p.provider)).join(' and ');
  const paused = providers.filter((p) => p.state === 'open');
  if (paused.length)
    return { text: `${names(paused)} ${paused.length > 1 ? 'are' : 'is'} paused`, cls: 'crit' };
  const recovering = providers.filter((p) => p.state === 'half-open');
  if (recovering.length)
    return { text: `${names(recovering)} ${recovering.length > 1 ? 'are' : 'is'} recovering`, cls: 'warn' };
  return { text: "everything's healthy", cls: '' };
}

/** The failover summary sentence — the whole point of the widget. */
export function healthSummary(providers = []) {
  const names = (list) => list.map((p) => providerDisplay(p.provider)).join(' and ');
  const paused = providers.filter((p) => p.state === 'open');
  if (paused.length)
    return `${names(paused)} ${paused.length > 1 ? 'are' : 'is'} paused — traffic is failing over to other providers.`;
  const recovering = providers.filter((p) => p.state === 'half-open');
  if (recovering.length)
    return `${names(recovering)} ${recovering.length > 1 ? 'are' : 'is'} recovering — traffic is ramping back up.`;
  return 'Everything is routing normally.';
}

// ---- cache health ----

/** Token-weighted cache hit rate across buckets, 0.0–1.0. */
export function overallHitRate(buckets = []) {
  let cached = 0, prompt = 0;
  for (const b of buckets) {
    cached += +b.tokens_cached || 0;
    prompt += +b.tokens_prompt || 0;
  }
  return prompt > 0 ? cached / prompt : 0;
}
