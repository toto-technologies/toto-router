// Fail-policy wire-shape helpers (W2-C7). The gateway stores routing `fail_policy` as EITHER a
// scalar 'open'|'closed' (applies to every failure reason — the historical shape) OR a per-reason
// object keyed by these three reasons (routes/admin_routing.py FAIL_REASONS). The console edits a
// full 3-key matrix; these two convert to/from the API shape, collapsing to a scalar when the rows
// agree so the common case keeps the exact wire shape the API has always emitted.
export const FAIL_REASON_KEYS = ['classify_failed', 'breaker_open', 'policy_error'];

/** API value (scalar | object | null/undefined) → a full {reason: 'open'|'closed'} matrix.
 *  A scalar expands to every reason the same; a partial object defaults missing reasons to 'open'. */
export function toFailMatrix(fp) {
  if (typeof fp === 'string') return Object.fromEntries(FAIL_REASON_KEYS.map((k) => [k, fp]));
  const o = fp && typeof fp === 'object' ? fp : {};
  return Object.fromEntries(FAIL_REASON_KEYS.map((k) => [k, o[k] ?? 'open']));
}

/** Matrix → the smallest API value: a scalar when every reason agrees, else the per-reason object. */
export function failPolicyBody(matrix) {
  const vals = FAIL_REASON_KEYS.map((k) => matrix[k]);
  return vals.every((v) => v === vals[0]) ? vals[0] : { ...matrix };
}
