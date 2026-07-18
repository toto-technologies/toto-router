// Typed client for /v1/admin/* — one function per real endpoint, shapes mirror the FastAPI routes
// (toto_gateway/routes/admin_{tenancy,catalog,routing,usage,audit}.py). See ./types.d.ts.
//
// org scoping: a normal admin/owner is pinned to their OWN org server-side (org_id from the verified
// session), so `orgId` is OPTIONAL and only needed by the operator credential (which has no home org
// and must name one). Query-based routes take it as ?org_id; body-based routes fold it into the body,
// exactly as the routes accept it. Policy routes (org + team) also take an optional ?org_id for the
// operator case.
import { get, post, patch, put, del } from './client.js';

// ---- Teams (admin) --------------------------------------------------------------------------

/** @returns {Promise<import('./types').TeamsResponse>} */
export const listTeams = (orgId) => get('/v1/admin/teams', { query: { org_id: orgId } });

/** @returns {Promise<import('./types').Team>} */
export const createTeam = (name, orgId) =>
  post('/v1/admin/teams', { name, org_id: orgId });

export const renameTeam = (teamId, name, orgId) =>
  patch(`/v1/admin/teams/${teamId}`, { name }, { query: { org_id: orgId } });

export const deleteTeam = (teamId, orgId) =>
  del(`/v1/admin/teams/${teamId}`, { query: { org_id: orgId } });

// ---- Members + invitations ------------------------------------------------------------------

/** @returns {Promise<import('./types').MembersResponse>} */
export const listMembers = (orgId) => get('/v1/admin/members', { query: { org_id: orgId } });

/** @returns {Promise<import('./types').Invitation>} */
export const createInvitation = (email, role = 'member', orgId) =>
  post('/v1/admin/invitations', { email, role, org_id: orgId });

/** @returns {Promise<import('./types').InvitationsResponse>} */
export const listInvitations = (orgId) =>
  get('/v1/admin/invitations', { query: { org_id: orgId } });

/** The invited (already-signed-in) user redeems their token. */
export const acceptInvitation = (token) =>
  post(`/v1/admin/invitations/${token}/accept`);

/** Owner-only server-side (granting admin/owner). */
export const setMemberRole = (userId, role, orgId) =>
  post(`/v1/admin/members/${userId}/role`, { role }, { query: { org_id: orgId } });

export const removeMember = (userId, orgId) =>
  del(`/v1/admin/members/${userId}`, { query: { org_id: orgId } });

// ---- Identity -------------------------------------------------------------------------------

/** The signed-in caller (SPA boot probe). 401 when nobody is signed in.
 *  @returns {Promise<{user_id: string, email: string|null, email_verified: boolean, has_google: boolean, is_operator: boolean}>} */
export const getMe = () => get('/v1/auth/me');

/** Sign in — on 200 the gateway sets the httpOnly `toto_session` cookie same-origin (no token in JS).
 *  Throws ApiError(401, 'invalid_credentials') / 403 'email_unverified' / 429 on failure. */
export const login = (email, password) => post('/v1/auth/login', { email, password });

/** Create an account. Enumeration-safe: returns a generic 200 whether or not the email is new.
 *  403 'invite_required' when this deploy gates registration; 400 on weak password / bad email. */
export const register = (email, password, inviteCode) =>
  post('/v1/auth/register', { email, password, invite_code: inviteCode || undefined });

/** Revoke the session + clear the cookie (204). */
export const logout = () => post('/v1/auth/logout');

// ---- Multi-org (W2-C1): the caller's memberships + the active-org switch --------------------

/** Every org the caller belongs to, plus which one is currently active (resolved server-side).
 *  @returns {Promise<{memberships: Array<{org_id: string, org_name: string, role: string}>, active_org_id: string|null}>} */
export const getMemberships = () => get('/v1/auth/memberships');

/** Switch the current session's active org (403 for a non-member, 400 without a session cookie).
 *  Every org-scoped control (allowlist, zero-retention, routing, RBAC) follows this switch.
 *  @returns {Promise<{ok: boolean, active_org_id: string}>} */
export const setActiveOrg = (orgId) => post('/v1/auth/active-org', { org_id: orgId });

// ---- Org (owner) ----------------------------------------------------------------------------

/** @returns {Promise<import('./types').Org>} */
export const getOrg = (orgId) => get('/v1/admin/org', { query: { org_id: orgId } });

export const renameOrg = (name, orgId) =>
  patch('/v1/admin/org', { name }, { query: { org_id: orgId } });

// Zero-retention (W1-C4): the org-wide privacy switch. getOrg already returns `zero_retention` on
// the org row, so reads piggyback on it; this only writes. Owner/admin-gated, org-scoped server-side.
export const setZeroRetention = (on, orgId) =>
  put('/v1/admin/org/zero-retention', { zero_retention: on }, { query: { org_id: orgId } });

