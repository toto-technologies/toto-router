// Pure helpers for board cards (canvas kind `board`) surfaced inside the chat thread: the
// resolved-item predicate, the section/board progress rollup that drives the n/n counters, and
// boardPreview — the capped flatten that keeps a 30-item board from dominating a chat message.
// Ported verbatim from frontend/src/lib/boardCard.js (itemResolved/progressOf/boardProgress) so
// the console board card and the canvas board render the same object; node --test'd here.

/**
 * Is this board item "resolved" (counts toward progress)? Type-driven:
 *   check   → ticked (done)
 *   verdict → a choice picked (stamp/overrule)
 *   qa      → a choice picked (pass/issues)
 *   input   → non-empty text
 *   note    → static signage, never counted (returns null so callers can exclude it)
 * @param {{ type: string, done?: boolean, choice?: string, note?: string }} it
 * @returns {boolean | null} true/false for actionable items, null for non-actionable (note)
 */
export function itemResolved(it) {
  if (it.type === 'check') return !!it.done;
  if (it.type === 'verdict' || it.type === 'qa') return !!it.choice;
  if (it.type === 'input') return !!(it.note && it.note.trim());
  return null; // note — not an actionable item
}

/**
 * Roll a flat list of items up to { done, total }, skipping non-actionable (note) items.
 * @param {{ type: string }[]} items
 * @returns {{ done: number, total: number }}
 */
export function progressOf(items) {
  let done = 0;
  let total = 0;
  for (const it of items ?? []) {
    const r = itemResolved(it);
    if (r === null) continue;
    total++;
    if (r) done++;
  }
  return { done, total };
}

/**
 * Whole-board progress: every section's items flattened, then rolled up.
 * @param {{ sections?: { items?: any[] }[] }} payload
 * @returns {{ done: number, total: number }}
 */
export function boardProgress(payload) {
  return progressOf((payload?.sections ?? []).flatMap((s) => s.items ?? []));
}

/**
 * Compact preview for the in-chat card: flatten the board's sections into a single ordered row
 * list, capping at `max` ITEM rows (section headers don't count against the cap) so a long board
 * stays a glanceable summary, not a wall. Server order is preserved — never re-sorted.
 * Each row is { kind:'section', title } or { kind:'item', type, title, resolved } where resolved
 * is true|false|null (null = a note, shown but never a checkbox glyph). `more` is the count of
 * item rows dropped past the cap ("+N more…").
 * @param {{ sections?: { title?: string, items?: any[] }[] }} payload
 * @param {number} max  max item rows to include (default 6)
 * @returns {{ rows: Array<object>, more: number }}
 */
export function boardPreview(payload, max = 6) {
  const rows = [];
  let shown = 0;
  let more = 0;
  for (const sec of payload?.sections ?? []) {
    const items = sec.items ?? [];
    if (!items.length) continue;
    // Only spend a header row if we still have item budget to show under it.
    if (shown < max) rows.push({ kind: 'section', title: sec.title || '' });
    for (const it of items) {
      if (shown < max) {
        rows.push({ kind: 'item', type: it.type, title: it.title || '', resolved: itemResolved(it) });
        shown++;
      } else {
        more++;
      }
    }
  }
  return { rows, more };
}
