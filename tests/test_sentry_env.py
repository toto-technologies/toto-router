"""O3 — Sentry environment tag (observability.md chunk 3).

init_sentry must forward `environment` so testing/staging/prod issues split in the Sentry UI.
"""

from __future__ import annotations

from toto_gateway.config import Settings
from toto_gateway.obs import init_sentry

_DSN = "https://k@example.ingest.sentry.io/1"


def _settings(**kw) -> Settings:
    return Settings(catalog="catalog.yaml", db=":memory:", **kw)


def test_sentry_init_forwards_environment(monkeypatch):
    import sentry_sdk

    captured: dict = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
    assert init_sentry(_settings(sentry_dsn=_DSN, sentry_environment="testing")) is True
    assert captured["environment"] == "testing"


def test_sentry_environment_defaults_to_unknown(monkeypatch):
    import sentry_sdk

    captured: dict = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
    assert init_sentry(_settings(sentry_dsn=_DSN)) is True  # env unset
    assert captured["environment"] == "unknown"  # never an empty bucket