// Content-plane retention (W3-C6): per-sink retention DAYS over user-invoked product storage
// (documents + embeddings, explicit memory facts). 0 = keep forever, per sink. Owner/admin-gated;
// GET is auditor-readable. `.../retention/run` triggers one sweep now and returns deleted totals.
export const getOrgRetention = (orgId) =>
  get('/v1/admin/org/retention', { query: { org_id: orgId } });
export const setOrgRetention = (policy, orgId) =>
  put('/v1/admin/org/retention', policy, { query: { org_id: orgId } });
export const runOrgRetention = (orgId) =>
  post('/v1/admin/org/retention/run', null, { query: { org_id: orgId } });

// ---- API tokens (per-user bearer, cookie-session owned) -------------------------------------
// toto_gateway/routes/tokens.py — a normally-logged-in user owns these; the operator token can't
// (403 operator_cannot_own_tokens). The secret comes back ONCE from mint and is never re-shown.

/** Mint a token; `orgId` (W2-C1) binds it to one of the caller's orgs, else it resolves to the
 *  default (oldest) membership. `expiresInDays` (W2-C3) is clamped down to the org lifetime cap.
 *  403 not_a_member for a foreign org_id.
 *  @returns {Promise<{token: string, token_id: string, label: string, org_id: string|null}>} — `token` shown once. */
export const mintToken = (label, orgId, expiresInDays) =>
  post('/v1/tokens', { label, org_id: orgId || undefined,
                       expires_in_days: expiresInDays || undefined });

/** @returns {Promise<{tokens: Array<{token_id: string, label: string, created?: number, created_at?: number, last_used: number|null, expires_at?: number, rotated_at?: number|null, org_id: string|null, org_name: string|null}>}>} */
export const listTokens = () => get('/v1/tokens');

/** 204 on success; 404 token_not_found for someone else's / a missing id. */
export const revokeToken = (tokenId) => del(`/v1/tokens/${tokenId}`);

/** Rotate one of your tokens (W2-C3): a NEW secret, shown ONCE; the old works for the org grace
 *  window then dies. 404 for someone else's / a missing id.
 *  @returns {Promise<{token: string, token_id: string, old_token_id: string, old_expires_at: number|null}>} */
export const rotateToken = (tokenId) => post(`/v1/tokens/${tokenId}/rotate`);

// ---- Org token administration (W2-C3) -------------------------------------------------------
// toto_gateway/routes/admin_tokens.py + admin_tenancy.py. Service tokens are org-owned CI creds;
// bulk-revoke + the compliance list + the lifetime/grace policy are org-level admin surfaces.

/** @returns {Promise<{org_id: string, tokens: Array<{token_id: string, label: string, created_at: number, last_used: number|null, expires_at: number, rotated_at: number|null}>}>} */
export const listServiceTokens = (orgId) => get('/v1/admin/service-tokens', { query: { org_id: orgId } });

/** Mint an org-owned service token — the `token` is shown ONCE. Owner/admin only.
 *  @returns {Promise<{token: string, token_id: string, label: string, org_id: string}>} */
export const mintServiceToken = (label, orgId, expiresInDays) =>
  post('/v1/admin/service-tokens', { label, org_id: orgId,
                                     expires_in_days: expiresInDays || undefined });

export const revokeServiceToken = (tokenId, orgId) =>
  del(`/v1/admin/service-tokens/${tokenId}`, { query: { org_id: orgId } });

/** Bulk-revoke. `{ user_id }` = one member (admin); `{ org_wide: true }` = the whole org (owner).
 *  @returns {Promise<{ok: boolean, org_id: string, counts: Record<string, number>}>} */
export const revokeAllTokens = (body, orgId) =>
  post('/v1/admin/tokens/revoke-all', { ...body, org_id: orgId });

/** The org compliance list: every live api + service credential with owner/label, purpose, binding,
 *  created, expires, last_used, rotated_at. Auditor-readable.
 *  @returns {Promise<{org_id: string, credentials: Array<object>}>} */
export const listOrgTokens = (orgId) => get('/v1/admin/tokens', { query: { org_id: orgId } });

/** @returns {Promise<{max_token_lifetime_days: number, token_rotation_grace_minutes: number}>} */
export const getTokenPolicy = (orgId) => get('/v1/admin/org/token-policy', { query: { org_id: orgId } });

export const setTokenPolicy = (maxDays, graceMinutes, orgId) =>
  put('/v1/admin/org/token-policy',
      { max_token_lifetime_days: maxDays, token_rotation_grace_minutes: graceMinutes },
      { query: { org_id: orgId } });

// ---- Catalog policy (per team) --------------------------------------------------------------

/** @returns {Promise<import('./types').CatalogPolicy>} */
export const getCatalogPolicy = (teamId, orgId) =>
  get(`/v1/admin/teams/${teamId}/catalog-policy`, { query: { org_id: orgId } });

/** Full-replace. @param {import('./types').CatalogPolicyInput} policy */
export const putCatalogPolicy = (teamId, policy, orgId) =>
  put(`/v1/admin/teams/${teamId}/catalog-policy`, policy, { query: { org_id: orgId } });

