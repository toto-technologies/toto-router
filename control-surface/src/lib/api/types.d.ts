// Response/request shapes for /v1/admin/*, mirrored from the FastAPI routes. Types-only (no runtime
// output); admin.js references these via JSDoc `import('./types').X`. Kept faithful to what the
// routes actually return — the effective routing view is verbatim from admin_routing.get_routing_policy.

export type Role = 'owner' | 'admin' | 'member' | 'auditor';

// ---- Tenancy ----

export interface Team {
  team_id: string;
  org_id: string;
  name: string;
}
export interface TeamsResponse {
  org_id: string;
  teams: Team[];
}

export interface Member {
  user_id: string;
  email: string | null;
  role: Role;
  [k: string]: unknown; // store row may carry extra columns (joined_at, etc.)
}
export interface MembersResponse {
  org_id: string;
  members: Member[];
}

export interface Invitation {
  id: string;
  org_id: string;
  email: string;
  role: Role;
  token?: string;
  accepted?: boolean;
  [k: string]: unknown;
}
export interface InvitationsResponse {
  org_id: string;
  invitations: Invitation[];
}

export interface Org {
  org_id: string;
  name: string;
  zero_retention?: boolean;  // W1-C4 privacy switch (returned on the org row)
  [k: string]: unknown;
}

// ---- Catalog policy ----

export type CatalogMode = 'allow' | 'deny';
/** GET shape (an unset policy comes back as the explicit empty/version-0 form). */
export interface CatalogPolicy {
  team_id: string;
  org_id: string;
  mode: CatalogMode;
  models: string[];
  residency: string[] | null;
  default_model: string | null;
  version: number;
}
/** PUT body (full replace). */
export interface CatalogPolicyInput {
  mode?: CatalogMode;
  models?: string[] | null;
  residency?: string[] | null;
  default_model?: string | null;
}

// ---- Org catalog policy (W1-C3 governance) ----

export type OrgCatalogMode = 'allow_all' | 'allowlist';
/** GET shape. No row → allow_all, version 0. `models` is the org's approved set (allowlist mode);
 *  `denied_count` is data-plane denials over the trailing `denied_window_days`. */
export interface OrgCatalogPolicy {
  org_id: string;
  scope: 'org';
  mode: OrgCatalogMode;
  models: string[];
  version: number;
  denied_count: number;
  denied_window_days: number;
}
/** PUT body (full replace — omitting `models` clears the approved list). */
export interface OrgCatalogPolicyInput {
  mode: OrgCatalogMode;
  models?: string[];
}

// ---- Cache policy (rides the routing-policy row) ----

/** Per-org cache-behavior overrides. Every field optional — an absent field inherits the global
 *  env default at request time; an empty object is pure inherit. Unknown keys are a 400
 *  invalid_cache (full-replace, like the sibling overlays). */
export interface CachePolicy {
  preset?: string | null; // console bookkeeping: 'off' | 'balanced' | 'max' | 'custom'
  auto_inject?: boolean; // add cache markers to continuing conversations the client didn't cache
  auto_inject_min_messages?: number; // int 1–50: conversation length that counts as "continuing"
  warmth_routing?: boolean; // keep a conversation on its model while its cache is warm
}

// ---- Routing policy (effective view) ----

