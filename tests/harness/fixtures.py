"""Pytest fixtures published by the harness (registered as a plugin in tests/conftest.py).

Opt-in — the existing sync `test_client` fixture is untouched, so the 95 existing tests are
unaffected. New async tests just request `app_client` / `faults`.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from .appharness import in_process_app
from .faults import Faults


@pytest_asyncio.fixture
async def app_client():
    """(AsyncClient, app) bound to the real create_app in-process — fake-lane, driver on,
    operator bearer pre-stamped, lifespan startup + drain honored. Real concurrency."""
    async with in_process_app() as (client, app):
        yield client, app


@pytest.fixture
def faults() -> Faults:
    """Composable provider-fault factory for the OpenAI runner wire (see harness/faults.py)."""
    return Faults()