// ---- Org catalog policy (W1-C3 governance: allow_all | allowlist) ---------------------------
// toto_gateway/routes/admin_catalog.py — the ORG-DEFAULT catalog governance mode, distinct from the
// per-team catalog policy above. allowlist = deny-by-default: only the approved `models` (plus the
// org's catalog adoptions) resolve; anything else 403s (catalog.model_denied) before the wire. No
// row → allow_all, version 0. PUT full-replaces: omitting `models` CLEARS the approved list, so
// always send the current list. Scoped admin is server-pinned to their org; the operator names
// ?org_id=. Invalid mode → 422 invalid_mode.

/** @returns {Promise<import('./types').OrgCatalogPolicy>} */
export const getOrgCatalogPolicy = (orgId) =>
  get('/v1/admin/org/catalog-policy', { query: { org_id: orgId } });

/** Full-replace. @param {import('./types').OrgCatalogPolicyInput} policy */
export const putOrgCatalogPolicy = (policy, orgId) =>
  put('/v1/admin/org/catalog-policy', policy, { query: { org_id: orgId } });

// ---- Routing policy (per team) --------------------------------------------------------------

/** The EFFECTIVE routing view (labels table + optimize + custom_labels).
 *  @returns {Promise<import('./types').RoutingPolicyView>} */
export const getRoutingPolicy = (teamId, orgId) =>
  get(`/v1/admin/teams/${teamId}/routing-policy`, { query: { org_id: orgId } });

/** Full-replace the overlay. @param {import('./types').RoutingPolicyInput} policy */
export const putRoutingPolicy = (teamId, policy, orgId) =>
  put(`/v1/admin/teams/${teamId}/routing-policy`, policy, { query: { org_id: orgId } });

/** The ORG-DEFAULT routing view — the policy that applies to a caller with NO team (a personal-org
 *  owner, the pi / API-token case). This is what an owner's own smart-routed traffic resolves. */
export const getOrgRoutingPolicy = (orgId) =>
  get('/v1/admin/org/routing-policy', { query: { org_id: orgId } });

/** Full-replace the org-default overlay. @param {import('./types').RoutingPolicyInput} policy */
export const putOrgRoutingPolicy = (policy, orgId) =>
  put('/v1/admin/org/routing-policy', policy, { query: { org_id: orgId } });

// ---- Model catalog (the running catalog — id/tier/via/price/context/residency) --------------

/** The full running catalog the Section B table renders (public /v1/models, Toto-extended).
 *  @returns {Promise<import('./types').ModelsResponse>} */
export const listModels = () => get('/v1/models');

/** The EFFECTIVE catalog for the scope being EDITED — shipped base + that scope's adoptions.
 *  The one legitimate source for pickers that WRITE model ids (routing task-type bindings,
 *  governance approvals): /v1/models is pinned to the caller's own identity (empty adoptions
 *  under the operator credential, blind to the org/team switcher) and getCatalogModels is the
 *  shipped base only. Rows are /v1/admin/catalog/models-shaped; adopted rows carry
 *  source: 'adopted'. @returns {Promise<{scope_key: string, models: Array<object>}>} */
export const getEffectiveModels = ({ teamId, orgId } = {}) =>
  get('/v1/admin/catalog/effective-models', { query: { team_id: teamId, org_id: orgId } });

/** Every catalog entry with full admin detail (aliases, endpoint/base_url, key env, upstream ref,
 *  fine_tuned flag, source fragment) — the provider-module view of the Catalog page.
 *  @returns {Promise<import('./types').AdminCatalogResponse>} */
export const getCatalogModels = () => get('/v1/admin/catalog/models');

/** Manual price overrides — platform rows + the caller scope's rows, each tagged scope_key.
 *  Prices come back in BOTH scales (per-Mtok for humans, per-1k as dispatch bills).
 *  @returns {Promise<{overrides: Array<object>}>} */
export const getPriceOverrides = () => get('/v1/admin/catalog/price-overrides');

/** Upsert a manual price for one model in the caller's scope (operator → the platform layer).
 *  Per-Mtok in; the server owns the exact ÷1000. `free: true` is the explicit confirm the
 *  server demands before accepting a zero-for-both price (silent-$0 money-bug guard).
 *  @returns {Promise<{override: object, known: boolean}>} */
export const putPriceOverride = (modelId, { inMtok, outMtok, free } = {}) =>
  put(`/v1/admin/catalog/price-overrides/${encodeURIComponent(modelId)}`, {
    prompt_usd_per_mtok: inMtok, completion_usd_per_mtok: outMtok,
    ...(free ? { free: true } : {}),
  });

/** Remove the caller scope's override — scope-pinned (a platform row 404s for an org admin,
 *  which is why the UI disables that button instead). */
export const deletePriceOverride = (modelId) =>
  del(`/v1/admin/catalog/price-overrides/${encodeURIComponent(modelId)}`);

