# Caching

The gateway has two independent cache mechanisms and a set of routing behaviors that exist to keep
**upstream** prompt caches warm. They all surface in the console's **Caching** tab.

## 1. Exact-match response cache

`cache/exact.py` (`ExactCache`) is an exact-match response cache, gated by `TOTO_GW_CACHE`. When on,
an identical request returns the stored response without dispatching upstream (`cache_hit: true` on
the trace).

- **Key** = sha256 of a normalized request tuple: tenant, model, message roles+text, temperature,
  max_tokens, and tools. Fields that don't affect the completion (stream options, user) are excluded.
- **Isolation** — each tenant gets its own key prefix, so identical prompts never share hits across
  tenants.
- **Storage** — a bounded in-memory FIFO (1000 entries) by default; pass a SQLite path to persist
  across restarts (L1 memory, L2 SQLite, write-through). Stdlib `hashlib` + `json` + `sqlite3` only,
  no Redis, no cache library.

This is a same-input-same-output cache. It is safe to enable independently of the routing brain.

## 2. Upstream prompt-cache economics

Most of the "caching" value is not the response cache — it's keeping the *provider's* prompt cache
warm across the turns of a conversation, where a cache read costs ~10% of a fresh input token. Three
behaviors protect that:

### Prompt-cache auto-inject

Most OpenAI-shaped clients never send Anthropic `cache_control`, so a continuing Anthropic
conversation pays full input price every turn. With `TOTO_GW_ANTHROPIC_AUTO_CACHE` on (the default),
when a request carries **no** client cache breakpoint **and** looks continuous (it has tools, or at
least `TOTO_GW_ANTHROPIC_AUTO_CACHE_MIN_MESSAGES` messages), the frontier runner adds Anthropic's
top-level automatic `cache_control` so the prefix caches. A one-shot request is left alone — it would
only pay the 1.25× write premium with nothing to reuse.

### Warmth routing (incumbent hold)

`TOTO_GW_WARMTH_ROUTING` (default on) keeps a conversation on its warm incumbent model rather than
swapping to a freshly-resolved one while the incumbent's upstream prefix cache is still live.
Swapping mid-window forfeits the read discount. This is the routing-side lever; the mechanism and
its precedence (explicit bindings always beat warmth) are described in
[routing](routing.md#step-5--warmth-routing-incumbent-hold).

The warm window per model is `cache_ttl_s` on the catalog entry when set, else a per-family default:
Anthropic ~5 min, OpenAI ~30 min, DeepSeek best-effort ~24 h, everything else 5 min.

### Stickiness TTLs

Per-task-type hold lengths (`TOTO_GW_STICK_TTLS`, a JSON `{label: seconds}` map) decide how long a
conversation's classified label — and therefore its model — stays pinned. A code thread stays sticky
longer than a one-shot classification. See
[routing](routing.md#step-4--stickiness-agentic-conversations).

## 3. Prewarm

`POST /v1/prewarm` primes a model's upstream prompt cache ahead of a burst so the first real turn is
already a cache read rather than a cache write. In the console it is a toggle (default off). It is a
deliberate action, not automatic — you warm a prefix you know you're about to hammer.

## Observability

The Usage surface reports cache health and realized savings:

- `GET /v1/admin/usage/cache-health` — hit rates and warmth stats.
- `GET /v1/admin/usage/cache-savings` — dollar savings attributable to cache reads.

Cache accounting is computed from the gateway's own trace of each turn (the usage the provider
reported), never from anything the client claims. Per-turn cost, `cache_hit`, `tokens_cached`, and
`tokens_cache_write` all land on the trace record — see
[trace-and-metering](../domain/trace-and-metering.md).
