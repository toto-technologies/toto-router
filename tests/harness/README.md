# Engine-hardening test harness (Wave 0)

The substrate every other hardening chunk tests against. Pure-Python, offline, no new
dependencies (reuses `httpx.ASGITransport`, `asgi-lifespan`, `respx`/`httpx.MockTransport`,
already in `pyproject.toml`). k6 (`scripts/loadtest/`) stays as-is for the staging design-point;
this harness is the in-CI, in-process signal.

## The test contract

**Every chunk ships:**

1. **A unit test** — the logic in isolation.
2. **An integration test** driving the real `create_app` in-process, with providers faked **at the
   wire** via the `faults` fixtures (never a bypassed fake callable). Import `in_process_app` /
   `app_client`.
3. **A load or failure assertion where it touches a perf or failure surface** — an in-process
   microbench delta (`harness.loaddriver.bench`, assert the SLO) and/or a `faults` injection
   asserting the observable resilience behavior.
4. **Dual-dialect where it touches SQL** — the same tests run green on SQLite **and** Postgres
   (`pytest -m pg`, gated on `TOTO_GW_TEST_DATABASE_URL`). *(Marker/lane is Chunk H5.)*

The `/ship` gate enforces 1–3 offline in the CI `test` job; 4 runs in the CI `postgres` job.

## What's here

| Module | What it gives you |
|---|---|
| `appharness.py` | `in_process_app(gateway=None, **settings)` → `(AsyncClient, app)` over `ASGITransport` + `LifespanManager`, operator bearer pre-stamped, real concurrency. `drive_to_terminal(app, run_id)` polls a run to a terminal state. |
| `faults.py` | `Faults()` — composable provider faults on the **OpenAI runner wire**: `ok`, `http_500`, `http_429(retry_after=)`, `http_400`, `timeout`, `slow(ms)`, `flaky(fail_first_n)`, `partial_stream(cut_after_n)`. `.gateway(faults, ...)` builds a real `Gateway` whose OpenAI runners are faulted (single handler = all models, or a `{model_id: handler}` map). Faults travel through the real OpenAI SDK exception mapping. |
| `loaddriver.py` | `bench(client, path, n, concurrency, ...) → Stats` (p50/p95/p99 + error rate). `python -m harness.loaddriver` runs the percentile self-check. |
| `asserts.py` | `assert_span(spans, node, **fields)`, `find_spans`, `assert_log_field` — for span/event/log assertions (works on both `observe=list.append` spans and `store.events_after` rows). |
| `fixtures.py` | Pytest fixtures `app_client` (→ `(AsyncClient, app)`) and `faults` (→ `Faults`), registered via `pytest_plugins` in `tests/conftest.py`. |

## Recipes

```python
# (b) integration through the real app, provider faked at the wire
async def test_it(app_client):           # fake-lane default app
    client, app = app_client
    r = await client.post("/v1/sessions", json={"query": "hi"})
    assert r.status_code == 202

# fault the wire and drive the whole app path
from harness.appharness import default_settings, in_process_app, drive_to_terminal
gw = faults.gateway({"wire-a": faults.http_429()})              # wire-a 429s, rest ok
settings = default_settings(fake_exec=False, driver_model="wire-a", triage_model="wire-a")
async with in_process_app(gw, settings=settings) as (client, app):
    run_id = (await client.post("/v1/sessions", json={"query": "hi"})).json()["run_id"]
    row = await drive_to_terminal(app, run_id)                  # → "done" via fallback

# (c) load / SLO delta
from harness.loaddriver import bench
s = await bench(client, "/readyz", n=500, concurrency=50, ok=(200,))
assert s.p95 < 300 and s.error_rate < 0.01
```

## CI

The new async / failure / microbench tests live under `tests/` and are collected by the existing
`test` job's `pytest -v` — no separate lane, no server, seconds of runtime. The dialect lane
(`-m pg`) is the `postgres` job (Chunk H5).