export interface RoutingLabelRow {
  label: string;
  desc: string | null;
  model: string | null; // what routing actually uses for this team (override || global default)
  default_model: string | null; // the global labels.yaml auto-selection
  overridden: boolean;
  bindable: boolean; // false for privacy-governed labels such as redact — UI disables the row
  custom: boolean; // a team-invented custom task type (CT)
}
export interface CustomLabel {
  name: string;
  desc: string;
  model: string;
}
export interface RoutingPolicyView {
  team_id: string | null;
  org_id: string;
  scope?: 'org';
  optimize: string; // team preset, or the global default when unset
  optimize_overridden: boolean;
  version: number;
  prewarm?: boolean; // per-org cache pre-warm toggle (default OFF)
  // 'open'|'closed' scalar, or a W2-C7 per-reason matrix (API-widened; console emits scalar only)
  fail_policy?: 'open' | 'closed' | Record<string, 'open' | 'closed'>;
  taxonomy?: DataTaxonomy; // W2-C7 data-classification labels bound to residency constraints ({} = none)
  stick_ttls?: Record<string, number>; // label -> hold seconds; absent label = deploy default
  cache?: CachePolicy; // {} when the org never chose a strategy (inherit global defaults)
  labels: RoutingLabelRow[];
  custom_labels: CustomLabel[];
}
/** W2-C7 data-classification taxonomy: org sensitivity labels bound to residency constraints. */
export interface DataTaxonomy {
  labels?: Record<string, { constraint: 'allow' | 'local_only' | 'deny'; desc?: string }>;
  default?: string | null; // the label whose constraint applies to an unclassifiable request
}
/** PUT body (full replace of the overlay). */
export interface RoutingPolicyInput {
  bindings?: Record<string, string>; // label -> catalog model id
  optimize?: string | null;
  custom_labels?: CustomLabel[] | null;
  prewarm?: boolean; // per-org cache pre-warm toggle (default OFF)
  fail_policy?: 'open' | 'closed' | Record<string, 'open' | 'closed'>; // full-replace: omitting resets to 'open'
  taxonomy?: DataTaxonomy; // W2-C7 full-replace: absent/{} wipes the org's data-classification labels
  stick_ttls?: Record<string, number>; // per-task-type hold seconds (non-default only)
  cache?: CachePolicy; // full-replace: absent/{} wipes the org's overrides back to inherit
}

// ---- Model catalog (GET /v1/models) ----

export interface CatalogModel {
  id: string; // the or-* routing alias (internal routing key; demoted to a sub-label in the UI)
  lane: string | null; // economy | frontier | fake  (no longer surfaced as a "tier" word — price is the signal)
  residency_class: string | null; // in_perimeter | cloud
  upstream_model: string | null; // REAL provider model name, e.g. "anthropic/claude-sonnet-5" — the primary identity
  provider: string | null; // anthropic | openai | fireworks | openrouter | local  (rendered as the provider badge)
  via: string | null; // provider path: "local" for in-perimeter, else the provider keyword
  price_in: number | null; // USD / 1k prompt tokens
  price_out: number | null; // USD / 1k completion tokens
  context_window: number | null;
}
export interface ModelsResponse {
  object: 'list';
  data: CatalogModel[];
}

// ---- Benchmark evidence + provider availability ----

export interface CredentialScope {
  kind: 'platform' | 'organization' | 'user' | string;
  scope_id?: string;
}

export interface BenchmarkOffer {
  provider: string;
  offer?: string;
  offer_id?: string;
  route?: string;
  route_id?: string;
  upstream_model_id?: string;
  available?: boolean;
  availability?: string;
  status?: string;
  snapshot_status?: string;
  inventory_status?: BenchmarkInventoryState;
  freshness?: string;
  stale?: boolean;
  partial?: boolean;
  route_kind?: string;
  via?: string;
  credential_scope?: CredentialScope | string;
  scope?: CredentialScope | string;
  [k: string]: unknown;
}

export interface BenchmarkInventoryState {
  status?: 'success' | 'partial' | 'failed' | 'stale' | string;
  snapshot_status?: string | null;
  last_attempt_status?: string | null;
  freshness?: string;
  stale?: boolean;
  partial?: boolean;
  completed_at?: number | null;
  expires_at?: number | null;
  [k: string]: unknown;
}

export interface BenchmarkEligibilityReason {
  code?: string;
  message?: string;
  reason?: string;
  provider?: string;
  offer_id?: string;
  [k: string]: unknown;
}

export interface BenchmarkScore {
  score: number;
  n: number;
}

export interface BenchmarkModelRow {
  id: string;
  display_name: string;
  identity: Record<string, unknown> | null;
  provider: string;
  provider_available: boolean;
  smart_route_eligible: boolean;
  catalog_pinned: boolean;
  catalog_ids: string[];
  offers: BenchmarkOffer[];
  credential_scope?: CredentialScope | string;
  scope?: CredentialScope | string;
  inventory?: BenchmarkInventoryState[] | Record<string, BenchmarkInventoryState>;
  inventory_status?: string | BenchmarkInventoryState;
  freshness?: string | BenchmarkInventoryState;
  inventory_stale?: boolean;
  inventory_partial?: boolean;
  eligibility_reasons: Array<string | BenchmarkEligibilityReason>;
  scores: Record<string, BenchmarkScore>;
  fact_count: number;
  latest_retrieved_at: number | null;
}