/** Latest cyclical availability probe: per provider base_url, vanished (declared ids gone
 *  upstream) + undeclared (live ids the catalog doesn't declare) + fetch error. Empty until
 *  the first scheduled tick. @returns {Promise<{checked_at: number|null, providers: object}>} */
export const getCatalogAvailability = () => get('/v1/admin/catalog/availability');

/** "Check now" — probe every keyed provider immediately (admin-gated; the scheduled probe
 *  rides the inventory cadence otherwise). Returns the same shape as the GET. */
export const probeCatalogAvailability = () => post('/v1/admin/catalog/availability');

/** Fireworks account ⇄ catalog reconciliation. Always 200 — a missing key or an upstream failure
 *  comes back as {key_present, error} for the panel to render calmly, never as an HTTP error.
 *  @returns {Promise<import('./types').FireworksSync>} */
export const getFireworksSync = () => get('/v1/admin/catalog/sync/fireworks');

/** Everything OpenRouter offers, reconciled against the catalog (cataloged/catalog_id flags).
 *  Always 200; `error` string on upstream failure; works keyless (public endpoint).
 *  @returns {Promise<import('./types').OpenRouterDiscovery>} */
export const getOpenRouterDiscovery = () => get('/v1/admin/catalog/discovery/openrouter');

/** Everything the Fireworks platform offers (deprecated/embedding models pre-filtered),
 *  reconciled against the catalog. Always 200; key_present:false when FIREWORKS_API_KEY is unset.
 *  @returns {Promise<import('./types').FireworksDiscovery>} */
export const getFireworksDiscovery = () => get('/v1/admin/catalog/discovery/fireworks');

// ---- Catalog adoptions (one-click Add to Catalog, scoped to the caller's org) -----------------

/** Adopt an upstream model into the caller's catalog — the server derives the id and all facts
 *  from its own inventory; live immediately, no redeploy. 201 (or 200 on idempotent re-adopt)
 *  → {entry}. 400 unknown slug, 403 role.
 *  @param {'openrouter'|'fireworks'} source
 *  @returns {Promise<{entry: import('./types').CatalogEntry}>} */
export const createAdoption = (source, slug) =>
  post('/v1/admin/catalog/adoptions', { source, slug });

/** Remove an adopted model (base-catalog entries can't be removed). 200/204; 404 unknown id. */
export const deleteAdoption = (id) => del(`/v1/admin/catalog/adoptions/${id}`);

/** The caller-scope adoptions — what "added by you" means on the Library.
 *  @returns {Promise<{adoptions: Array<{id: string, upstream_model: string, provider: string}>}>} */
export const listAdoptions = () => get('/v1/admin/catalog/adoptions');

// ---- Benchmarks -----------------------------------------------------------------------------
// Compatibility exports. The bounded provider-inventory-aware client lives in benchmarks.js.
export {
  getBenchmarkModels,
  getBenchmarkModel,
  getBenchmarkCoverage,
  refreshBenchmarks,
  getBenchmarkAliases,
} from './benchmarks.js';

// ---- Tuning (experimentation platform: datasets → jobs → models → evals + the ladder) ---------
// toto_gateway/routes/admin_tuning.py. Member-read like benchmarks, but data is ORG-SCOPED — the
// operator must name ?org_id (org_id_required otherwise); a normal admin is server-pinned.

/** @returns {Promise<{datasets: Array<{id: string, task: string, generator: string, seed: number, train_examples: number, eval_examples: number, train_tokens: number, source_manifest: string, path: string, notes: string, created_at: number}>}>} */
export const getTuningDatasets = (orgId) =>
  get('/v1/admin/tuning/datasets', { query: { org_id: orgId } });

/** @returns {Promise<{jobs: Array<{id: string, dataset_id: string, method: string, base_model: string, hyperparams: string, provider: string, provider_job: string, state: string, cost_estimate_usd: number|null, cost_actual_usd: number|null, created_at: number, completed_at: number|null}>}>} */
export const getTuningJobs = (orgId) =>
  get('/v1/admin/tuning/jobs', { query: { org_id: orgId } });

/** @returns {Promise<{models: Array<{id: string, job_id: string, dataset_id: string, catalog_id: string, serving: string, created_at: number}>}>} */
export const getTuningModels = (orgId) =>
  get('/v1/admin/tuning/models', { query: { org_id: orgId } });

/** Rates are 0..1 fractions; mean_para_diff is lower-is-better.
 *  @returns {Promise<{evals: Array<{id: string, dataset_id: string, model_ref: string, label: string, n: number, valid_rate: number, applied_rate: number, match_rate: number, mean_para_diff: number, metrics: string, created_at: number}>}>} */
export const getTuningEvals = (orgId) =>
  get('/v1/admin/tuning/evals', { query: { org_id: orgId } });

/** @returns {Promise<{experiments: Array<{id: string, title: string, method: string, hypothesis: string, cost_estimate: string, status: 'planned'|'running'|'done', result: string, sort_order: number, created_at: number}>}>} */
export const getTuningExperiments = (orgId) =>
  get('/v1/admin/tuning/experiments', { query: { org_id: orgId } });

