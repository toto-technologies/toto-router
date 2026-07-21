# Routing

Routing is the point of the gateway: given a request, pick the model that fits the work — the
cheapest one that will do the job, subject to hard privacy and context constraints. This note
traces the whole path for a `smart` request, from task-type classification through binding,
fallback, and stickiness.

Everything here is **data-driven**. The task-type vocabulary and the label→model bindings live in
`toto_gateway/routing/labels.yaml`; the hard constraints live in `toto_gateway/routing/policy.yaml`;
model prices, lanes, and residency live in the catalog. Changing routing is editing data, not code.

## Triggering smart routing

A request whose `model` is the sentinel `smart` (or `toto-smart`, or a `toto/smart` provider-prefixed
id) is classified and rewritten to a real catalog id before it is served. Any other model id resolves
directly (`route_reason: catalog`). The same smart path is reachable from an OpenAI client
(`{"model":"smart"}`) and from Claude Code (`--model smart`).

`smart_route()` in `toto_gateway/routing/smart.py` is the pure resolver. The gateway hands it an
async classify function; it never dispatches a user-facing turn itself.

## Step 1 — classify the task type

The request's text is sent to a small, fast **classifier model** (`TOTO_GW_LABEL_CLASSIFIER_MODEL`,
a catalog entry id — by default a Haiku-class model) with the closed label vocabulary. The classifier
returns exactly one label from that vocab. The vocabulary is the eleven NVIDIA task types plus
toto's `redact`:

| Label | What it is | Default binding | Fallback category |
|-------|-----------|-----------------|-------------------|
| `code_generation` | write / complete / debug code, SQL, regex, scripts | `or-qwen3-coder-flash` | coding |
| `open_qa` | factual question from general knowledge | `or-sonnet-5` | knowledge |
| `closed_qa` | answer strictly from provided context | `or-gemini-2.5-flash` | long_context |
| `summarization` | condense given text | `or-gemini-2.5-flash` | instruction_following |
| `text_generation` | compose original prose | `or-sonnet-5` | writing |
| `rewrite` | rephrase / reformat / translate | `or-haiku-4.5` | instruction_following |
| `classification` | assign categories / tags / sentiment | `or-haiku-4.5` | reasoning |
| `extraction` | pull structured fields from text | `or-haiku-4.5` | reasoning |
| `brainstorming` | open-ended idea generation | `or-sonnet-5` | writing |
| `chatbot` | conversational / casual replies | `or-gemini-2.5-flash` | conversation |
| `other` | none of the above (the generalist catch-all) | `or-sonnet-5` | — |
| `redact` | involves sensitive data to mask/anonymize | *(unbound — routes by residency)* | — |

The default bindings above name entries in `catalog.openrouter.yaml` (the default catalog when
`OPENROUTER_API_KEY` is set). If you point `TOTO_GW_CATALOG` at models you run locally, rebind
`labels.yaml` to those ids.

The classifier call is **hard-capped** by `TOTO_GW_LABEL_TIMEOUT_MS`. Any failure — the classifier
model absent from the catalog, a timeout, an unparseable answer — degrades to the fallback pick
below, never raises. The prompt variant is tunable (`TOTO_GW_LABEL_PROMPT_VARIANT`, default
`fewshot`, which outperforms `baseline`).

## Step 2 — resolve the label to a model (the binding ladder)

For a classified label, `smart_route` walks this precedence and stops at the first model that both
exists in the catalog and can satisfy the request's tool needs (a tools-bearing request never
resolves to an entry that can't speak native tool calling):

1. **Custom / team binding** — a per-caller routing overlay's `label_bindings[label]`, or a
   custom-label model. (In the open edition there is one caller — the operator — whose overlay lives
   under the `local` scope and is edited in the console; see below.) `route_reason: label:<l>:team`.
2. **Global binding** — `labels.yaml`'s default binding for the label. `route_reason: label:<l>`.
3. **Benchmark-best on the label's category** — if the label is classified but *unbound* (its binding
   was cleared, or it's `other`/`redact`), route to the benchmark-best real entry for the label's
   evidence category. `route_reason: label:<l>:benchmark_best:<cat>`.
4. **Fallback model** — the designated catch-all (`other` binding), else the driver's benchmark
   argmax with empty metadata, else any usable catalog entry. Also the landing spot when the
   classifier fails entirely: `route_reason: smart:classify_failed`.

