# Console

The admin console is a static SvelteKit app served by the gateway at `/console`. It is the operator's
window onto everything the gateway does — routing, providers, cost, cache — and the place to
configure them without touching env vars.

## Building it

The console lives in `control-surface/`. The OSS build compiles exactly the seven tabs below,
nothing else:

```bash
cd control-surface
npm ci
npm run build:oss     # VITE_EDITION=oss, CONSOLE_BASE=/console
```

Restart the gateway and open `http://127.0.0.1:8080/console`. The gateway mounts `/console` whenever
a build exists at `control-surface/build`, regardless of the plane setting — the `/v1/admin` API it
drives lives in the always-on gateway plane.

## Logging in

The console authenticates with the operator token (`TOTO_GW_AUTH_TOKEN`):

- **Auto-login** — at boot the launcher logs a ready-to-open URL with the token in the URL fragment:
  `http://127.0.0.1:8080/console/overview#token=<token>`. Open it and the SPA authenticates and
  strips the token from the address bar. The fragment never reaches the server.
- **Manual** — paste the same token into the console's token field.

Details in [identity-and-auth](../domain/identity-and-auth.md#what-the-console-login-flow-looks-like).

## The seven tabs

| Tab | What it shows | Backing endpoints |
|-----|---------------|-------------------|
| **Overview** | health, live provider status, headline numbers | `/v1/admin/providers/health`, `/statusz` |
| **Activity** | the request log + per-request drill-down (incl. captured prompt/response) | `/v1/admin/requests`, `/v1/admin/requests/{id}` |
| **Models** | the effective catalog, availability, and provider discovery views | `/v1/admin/catalog/models`, `.../effective-models`, `.../discovery/*` |
| **Catalog** | adopt / remove provider-library models; price overrides | `/v1/admin/catalog/adoptions`, `.../price-overrides/*` |
| **Caching** | response cache, prompt-cache auto-inject, warmth routing, stickiness TTLs, prewarm | `/v1/prewarm`, `/v1/admin/usage/cache-health`, `.../cache-savings` |
| **Usage** | aggregate spend and volume by model / task type; export | `/v1/admin/usage`, `.../usage/export` |
| **Settings** | provider API keys (stored encrypted, stored-beats-env); routing bindings | `/v1/credentials`, `/v1/admin/org/routing-policy` |

The tabs are all reads/writes against the gateway plane, so the console is a thin client — every
capability it exposes is also reachable directly over the API.

## What each tab configures

- **Settings → provider keys** — paste an OpenRouter / Fireworks / Cloudflare / OpenAI / Gemini /
  Anthropic key; it is encrypted at rest and used on the next request. Each row carries a
  collapsed "How to get this key" recipe; the acquisition facts (verified 2026-07-21):

  | Provider | Where to create it | Fields | Permission / scope |
  |----------|--------------------|--------|--------------------|
  | OpenRouter | openrouter.ai/keys | API key (`sk-or-v1-…`) | default |
  | Fireworks | app.fireworks.ai/settings/users/api-keys | API key | default |
  | Cloudflare | dash.cloudflare.com/profile/api-tokens | API token (40 chars) + **account ID: the 32-hex code in the dashboard URL**, `dash.cloudflare.com/<account-id>` | **"Workers AI" token template**, Account Resources: include your account |
  | OpenAI | platform.openai.com/api-keys | API key (`sk-proj-…`) | default |
  | Gemini | aistudio.google.com/apikey | API key (`AIza…`) | default |
  | Anthropic | console.anthropic.com/settings/keys | API key (`sk-ant-…`) | default |

  The Cloudflare account ID is hard-validated (exactly 32 hex chars, client + server) because a
  wrong id stores fine and then every request 404s; key fields get soft shape warnings only. See
  [catalog-and-providers](../domain/catalog-and-providers.md#bring-your-own-key-byok-provider-keys).
- **Settings → routing** — bind task types to models; the binding governs the operator's own traffic
  live (it is stored under the `local` scope). See [routing](../architecture/routing.md#configuring-routing-open-edition).
- **Catalog** — one-click adopt a discovered model into the effective catalog, or remove an adoption.
  See [catalog-and-providers](../domain/catalog-and-providers.md#catalog-adoption).
- **Caching** — toggle the response cache, prompt-cache auto-inject, and warmth routing; set
  per-task-type stickiness TTLs; run prewarm. See [caching](../architecture/caching.md).
