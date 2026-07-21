// Pure view-model helpers behind the four org widgets (Models, Teams & People, Benchmarks,
// Recent Activity) — rune-free so node --test covers them (tests/overview-org.test.js).
// Relative import (not $lib) so node --test can load this module without the SvelteKit alias.
import { rankModels, score100, benchmarkModelLabel, catLabel, providerMark } from '../benchmarks.js';
import { fmtUsd } from '../usage.js';

export { providerMark };

// ---- range plumbing (brief §3.3: the page range feeds every range-aware widget) ----------------
export const RANGES = { '24h': 24 * 3600e3, '7d': 7 * 864e5, '30d': 30 * 864e5 };
export const rangeStartISO = (r, nowMs = Date.now()) =>
  new Date(nowMs - (RANGES[r] ?? RANGES['24h'])).toISOString();
export const prevRangeStartISO = (r, nowMs = Date.now()) =>
  new Date(nowMs - 2 * (RANGES[r] ?? RANGES['24h'])).toISOString();

// ---- formatting (brief §4 common grammar) ------------------------------------------------------
/** "$12.4k" above $10k; below, fmtUsd's honest cents/sub-cent rendering. */
export function usd(n) {
  n = +n || 0;
  return n >= 10000 ? '$' + (n / 1000).toFixed(1) + 'k' : fmtUsd(n);
}