export interface BenchmarkCoverageSource {
  facts: number;
  latest_retrieved_at: number | null;
}

export interface BenchmarkCoverage {
  models: number;
  facts: number;
  sources: Record<string, BenchmarkCoverageSource>;
}

export interface BenchmarkModelsResponse {
  generated_at: number;
  coverage: BenchmarkCoverage;
  categories: string[];
  scope: 'all_evidence' | 'provider_available' | 'smart_route_eligible';
  category: string;
  search: string;
  provider: string;
  models: BenchmarkModelRow[];
  next_cursor: string | null;
}

export interface BenchmarkFact {
  benchmark: string;
  benchmark_name: string;
  category: string;
  value: number;
  value_raw: string;
  unit: 'pct' | 'fraction' | 'elo' | 'arena' | 'index' | string;
  higher_is_better: boolean;
  version: string;
  source: string;
  source_url: string;
  retrieved_at: number;
  license: string;
}

export interface BenchmarkModelDetail extends BenchmarkModelRow {
  facts: BenchmarkFact[];
}

export interface BenchmarkRefreshResult {
  facts: number;
  aliases: number;
  status: string;
  error?: string;
}

// ---- Admin catalog (GET /v1/admin/catalog/models) ----

export interface AdminCatalogModel {
  id: string;
  aliases: string[];
  lane: string | null;
  provider: string | null;
  endpoint: string | null; // wire shape: 'openai' | 'anthropic' | …
  base_url: string | null;
  api_key_env: string | null;
  residency_class: string | null;
  upstream_model: string | null; // may carry a '#accounts/…/deployments/…' pin (Fireworks)
  price_in: number | null;
  price_out: number | null;
  context_window: number | null;
  tools: boolean;
  fine_tuned: boolean;
  source: string | null; // catalog fragment file, e.g. 'catalog.fireworks.yaml'
}
export interface AdminCatalogResponse {
  models: AdminCatalogModel[];
}

// ---- Fireworks sync (GET /v1/admin/catalog/sync/fireworks) ----
// Always 200: key_present:false + error for a missing key; error set on an upstream failure.

export interface FireworksDrift {
  kind: 'not_cataloged' | 'cataloged_not_deployed' | 'stale_suffix';
  severity: 'warn' | 'info';
  model?: string; // not_cataloged: the account model name
  deployment?: string | null; // not_cataloged: live deployment, when one exists
  suggested_yaml?: string; // not_cataloged: paste-ready catalog fragment
  catalog_id?: string; // cataloged_not_deployed / stale_suffix
  upstream_model?: string;
  cataloged_deployment?: string; // stale_suffix
  live_deployment?: string; // stale_suffix
  suggested_upstream?: string; // stale_suffix: corrected upstream_model value
}
export interface FireworksSync {
  provider: 'fireworks';
  key_present: boolean;
  account: string | null;
  checked_at: number | null; // epoch seconds
  error: string | null;
  account_models?: Array<{ name: string; display_name?: string; state?: string; base_model?: string; create_time?: string }>;
  deployments?: Array<{ name: string; base_model?: string; state?: string; create_time?: string }>;
  catalog_entries?: Array<{ id: string; upstream_model: string | null; status: string }>;
  drift?: FireworksDrift[];
  ok?: Array<{ catalog_id: string; deployment: string; deployment_state?: string }>;
}

// ---- OpenRouter discovery (GET /v1/admin/catalog/discovery/openrouter) ----
// Always 200: `error` set on upstream failure; key_present:false is fine (public endpoint).

export interface DiscoveryModel {
  slug: string; // 'moonshotai/kimi-k2.5' — the vendor prefix carries the family
  name: string | null;
  context_window: number | null;
  price_in: number | null; // USD per 1k, same unit as the catalog
  price_out: number | null;
  tools: boolean;
  vision: boolean;
  cataloged: boolean;
  catalog_id: string | null;
}
export interface OpenRouterDiscovery {
  provider: 'openrouter';
  key_present: boolean;
  checked_at: number | null; // epoch seconds
  error: string | null;
  total: number;
  models?: DiscoveryModel[];
}

