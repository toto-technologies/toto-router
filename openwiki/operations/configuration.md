# Configuration

All configuration is environment-driven (`toto_gateway/config.py`, a pydantic `Settings`). Gateway
settings use the **`TOTO_GW_`** prefix; provider API keys use each provider's own conventional env
var (no prefix). A `.env` file in the working directory is read.

## Server & process

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_HOST` | `127.0.0.1` | bind host (the Docker image sets `0.0.0.0`) |
| `TOTO_GW_PORT` | `8080` | bind port |
| `TOTO_GW_LOG_LEVEL` | `info` | log level |
| `TOTO_GW_DRAIN_SECONDS` | `30` | graceful-drain window on SIGTERM |
| `TOTO_GW_CORS` | off | permissive CORS for a local cross-origin UI (dev only); also un-hides `/docs` |

## Auth

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_AUTH_TOKEN` | — | operator bearer token; empty → open auth (single-operator dev) |

See [identity-and-auth](../domain/identity-and-auth.md).

## Catalog & providers

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_CATALOG` | *(resolved)* | catalog file, or a comma-separated list composed left→right. Unset → `catalog.openrouter.yaml` when `OPENROUTER_API_KEY` is set, else `catalog.yaml` |
| `OPENROUTER_API_KEY` | — | OpenRouter key; also selects the default catalog |
| `ANTHROPIC_API_KEY` | — | native Anthropic lane |
| `OPENAI_API_KEY` | — | OpenAI (and the fallback for OpenAI-compatible entries) |
| `FIREWORKS_API_KEY` | — | Fireworks |
| `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` | — | Cloudflare Workers AI (two-part credential) |
| `GEMINI_API_KEY` | — | Google Gemini |

A key stored in the console Settings page beats its env var. See
[catalog-and-providers](../domain/catalog-and-providers.md).

## Routing

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_ROUTING` | off | guard + policy router on the passthrough plane |
| `TOTO_GW_LABEL_ROUTING` | on | task-type label routing (the smart brain); off → benchmark-only |
| `TOTO_GW_LABEL_CLASSIFIER_MODEL` | `or-haiku-4.5` | catalog id the classifier runs on |
| `TOTO_GW_LABEL_TIMEOUT_MS` | `10000` | hard cap on the classifier call |
| `TOTO_GW_LABEL_PROMPT_VARIANT` | `fewshot` | classifier prompt variant (`fewshot` beats `baseline`) |
| `TOTO_GW_WARMTH_ROUTING` | on | keep a conversation on its warm incumbent model |
| `TOTO_GW_STICK_TTLS` | — | JSON `{label: seconds}` per-task-type stickiness holds |
| `TOTO_GW_DRIVER` | off | driver plane (`POST /v1/route`, `/v1/sessions`) |

See [routing](../architecture/routing.md).

## Caching

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_CACHE` | off | exact-match response cache |
| `TOTO_GW_ANTHROPIC_AUTO_CACHE` | on | auto-inject Anthropic prompt-cache breakpoints |
| `TOTO_GW_ANTHROPIC_AUTO_CACHE_MIN_MESSAGES` | `3` | min messages before auto-inject fires |

See [caching](../architecture/caching.md).

## Offline / demo

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_FAKE_EXEC` | off | real routing decisions, offline execution (every lane runs as the fake runner) |

`TOTO_GW_FAKE_EXEC=1` is the offline demo path: routing, guards, cache, and provenance are all real;
only the answer text is a deterministic stub, so no keys, network, or GPU are needed.

## Trace sinks

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_TRACE_STDOUT` | on | one JSON trace line per turn to stdout |
| `TOTO_GW_TRACE_JSONL` | `traces.jsonl` | append-only trace file |
| `TOTO_GW_TRACE_DB` | — | trace DB: a SQLite path or a `postgresql://…` URL. **Required for the console's Activity/Usage tabs** |
| `TOTO_GW_LOG_CONTENT` | on | store prompt + response text in `request_content` (needs a trace DB) |
| `TOTO_GW_CONTENT_RETENTION_DAYS` | `30` | age out captured content (0 = keep forever) |

See [trace-and-metering](../domain/trace-and-metering.md).

## Databases

There are two stores, both defaulting to SQLite files under `data/`:

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_DB` | `data/gateway.db` | the app store (accounts/auth, and the driver's sessions when on). `:memory:` in tests |
| `TOTO_GW_DATABASE_URL` | — | set to a `postgresql://…` URL to run the stores on Postgres instead of SQLite |
| `TOTO_GW_TRACE_DB` | — | the trace/metering store (above), independent of the app store |

Postgres connection-pool tunables (`TOTO_GW_POOL_MIN` / `_MAX` / `_TIMEOUT`) apply in PG mode only.

## At-rest encryption secret

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_CREDENTIALS_SECRET` | — | Fernet key material for stored provider keys |
| `TOTO_GW_CREDENTIALS_SECRET_OLD` | — | previous secret, decrypt-only, during a rotation window |

In the open edition with a SQLite-file DB, an unset secret is **auto-generated** and persisted beside
the DB as `credentials.secret` (mode 0600) — pasting a provider key in Settings then works with zero
env vars. Tradeoff and the fail-closed cases are covered in
[catalog-and-providers](../domain/catalog-and-providers.md#bring-your-own-key-byok-provider-keys). A
HashiCorp Vault KMS source is available (`TOTO_GW_KMS_PROVIDER=vault`) for keeping the key off the
DB's disk.

## Edition & planes

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_EDITION` | `oss` | `oss` mounts only the gateway plane and no org-shaped admin routes; enterprise is the hosted build |
| `TOTO_GW_PLANES` | `gateway,app` | which product planes mount; the `oss` edition forces gateway-only |

This is the open-core seam. In this repo the edition is `oss`, so the app plane and every org-shaped
route are absent. See [architecture](../architecture/architecture.md#app-wiring--the-open-core-seam).

## Persona

| Env | Default | Effect |
|-----|---------|--------|
| `TOTO_GW_PERSONA` | `toto` | the one brand-carrying surface: `toto`, `neutral` (brand-free), or a file path / inline system string |

The routing engine carries zero brand; the persona composes only on the user-facing answer prompts.