/** One model version's flat dataset→job→model chain + its eval rows (404 model_not_found). */
export const getTuningLineage = (modelId, orgId) =>
  get('/v1/admin/tuning/lineage', { query: { model_id: modelId, org_id: orgId } });

// ---- Usage / billing ------------------------------------------------------------------------

/**
 * Usage rollup for the caller's org, sliced by dimensions + time window.
 * @param {{groupBy?: string[], start?: string, end?: string, granularity?: 'day'|'hour', orgId?: string}} [opts]
 * @returns {Promise<import('./types').UsageResponse>}
 */
export const getUsage = ({ groupBy, start, end, granularity, orgId } = {}) =>
  get('/v1/admin/usage', {
    query: {
      group_by: groupBy?.length ? groupBy.join(',') : undefined,
      start,
      end,
      granularity,
      org_id: orgId,
    },
  });

/**
 * Stripe-shaped billing records for one period (export seam — no invoice created).
 * @param {{period: string, format?: string, orgId?: string}} opts  period = 'YYYY-MM'
 * @returns {Promise<import('./types').UsageExport>}
 */
export const exportUsage = ({ period, format = 'stripe', orgId } = {}) =>
  get('/v1/admin/usage/export', { query: { period, format, org_id: orgId } });

// ---- Budgets (W2-C5) ------------------------------------------------------------------------

/** Org-default + per-team monthly budgets with this month's spend and pct. */
export const getBudgets = (orgId) => get('/v1/admin/budgets', { query: { org_id: orgId } });

/**
 * Set a budget: org-default (no teamId/userId), a team budget (teamId), or a member budget (userId).
 * teamId and userId are mutually exclusive. thresholds optional (defaults 0.5/0.8/1.0).
 */
export const putBudget = ({ teamId, userId, monthlyUsd, action, thresholds, orgId } = {}) =>
  put('/v1/admin/budgets',
    { team_id: teamId || undefined, user_id: userId || undefined,
      monthly_usd: monthlyUsd, action, thresholds },
    { query: { org_id: orgId } });

/** Clear a member's cap so they re-inherit the team/org-default budget. */
export const deleteMemberBudget = ({ userId, orgId } = {}) =>
  del('/v1/admin/budgets', { query: { user_id: userId, org_id: orgId } });

/** Same-origin href for the chargeback CSV download (cookie auth rides the request). */
export const budgetsExportHref = ({ month, orgId } = {}) => {
  const q = new URLSearchParams({ month });
  if (orgId) q.set('org_id', orgId);
  return `/v1/admin/budgets/export?${q}`;
};

/**
 * The org's caching ledger over [from, to] (unix-seconds, both optional): per-model read savings,
 * write premium, and net — the number behind "caching saved your org $X". 503 no_trace_db when
 * this deploy has no trace database, exactly like the sibling usage endpoints.
 * @param {{from?: number, to?: number, orgId?: string}} [opts]
 * @returns {Promise<import('./types').CacheSavingsResponse>}
 */
export const getCacheSavings = ({ from, to, orgId } = {}) =>
  get('/v1/admin/usage/cache-savings', { query: { from, to, org_id: orgId } });

/**
 * A caching-health time series for the caller's org over [from, to] (unix-seconds, both optional):
 * per day/hour bucket the request count, prompt/cached/write token totals, warm-hold turn count,
 * and cache hit rate (0.0–1.0). 503 no_trace_db degradation, exactly like the sibling endpoints.
 * @param {{from?: number, to?: number, granularity?: 'day'|'hour', orgId?: string}} [opts]
 * @returns {Promise<import('./types').CacheHealthResponse>}
 */
export const getCacheHealth = ({ from, to, granularity, orgId } = {}) =>
  get('/v1/admin/usage/cache-health', { query: { from, to, granularity, org_id: orgId } });

/**
 * Live provider health for the Overview panel: per provider its circuit-breaker state
 * (closed|open|half-open, retry_in seconds while open), windowed traffic (requests, errors,
 * error_rate, p50/p95/avg latency ms — nulls when the deploy has no trace DB), and served model ids.
 * @param {{window?: number, orgId?: string}} [opts]  window = lookback seconds (default 1h, max 7d)
 * @returns {Promise<import('./types').ProviderHealthResponse>}
 */
export const getProviderHealth = ({ window, orgId } = {}) =>
  get('/v1/admin/providers/health', { query: { window, org_id: orgId } });

/**
 * Gateway-overhead latency summary for the caller's org over the last `days` (default 7, 1..90):
 * the ms the gateway itself adds on top of the model (p50/p95 overhead), per-stage timings
 * (classify/plan/upstream avg+p95), and the fast-path share (requests that skipped classification).
 * An empty window comes back as requests:0 with null stats + share 0.0 — a real, non-error state.
 * @param {{days?: number, orgId?: string}} [opts]
 * @returns {Promise<{org_id: string, days: number, start: string, requests: number, overhead_ms: {p50: number|null, p95: number|null}, stages: Record<'classify_ms'|'plan_ms'|'upstream_ms', {avg: number|null, p95: number|null}>, fast_path: {requests: number, share: number}}>}
 */
