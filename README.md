# toto-router

An OpenAI-compatible LLM gateway with a routing brain. It sits between any OpenAI-shaped
client and upstream model providers (Anthropic, OpenAI/OpenRouter, local MLX or any
OpenAI-compatible server), forwards requests, and records a complete provenance trace for
every call: model, lane, tokens, cost, latency, residency. On top of the raw passthrough
sits an optional driver plane — an agent that decomposes a request into sub-tasks, routes
each to the right model by task type, and synthesizes the answer — and a six-tab admin
console (overview, activity, models, catalog, usage, settings).

Routing intelligence is data-driven: a label routing table (`toto_gateway/routing/labels.yaml`)
maps classifier-assigned task types to catalog models, and a policy layer
(`toto_gateway/routing/policy.yaml`) enforces hard privacy and context-window constraints.
Model availability, pricing, and lanes live in `catalog.yaml`.

Note: on a zero-key first boot the log prints `label routing disabled` — the shipped label
table binds cloud models you haven't enabled yet. That's the honest state, not a crash:
passthrough and the fake lane work regardless, and label routing switches on by itself once
the bound models exist in your catalog (e.g. after adding an OpenRouter key, or after
rebinding `labels.yaml` to models you run locally).

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

This tree is the single-user gateway: your own keys, your own machine, full routing and
provenance, free forever. Org features — shared deployments with per-user tokens, teams,
budgets, catalog governance, SSO/SCIM, audit exports, org-level observability — are the
hosted and enterprise product built on this same core. If your usage is becoming
multi-person, that's the line: contact the maintainers or use the hosted offering.

## License

[FSL-1.1-ALv2](LICENSE) (Functional Source License). In short: use it freely — internally,
commercially, modified or not — for anything except offering it (or a substantially similar
service) to others as a competing product. Each release automatically converts to
Apache-2.0 two years after it ships. See the LICENSE file for the exact terms.
