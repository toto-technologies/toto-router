# Architecture overview

toto-router is a FastAPI app that wraps a **Gateway** object. The gateway is the request engine:
it resolves the requested model against the catalog, applies routing and guard decisions, dispatches
to a provider runner, meters the result, and writes a trace. Everything else — the console, the
catalog, the auth store, the driver plane — hangs off that spine.

## The two entry surfaces

Both surfaces resolve the same model against the same catalog and produce the same trace record.

- **`POST /v1/chat/completions`** — the OpenAI Chat Completions surface. The native shape the
  gateway works in internally (`schemas.ChatCompletion*`).
- **`POST /v1/messages`** — the Anthropic Messages surface. A thin wire-format boundary
  (`anthropic_surface.py`) translates the body to the internal OpenAI shape on the way in and back
  to Anthropic on the way out. See [anthropic-surface](anthropic-surface.md).

A request naming the sentinel model `smart` (or `toto-smart`) is classified and routed by task type
before it is served (see [routing](routing.md)). Any other model id is resolved directly against the
catalog (`route_reason: catalog`).

## Request lifecycle

1. **Auth** (`routes/deps.require_auth`) resolves the caller to an `Identity` — operator bearer,
   per-user API token, or session cookie — or returns `401`. It also decrypts the caller's stored
   provider keys into a request-scoped overlay. See [identity-and-auth](../domain/identity-and-auth.md).
2. **Routing / planning** (`Gateway._plan`) resolves the model id against the effective catalog,
   runs the guard + policy layer (residency and context-window constraints), and — for `smart` —
   the classify → bind → fallback ladder.
3. **Cache check** (optional, `TOTO_GW_CACHE`) — an exact-match lookup can short-circuit dispatch.
   See [caching](caching.md).
4. **Dispatch** — the runner registry maps the resolved catalog entry to a live runner (fake,
   OpenAI-compatible, native Anthropic, or local MLX) and executes the call, with per-provider
   timeouts, retries, a circuit breaker, and cross-residency fallback.
5. **Metering + trace** — usage is priced from the catalog, and a provenance record is written to
   every configured sink. See [trace-and-metering](../domain/trace-and-metering.md).

## Runners

`runners/registry.py` picks a runner from the catalog entry, lazily and cached per
(id, provider, base_url, upstream_model, api_key_env):

- **fake** (`endpoint: fake`) — deterministic echo, never touches the network. Powers the offline
  demo and the whole test suite. `TOTO_GW_FAKE_EXEC=1` forces every lane through the fake runner so
  routing and provenance are real while execution stays offline.
- **openai** (`endpoint: openai`) — any OpenAI-compatible host: OpenAI, OpenRouter, Fireworks,
  Cloudflare Workers AI, Together, Groq, a direct lab. `base_url` + `api_key_env` on the entry select
  the host. This is how most real providers are reached.
- **frontier** (`endpoint: anthropic`) — the native Anthropic Messages client.
- **economy / MLX** — an OpenAI-compatible upstream at a bare local URL (`mlx_lm.server`, LM Studio,
  Ollama).

## The driver plane (optional)

With `TOTO_GW_DRIVER=1`, the gateway also serves a **driver plane**: a LangGraph agent that
decomposes a request into sub-tasks, routes each sub-task to the right model by its metadata, and
synthesizes an answer. It backs `POST /v1/route` and the `/v1/sessions` surface. The routes are
always mounted and return a clean `503` when the driver is disabled, so the surface is discoverable
either way. The driver reuses the same classifier, label bindings, and benchmark ladder the smart
passthrough uses — no separate routing logic.

## App wiring & the open-core seam

`app/factory.py:create_app` builds the gateway, constructs the stores (`AuthStore`, and — when the
driver is on — `RunStore`), mounts the routers, and serves the console SPA at `/console`.

Two settings gate what is mounted:

- **`TOTO_GW_EDITION`** — defaults to `oss`. The open edition mounts only the always-on **gateway
  plane** and skips every org-shaped admin router; those modules are absent from this tree entirely.
- **`TOTO_GW_PLANES`** — the OSS edition forces `gateway`-only. The `app` plane (the hosted Toto
  product surface) does not exist here.

The result in this repo is a focused gateway-plane API: models, chat, messages, routing, sessions,
credentials/provider-keys, and the admin catalog / requests / usage endpoints the console drives.
See [configuration](../operations/configuration.md) for the full list.

## Process model

`python -m toto_gateway` (the `toto-router` entry point) is the production launcher: uvicorn with a
single worker per replica on purpose — one event loop and an in-process wake bus assume one process,
so horizontal scale is **replicas, not workers**. Shutdown is a bounded graceful drain
(`TOTO_GW_DRAIN_SECONDS`).
