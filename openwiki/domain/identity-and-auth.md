# Identity & auth

Login is required on every non-public route. A caller with no valid credential gets `401`. The
public exceptions are liveness/readiness (`/healthz`, `/readyz`, `/statusz`), the console static
assets, and the auth routes themselves.

## Credentials

`routes/deps.py:require_auth` resolves a caller to an `Identity`. Three credential forms are
accepted, in this order:

1. **Operator bearer token** — `TOTO_GW_AUTH_TOKEN`, a permanent service credential, compared
   timing-safe. This is the single-operator credential for the whole deployment. Sent as
   `Authorization: Bearer <token>`, or (for Anthropic clients) as `x-api-key: <token>` — the gateway
   aliases `x-api-key` to a bearer, so the same token works on both surfaces.
2. **Per-user API token** — minted at `POST /v1/tokens`, stored as a sha256 hash, resolves to a
   normal user identity. Managed under `/v1/tokens` (list, mint, rotate, delete).
3. **Session cookie** — `toto_session`, a verified browser user, used by the console.

If `TOTO_GW_AUTH_TOKEN` is empty, auth is open — the single-operator dev posture. Set a token for
anything beyond a throwaway local run.

## The operator identity and the `local` scope

The open edition is single-tenant, so the operator **is** the only tenant. Rather than leave the
operator unscoped, the OSS edition binds it to a well-known sentinel scope: `OSS_LOCAL_ORG = "local"`.

This binding is what makes the console governance actually govern the operator's own traffic. When the
operator edits a routing binding or adopts a catalog model in the console (authed as operator), those
writes land under the `local` key. When the operator then makes a `Bearer <token>` API request,
`require_auth` resolves the operator identity **carrying the routing overlay and adoptions stored
under `local`** — resolved fresh per request, so console edits apply live. You can see this on the
trace: every operator-served turn records `org_id: "local"`.

(In the hosted enterprise edition the operator stays unscoped and multi-tenant callers name their own
org. That machinery is not part of this tree.)

## What the console login flow looks like

The console is a static SPA served at `/console`. It authenticates with the operator token:

- **Auto-login** — at boot, when token auth is on and the edition is `oss`, the launcher logs a
  ready-to-open URL with the token in the URL **fragment**:
  `http://127.0.0.1:8080/console/overview#token=<token>`. The fragment never reaches the server (so
  it stays out of access logs and `Referer` headers); the SPA reads it on load, authenticates, and
  strips it from the address bar.
- **Manual** — paste the same `TOTO_GW_AUTH_TOKEN` value into the console's token field.

See [the console note](../operations/console.md) for the tabs it exposes.

## Provider keys on the request

`require_auth` also decrypts the caller's stored provider keys into a request-scoped overlay
(`byok_keys`), which the OpenAI-compatible runner reads at dispatch — the caller's own key beats the
platform env key. The mechanics of storage and the at-rest secret are in
[catalog-and-providers](catalog-and-providers.md#bring-your-own-key-byok-provider-keys).

## Cross-origin protection

State-changing requests (`POST`/`PUT`/`PATCH`/`DELETE`) are checked: when an `Origin` header is
present, its host must match the request host, else `403`. Absent `Origin` (curl, scripts) passes;
`SameSite=Lax` on the session cookie already covers the browser cross-site case.
