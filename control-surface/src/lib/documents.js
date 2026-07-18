// Pure formatting for the Documents page — display strings only, no fetch, no DOM,
// so `node --test` covers it directly (same contract as chat.js / time.js).

/** Human-readable size: "812 B", "12.4 KB", "128 KB", "1.2 MB". '—' for garbage. */
export function humanSize(bytes) {
  const b = bytes == null ? NaN : Number(bytes); // null/undefined = unknown, not zero bytes
  if (!Number.isFinite(b) || b < 0) return '—';
  if (b < 1024) return `${Math.round(b)} B`;
  const kb = b / 1024;
  if (kb < 1024) return `${kb >= 100 ? Math.round(kb) : kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

/** The document's title, or a plain-language fallback — never a blank row. */
export function docTitle(doc) {
  const t = String(doc?.title ?? '').trim();
  return t || 'Untitled document';
}

/** Short handle for the source session (run ids are long hashes). '—' when absent. */
export function sessionLabel(runId) {
  const id = String(runId ?? '').trim();
  return id ? id.slice(0, 8) : '—';
}
