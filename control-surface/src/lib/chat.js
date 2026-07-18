// Pure chat-page logic (node --test'd): provenance line, work-run event reduction for the
// sub-agent cards, model-lever options, recommendation-chip display model. The Svelte page
// owns state + network; nothing here touches the DOM.
import { prettyModel, providerLabel, priceFmt } from './models.js';

/** The quiet per-answer provenance line: "Sonnet 5 · $0.0042". Null model → cost only;
 *  nothing meaningful → '' (render nothing). Sub-cent costs keep enough digits to be real. */
export function provLine(model, cost) {
  const parts = [];
  if (model) parts.push(prettyModel(model));
  if (typeof cost === 'number' && cost > 0) {
    parts.push('$' + (cost >= 0.01 ? cost.toFixed(2) : parseFloat(cost.toPrecision(2))));
  }
  return parts.join(' · ');
}

/** A fresh sub-agent card (session_ref {run_id, query} → one card in the thread). */
export function newCard(runId, query) {
  return { run_id: runId, query, status: 'running', activity: 'Starting…', steps: [], answer: '', error: '' };
}

/**
 * Fold one work-run SSE event into a card. Work runs speak the driver span vocabulary
 * (driver/core.py _emit call sites): triage, decompose {n_tasks}, label {task}, dispatch
 * {task, model}, dispatch_error, guard_block, synthesize, answer_delta, model_fallback,
 * run_done/run_failed {status, error}. Unknown kinds are ignored — the vocabulary can grow
 * without breaking cards. Returns true when the event changed anything (page re-renders).
 */
export function reduceWorkEvent(card, kind, data = {}) {
  switch (kind) {
    case 'triage':
      card.activity = 'Sizing up the task…';
      return true;
    case 'decompose':
      card.activity = data.n_tasks ? `Planned ${data.n_tasks} step${data.n_tasks === 1 ? '' : 's'}` : 'Planning…';
      return true;
    case 'dispatch':
      if (data.task) {
        for (const s of card.steps) if (s.running) s.running = false; // previous step finished
        card.steps.push({ task: data.task, model: data.model || null, running: true });
      }
      card.activity = data.task ? `Running: ${data.task}` : card.activity;
      return true;
    case 'dispatch_error':
    case 'guard_block': {
      const s = card.steps.find((x) => x.task === data.task && x.running);
      if (s) {
        s.running = false;
        s.blocked = true;
      }
      return true;
    }
    case 'synthesize':
      for (const s of card.steps) s.running = false;
      card.activity = 'Pulling it together…';
      return true;
    case 'answer_delta':
      card.answer += data.text ?? '';
      card.activity = null;
      return true;
    case 'run_done':
    case 'run_failed':
    case 'run_cancelled':
      card.status = data.status || (kind === 'run_done' ? 'done' : kind.slice(4));
      card.error = data.error || '';
      card.activity = null;
      for (const s of card.steps) s.running = false;
      return true;
    default:
      return false;
  }
}

/** Seal a card from its session snapshot (GET /v1/sessions/{run_id}) — the authoritative
 *  reload/terminal shape: query/answer/status/error/tasks. Used both when a live card's
 *  terminal event lands and when reconstructing cards after a page reload. */
export function sealCard(card, snap) {
  card.status = snap.status;
  card.answer = snap.answer || card.answer;
  card.error = snap.error || '';
  card.activity = null;
  if (Array.isArray(snap.tasks) && snap.tasks.length) {
    card.steps = snap.tasks.map((t) => ({
      task: t.task || t.name || '', model: t.model || null, running: false, blocked: !!t.blocked,
    }));
  } else {
    for (const s of card.steps) s.running = false;
  }
  return card;
}

/**
 * Recommendation chip (recommend_model tool_done payload) → display model for the inline card.
 * Everything the template shows is pre-formatted here so it's node --test'able:
 *   header      "agentic · optimizing for quality" (plain words, no jargon)
 *   candidates  [{rank, name, source, pct, evidence, thin, benchmarks, price, tools}]
 *               name = real upstream name; source = friendly provider, else the lane's plain
 *               word (never a naked host/port); pct = score as 0-100; thin = n<=1 evidence;
 *               benchmarks = names only (raw fact values never render); price = $/1k mono line.
 *   unscored    one quiet sentence, or '' when everything scored.
 */
export function recChip(chip = {}) {
  const header =
    [chip.category, chip.optimize && `optimizing for ${chip.optimize}`].filter(Boolean).join(' · ') ||
    'Model recommendation';
  const candidates = (chip.candidates ?? []).map((c, i) => {
    // provider is often null for YAML catalog entries; anything host-shaped is not a name.
    const friendly = c.provider && !/[:./]/.test(c.provider) ? providerLabel(c.provider) : '';
    const n = c.evidence_n ?? 0;
    return {
      rank: i + 1,
      name: prettyModel(c.model),
      source: friendly || (c.lane ? String(c.lane).toLowerCase() : ''),
      pct: Math.max(0, Math.min(100, Math.round((c.score ?? 0) * 100))),
      evidence: `from ${n} benchmark${n === 1 ? '' : 's'}`,
      thin: n <= 1,
      benchmarks: [...new Set((c.facts ?? []).map((f) => f.benchmark).filter(Boolean))].join(' · '),
      price:
        c.price_in == null && c.price_out == null
          ? ''
          : `$${priceFmt(c.price_in)} in · $${priceFmt(c.price_out)} out /1k`,
      tools: !!c.tools,
    };
  });
  const unscored = (chip.unscored ?? []).map(prettyModel).join(', ');
  return { header, candidates, unscored: unscored ? `No benchmark data for: ${unscored}` : '' };
}

/** Model-lever options from GET /v1/models ({data: [{id, provider, …}]}): stable order,
 *  deduped, labeled for humans. */
export function modelOptions(payload) {
  const seen = new Set();
  const out = [];
  for (const m of payload?.data ?? []) {
    if (!m?.id || seen.has(m.id)) continue;
    if (m.id === 'smart') continue; // the routing sentinel is its own radio, not a pinnable model
    seen.add(m.id);
    out.push({ id: m.id, label: prettyModel(m.id) });
  }
  return out;
}
