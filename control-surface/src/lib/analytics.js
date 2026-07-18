// Pure view-model transforms for the Analytics page. No Svelte, no DOM — `node --test`
// (tests/analytics.test.js) exercises the KPI/share math directly. Input shapes are exactly what
// GET /v1/admin/analytics/activity and /insights return (toto_gateway/routes/admin_analytics.py).
// Chart stacking + formatters are reused from ./usage.js — only what toStacks/toBreakdown can't do
// lives here.

/**
 * KPI tile model from the activity bundle: totals, top task type (by requests, excluding
 * "unclassified" — it isn't a task type), and the unclassified share of requests.
 * @param {{totals?:object, by_label?:any[]}} [bundle]
 * @returns {{requests:number, savings:number, top:{label:string, share:number}|null, unclassifiedShare:number}}
 */
export function toKpis({ totals, by_label } = {}) {
  const requests = Number(totals?.requests) || 0;
  const labels = (by_label ?? []).map((r) => ({ label: r.label, requests: Number(r.requests) || 0 }));
  const named = labels.filter((l) => l.label !== 'unclassified').sort((a, b) => b.requests - a.requests);
  const top = named[0] && named[0].requests > 0 ? named[0] : null;
  const unclassified = labels.find((l) => l.label === 'unclassified');
  return {
    requests,
    savings: Number(totals?.savings_usd) || 0,
    top: top ? { label: top.label, share: requests ? top.requests / requests : 0 } : null,
    unclassifiedShare: requests ? (unclassified?.requests ?? 0) / requests : 0,
  };
}

/**
 * Task-type breakdown rows: numbers coerced, request-share computed, sorted by requests desc.
 * @param {any[]} [byLabel]  by_label rows: {label, requests, tokens, cost_usd, savings_usd}
 */
export function toLabelBreakdown(byLabel) {
  const rows = (byLabel ?? []).map((r) => ({
    label: r.label,
    requests: Number(r.requests) || 0,
    tokens: Number(r.tokens) || 0,
    cost: Number(r.cost_usd) || 0,
    savings: Number(r.savings_usd) || 0,
  })).sort((a, b) => b.requests - a.requests);
  const total = rows.reduce((a, r) => a + r.requests, 0);
  return rows.map((r) => ({ ...r, share: total ? r.requests / total : 0 }));
}

/**
 * Normalize the /insights envelope into one render model. `ok:false` + `error` is the degraded
 * state the route promises (200 + insights:null) — never throw off it.
 * @param {{insights?:object|null, error?:string|null, generated_at?:string, cached?:boolean}} [data]
 */
export function insightsView(data) {
  if (!data) return null;
  const at = data.generated_at ? new Date(data.generated_at) : null;
  const stampTime = at && !Number.isNaN(at.getTime())
    ? at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
  return {
    ok: data.insights != null,
    error: data.error ?? null,
    headline: data.insights?.headline ?? '',
    findings: data.insights?.insights ?? [],
    recommendations: data.insights?.recommendations ?? [],
    stamp: stampTime ? `generated ${stampTime}${data.cached ? ' · cached' : ''}` : '',
  };
}

/**
 * Cache-savings render model from GET /v1/admin/usage/cache-savings. Dollars lead; models with no
 * cache activity in the window are dropped (every ok request produces a rollup row — a model that
 * neither read nor wrote a cache says nothing about caching). Sorted by net saved, best first.
 * @param {{total?:object, models?:any[]}} [data]
 * @returns {{net:number, readSavings:number, writePremium:number, tokensCached:number,
 *   tokensWritten:number, hasActivity:boolean,
 *   models:{model:string, requests:number, tokensCached:number, tokensWritten:number, saved:number}[]}|null}
 */
export function toCacheSavings(data) {
  if (!data) return null;
  const num = (x) => Number(x) || 0;
  const t = data.total ?? {};
  const models = (data.models ?? [])
    .map((m) => ({
      model: m.model_name ?? m.model ?? 'unattributed',  // real upstream name when the API repairs it
      requests: num(m.requests),
      tokensCached: num(m.tokens_cached),
      tokensWritten: num(m.tokens_cache_write),
      saved: num(m.net_usd),
    }))
    .filter((m) => m.tokensCached > 0 || m.tokensWritten > 0)
    .sort((a, b) => b.saved - a.saved);
  return {
    net: num(t.net_usd),
    readSavings: num(t.read_savings_usd),
    writePremium: num(t.write_premium_usd),
    tokensCached: num(t.tokens_cached),
    tokensWritten: num(t.tokens_cache_write),
    hasActivity: num(t.tokens_cached) + num(t.tokens_cache_write) > 0,
    models,
  };
}

/** Cents-honest currency for cache figures (fmtUsd rounds to whole dollars — too coarse here):
 *  two decimals, thousands-separated; tiny-but-real amounts say '<$0.01' instead of a dishonest
 *  '$0.00'; negatives wear a real minus sign. */
export function fmtUsdFine(n) {
  n = Number(n) || 0;
  const a = Math.abs(n);
  if (a > 0 && a < 0.005) return '<$0.01'; // rounds to $0.00 either side — sign is immaterial
  const s = a.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${n < 0 ? '−' : ''}$${s}`;
}

/** Percent for share fractions — '<1%' instead of a dishonest '0%' for tiny-but-real traffic. */
export function fmtPct(x) {
  const n = Number(x) || 0;
  if (n > 0 && n < 0.005) return '<1%';
  return `${Math.round(n * 100)}%`;
}
