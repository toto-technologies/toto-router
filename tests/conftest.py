"""Shared test fixtures for toto-gateway.

Provides:
  - catalog: the real catalog loaded from catalog.yaml
  - gateway: a Gateway wired to the fake lane with a MemoryTraceWriter
  - memory_writer: the MemoryTraceWriter attached to the gateway fixture
  - test_client: a TestClient against the full FastAPI app (fake lane only)
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from toto_gateway.app import create_app
from toto_gateway.catalog import Catalog
from toto_gateway.config import Settings
from toto_gateway.gateway import Gateway
from toto_gateway.runners.registry import RunnerRegistry
from toto_gateway.trace import MemoryTraceWriter

CATALOG_PATH = "catalog.yaml"  # relative to the repo root where pytest is invoked

# Engine-hardening harness (Wave 0): publishes the async `app_client` + `faults` fixtures.
# Opt-in — existing sync tests are unaffected. See tests/harness/README.md.
pytest_plugins = ("harness.fixtures",)

# --- Auth posture: login required, no anon (2026-07-04) --------------------------------------
# The operator bearer is the simplest test identity. Two pieces work together:
#   1. Every TestClient sends `Authorization: Bearer OP_TOKEN` by default (patched below at import).
#      This is INERT on an app whose auth_token is empty — _resolve_identity only checks the bearer
#      when a token is configured — and falls through on a mismatched token, so cookie/session
#      tests are unaffected. It authenticates only apps built with auth_token == OP_TOKEN.
#   2. The `_operator_auth` autouse fixture upgrades an empty auth_token to OP_TOKEN for every app,
#      EXCEPT the cookie-identity suite (test_auth), which must keep anon/cookie resolution so its
#      per-user isolation and login-required assertions stay meaningful.
OP_TOKEN = "test-operator-token"

_orig_testclient_init = TestClient.__init__


def _testclient_init(self, *args, **kwargs):
    _orig_testclient_init(self, *args, **kwargs)
    if "authorization" not in self.headers:
        self.headers["authorization"] = f"Bearer {OP_TOKEN}"


TestClient.__init__ = _testclient_init


@pytest.fixture(autouse=True)
def _operator_auth(request, monkeypatch):
    if request.module.__name__.rsplit(".", 1)[-1] in ("test_auth", "test_user_tokens", "test_multiorg"):
        yield  # cookie/anon/user-token identity tests manage auth themselves
        return
    orig = Settings.__init__

    def patched(self, *a, **k):
        orig(self, *a, **k)
        if not self.auth_token:
            object.__setattr__(self, "auth_token", OP_TOKEN)

    monkeypatch.setattr(Settings, "__init__", patched)
    yield


@pytest.fixture(autouse=True)
def _reset_session_rate_limiter():
    """The sessions rate limiter is a module-global deque; clear it between tests so POSTs in
    one test don't exhaust the 12/min window for another (they share one 60s wall window)."""
    from toto_gateway.routes import auth, sessions

    sessions._recent.clear()
    auth._hits.clear()  # per-IP fixed-window limiter on register/login/resend
    # Drain is a module-global flag set on lifespan shutdown; a prior `with TestClient` would
    # otherwise leave it True and 503 every later session POST.
    sessions._draining = False
    sessions._live_run_ids.clear()
    sessions._sse_connections = 0
    # Smart routing's label memo is a module-global TTL dict; a label cached by one test must
    # not answer another test's classify (SR2 stickiness cache).
    from toto_gateway.routing import smart

    smart._label_cache.clear()
    yield


# --- Dialect-parity store fixture (testing.md H5) --------------------------------------------
# The store-touching fixture parametrized over BOTH dialects. The `sqlite` param is a fresh
# in-memory RunStore (no cleanup); the `postgres` param talks to a real PG (skipif-gated on
# TOTO_GW_TEST_DATABASE_URL, reusing test_pg_store.py:18) and truncates + releases the pool between
# tests. The postgres param carries `pytest.mark.pg`, so `pytest -m pg` (the CI postgres job)
# selects exactly the Postgres side, while an unkeyed `pytest` runs the sqlite side and skips PG.
_PG_URL = os.environ.get("TOTO_GW_TEST_DATABASE_URL")
_PARITY_TABLES = ("events", "sessions", "feedback", "preferences", "lists", "list_items",
                  "canvas_positions", "canvas_objects", "user_memory", "companion_tool_calls")


@pytest_asyncio.fixture(params=[
    pytest.param("sqlite", id="sqlite"),
    pytest.param("postgres", id="postgres", marks=[
        pytest.mark.pg,
        pytest.mark.skipif(not _PG_URL, reason="set TOTO_GW_TEST_DATABASE_URL for the PG lane"),
    ]),
])
async def sql_store(request):
    """A RunStore on each dialect. Same behavioural assertions must hold on both — that IS the
    dialect-parity check (a Postgres-only break like `INSERT OR IGNORE` passes sqlite, fails pg)."""
    from toto_gateway.runs import RunStore

    if request.param == "sqlite":
        yield RunStore(":memory:")          # fresh in-memory DB per test; nothing to clean up
        return
    r = RunStore(database_url=_PG_URL)
    for t in _PARITY_TABLES:
        r._db.execute(f"DELETE FROM {t}")   # truncate between tests (shared PG, sync init conn)
    try:
        yield r
    finally:
        await r.close_pool()                # release the async pool (else conns leak across tests)


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return Catalog.load(CATALOG_PATH)


@pytest.fixture()
def memory_writer() -> MemoryTraceWriter:
    return MemoryTraceWriter()


@pytest.fixture()
def gateway(catalog: Catalog, memory_writer: MemoryTraceWriter) -> Gateway:
    """Gateway backed by the real catalog + FakeRunner for all lanes + MemoryTraceWriter."""
    from toto_gateway.runners.fake import FakeRunner

    registry = RunnerRegistry(factory=lambda entry: FakeRunner(entry))
    return Gateway(catalog=catalog, registry=registry, writer=memory_writer)


@pytest.fixture()
def test_client(catalog: Catalog) -> TestClient:
    """TestClient against the full FastAPI app, fake lane only, no trace files."""
    settings = Settings(
        catalog=CATALOG_PATH,
        trace_jsonl="",
        trace_db="",
        trace_stdout=False,
        auth_token=OP_TOKEN,  # operator identity; the default client sends the matching bearer
        db=":memory:",
    )
    from toto_gateway.runners.fake import FakeRunner

    registry = RunnerRegistry(factory=lambda entry: FakeRunner(entry))
    writer = MemoryTraceWriter()
    gw = Gateway(catalog=catalog, registry=registry, writer=writer)
    app = create_app(settings=settings, gateway=gw)
    return TestClient(app, raise_server_exceptions=True)
