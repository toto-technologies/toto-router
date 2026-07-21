# Testing

The suite is **fully offline**: no secrets, no network, no GPU. It runs entirely on the fake/echo
lanes and an isolated in-memory or temp-file database, so a plain `pytest` is deterministic and safe
to run anywhere.

## Running it

```bash
pip install -e ".[dev]"       # or: uv pip install -e ".[dev]"
pytest -q                     # the whole suite
pytest -q -k routing          # by keyword
```

`asyncio_mode = auto` is set, so async tests need no per-test decorator. `testpaths = ["tests"]`.

## Why it stays offline

- **Fake / echo lanes.** The `fake` runner (`endpoint: fake`) is a deterministic echo that never
  touches the network. `catalog.yaml` ships `echo-local` and `echo-cloud` for exactly this. Tests
  route against these, so routing, guards, caching, metering, and the trace path all exercise real
  code while execution is a stub.
- **`TOTO_GW_FAKE_EXEC`** forces every lane — including entries that would otherwise call a real
  provider — through the fake runner, so a test can exercise the *real* routing decision for a
  frontier model without a key.
- **Isolated DB.** Tests use `:memory:` (or a temp file) for the app store, so no state leaks between
  runs.

## The app harness

`tests/harness/` provides the fixtures that boot the app in-process without a live server
(`appharness.py`), drive it, and assert on traces and faults (`asserts.py`, `faults.py`,
`fixtures.py`, `loaddriver.py`). Use these rather than standing up a real uvicorn — they give you the
full app (routing brain, stores, trace sinks) against the fake lanes.

## Markers (skipped by default)

Two marked lanes are **not** part of a normal `pytest` run — each skips unless the relevant
environment is present, so an unkeyed local run never hits a provider or needs Postgres:

| Marker | Runs against | Gate |
|--------|--------------|------|
| `integration` | a real upstream (Fireworks / OpenRouter) | skipped unless the provider key env var is set; run with `pytest -m integration` |
| `pg` | a real Postgres (dialect-parity SQL tests) | skipped unless `TOTO_GW_TEST_DATABASE_URL` is set; run with `pytest -m pg` |

`filterwarnings = ["error::DeprecationWarning:toto_gateway.*"]` turns the project's own deprecation
warnings into failures, so internal API drift surfaces in CI.

## Lint

```bash
ruff check .
```

Run both green before pushing.
