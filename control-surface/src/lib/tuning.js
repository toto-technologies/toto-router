// Tuning view-model — pure transforms behind routes/tuning (unit-tested via `node --test`).
// Shapes come from /v1/admin/tuning/* (toto_gateway/routes/admin_tuning.py): rates are 0..1
// fractions, timestamps are epoch SECONDS, hyperparams travels as a JSON string.

/** 'accounts/toto-tech/models/docx-formatting-editor-v1#accounts/…/deployments/x' →
 *  'docx-formatting-editor-v1'; 'anthropic/claude-sonnet-4.6' → 'claude-sonnet-4.6'. */
export function shortRef(ref) {
  if (!ref) return '—';
  const segs = String(ref).split('#')[0].split('/').filter(Boolean);
  return segs[segs.length - 1] ?? String(ref);
}

/** 0..1 fraction → '92%' ('—' for null/undefined). */
export const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`);

/** 3002900 → '3.0M' · 4500 → '4.5k' · 300 → '300' · null → '—'. */
export function fmtTokens(n) {
  if (n == null) return '—';
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

/** Dollars with cents — tuning runs are small-money, so '$3.00' never rounds to '$3'. */
export const money = (n) => (n == null ? '—' : `$${Number(n).toFixed(2)}`);

/** created→completed span (epoch seconds) in plain language: '27 min' / '1.5 h' ('—' open-ended). */
export function fmtDuration(startS, endS) {
  if (!startS || !endS || endS < startS) return '—';
  const m = Math.round((endS - startS) / 60);
  if (m < 1) return '<1 min';
  if (m < 90) return `${m} min`;
  return `${(m / 60).toFixed(1)} h`;
}

/** Scoreboard order: match_rate desc — row [0] is the champion the hero strip reads. */
export const sortEvals = (evals) =>
  [...(evals ?? [])].sort((a, b) => (b.match_rate ?? -1) - (a.match_rate ?? -1));

/** True when an eval's model_ref points at this tuning model version. */
export const refMatchesModel = (ref, modelId) => shortRef(ref) === modelId;

/** Join models→jobs→datasets (+ their eval rows) into renderable provenance chains, one per
 *  model version. Broken links degrade to null stages — render '—', never throw. */
export function lineageChains(models, jobs, datasets, evals) {
  const jobById = new Map((jobs ?? []).map((j) => [j.id, j]));
  const dsById = new Map((datasets ?? []).map((d) => [d.id, d]));
  return (models ?? []).map((m) => ({
    model: m,
    job: jobById.get(m.job_id) ?? null,
    dataset: dsById.get(m.dataset_id) ?? null,
    evals: (evals ?? []).filter((e) => refMatchesModel(e.model_ref, m.id)),
  }));
}

/** Method ids → short display labels (unknown ids pass through untouched). */
const METHOD_LABEL = {
  'sft-lora': 'SFT · LoRA',
  sft: 'SFT',
  rft: 'RFT',
  dpo: 'DPO',
  'test-time': 'Test-time',
};
export const methodLabel = (m) => METHOD_LABEL[m] ?? (m || '—');

/** hyperparams is a JSON string on the wire; garbage degrades to {}. */
export function parseHyper(s) {
  try {
    return JSON.parse(s || '{}') ?? {};
  } catch {
    return {};
  }
}