The `optimize` preset (`quality` | `balanced` | `cost`) breaks benchmark ties — but only on the
fallback / benchmark-best paths. **A bound label is a fixed pick;** optimize does not second-guess it.

## Step 3 — hard constraints (guard + policy)

After a model is resolved, `Gateway._plan` applies the policy layer (`routing/policy.py`) on the
resolved entry. Policy **beats** the routing pick. Two kinds of constraint ship in `policy.yaml`:

- **Residency** — privacy keys off data *location*, not price tier. A `redact` intent, or a request
  whose intent text contains a sensitive keyword (e.g. `mnpi`), is forced `in_perimeter`: it must
  physically stay inside the perimeter and never land on a cheap cloud model. Sensitive work is
  selected by `residency_class == in_perimeter`, not by lane.
- **Context window** — a prompt over `max_local_context` (default 32768 tokens) forces the frontier
  tier, because economy models can't hold it.

Residency and tier are orthogonal and reported separately. The guard action lands on the trace as
`guard_action` (`allow` / a downgrade reason).

## Step 4 — stickiness (agentic conversations)

A multi-turn conversation — an agentic tool loop or a genuine chat — should not re-classify and
flip models every turn: a mid-conversation model switch breaks tool-call continuity and forfeits the
upstream prompt cache. So the classification is **memoized per conversation**.

The memo key is the conversation anchor (`conversation_key`: a hash of the system + first user
message) plus the label vocab. Every later turn of the same conversation reuses the first turn's
label with zero added classifier latency and lands on the same model. Bindings still resolve fresh
each request, so a console policy change takes effect immediately; only the *label* is held.

How long a conversation stays pinned is a **stickiness policy** (`TotoStickiness`), a precedence
ladder:

1. **Declared session** — a client that names its own session identity (`x-session-id` header, a
   body `session_id`, or `prompt_cache_key`) is telling the gateway "these turns are one task". Such
   a conversation commits eagerly to a long (4h) identity-based hold.
2. **Label-aware TTL** — the hold varies by task type. A `code_generation` thread stays pinned longer
   than a one-shot `classification`. Per-label holds come from `TOTO_GW_STICK_TTLS` (a JSON
   `{label: seconds}` map), else a flat 900s default.
3. **Warmth floor** — a "hot" conversation (a live upstream prefix cache, or several turns deep)
   extends the hold. Warmth only ever *lengthens* the pin, never shortens it.

## Step 5 — warmth routing (incumbent hold)

Separately, when `TOTO_GW_WARMTH_ROUTING` is on (the default), a resolved pick passes through
`_warm_hold`: if this conversation was served a *different* model on a recent turn and that
incumbent's upstream prefix cache is still warm (last turn had a cache hit, inside the incumbent's
provider cache TTL) and the incumbent still satisfies every hard constraint, the gateway **keeps the
incumbent** rather than swapping. Swapping mid-window would forfeit the ~90% prefix-cache discount.

**Explicit bindings beat warmth.** An operator rebind takes effect on the next turn even while the
incumbent is warm; warmth only holds against benchmark/optimize *drift* on a derived pick — the churn
the cache economics actually want damped. The held-over pick is recorded in `label_metadata.warm_hold`
so the console can show what was kept and why. See [caching](caching.md) for the cache side of this.

## The driver's metadata classifier

The driver plane (`/v1/route`) uses a second, complementary classifier: `driver/classify.py`, a
pure metadata lookup with no I/O. Where the smart path classifies raw request *text* into a task
label, the driver classifies a decomposed task's *structured metadata* (intent, scope, keywords,
`requires`) into a lane + a benchmark-best model. It applies the same residency-first privacy rule.
Both paths converge on the same catalog and the same benchmark ladder.

## Configuring routing (open edition)

In this single-tenant edition the operator is the only caller, bound to a `local` scope sentinel
(see [identity-and-auth](../domain/identity-and-auth.md)). The console's routing tab writes the
operator's routing overlay under that `local` key, so a binding set in the console governs the
operator's own `Bearer <token>` traffic — resolved fresh per request, so edits apply live. The
overlay carries per-label bindings, custom labels, the optimize preset, and the per-label stick TTLs.
Team- and org-scoped policies are a hosted-product concern and are absent here.
