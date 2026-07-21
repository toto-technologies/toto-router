# Catalog & providers

The **catalog** is the single source of truth for which models exist, what they cost, which lane and
residency class they belong to, and which upstream they reach. The gateway never hard-codes "the
frontier model" — even a plain passthrough resolves the incoming `model` field against the catalog to
pick a lane, an upstream, and a price.

## The catalog entry

Each entry (`catalog.py:CatalogEntry`) carries:

- **`id`** — the client-facing model id. Names a real model, never a tier word (`economy`, `frontier`,
  `smart`, … are banned from ids and enforced by a test).
- **`lane`** — the cost/compute tier: `economy` | `frontier` | `fake` | `provider`.
- **`residency_class`** — `in_perimeter` | `cloud`. Orthogonal to lane; this is what privacy routing
  keys off (see [routing](../architecture/routing.md)).
- **`endpoint`** — `fake`, a bare local URL, `anthropic`, or `openai` (any OpenAI-compatible host).
- **`base_url` + `api_key_env`** — for OpenAI-compatible providers: which host, and which env var
  holds the key.
- **`upstream_model`** — the concrete provider model id this entry pins.
- **`price_usd_per_1k`** — prompt / completion rates per 1k tokens, plus cache read/write multipliers.
- **`context_window`, `cache_ttl_s`, `tools`** — capability knobs.

## Provider modules

Every provider is "just a catalog entry" — `endpoint: openai` + a `base_url` + an `api_key_env`. The
same OpenAI-compatible runner reaches them all. The bundled fragments:

| Provider | File | `endpoint` | Key env | Notes |
|----------|------|-----------|---------|-------|
| Fake / echo | `catalog.yaml` | `fake` | — | offline test models `echo-local`, `echo-cloud` |
| Local (MLX/LM Studio/Ollama) | `catalog.yaml` | bare `http://127.0.0.1:8081/v1` | — | in-perimeter, runs on your box |
| Anthropic (native) | `catalog.yaml` | `anthropic` | `ANTHROPIC_API_KEY` | native Messages client |
| OpenAI | `catalog.yaml` | `openai` | `OPENAI_API_KEY` | direct |
| OpenRouter | `catalog.openrouter.yaml` | `openai` | `OPENROUTER_API_KEY` | the default smart-routing catalog |
| Fireworks | `catalog.fireworks.yaml` | `openai` | `FIREWORKS_API_KEY` | serverless + fine-tunes |
| Cloudflare Workers AI | `catalog.cloudflare.yaml` | `openai` | `CLOUDFLARE_API_TOKEN` (+ `CLOUDFLARE_ACCOUNT_ID`) | edge GPU inference |

Cloudflare is the one two-part credential: the account id is interpolated into `base_url` via
`${CLOUDFLARE_ACCOUNT_ID}`, expanded at client-construction time (stored credentials win over the
environment). Every other provider is a single key.

## Composing catalogs

Each provider owns its own fragment so no provider's models sit inside another's file. `Catalog.load`
merges a comma-separated list left to right; a later file extends or overrides an earlier one by id:

```bash
TOTO_GW_CATALOG="catalog.openrouter.yaml,catalog.fireworks.yaml,catalog.cloudflare.yaml"
```

### Default catalog resolution

When `TOTO_GW_CATALOG` is unset, the catalog defaults to `catalog.openrouter.yaml` if
`OPENROUTER_API_KEY` is in the environment (so a fresh clone with just that key gets working smart
routing — the shipped `labels.yaml` binds OpenRouter entries), else `catalog.yaml`. An explicit
`TOTO_GW_CATALOG` is never second-guessed. A key **stored in the console** (rather than an env var)
counts from the next boot: the OSS boot path peeks at stored provider keys and upgrades a defaulted
catalog accordingly.

## Provider inventory discovery

Read-only discovery endpoints list a provider's available models so you can see what's on offer
before adopting:

- `GET /v1/admin/catalog/discovery/openrouter`
- `GET /v1/admin/catalog/discovery/fireworks`
- `GET /v1/admin/catalog/discovery/cloudflare`
- `GET /v1/admin/catalog/models` / `.../effective-models` / `.../availability`

## Catalog adoption

Discovery shows what's available; **adoption** pins a specific provider-library model into your
effective catalog so routing can use it. Adoption is a stored, one-click action rather than a YAML
edit:

- `POST /v1/admin/catalog/adoptions` — adopt a model (materialized as a catalog entry, stamped
  `source: adopted`).
- `GET /v1/admin/catalog/adoptions` — list adopted models.
- `DELETE /v1/admin/catalog/adoptions/{id}` — **remove** an adoption.

Adopted entries are merged into the base catalog at the `effective_catalog` seam
(`catalog.py:effective_catalog`) at request time; the base catalog wins on an id collision. In the
open edition, adoptions live under the operator's `local` scope so the operator's own dispatch and
`/v1/models` see them.

**Price overrides** ride the same seam: `PUT`/`GET`/`DELETE /v1/admin/catalog/price-overrides/{model_id}`
replace an entry's prices (stamped `price_source: manual`) for cost accounting when the shipped or
discovered numbers are wrong for your account.

## Bring-your-own-key (BYOK) provider keys

`credentials.py` stores a caller's own provider API keys, **encrypted at rest** with Fernet. At
dispatch, the OpenAI-compatible runner uses the caller's stored key instead of the platform env key;
the env key is the fallback. Precedence is **stored beats env**.

- **Where** — the console **Settings** page (`PUT /v1/credentials/{provider}`, `GET /v1/credentials`,
  `DELETE /v1/credentials/{provider}`). A pasted key is used on the very next request, no restart.
- **At-rest secret** — the Fernet key is derived from `TOTO_GW_CREDENTIALS_SECRET`. In the open
  edition, if that's unset and the DB is a real SQLite file, the gateway generates a secret once and
  persists it beside the DB as `credentials.secret` (mode 0600) so pasting a key works with zero env
  vars. Stated tradeoff: that file sits on the same disk as the DB, so it defends a leaked DB file or
  backup, not a fully compromised host — set `TOTO_GW_CREDENTIALS_SECRET` (or the Vault KMS provider)
  to separate them. With no secret and no file (`:memory:` / Postgres), the write path fails closed
  (`503`) rather than storing plaintext.
- **Rotation** — `TOTO_GW_CREDENTIALS_SECRET_OLD` keeps the previous secret as a decrypt-only
  fallback during a zero-downtime rotation window.

The known BYOK providers are OpenRouter, Fireworks, Cloudflare, OpenAI, and Gemini. Multi-user and
org-wide key governance is a hosted-product concern; the open edition's BYOK is the single operator's
own keys.
