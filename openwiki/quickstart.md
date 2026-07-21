# Quickstart — toto-router

**toto-router** is an OpenAI-compatible LLM gateway with a routing brain. It sits between any
OpenAI-shaped client (an agentic harness, OpenCode, curl) — or an Anthropic client like Claude
Code — and your upstream model providers. It forwards each request, records a complete provenance
trace for it (model, lane, tokens, cost, latency, residency), and layers smart task-type routing,
response caching, a provider catalog, and an admin console on top.

It speaks **two wire formats on the same catalog**: the OpenAI Chat Completions surface
(`POST /v1/chat/completions`) and the Anthropic Messages surface (`POST /v1/messages`, streaming
and non-streaming). An OpenAI client and Claude Code can both route through one gateway to the same
models.

This is the single-tenant open-source edition. Multi-tenant features (teams, organizations, SSO,
budgets, audit export) live in the hosted product and are not part of this tree.

---

## Install & run (offline, zero secrets)

The **fake lane** routes for real and executes offline — no API keys, no GPU, no network. It is
the fastest way to see the gateway work end to end.

```bash
# Python 3.12+ (3.13 recommended)
python -m venv .venv && source .venv/bin/activate
pip install -e .                 # or: uv pip install -e .

TOTO_GW_FAKE_EXEC=1 TOTO_GW_AUTH_TOKEN=dev toto-router   # or: python -m toto_gateway
```

On boot the server logs a ready-to-open console URL carrying the operator token in the URL
fragment, e.g. `console ready — open http://127.0.0.1:8080/console/overview#token=dev` (see
[operations/console](operations/console.md)).

Drive it with any HTTP client. The bearer token is whatever you set in `TOTO_GW_AUTH_TOKEN`:

```bash
curl http://127.0.0.1:8080/v1/models -H 'authorization: Bearer dev'

curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'authorization: Bearer dev' -H 'content-type: application/json' \
  -d '{"model":"echo-local","messages":[{"role":"user","content":"hello"}]}'
```

Every response carries a full provenance record: the OpenAI surface embeds it as the `x_toto`
block in the response body; the Anthropic surface returns it as `x-toto-*` response headers. The
same record is written to the trace sinks (see [domain/trace-and-metering](domain/trace-and-metering.md)).

> Login is required. A request with no valid credential gets a `401`. Set `TOTO_GW_AUTH_TOKEN` for
> anything beyond a throwaway local run.

---

## Enabling real providers

Models marked `lane: fake` in the catalog never touch the network. To reach real upstreams, the
easiest path is the console **Settings** page — paste an OpenRouter / Fireworks / Cloudflare /
OpenAI / Gemini key and it is used on the very next request, no restart. Keys are encrypted at
rest and a stored key wins over the matching env var. See
[domain/catalog-and-providers](domain/catalog-and-providers.md) and
[domain/identity-and-auth](domain/identity-and-auth.md).

Alternatively set the env var a catalog entry names:

| Kind | Catalog `endpoint` | Env var |
|------|--------------------|---------|
| local OpenAI-compatible | `http://127.0.0.1:8081/v1` | — (run `mlx_lm.server`, LM Studio, or Ollama) |
| Anthropic (native) | `anthropic` | `ANTHROPIC_API_KEY` |
| OpenAI / OpenRouter / Fireworks / Cloudflare | `openai` | `OPENAI_API_KEY` / `OPENROUTER_API_KEY` / … |

**Smart routing out of the box:** set `OPENROUTER_API_KEY` and leave `TOTO_GW_CATALOG` unset — the
gateway defaults to the bundled OpenRouter catalog, whose models the shipped label table binds, so
task-type routing is live on first boot. With no key and the default catalog, the log prints a
`smart task-type routing disabled` warning naming that env var; passthrough and the fake lane still
work. See [architecture/routing](architecture/routing.md).

---

## Point Claude Code at the router

The Anthropic Messages surface means Claude Code and the `anthropic` SDK route through the gateway
with no code change:

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8080 ANTHROPIC_API_KEY=$TOTO_GW_AUTH_TOKEN \
  claude --model <catalog-model-id>     # or --model smart to let the router choose
```

Details in [architecture/anthropic-surface](architecture/anthropic-surface.md).

---

## Where to go next

- **[Architecture overview](architecture/architecture.md)** — request lifecycle, planes, how the app is wired.
- **[Routing](architecture/routing.md)** — the heart: task-type classification → policy → binding/fallback → stickiness.
- **[Caching](architecture/caching.md)** — exact-match cache, prompt-cache auto-inject, warmth routing, prewarm.
- **[Anthropic surface](architecture/anthropic-surface.md)** — the `/v1/messages` wire boundary.
- **[Catalog & providers](domain/catalog-and-providers.md)** — the model catalog, provider modules, adoption, BYOK keys.
- **[Identity & auth](domain/identity-and-auth.md)** — the token gate, the operator identity, user tokens.
- **[Trace & metering](domain/trace-and-metering.md)** — the provenance record and the Activity/Usage surfaces.
- **[Configuration](operations/configuration.md)** — env vars, flags, ports, databases, the at-rest secret.
- **[Console](operations/console.md)** — the seven-tab admin console and how to build it.
- **[Testing](testing/testing.md)** — the offline suite and its fake/echo lanes.