export const getLatencySummary = ({ days, orgId } = {}) =>
  get('/v1/admin/latency/summary', { query: { days, org_id: orgId } });

// ---- Audit ----------------------------------------------------------------------------------

/**
 * This org's audit trail, newest first. `from`/`to` are unix-seconds; the route reads `from` (alias).
 * @param {{action?: string, actor?: string, from?: number, to?: number, limit?: number, offset?: number}} [opts]
 * @returns {Promise<import('./types').AuditResponse>}
 */
export const listAudit = ({ action, actor, from, to, limit = 50, offset = 0 } = {}) =>
  get('/v1/admin/audit', { query: { action, actor, from, to, limit, offset } });

// ---- Activity (per-request decision log) ----------------------------------------------------

/**
 * Per-request routing decision trail, newest first. The server scopes it by the cookie session — a
 * member sees only their own requests, an admin the whole org. Metadata only: NO prompt/response
 * content. `from`/`to` are unix-seconds; `user` is admin-only.
 * @param {{from?: number, to?: number, model?: string, label?: string, user?: string, limit?: number, offset?: number}} [opts]
 * @returns {Promise<{requests: Array<object>, next_offset: number|null}>}
 */
export const getRequests = ({ from, to, model, label, user, limit = 50, offset = 0 } = {}) =>
  get('/v1/admin/requests', { query: { from, to, model, label, user, limit, offset } });

/**
 * One request's full detail: the metadata trail plus (when content logging is on and this was a
 * served turn still within retention) the actual prompt + response. Cookie-scoped like the list.
 * @param {string} id  the stable row id from getRequests
 * @returns {Promise<{request: object, content_available: boolean, prompt?: Array<{role: string, content: any}>, response?: string}>}
 */
export const getRequestDetail = (id) => get(`/v1/admin/requests/${id}`);

// ---- Activity analytics (org aggregate bundle + LLM governance insights) ---------------------
// toto_gateway/routes/admin_analytics.py. Metadata only — aggregate numbers, never content.

/**
 * Activity aggregate bundle for the caller's org over [start, end) (default: last 30 days).
 * @param {{start?: string, end?: string, orgId?: string}} [opts]  ISO date strings
 * @returns {Promise<{org_id: string, start: string|null, end: string|null, totals: object, by_label: any[], by_label_day: any[], by_model: any[], by_user: any[], by_user_truncated: boolean}>}
 */
export const getActivityAnalytics = ({ start, end, orgId } = {}) =>
  get('/v1/admin/analytics/activity', { query: { start, end, org_id: orgId } });

/**
 * LLM governance insights over the same window's AGGREGATE numbers. Server-cached ~15 min per
 * (org, window); `refresh` bypasses. Degrades, never 500s: a failure is 200 with
 * `insights: null` + an honest `error` string.
 * @param {{start?: string, end?: string, refresh?: boolean, orgId?: string}} [opts]
 * @returns {Promise<{org_id: string, model: string, generated_at: string, cached: boolean, insights: {headline: string, insights: Array<{finding: string, evidence: string}>, recommendations: string[]}|null, error: string|null}>}
 */
export const getActivityInsights = ({ start, end, refresh, orgId } = {}) =>
  get('/v1/admin/analytics/insights', {
    query: { start, end, refresh: refresh ? 'true' : undefined, org_id: orgId },
  });

/**
 * Per-model drill-down: token-type split + per-task-type breakdown for ONE real upstream model.
 * `model` is the real name shown in by_model (a catalog id also resolves). Unknown/quiet models
 * return zeroed totals + empty by_label, never 404.
 * @param {{model: string, start?: string, end?: string, orgId?: string}} opts
 * @returns {Promise<{org_id: string, model: string, catalog_ids: string[], totals: object, by_label: Array<{label: string, requests: number, tokens_prompt: number, tokens_completion: number, tokens_cached: number, tokens: number, cost_usd: number, savings_usd: number, share: number}>}>}
 */
export const getModelDrilldown = ({ model, start, end, orgId }) =>
  get('/v1/admin/analytics/model', { query: { model, start, end, org_id: orgId } });

// ---- Labeling (routing-verdict plane — the gamified routing-review page) ----------------------
// toto_gateway/routes/admin_labeling.py. All admin-role; org_id only needed by the operator.

/**
 * Recent routing decisions this judge hasn't verdicted yet, newest first.
 * @param {{orgId?: string, limit?: number}} [opts]
 * @returns {Promise<{org_id: string, queue: Array<{request_id: string, ts: number, label: string, label_desc: string|null, bound_model: string|null, model_served: string|null, query_text: string}>}>}
 */
export const getLabelingQueue = ({ orgId, limit = 50 } = {}) =>
  get('/v1/admin/labeling/queue', { query: { org_id: orgId, limit } });