// ---- Fireworks discovery (GET /v1/admin/catalog/discovery/fireworks) ----
// Always 200; key_present:false when FIREWORKS_API_KEY is unset (panel shows the setup hint).
// No price fields — the platform API doesn't expose them.

export interface FireworksDiscoveryModel {
  slug: string; // 'accounts/fireworks/models/glm-5p2'
  name: string | null;
  context_window: number | null;
  tunable: boolean; // LoRA fine-tunable on the tuning plane
  tools: boolean;
  vision: boolean;
  cataloged: boolean;
  catalog_id: string | null;
}
export interface FireworksDiscovery {
  provider: 'fireworks';
  key_present: boolean;
  checked_at: number | null; // epoch seconds
  error: string | null;
  total: number;
  filtered_out: number; // deprecated/embedding models hidden server-side
  models?: FireworksDiscoveryModel[];
}

// ---- Usage / billing ----

export interface UsageRow {
  [dimension: string]: string | number | null; // group_by dims + requests/tokens/cost/savings
}
export interface UsageResponse {
  org_id: string;
  group_by: string[];
  granularity: 'day' | 'hour' | null;
  rows: UsageRow[];
}
export interface UsageExport {
  period: string;
  org_id: string;
  format: string;
  line_items: Record<string, unknown>[];
}
export interface CacheSavingsModelRow {
  model: string;
  lane: string | null; // trace dimension only — never rendered as "lane" in UI copy
  requests: number;
  tokens_cached: number;
  tokens_cache_write: number;
  read_savings_usd: number;
  write_premium_usd: number;
  net_usd: number; // read_savings − write_premium
}
export interface CacheSavingsResponse {
  total: {
    net_usd: number;
    read_savings_usd: number;
    write_premium_usd: number;
    tokens_cached: number;
    tokens_cache_write: number;
  };
  models: CacheSavingsModelRow[];
  from: string | null; // ISO echo of the requested window
  to: string | null;
}

// ---- Cache health (GET /v1/admin/usage/cache-health) ----

export interface CacheHealthBucket {
  bucket: string; // '2026-07-08' (day) or '2026-07-08T14' (hour)
  requests: number;
  tokens_prompt: number;
  tokens_cached: number;
  tokens_cache_write: number;
  warm_hold_requests: number; // turns kept on their model because their cache was warm
  hit_rate: number; // tokens_cached / tokens_prompt, 0.0–1.0 (0.0 for an empty bucket)
}
export interface CacheHealthResponse {
  buckets: CacheHealthBucket[];
  from: string | null; // ISO echo of the requested window
  to: string | null;
  granularity: 'day' | 'hour';
}

// ---- Provider health ----

export interface ProviderHealth {
  provider: string; // display host (openrouter.ai, api.fireworks.ai, "openai", …)
  base_url: string | null;
  breaker_key: string; // provider_key(base_url); providers sharing it share one breaker
  state: 'closed' | 'open' | 'half-open';
  retry_in: number | null; // seconds until the half-open trial, when open
  consecutive_failures: number;
  models: string[]; // catalog model ids this provider serves
  stats: {
    requests: number;
    errors: number;
    error_rate: number;
    latency_p50_ms: number | null; // null when the deploy has no trace DB / no rows
    latency_p95_ms: number | null;
    latency_avg_ms: number | null;
  };
}
export interface ProviderHealthResponse {
  org_id: string;
  window_seconds: number;
  since: string; // ISO lower bound of the lookback window
  trace_db: boolean; // false → stats are all zero/null (no trace database configured)
  providers: ProviderHealth[];
}

// ---- Audit ----

export interface AuditEvent {
  action: string;
  user_id: string | null; // the actor (the /v1/admin/audit row field; NOT actor_user_id)
  org_id: string | null;
  target_type: string | null;
  target_id: string | null;
  metadata: Record<string, unknown> | null;
  ts: number;
  [k: string]: unknown;
}
export interface AuditResponse {
  events: AuditEvent[];
  limit: number;
  offset: number;
  org_id: string | null;
}
