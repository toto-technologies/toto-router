// Time formatting for the activity log. The gateway serializes `ts` as a UTC ISO-8601 STRING
// (gateway_events.ts_start is TEXT; see routes/admin_requests._iso), e.g. "2026-07-08T21:53:20+00:00"
// — NOT epoch seconds. These helpers parse that contract and are hardened to never throw on a
// malformed value: one bad timestamp must never abort the row render and freeze the whole table
// (which is exactly the bug this replaced — `new Date(ts*1000).toISOString()` threw RangeError).

/** Parse the wire `ts` to epoch ms. Accepts the canonical ISO string; tolerates a numeric epoch
 *  (seconds) for safety. Returns NaN for anything unparseable. */
export function tsToMs(ts) {
  if (ts == null) return NaN;
  if (typeof ts === 'number') return ts * 1000; // epoch seconds, defensive — API sends ISO strings
  return Date.parse(ts); // ISO-8601 → ms, NaN if malformed
}

/** Absolute local time, e.g. "Jul 8, 09:53pm". '—' if the timestamp is unparseable. */
export function fmtTime(ts) {
  const ms = tsToMs(ts);
  if (Number.isNaN(ms)) return '—';
  return new Date(ms).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  });
}

/** Relative age: "3s ago" / "5m ago" / "2h ago" / "4d ago". '' if unparseable. */
export function relTime(ts) {
  const ms = tsToMs(ts);
  if (Number.isNaN(ms)) return '';
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

/** A valid `datetime=""` attribute value, or '' when unparseable — NEVER throws (the old
 *  `.toISOString()` on an Invalid Date threw and froze the render). */
export function isoAttr(ts) {
  const ms = tsToMs(ts);
  return Number.isNaN(ms) ? '' : new Date(ms).toISOString();
}