/**
 * Record a verdict (idempotent per request+judge — re-posting overwrites; that's how undo works).
 * A 'good' verdict must not carry a corrected_label; a correction must be in the label vocab.
 * @param {{requestId: string, verdict: 'good'|'bad', correctedLabel?: string|null, orgId?: string}} opts
 * @returns {Promise<{verdict: object}>}
 */
export const postLabelVerdict = ({ requestId, verdict, correctedLabel, orgId }) =>
  post('/v1/admin/labeling/verdict',
    { request_id: requestId, verdict, corrected_label: correctedLabel || undefined },
    { query: { org_id: orgId } });

/**
 * Rollups for the review page: totals, today, good/bad split, per-label bad counts, judges.
 * @returns {Promise<{org_id: string, judged_total: number, judged_today: number, good: number, bad: number, bad_by_label: Record<string, number>, distinct_judges: number}>}
 */
export const getLabelingStats = (orgId) =>
  get('/v1/admin/labeling/stats', { query: { org_id: orgId } });

// ---- Org-wide inference provider keys (BYOK) --------------------------------------------------
// toto_gateway/routes/org_credentials.py. Org-wide runner keys (OpenRouter/Fireworks): every
// member's traffic runs under them unless the member set a personal key. Encrypted at rest,
// never echoed — responses carry configured + last4 only.

/** @returns {Promise<{org_id: string, keys: Array<{provider: string, label: string, powers: string, configured: boolean, last4: string|null}>}>} */
export const getProviderKeys = (orgId) =>
  get('/v1/admin/provider-keys', { query: { org_id: orgId } });

/** Store (or replace) an org-wide provider key. Owner-only (403 otherwise); 503 when this deploy
 *  has no key-encryption secret configured.
 *  @returns {Promise<{provider: string, configured: boolean, last4: string}>} */
export const putProviderKey = (provider, key, orgId) =>
  put(`/v1/admin/provider-keys/${provider}`, { key }, { query: { org_id: orgId } });

/** Owner-only. @returns {Promise<{provider: string, configured: boolean, last4: null}>} */
export const deleteProviderKey = (provider, orgId) =>
  del(`/v1/admin/provider-keys/${provider}`, { query: { org_id: orgId } });

// ---- Org OIDC SSO (owner-gated) --------------------------------------------------------------
// toto_gateway/routes/admin_sso.py. The client secret is write-only — GET returns has_secret only,
// never the secret. PUT with no client_secret keeps the stored one (metadata-only edit).

/** @returns {Promise<{org_id: string, configured: boolean, issuer: string, client_id: string,
 *  domains: string[], sso_required: boolean, has_secret: boolean, scim_enabled: boolean,
 *  scim_group_role_map: Record<string,string>, scim_has_token: boolean, scim_base_url: string}>} */
export const getOrgSSO = (orgId) => get('/v1/admin/org/sso', { query: { org_id: orgId } });

/** Owner-only. 422 on a bad issuer URL / empty domains / missing first-time secret; 409 when a
 *  domain is already claimed by another org; 503 when this deploy has no encryption secret.
 *  Also persists scim_enabled + scim_group_role_map (W2-C2).
 *  @returns {Promise<{configured: boolean, has_secret: boolean, discovery_ok: boolean|null}>} */
export const putOrgSSO = (cfg, orgId) =>
  put('/v1/admin/org/sso', cfg, { query: { org_id: orgId } });

// ---- Org storage connector (BYOS) ------------------------------------------------------------
// toto_gateway/routes/admin_storage.py. The org's private S3-compatible bucket for object writes
// (documents, artifacts). The bucket secret is write-only: stored encrypted, never returned —
// the GET carries has_s3_secret only.

/** @returns {Promise<{org_id: string, configured: boolean, enabled: boolean, s3_endpoint: string,
 *  s3_bucket: string, s3_region: string, s3_access_key: string, s3_force_path_style: boolean,
 *  has_s3_secret: boolean, last_test: number|null, last_error: string|null}>} */
export const getOrgStorage = (orgId) => get('/v1/admin/org/storage', { query: { org_id: orgId } });

/** Admin-only. 422 when enabling an incomplete connector; 503 when this deploy has no
 *  encryption secret. Omit s3_secret to keep the stored one. */
export const putOrgStorage = (cfg, orgId) =>
  put('/v1/admin/org/storage', cfg, { query: { org_id: orgId } });

/** Admin-only. Round-trips a probe object (put→get→delete) against the SUBMITTED connector —
 *  the stored secret is used when s3_secret is omitted. @returns {Promise<{ok: boolean, error: string|null}>} */
export const testOrgStorage = (cfg, orgId) =>
  post('/v1/admin/org/storage/test', cfg, { query: { org_id: orgId } });

/** Owner-only. Mint/rotate the org's SCIM bearer — shown ONCE. @returns {Promise<{token: string, base_url: string}>} */
export const generateScimToken = (orgId) =>
  post('/v1/admin/org/scim-token', {}, { query: { org_id: orgId } });

