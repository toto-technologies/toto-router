# The Anthropic Messages surface

`POST /v1/messages` accepts Anthropic Messages requests (streaming and non-streaming) and routes
them through the same catalog and routing brain as the OpenAI surface. The point is that an
Anthropic client — Claude Code, the `anthropic` SDK — can route through the gateway with no code
change.

## How it works

Everything inside the gateway is OpenAI-shaped (`schemas.ChatCompletion*`).
`toto_gateway/anthropic_surface.py` is the **only** place Anthropic-shaped bodies exist. It is a thin
boundary:

- `to_chat_request()` — Anthropic request body → the internal OpenAI request.
- `to_anthropic_response()` — internal response → Anthropic Message body.
- `stream_events()` — internal OpenAI chunk stream → the Anthropic named-event stream
  (`message_start` … `content_block_delta` … `message_stop`).

The actual translation — including the streaming state machine (content-block open/close, tool-call
argument deltas, stop-reason mapping) — is delegated to **nemo-switchyard**'s Rust translation
engine (a runtime dependency, Apache-2.0). The Python module just wires it to the gateway.

Between the boundaries, the request is planned, routed, cached, dispatched, metered, and traced
exactly like a `/v1/chat/completions` request. `smart` works here too: `--model smart` classifies and
routes.

## Auth

Anthropic SDK clients authenticate with an `x-api-key` header rather than a bearer. The gateway
aliases it: an `x-api-key` value is treated as `Authorization: Bearer <value>`, so the same
credential (the operator token, or a per-user API token) works on both surfaces. See
[identity-and-auth](../domain/identity-and-auth.md).

## Provenance headers

The OpenAI surface embeds provenance in the response body's `x_toto` block. The Anthropic surface —
whose body must stay a valid Anthropic Message — returns it as response headers instead:

| Header | Meaning |
|--------|---------|
| `x-toto-request-id` | the request id, also the trace key |
| `x-toto-model` | the catalog id actually served |
| `x-toto-lane` | the lane that served it (`fake` / `economy` / `frontier` / `provider`) |
| `x-toto-cost-usd` | metered cost of the turn |

The full trace record (task label, tokens, cache hit, latency stages, residency, and more) is written
to the trace sinks for every turn regardless of surface. See
[trace-and-metering](../domain/trace-and-metering.md).

## Wire it up

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8080 ANTHROPIC_API_KEY=$TOTO_GW_AUTH_TOKEN \
  claude --model <catalog-model-id>     # or --model smart
```

A quick check against the fake lane:

```bash
curl -s -D - http://127.0.0.1:8080/v1/messages \
  -H 'x-api-key: dev' -H 'content-type: application/json' \
  -d '{"model":"echo-local","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'
# → 200, an Anthropic Message body + x-toto-* headers
```
