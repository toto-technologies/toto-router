// Org catalog-policy view-model — the pure bits behind the Governance "Model access" section
// (W1-C3). Shapes come from /v1/admin/org/catalog-policy. Unit-tested via `node --test`.
//
// The PUT FULL-REPLACES: omitting `models` clears the approved list server-side. So every save must
// carry the current approved set, even when switching to allow_all — otherwise flipping the mode
// would silently wipe the org's approvals. This is the C3 full-replace trap, the same class the C1
// cache Save guards (see tests/cache.test.js policyPassthrough).

/** Full-replace PUT body. ALWAYS sends `models` so a mode change never clears the approved set.
 *  Ids are de-duped and order-stable; empty/nullish entries drop. */
export function orgPolicyBody(mode, models) {
  const seen = new Set();
  const clean = [];
  for (const id of models ?? []) {
    if (id && !seen.has(id)) { seen.add(id); clean.push(id); }
  }
  return { mode, models: clean };
}

/** Catalog ids not yet approved — what the "Add model" picker offers. Order follows the catalog. */
export function addableModels(catalogIds, approved) {
  const have = new Set(approved ?? []);
  return (catalogIds ?? []).filter((id) => !have.has(id));
}

/** allowlist + no approved models = every request 403s. The one state that needs a loud warning. */
export const blocksAllTraffic = (mode, models) =>
  mode === 'allowlist' && (models ?? []).length === 0;

/** Order-independent set equality — is the edited approved list unchanged from the stored one? */
export function sameApproved(a, b) {
  if ((a?.length ?? 0) !== (b?.length ?? 0)) return false;
  const s = new Set(b ?? []);
  return (a ?? []).every((x) => s.has(x));
}