/** Owner-only. Revoke the org's SCIM bearer (204). */
export const revokeScimToken = (orgId) =>
  del('/v1/admin/org/scim-token', { query: { org_id: orgId } });

// ---- Org audit export (W2-C4: the SIEM/compliance surface) -----------------------------------
// toto_gateway/routes/admin_audit_export.py. Admin configures where hash-chained JSONL audit
// batches ship (gateway store and/or a customer S3 bucket), cadence, retention. The S3 secret is
// write-only: GET returns has_s3_secret only; PUT with an empty s3_secret keeps the stored one.
// Batches carry the sha256/prev_sha256 hash chain an auditor verifies against. Auditor-readable.

/** @returns {Promise<{org_id: string, configured: boolean, enabled: boolean, cadence_hours: number,
 *  retention_days: number, destination: 'gateway'|'s3'|'both', s3_endpoint: string, s3_bucket: string,
 *  s3_region: string, s3_access_key: string, s3_prefix: string, has_s3_secret: boolean,
 *  last_run: number|null, last_error: string|null}>} */
export const getAuditExport = (orgId) => get('/v1/admin/org/audit-export', { query: { org_id: orgId } });

/** Admin-only. Omit s3_secret to keep the stored one. 422 when an s3/both destination is incomplete
 *  or missing its secret; 503 when this deploy has no credential-encryption secret. */
export const putAuditExport = (cfg, orgId) =>
  put('/v1/admin/org/audit-export', cfg, { query: { org_id: orgId } });

/** The hash-chain listing an auditor verifies against — each batch carries sha256 + prev_sha256 +
 *  rows + a same-origin `download` path. Newest-first per stream. `stream` optionally filters.
 *  @returns {Promise<{org_id: string, batches: Array<{stream: string, batch: number, sha256: string,
 *  prev_sha256: string, rows: number, created_at: number, object_key: string, download: string}>}>} */
export const listAuditBatches = (orgId, stream) =>
  get('/v1/admin/org/audit-export/batches', { query: { org_id: orgId, stream } });

/** Trigger an export cycle now (admin). 404 not_configured; 502 export_failed carries the reason.
 *  @returns {Promise<object>} the run summary (per-stream batch/rows counts). */
export const runAuditExport = (orgId) =>
  post('/v1/admin/org/audit-export/run', {}, { query: { org_id: orgId } });

// ---- Egress allowlist (W2-C6: read-only network-egress posture) -------------------------------
// toto_gateway/routes/admin_egress.py. The host→source allowlist the gateway may reach, the
// enforce/observe mode, and 7-day observed/blocked counts. There is NO write route by design — the
// allowlist is derived from deploy config at startup (edit config + restart). Auditor-readable.

/** @returns {Promise<{enforce: boolean, hosts: Array<{host: string, source: string}>,
 *  observed_violations: number, blocked: number}>} */
export const getEgress = () => get('/v1/admin/egress');

// ---- Org observability (provider org-admin keys + derived insights) --------------------------
// toto_gateway/routes/admin_observability.py. Providers here: 'anthropic' | 'openai' (org ADMIN
// keys, distinct from the BYOK inference credentials). Keys are write-only: stored encrypted,
// never echoed back — reads return {configured, last4, org_name} only.

/** Which provider admin keys this org has stored.
 *  @returns {Promise<{org_id: string, keys: Record<string, {configured: boolean, last4?: string, org_name?: string|null}>}>} */
export const getObservabilityKeys = (orgId) =>
  get('/v1/admin/observability/keys', { query: { org_id: orgId } });

/** Store (or replace) a provider org admin key. Owner-only (403 otherwise); the gateway verifies
 *  it live against the provider first — 400 carries the provider's own message on a bad key;
 *  503 when this deploy has no key-encryption secret configured.
 *  @returns {Promise<{provider: string, configured: boolean, last4: string, org_name: string|null}>} */
export const putObservabilityKey = (provider, apiKey, orgId) =>
  put(`/v1/admin/observability/keys/${provider}`, { api_key: apiKey }, { query: { org_id: orgId } });

/** Owner-only. @returns {Promise<{deleted: boolean}>} */
export const deleteObservabilityKey = (provider, orgId) =>
  del(`/v1/admin/observability/keys/${provider}`, { query: { org_id: orgId } });

/** The one plug-and-play read: provider-observed usage/cost/members for the window, derived into
 *  summary tiles, spend_by_day/model, top scopes/actors, members, and savings candidates.
 *  Snapshot-cached server-side (~15 min TTL) — `cache.age_seconds` says how stale.
 *  @param {{orgId?: string, days?: number}} [opts]
 *  @returns {Promise<object>} the insights payload (see docs/plans/2026-07-09-org-observability.md) */
export const getOrgInsights = ({ orgId, days } = {}) =>
  get('/v1/admin/observability/insights', { query: { org_id: orgId, days } });
