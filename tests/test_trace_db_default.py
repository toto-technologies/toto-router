"""OSS zero-config tracing: the trace DB defaults beside a file-backed gateway DB so Activity
and Usage work out of the box; explicit values and the `off` sentinel always win; the header
spend endpoint answers an honest empty rollup (never a 503) when no trace DB exists."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from toto_gateway.app import create_app
from toto_gateway.config import Settings


def _settings(**over) -> Settings:
    base = dict(
        catalog="catalog.yaml", trace_jsonl="", trace_stdout=False,
        driver=True, fake_exec=True, db=":memory:", toto_token="",
        driver_model="echo-cloud", triage_model="echo-local", cookie_secure=False,
    )
    return Settings(**{**base, **over})


# --- the resolver -------------------------------------------------------------


def test_unset_defaults_beside_file_gateway_db(tmp_path):
    s = _settings(db=f"{tmp_path}/data/gw.db")
    assert s.trace_db == f"sqlite:///{(tmp_path / 'data' / 'traces.db').resolve()}"
    assert Path(f"{tmp_path}/data").is_dir()  # created so sqlite can open the file at boot


def test_explicit_url_always_wins(tmp_path):
    s = _settings(db=f"{tmp_path}/gw.db", trace_db="postgresql://elsewhere/traces")
    assert s.trace_db == "postgresql://elsewhere/traces"


def test_off_sentinel_disables_tracing(tmp_path):
    s = _settings(db=f"{tmp_path}/gw.db", trace_db="off")
    assert s.trace_db == ""
    s = _settings(db=f"{tmp_path}/gw.db", trace_db="OFF")  # case-insensitive
    assert s.trace_db == ""


def test_memory_and_postgres_boots_get_no_default(tmp_path):
    assert _settings().trace_db == ""  # :memory:
    assert _settings(db=f"{tmp_path}/gw.db",
                     database_url="postgresql://pg/app").trace_db == ""


def test_enterprise_edition_gets_no_default(tmp_path):
    assert _settings(db=f"{tmp_path}/gw.db", edition="enterprise").trace_db == ""


# --- spend endpoint: no engine → honest empty, not 503 ------------------------


def test_usage_without_trace_db_is_empty_not_503():
    with TestClient(create_app(settings=_settings())) as client:  # :memory: → no engine
        r = client.get("/v1/admin/usage")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rows"] == []
        assert body["trace_db"] is False
        # the drill-down routes keep their informative 503 (a real requirement, stated clearly)
        assert client.get("/v1/admin/usage/cache-savings").status_code == 503


def test_usage_with_trace_db_reports_rows_after_traffic(tmp_path):
    """Out-of-box Activity/Usage: a zero-trace-env file-DB boot traces fake traffic and both
    surfaces answer with data."""
    with TestClient(create_app(settings=_settings(db=f"{tmp_path}/gw.db"))) as client:
        r = client.post("/v1/chat/completions",
                        json={"model": "echo-local", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text

        usage = client.get("/v1/admin/usage").json()
        assert usage.get("trace_db") is not False
        assert len(usage["rows"]) == 1

        activity = client.get("/v1/admin/requests")
        assert activity.status_code == 200, activity.text
        assert activity.json()["requests"], "traced request missing from the activity log"
    assert (tmp_path / "traces.db").is_file()