/** Compact count: 1.2K / 3.40M / 1.01B. */
export function compact(n) {
  n = +n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

export function ago(ts) {
  const s = Math.max(0, Date.now() / 1e3 - ts);
  if (s < 60) return Math.floor(s) + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

/** Trend chip vs the prior equal-length window. null when there is no honest baseline. */
export function deltaPct(cur, prev) {
  if (prev == null || !(prev > 0)) return null;
  const pct = Math.round(((+cur || 0) - prev) / prev * 100);
  return { pct, dir: pct > 0 ? 'up' : pct < 0 ? 'down' : 'flat', label: (pct > 0 ? '+' : '') + pct + '%' };
}

// ---- W3 · Models -------------------------------------------------------------------------------
// Model-maker vendor for the logo tile. Catalog upstream slugs carry the vendor as the path prefix
// (anthropic/claude-sonnet-5); Fireworks paths (accounts/fireworks/models/x) and bare ids fall back
// to a name-prefix guess, then the serving provider. logoFor(null) → monogram, never an empty tile.
const NAME_VENDOR = [
  ['claude', 'anthropic'], ['gpt', 'openai'], ['o1', 'openai'], ['o3', 'openai'], ['o4', 'openai'],
  ['gemini', 'google'], ['llama', 'meta'], ['qwen', 'qwen'], ['deepseek', 'deepseek'],
  ['kimi', 'moonshotai'], ['mistral', 'mistralai'], ['glm', 'zhipu'],
];
export function vendorOf(entry) {
  const up = typeof entry === 'string' ? entry : (entry?.upstream_model ?? entry?.id ?? '');
  if (up.includes('/')) {
    const head = up.split('/')[0];
    if (head !== 'accounts') return head;
  }
  const name = up.split('/').pop().replace(/^(or|fw)-/, '').toLowerCase();
  for (const [prefix, vendor] of NAME_VENDOR) if (name.startsWith(prefix)) return vendor;
  return (typeof entry === 'object' && entry?.provider) || null;
}

/**
 * Join usage rows (grouped by model+residency) with the catalog and the prior window's
 * per-model calls → top-spend rows with share-of-spend, majority residency, vendor/provider,
 * and a calls trend delta (null when the model had no prior traffic).
 */
export function topModels(curRows = [], prevRows = [], catalogModels = [], limit = 5) {
  const byId = new Map();
  for (const r of curRows) {
    const id = r.model ?? '—';
    const m = byId.get(id) ?? { id, calls: 0, cost: 0, cloud: 0, perim: 0 };
    const req = +r.requests || 0;
    m.calls += req;
    m.cost += +r.cost_usd || 0;
    if (r.residency === 'cloud') m.cloud += req;
    else m.perim += req;
    byId.set(id, m);
  }
  const total = [...byId.values()].reduce((s, m) => s + m.cost, 0) || 1;
  const prevCalls = new Map();
  for (const r of prevRows) {
    const id = r.model ?? '—';
    prevCalls.set(id, (prevCalls.get(id) ?? 0) + (+r.requests || 0));
  }
  const cat = new Map(catalogModels.map((c) => [c.id, c]));
  return [...byId.values()]
    .sort((a, b) => b.cost - a.cost)
    .slice(0, limit)
    .map((m) => {
      const c = cat.get(m.id);
      return {
        id: m.id,
        calls: m.calls,
        cost: m.cost,
        share: Math.round((m.cost / total) * 100),
        residency: m.cloud >= m.perim ? 'cloud' : 'in-perimeter',
        vendor: c ? vendorOf(c) : vendorOf(m.id),
        provider: c?.provider ?? null,
        delta: deltaPct(m.calls, prevCalls.get(m.id) ?? null),
      };
    });
}

// ---- W6 · Teams & People -----------------------------------------------------------------------
/** Memberships are (user, team) rows — people = distinct users. */
export function uniquePeople(members = []) {
  return new Set(members.map((m) => m.user_id)).size;
}

/** Names of up to `max` teams with real traffic in the usage window, busiest first. Rows whose
 *  team id doesn't resolve to a known team (no team / deleted team) are dropped — never faked. */
export function activeTeams(usageRows = [], teams = [], max = 2) {
  const names = new Map(teams.map((t) => [t.team_id, t.name]));
  const byTeam = new Map();
  for (const r of usageRows) {
    if (!names.has(r.team)) continue;
    byTeam.set(r.team, (byTeam.get(r.team) ?? 0) + (+r.requests || 0));
  }
  return [...byTeam.entries()]
    .filter(([, req]) => req > 0)
    .sort((a, b) => b[1] - a[1])
    .slice(0, max)
    .map(([id]) => names.get(id));
}

export function activitySentence(names) {
  if (!names?.length) return '';
  if (names.length === 1) return `${names[0]} was active in the last day.`;
  return `${names[0]} and ${names[1]} were active in the last day.`;
}

// ---- W7 · Benchmarks ---------------------------------------------------------------------------
/** Ranked rows for one category: rank, display label, provider, 0–100 score, catalog membership. */
export function benchRows(models = [], category, limit = 3) {
  return rankModels(models, category)
    .slice(0, limit)
    .map((r) => ({
      id: r.model.id,
      label: benchmarkModelLabel(r.model),
      provider: r.model.provider ?? null,
      score: score100(r.score),
      rank: r.rank,
      inCatalog: Boolean(r.model.catalog_pinned ?? r.model.routable),
    }));
}

/** The catalog tie-in sentence under the ranked rows (brief W7: two honest variants). */
export function benchSentence(rows, category) {
  const leader = rows?.[0];
  if (!leader) return null;
  const cat = catLabel(category).toLowerCase();
  return leader.inCatalog
    ? { text: `In your catalog, ${leader.label} leads ${cat} right now.`, addLink: false }
    : { text: `${leader.label} leads ${cat} — it's not in your catalog yet.`, addLink: true };
}

// ---- W8 · Recent Activity ----------------------------------------------------------------------
// Verb map over the audit `action` vocabulary (grep '"admin:…"' in toto_gateway/). Each entry
// renders the sentence AFTER the bold actor. `who` resolves an opaque user_id to a display name
// (the audit page's members-email join). Unmapped actions fall back to the cleaned action text —
// never a raw dotted key.
const q = (s) => (s ? `“${s}”` : '');
const VERBS = {
  'org.rename': (e) => `renamed the organization${e.metadata?.name ? ` to ${q(e.metadata.name)}` : ''}`,
  'team.create': (e) => `created the team ${q(e.metadata?.name) || 'a team'}`,
  'team.rename': (e) => `renamed a team${e.metadata?.name ? ` to ${q(e.metadata.name)}` : ''}`,
  'team.delete': (e) => `deleted the team ${q(e.metadata?.name) || (e.target_id ?? '')}`.trim(),
  'member.role': (e, who) =>
    `changed ${who(e.target_id) ?? 'a member'}’s role${e.metadata?.role ? ` to ${e.metadata.role}` : ''}`,
  'member.remove': (e, who) => `removed ${who(e.target_id) ?? 'a member'} from the org`,
  'invitation.create': (e) => `invited ${e.metadata?.email ?? 'someone'} to the org`,
  'invitation.accept': () => 'accepted an invitation and joined the org',
  'policy.update': () => 'updated the routing policy',
  routing_policy: () => 'changed the routing policy',
  catalog_policy: () => 'updated a team’s catalog policy',
  catalog_adoption: (e) => `added ${e.target_id ?? 'a model'} to the catalog`,
  catalog_unadoption: (e) => `removed ${e.target_id ?? 'a model'} from the catalog`,
  benchmarks_refresh: () => 'refreshed the benchmark sources',
  model_inventory_refresh: () => 'refreshed the model inventory',
  org_provider_key: (e) =>
    e.metadata?.action === 'delete'
      ? `removed the ${e.target_id ?? 'provider'} organization key`
      : `updated the ${e.target_id ?? 'provider'} organization key`,
  // auth family (routes/auth.py) — the first events every fresh org sees
  login: () => 'signed in',
  login_failed: () => 'failed to sign in',
  logout: () => 'signed out',
  register: () => 'created an account',
  verify: () => 'verified their email',
  token_mint: () => 'created an API token',
  token_revoke: () => 'revoked an API token',
};

export function humanAudit(e, who = (id) => id) {
  const key = (e?.action ?? '').replace(/^admin:/, '');
  const verb = VERBS[key];
  if (verb) return verb(e, who);
  return key.replace(/[._:]+/g, ' ').trim() || 'made a change';
}

/** Feed-icon tone (kept from the old Overview): crit for destructive, ok for recovery/accept. */
export function auditTone(a = '') {
  if (/kill|halt|delete|remove|revoke/.test(a)) return 'crit';
  if (/close|recover|accept|enable|lift|resolve/.test(a)) return 'ok';
  if (/catalog|routing|budget|policy|update/.test(a)) return 'policy';
  return '';
}
