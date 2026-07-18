// Pure view-model transforms for the Usage & Billing page. No Svelte, no DOM — so `node --test`
// (tests/usage.test.js) can exercise the stacking + breakdown math directly. The page (`+page.svelte`)
// turns these into markup with the mockup's .stack / table classes.
//
// Input rows are exactly what GET /v1/admin/usage returns (metering.rollup_usage): a group key per
// group_by dim, an optional `bucket` (when granularity is set), plus requests / tokens / cost_usd /
// frontier_baseline_usd / savings_usd.

/** The three metrics the controls toggle. `key` selects the field; `fmt` renders it. */
export const METRICS = {
  cost: { key: 'cost_usd', label: 'Cost', fmt: fmtUsd },
  tokens: { key: 'tokens', label: 'Tokens', fmt: fmtCompact },
  requests: { key: 'requests', label: 'Requests', fmt: fmtCompact },
};

/** Series palette — cycled by series index. Forest tokens (mockup uses accent/cloud/perimeter). */
export const SERIES_COLORS = [
  'var(--accent)', 'var(--cloud)', 'var(--perimeter)', 'var(--accent-2)', 'var(--warn)', 'var(--good)',
];

const label = (v) => (v == null || v === '' ? 'unattributed' : String(v));

/**
 * Stacked-bar model: one column per time bucket, one segment per group value.
 * @param {any[]} rows  bucketed rollup rows (granularity set → each has `bucket`)
 * @param {string} dim  the group_by dimension key, e.g. 'team'
 * @param {string} metricKey  'cost_usd' | 'tokens' | 'requests'
 * @returns {{series:{name:string,color:string}[], columns:{bucket:string,total:number,segments:{name:string,color:string,value:number}[]}[], max:number}}
 */
export function toStacks(rows, dim, metricKey) {
  // model rows carry model_name (the ACTUAL upstream model) — ids are routing handles
  const val = (r) => label(dim === 'model' ? (r.model_name ?? r.model) : r[dim]);
  const names = [...new Set(rows.map(val))].sort();
  const series = names.map((name, i) => ({ name, color: SERIES_COLORS[i % SERIES_COLORS.length] }));
  const byBucket = new Map();
  for (const r of rows) {
    const b = r.bucket ?? '—';
    if (!byBucket.has(b)) byBucket.set(b, new Map());
    const m = byBucket.get(b);
    m.set(val(r), (m.get(val(r)) ?? 0) + (Number(r[metricKey]) || 0));
  }
  const columns = [...byBucket.keys()].sort().map((bucket) => {
    const m = byBucket.get(bucket);
    const segments = series.map((s) => ({ name: s.name, color: s.color, value: m.get(s.name) ?? 0 }));
    return { bucket, total: segments.reduce((a, s) => a + s.value, 0), segments };
  });
  const max = columns.reduce((a, c) => Math.max(a, c.total), 0) || 1;
  return { series, columns, max };
}

/**
 * Breakdown table model: one row per group value with totals, plus a naive month-end forecast.
 * A row is flagged `estimated` when it spent but carries no frontier baseline — savings can't be
 * computed precisely, so the `~` marker warns the figure is partial (the rollup has no per-row
 * `cost_estimated` flag; this is the honest proxy for it).
 * @param {any[]} rows  non-bucketed rollup rows
 * @param {string} dim
 * @param {{periodFraction?:number}} [opts]  fraction of the billing period elapsed (for forecast)
 */
export function toBreakdown(rows, dim, { periodFraction = 1 } = {}) {
  const frac = periodFraction > 0 ? Math.min(periodFraction, 1) : 1;
  const items = rows.map((r) => {
    const cost = Number(r.cost_usd) || 0;
    const base = Number(r.frontier_baseline_usd) || 0;
    return {
      // model rows carry model_name (the ACTUAL upstream model) — ids are routing handles
      name: label(dim === 'model' ? (r.model_name ?? r.model) : r[dim]),
      requests: Number(r.requests) || 0,
      tokens: Number(r.tokens) || 0,
      cost,
      savings: Number(r.savings_usd) || 0,
      // ponytail: linear extrapolation, not a real forecast model — cost so far / period elapsed.
      forecast: frac < 1 ? cost / frac : cost,
      forecastEstimated: frac < 1,
      estimated: cost > 0 && base === 0,
    };
  });
  const total = items.reduce(
    (a, r) => ({
      requests: a.requests + r.requests,
      tokens: a.tokens + r.tokens,
      cost: a.cost + r.cost,
      savings: a.savings + r.savings,
      forecast: a.forecast + r.forecast,
    }),
    { requests: 0, tokens: 0, cost: 0, savings: 0, forecast: 0 },
  );
  return { items, total };
}

// ---- formatters (mono, tabular) -------------------------------------------------------------

export function fmtUsd(n) {
  return '$' + Math.round(Number(n) || 0).toLocaleString('en-US');
}

export function fmtCompact(n) {
  n = Number(n) || 0;
  if (n >= 1e6) { const v = n / 1e6; return v.toFixed(v < 10 ? 2 : 1) + 'M'; }
  if (n >= 1e3) return Math.round(n / 1e3) + 'K';
  return String(Math.round(n));
}
