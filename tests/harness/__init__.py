"""Engine-hardening test harness (Wave 0).

The substrate every other hardening chunk tests against:

  - appharness.in_process_app  — real create_app over httpx.ASGITransport + asgi_lifespan,
                                 with real concurrency (async client, lifespan + drain honored).
  - faults                     — composable respx/MockTransport provider faults on the OpenAI
                                 runner wire (500 / 429 / timeout / slow / partial-stream / flaky).
  - loaddriver.bench           — in-process asyncio load microbench (p50/p95/p99 + error rate).
  - asserts.assert_span / assert_log_field — span/log assertion helpers the obs chunk reuses.

Fixtures (`app_client`, `faults`) are published as a pytest plugin from `harness.fixtures`
(registered in tests/conftest.py). See tests/harness/README.md for the test contract.
"""
