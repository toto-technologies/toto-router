// Routing-review game rules (chunk 6) — PURE logic, no DOM, no fetch, fully node --test'able.
// The page (routes/labeling) owns Svelte state, localStorage, and the API calls; everything that
// decides *what the game does* lives here so the rules are verifiable without a browser.

/** Session milestones — a burst fires the moment the session count reaches each. */
export const MILESTONES = [5, 10, 20];

/** A pause longer than this between judgments breaks the streak (honest "you're in flow" signal). */
export const STREAK_GAP_MS = 8000;

/** Daily goal for the progress ring. */
export const DEFAULT_GOAL = 20;

/** A fresh session: nothing judged, no streak, empty undo history. */
export function initialSession() {
  return { judged: 0, streak: 0, bestStreak: 0, lastAt: null, history: [], milestone: null };
}

/**
 * One judgment. Streak: +1 when this judgment lands within STREAK_GAP_MS of the last, else it
 * restarts at 1. `milestone` is set only on the transition INTO a milestone count (else null).
 * `entry` should carry whatever the page needs to undo ({card, verdict, corrected_label}); the
 * previous streak/timestamp are captured onto it so undo restores them exactly.
 */
export function applyJudge(s, entry, now) {
  const streak = s.lastAt != null && now - s.lastAt <= STREAK_GAP_MS ? s.streak + 1 : 1;
  const judged = s.judged + 1;
  return {
    judged,
    streak,
    bestStreak: Math.max(s.bestStreak, streak),
    lastAt: now,
    history: [...s.history, { ...entry, prevStreak: s.streak, prevLastAt: s.lastAt }],
    milestone: MILESTONES.includes(judged) ? judged : null,
  };
}

/**
 * Undo the last judgment: pop history, restore the pre-judgment streak clock. Returns the popped
 * entry so the page can put the card back (the server verdict stays until re-judged — re-posting
 * overwrites, which IS the undo mechanism). bestStreak is a high-water mark and never rolls back.
 */
export function applyUndo(s) {
  const entry = s.history[s.history.length - 1];
  if (!entry) return { state: s, entry: null };
  return {
    state: {
      judged: s.judged - 1,
      streak: entry.prevStreak,
      bestStreak: s.bestStreak,
      lastAt: entry.prevLastAt,
      history: s.history.slice(0, -1),
      milestone: null,
    },
    entry,
  };
}

/**
 * Drop one specific judgment by entry id — the failed-POST rollback. Unlike applyUndo it may hit
 * mid-history (later judgments landed while the POST was in flight). Streak is left alone: it's
 * ephemeral flow feedback, not an audited count. Milestone clears so a rollback never re-bursts.
 */
export function removeJudgment(s, id) {
  const i = s.history.findLastIndex((h) => h.id === id);
  if (i < 0) return s;
  return { ...s, judged: s.judged - 1, history: s.history.toSpliced(i, 1), milestone: null };
}

/** Skip = client-side only: the front card moves to the back of the deck. */
export function rotate(queue) {
  return queue.length > 1 ? [...queue.slice(1), queue[0]] : queue;
}

/** Goal-ring fill, clamped 0..1. */
export function goalProgress(count, goal = DEFAULT_GOAL) {
  return goal > 0 ? Math.min(1, count / goal) : 1;
}

// ---- correction-picker hotkeys -----------------------------------------------------------------

// Letters skip the game verbs (g/b/u/s) so an open picker never collides with them.
const PICKER_LETTERS = 'acdefhijklmnopqrtvwxyz';

/**
 * Assign a stable hotkey to each label: digits 1-9 then 0, then letters (reserved game keys
 * skipped). 12 labels -> 1..9, 0, a, c.
 * @param {Array<{label: string, desc?: string|null}>} labels
 * @returns {Array<{key: string, label: string, desc?: string|null}>}
 */
export function pickerKeys(labels) {
  const digits = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'];
  return labels.map((l, i) => ({
    key: i < digits.length ? digits[i] : (PICKER_LETTERS[i - digits.length] ?? ''),
    ...l,
  }));
}

/**
 * Dedup a confirmed verdict id. The day ring counts each REQUEST once per session — a re-judge
 * after undo overwrites the same verdict row server-side, so it must not increment again.
 * Increment day only when isNew.
 */
export function recordConfirmed(ids, id) {
  return ids.includes(id) ? { ids, isNew: false } : { ids: [...ids, id], isNew: true };
}

// ---- per-day persistence (pure over the raw string; the page owns localStorage) -----------------

/** Local calendar date as YYYY-MM-DD — the day key for the goal ring. */
export function todayStr(d = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** Parse a stored {date, count} blob; a different day (or garbage) reads as 0. */
export function dayCount(raw, today) {
  try {
    const v = JSON.parse(raw);
    return v && v.date === today ? Math.max(0, v.count | 0) : 0;
  } catch {
    return 0;
  }
}

/** Serialize today's count for storage. */
export function dayRaw(count, today) {
  return JSON.stringify({ date: today, count: Math.max(0, count) });
}

// ---- fallback label vocab ------------------------------------------------------------------------

// DRIFT RISK: mirrors toto_gateway/routing/labels.yaml (the classifier's closed vocabulary).
// The page prefers the live vocab from GET /v1/admin/org/routing-policy; this list is used only
// when that call is unavailable (e.g. the operator credential, which has no home org). If
// labels.yaml gains/loses a label, update this list too — a stale entry 400s on POST (unknown_label).
export const FALLBACK_LABELS = [
  { label: 'code_generation', desc: 'write, complete, or debug code, SQL, regex, or scripts' },
  { label: 'open_qa', desc: 'factual question answered from general knowledge' },
  { label: 'closed_qa', desc: 'answer strictly from context provided in the task' },
  { label: 'summarization', desc: 'condense given text into a shorter form' },
  { label: 'text_generation', desc: 'compose original prose, essays, memos, or reports' },
  { label: 'rewrite', desc: 'rephrase, reformat, or translate given text' },
  { label: 'classification', desc: 'assign categories, tags, or sentiment to inputs' },
  { label: 'extraction', desc: 'pull structured fields or values out of text' },
  { label: 'brainstorming', desc: 'open-ended idea generation or creative exploration' },
  { label: 'chatbot', desc: 'conversational exchange, greetings, casual replies' },
  { label: 'other', desc: 'none of the above fits' },
  { label: 'redact', desc: 'involves sensitive data to remove, mask, or anonymize' },
];
