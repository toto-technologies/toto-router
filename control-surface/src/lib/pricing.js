// Pricing view-model — pure functions only (node --test exercises this file directly).
// The Pricing tab shows every catalog model's price with PROVENANCE (where the number came
// from), lets an admin set a manual price-per-Mtok where the provider publishes none, and
// surfaces availability drift from the cyclical probe. The API stores per-1k; providers
// publish per-1M; humans compare per-1M — so display is per-Mtok everywhere and the exact
// ÷/×1000 lives at the API boundary, never in component code.

/** Per-1k stored price → per-Mtok display number. */
export function perMtok(per1k) {
  if (per1k === null || per1k === undefined || per1k === '') return null;
  const n = Number(per1k);
  return Number.isFinite(n) ? n * 1000 : null;
}

/** Format a per-Mtok price: "$1.25", "$0.435", "$0.0028" — 2 decimals above $1, up to 4
 *  significant below (provider pages quote sub-cent prices; rounding them to $0.00 lies). */
export function fmtMtok(v) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  if (n === 0) return '$0';
  if (n >= 1) return `$${n.toFixed(2)}`;
  return `$${Number(n.toPrecision(3))}`;
}

/** Human badge for where a price came from. Never show the raw enum. */
export const PROVENANCE = {
  yaml: { label: 'from catalog file', cls: 'src-yaml' },
  discovered: { label: 'provider-reported', cls: 'src-discovered' },
  manual: { label: 'set manually', cls: 'src-manual' },
};

export function provenance(row, override) {
  if (override) return PROVENANCE.manual;
  return PROVENANCE[row?.price_source] ?? PROVENANCE.yaml;
}

/** Merge the raw catalog rows with the caller's override list (platform + own scope) into
 *  display rows. An override REPLACES the shown price (that's what dispatch bills); the
 *  underlying catalog price is kept for the "reverts to" hint on delete. Narrower scope wins
 *  when both a platform and an own-scope override exist for the same id (mirrors the server). */
export function mergeOverrides(models, overrides) {
  const byId = new Map();
  for (const o of overrides ?? []) {
    const prev = byId.get(o.model_id);
    if (!prev || (prev.scope_key === 'platform' && o.scope_key !== 'platform')) byId.set(o.model_id, o);
  }
  const rows = (models ?? []).map((m) => {
    const o = byId.get(m.id) ?? null;
    return {
      id: m.id,
      provider: m.provider,
      upstream: m.upstream_model,
      base_in: perMtok(m.price_in),
      base_out: perMtok(m.price_out),
      in: o ? o.prompt_usd_per_mtok : perMtok(m.price_in),
      out: o ? o.completion_usd_per_mtok : perMtok(m.price_out),
      override: o,
      prov: provenance(m, o),
    };
  });
  // Overrides on ids not in the catalog (stored-but-inert) still deserve a row — invisible
  // config is how silent money bugs live long lives.
  const known = new Set(rows.map((r) => r.id));
  for (const [id, o] of byId) {
    if (known.has(id)) continue;
    rows.push({ id, provider: null, upstream: null, base_in: null, base_out: null,
      in: o.prompt_usd_per_mtok, out: o.completion_usd_per_mtok, override: o,
      prov: PROVENANCE.manual, inert: true });
  }
  return rows;
}

/** Platform rows belong to the operator; an org/team admin's DELETE would 404 (scope-pinned),
 *  so the button disables with a tooltip instead of letting them find that out the hard way. */
export function canDeleteOverride(override, isOperator = false) {
  if (!override) return false;
  return override.scope_key !== 'platform' || isOperator;
}

/** Client-side mirror of the server's PUT validation, so the form can explain a problem
 *  before a round-trip. Returns null when valid, else a human sentence. Zero-for-both is a
 *  separate signal (`needsFree`) because it's a confirm, not an error. */
export function validateDraft(inMtok, outMtok) {
  const p = Number(inMtok), c = Number(outMtok);
  if (inMtok === '' || outMtok === '' || !Number.isFinite(p) || !Number.isFinite(c))
    return { error: 'Both prices are required — dollars per million tokens.', needsFree: false };
  if (p < 0 || c < 0)
    return { error: 'Prices can’t be negative.', needsFree: false };
  if (p === 0 && c === 0)
    return { error: null, needsFree: true };
  return null;
}

/** Flatten the probe result's providers map into display rows, loudest problems first:
 *  fetch errors, then vanished (broken rows), then providers with only candidates. */
export function providerRows(availability) {
  const provs = availability?.providers ?? {};
  const rows = Object.entries(provs).map(([base_url, p]) => ({
    base_url,
    host: base_url.replace(/^https?:\/\//, '').replace(/\/v\d+$/, ''),
    vanished: p.vanished ?? [],
    undeclared: p.undeclared ?? [],
    error: p.error ?? null,
    checked_at: p.checked_at ?? null,
  }));
  const sev = (r) => (r.error ? 0 : r.vanished.length ? 1 : 2);
  return rows.sort((a, b) => sev(a) - sev(b) || a.host.localeCompare(b.host));
}

/** One sentence the header can carry: the drift state of the whole catalog at a glance. */
export function driftHeadline(rows) {
  if (!rows.length) return null;
  const broken = rows.reduce((n, r) => n + r.vanished.length, 0);
  const candidates = rows.reduce((n, r) => n + r.undeclared.length, 0);
  const errored = rows.filter((r) => r.error).length;
  if (!broken && !candidates && !errored) return 'Every declared model is live upstream.';
  const bits = [];
  if (broken) bits.push(`${broken} declared ${broken === 1 ? 'model is' : 'models are'} gone upstream`);
  if (candidates) bits.push(`${candidates} new upstream ${candidates === 1 ? 'model' : 'models'} not in the catalog`);
  if (errored) bits.push(`${errored} ${errored === 1 ? 'provider' : 'providers'} unreachable`);
  return bits.join(' · ');
}
