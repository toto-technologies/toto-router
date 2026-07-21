# toto-router

An LLM gateway with a routing brain that speaks both major wire formats. It sits between your
client and upstream model providers (Anthropic, OpenAI/OpenRouter, local MLX or any
OpenAI-compatible server), forwards requests, and records a complete provenance trace for
every call: model, lane, tokens, cost, latency, residency. It accepts requests on the
OpenAI-compatible surface (`POST /v1/chat/completions`) **and** the Anthropic Messages surface
(`POST /v1/messages`, streaming + non-streaming) — so an OpenAI client and Claude Code can both
route through the same gateway to the same catalog. On top of the raw passthrough sits an
optional driver plane — an agent that decomposes a request into sub-tasks, routes each to the
right model by task type, and synthesizes the answer — and a six-tab admin console (overview,
activity, models, catalog, usage, settings).

Routing intelligence is data-driven: a label routing table (`toto_gateway/routing/labels.yaml`)
maps classifier-assigned task types to catalog models, and a policy layer
(`toto_gateway/routing/policy.yaml`) enforces hard privacy and context-window constraints.
Model availability, pricing, and lanes live in `catalog.yaml`.

Smart routing works out of the box with one env var: when `OPENROUTER_API_KEY` is set and
`TOTO_GW_CATALOG` isn't, the gateway defaults to the bundled OpenRouter catalog
(`catalog.openrouter.yaml`), whose models the shipped label table binds — task-type routing is
live on first boot. With no key at all, the log prints a `smart task-type routing disabled`
warning naming that env var; passthrough and the fake lane work regardless, and an explicit
`TOTO_GW_CATALOG` always wins (rebind `labels.yaml` if you point it at models you run locally).

## Quickstart (10 seconds, zero secrets)

The fake lane routes for real and executes offline — no API keys, no GPU, no network.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

TOTO_GW_FAKE_EXEC=1 TOTO_GW_AUTH_TOKEN=dev toto-router   # or: python -m toto_gateway

curl http://127.0.0.1:8080/v1/models -H 'authorization: Bearer dev'
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'authorization: Bearer dev' -H 'content-type: application/json' \
  -d '{"model":"echo-local","messages":[{"role":"user","content":"hello"}]}'
```

Every response comes back with a full trace record — check the server log or the console's
activity tab. Point any OpenAI client at `http://127.0.0.1:8080/v1` (API key = the bearer
token you set) to route a real harness through the gateway.

## Enabling real lanes (bring your own keys)

Models in `catalog.yaml` marked `lane: fake` never touch the network. To enable real
upstreams, set the provider key env vars the catalog entries name:

| Lane | Catalog `endpoint` | Env var |
|------|--------------------|---------|
| local | `http://127.0.0.1:8081/v1` | — (run `mlx_lm.server`, LM Studio, or Ollama) |
| frontier | `anthropic` | `ANTHROPIC_API_KEY` |
| openai / openrouter | `openai` | `OPENAI_API_KEY` / `OPENROUTER_API_KEY` |

Key feature flags (all env-gated, `TOTO_GW_` prefix):

| Flag | Default | Effect |
|------|---------|--------|
| `TOTO_GW_ROUTING` | off | Guard + policy router on the passthrough plane |
| `TOTO_GW_DRIVER` | off | Driver plane (`POST /v1/route`, sessions) |
| `TOTO_GW_CACHE` | off | Exact-match response cache |
| `TOTO_GW_FAKE_EXEC` | off | Real routing decisions, offline execution |
| `TOTO_GW_AUTH_TOKEN` | — | Operator bearer token (set one for anything non-local) |

## Point Claude Code at your router

The Anthropic Messages surface means Claude Code (and the `anthropic` SDK) can route through
your gateway with no code change — just point them at it and let the router pick the model:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8080 ANTHROPIC_API_KEY=$TOTO_GW_AUTH_TOKEN \
  claude --model <catalog-model-id>     # or --model smart to let the router choose
```

Auth is the same token as everywhere else: the gateway accepts it as Anthropic's `x-api-key`
header (what the SDK sends) or as `Authorization: Bearer`. Every reply carries its provenance in
`x-toto-*` response headers (`x-toto-model`, `x-toto-lane`, `x-toto-cost-usd`) so you can see
exactly which model and lane served each turn.

## Observability

Beyond the per-call trace, agentic conversations carry **trajectory shadow signals**: a
stdlib-only score, computed from the turn's tool-result history (is the run exploring, erroring,
stuck, or settled?), stamped on every trace as `trajectory_score` / `trajectory_confidence`.
It's shadow-mode — computed and recorded, never routed on — so you can study how a run is going
without it changing any decision. `scripts/trajectory_report.py` reads these back as a
calibration report over your trace store.

## Console

The admin console is a static SvelteKit app served by the gateway at `/console`:

```bash
cd control-surface
npm ci
npm run build:oss     # the OSS build — six tabs, nothing else compiled in
```

Restart the gateway and open `http://127.0.0.1:8080/console`.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

Fully offline — no secrets, no network, no GPU.

## Running this for a team

This is the single-tenant gateway; multi-tenant and organization features live in the hosted product.

## License

[MIT](LICENSE). Use it freely — internally, commercially, modified or not. See the LICENSE
file for the exact terms.
