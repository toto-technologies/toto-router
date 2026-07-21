// Traffic view-model for the model picker's compare mode — pure functions plus one loader.
// The OSS gateway mounts no aggregate analytics/latency endpoint, so the console derives
// per-model p50 latency and per-task-type token/volume averages client-side from the SAME
// requests API the Activity page reads (/v1/admin/requests). All estimates come from this
// gateway's own observed traffic; when a task type has none, callers must show the static
// per-1M prices instead — never a fabricated estimate.
import { getRequests } from './api/admin.js';

const WINDOW_DAYS = 30;
const PAGE = 200; // the server-side limit cap
// ponytail: 600 newest rows bound the sample; add deeper paging if a busy gateway's window
// gets short enough to mislead (the span-based rate below stays honest either way).
const MAX_PAGES = 3;

/** Newest-first request rows for the trailing 30 days (up to MAX_PAGES × PAGE rows). */
export async function loadTrafficRows() {
  const now = Math.floor(Date.now() / 1000);
  const rows = [];
  let offset = 0;
  for (let i = 0; i < MAX_PAGES; i++) {
    const page = await getRequests({ from: now - WINDOW_DAYS * 86400, to: now, limit: PAGE, offset });
    rows.push(...(page.requests ?? []));
    if (page.next_offset == null) break;
    offset = page.next_offset;
  }
  return rows;
}

function median(xs) {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/**
 * Fold request rows into the two lookups the picker needs:
 *   perModel: Map(catalog id → {requests, p50_ms})            — observed p50 latency
 *   perLabel: Map(task type → {requests, avgTokensIn, avgTokensOut, perMonth})
 * `perMonth` extrapolates the label's observed request count over the observed span
 * (oldest fetched row → now, floored at 1 day so an hour of fresh traffic doesn't
 * explode into a fantasy month) to a 30-day rate — "at current volume".
 */
export function trafficStats(rows, now = Date.now()) {
  const perModel = new Map();
  const perLabel = new Map();
  let oldest = now;
  for (const r of rows ?? []) {
    const ts = Date.parse(r.ts);
    if (Number.isFinite(ts) && ts < oldest) oldest = ts;
    if (r.model) {
      const m = perModel.get(r.model) ?? { requests: 0, latencies: [] };
      m.requests += 1;
      if (r.latency_ms != null) m.latencies.push(r.latency_ms);
      perModel.set(r.model, m);
    }
    if (r.classified_as) {
      const l = perLabel.get(r.classified_as) ?? { requests: 0, tokIn: 0, tokOut: 0 };
      l.requests += 1;
      l.tokIn += r.tokens_prompt ?? 0;
      l.tokOut += r.tokens_completion ?? 0;
      perLabel.set(r.classified_as, l);
    }
  }
  const spanDays = Math.min(WINDOW_DAYS, Math.max(1, (now - oldest) / 86400e3));
  for (const [id, m] of perModel) {
    perModel.set(id, { requests: m.requests, p50_ms: median(m.latencies) });
  }
  for (const [label, l] of perLabel) {
    perLabel.set(label, {
      requests: l.requests,
      avgTokensIn: l.tokIn / l.requests,
      avgTokensOut: l.tokOut / l.requests,
      perMonth: (l.requests / spanDays) * 30,
    });
  }
  return { perModel, perLabel };
}

/** Estimated $/month serving `labelStat`'s observed traffic on `model` (prices stored per-1K
 *  tokens). null when the label has no traffic or the model has no price — the caller shows
 *  static prices instead of inventing a number. */
export function monthlyCost(labelStat, model) {
  if (!labelStat || !model) return null;
  if (model.price_in == null && model.price_out == null) return null;
  const perReq =
    (labelStat.avgTokensIn / 1000) * (model.price_in ?? 0) +
    (labelStat.avgTokensOut / 1000) * (model.price_out ?? 0);
  return perReq * labelStat.perMonth;
}

/** "≈ $0.11/mo" — sub-cent estimates keep 2 significant figures so cheap models don't all
 *  collapse to $0.00. */
export function fmtMonthly(v) {
  if (v == null || !Number.isFinite(v)) return '—';
  const n = v >= 0.01 ? v.toFixed(2) : Number(v.toPrecision(2));
  return `≈ $${n}/mo`;
}
