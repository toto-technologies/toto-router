# Trace & metering

Every request the gateway serves produces one **provenance record**. It is the audit trail, the cost
ledger, and the data the Activity and Usage surfaces read. Nothing is served without being traced.

## The provenance record

For each turn the gateway records the routing decision, the model actually served, token counts,
metered cost, latency stages, and residency. The record is delivered to the client and written to the
configured sinks:

- **OpenAI surface** — embedded in the response body as the `x_toto` block.
- **Anthropic surface** — returned as `x-toto-*` response headers (see
  [anthropic-surface](../architecture/anthropic-surface.md)).

Representative fields (from `trace.py`, observed on a live turn):

| Field | Meaning |
|-------|---------|
| `request_id` / `conversation_key` | the turn id; the conversation anchor used for stickiness |
| `model` / `lane` / `residency_class` | what served the turn, its tier, and where it ran |
| `route_reason` | how the model was chosen (`catalog`, `label:<l>`, `label:<l>:team`, `smart:classify_failed`, …) |
| `classified_as` / `label` | the task type the classifier assigned (smart routing) |
| `guard_action` / `signal_intent` | the policy verdict and any detected intent |
| `tokens_prompt` / `tokens_completion` / `tokens_cached` / `tokens_cache_write` | usage, cache reads, cache writes |
| `cost_usd` / `cost_estimated` / `frontier_baseline_usd` | metered cost, whether estimated, and the frontier-price baseline (the savings denominator) |
| `cache_hit` | response-cache hit |
| `latency_ms_total` / `latency_ms_gateway_overhead` / `plan_ms` / `upstream_ms` | latency broken into stages |
| `runner_id` / `provider` / `upstream_model` / `credential_scope` | dispatch provenance |
| `harness` | the calling surface (e.g. `anthropic-sdk`) |
| `trajectory_score` / `trajectory_confidence` | shadow signals (below) |

## Cost model

Cost is priced from the catalog entry's `price_usd_per_1k` (prompt/completion rates), with cache
reads discounted by `cache_read_multiplier` (~0.1× input) and cache writes charged a
`cache_write_multiplier` premium (Anthropic 1.25×). `frontier_baseline_usd` is what the same turn
would have cost on the frontier reference model — the denominator for "router savings". Prices are
verified per provider; where the shipped or discovered numbers are wrong for your account, override
them (see [catalog-and-providers](catalog-and-providers.md#catalog-adoption)).

## Trace sinks

Configured via env (`TOTO_GW_` prefix, see [configuration](../operations/configuration.md)):

- **stdout** (`TRACE_STDOUT`, default on) — one JSON `gateway.call` line per turn.
- **JSONL** (`TRACE_JSONL`, default `traces.jsonl`) — append-only file.
- **Trace DB** (`TRACE_DB`) — a SQLite path or a Postgres URL. This is what the console's Activity and
  Usage tabs read. Without it, those surfaces have nothing to query.

### Request content capture

With `TOTO_GW_LOG_CONTENT` on (the default) and a trace DB configured, the actual prompt and served
response text are stored in a sibling `request_content` table, so the Activity drill-down shows the
real work, not just the decision trail. A reaper ages rows out after `TOTO_GW_CONTENT_RETENTION_DAYS`
(default 30). With content logging off, the detail endpoint reports `content_available: false`.

## Activity & Usage surfaces

The console reads these gateway-plane endpoints:

- **Activity** — `GET /v1/admin/requests` (the request log) and
  `GET /v1/admin/requests/{id}` (per-request drill-down, including captured content).
- **Usage** — `GET /v1/admin/usage` (aggregate spend and volume, broken down by model / task type),
  plus `GET /v1/admin/usage/cache-health`, `GET /v1/admin/usage/cache-savings`, and
  `GET /v1/admin/usage/export`.

Metering and cache accounting are always computed from the gateway's own trace of each turn (the
usage the provider reported back), never from anything the client claims.

## Trajectory shadow signals

Agentic conversations additionally carry a **trajectory score**: a stdlib-only signal computed from
the turn's tool-result history (is the run exploring, erroring, stuck, or settled?), stamped on the
trace as `trajectory_score` / `trajectory_confidence`. It is **shadow-mode** — computed and recorded,
never routed on — so you can study how a run is going without it changing any decision.
`scripts/trajectory_report.py` reads these back as a calibration report over your trace store.
