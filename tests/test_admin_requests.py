"""Per-request activity log (analytics A2) — GET /v1/admin/requests + metering.list_requests.

The decision-trail list over `gateway_events`: newest-first, metadata-only, role-scoped. The
dual-dialect `trace_engine` fixture mirrors test_metering.py (an unkeyed `pytest` runs the sqlite
param + skips pg; `pytest -m pg` selects Postgres) so the list SQL — desc order, lexical ts range,
the column projection — is proven on both dialects. HTTP scoping (member-own-only, admin-org,
cross-org denied) rides the `activity_app` fixture with dependency-overridden identities.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from toto_gateway.metering import _REQUEST_COLS, list_requests
from toto_gateway.trace import TraceRow

_PG_URL = os.environ.get("TOTO_GW_TEST_DATABASE_URL")

# Every column gateway_events (and thus the trace) carries — the projection must expose NONE that
# smells like content. Content was never stored, but this asserts the boundary at the API shape.
_CONTENT_KEYS = {"prompt", "completion", "messages", "response", "content", "text", "body", "output"}


def _pg_engine_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@pytest.fixture(params=[
    pytest.param("sqlite", id="sqlite"),
    pytest.param("postgres", id="postgres", marks=[
        pytest.mark.pg,
        pytest.mark.skipif(not _PG_URL, reason="set TOTO_GW_TEST_DATABASE_URL for the PG lane"),
    ]),
])
def trace_engine(request):
    if request.param == "sqlite":
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
    else:
        engine = create_engine(_pg_engine_url(_PG_URL))
    SQLModel.metadata.create_all(engine)
    return engine


def _write(engine, **kw) -> None:
    row = dict(request_id="r", ts_start="2026-07-08T10:00:00+00:00", lane="frontier",
               runner_id="openrouter", model="or-sonnet", residency_class="cloud", status="ok")
    row.update(kw)
    with Session(engine) as s:
        s.add(TraceRow(**row))
        s.commit()


# --- metering.list_requests: shape, ordering, filters, pagination, metadata-only ---------------

def test_newest_first_and_decision_trail_shape(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, ts_start="2026-07-08T09:00:00+00:00", model="a")
    _write(trace_engine, org_id=org, ts_start="2026-07-08T11:00:00+00:00", model="b")
    _write(trace_engine, org_id=org, ts_start="2026-07-08T10:00:00+00:00", model="c")

    rows = list_requests(trace_engine, org_id=org)
    assert [r["model"] for r in rows] == ["b", "c", "a"]  # ts desc
    r = rows[0]
    assert set(r) == set(_REQUEST_COLS.values())  # exactly the decision-trail keys
    assert r["ts"] == "2026-07-08T11:00:00+00:00"
    # metadata only: not one key looks like prompt/response content
    assert not (_CONTENT_KEYS & set(r))


def test_decision_trail_field_mapping(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, model="or-sonnet", label="code_generation",
           route_reason="label:code_generation", lane="frontier", residency_class="cloud",
           tokens_prompt=100, tokens_completion=40, cost_usd=1.5, cost_estimated=True,
           frontier_baseline_usd=5.0, latency_ms_total=1234, guard_action="allow",
           status="ok", user_id="u1", team_id="t1")
    [r] = list_requests(trace_engine, org_id=org)
    assert isinstance(r.pop("id"), int)  # stable per-row id the detail endpoint opens
    assert r == {"ts": "2026-07-08T10:00:00+00:00", "model": "or-sonnet",
                 "conversation_key": None,
                 "classified_as": "code_generation", "route_reason": "label:code_generation",
                 "lane": "frontier", "residency": "cloud", "tokens_prompt": 100,
                 "tokens_cached": 0, "tokens_cache_write": 0, "tokens_completion": 40,
                 "cost_usd": 1.5, "cost_estimated": True,
                 "frontier_baseline_usd": 5.0, "latency_ms": 1234, "guard_action": "allow",
                 "status": "ok", "user_id": "u1", "team_id": "t1"}


def test_filters_model_label_and_time_window(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, ts_start="2026-07-08T09:00:00+00:00", model="or-sonnet",
           label="code_generation")
    _write(trace_engine, org_id=org, ts_start="2026-07-08T10:00:00+00:00", model="local-mlx",
           label="brainstorming")
    _write(trace_engine, org_id=org, ts_start="2026-07-09T10:00:00+00:00", model="or-sonnet",
           label="code_generation")

    assert len(list_requests(trace_engine, org_id=org, model="or-sonnet")) == 2
    assert len(list_requests(trace_engine, org_id=org, label="brainstorming")) == 1
    # inclusive [start, end] lexical window: only the two 07-08 rows
    win = list_requests(trace_engine, org_id=org, start="2026-07-08T00:00:00+00:00",
                        end="2026-07-08T23:59:59+00:00")
    assert {r["ts"] for r in win} == {"2026-07-08T09:00:00+00:00", "2026-07-08T10:00:00+00:00"}


def test_filter_by_conversation_key(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, request_id="a", conversation_key="deadbeefdeadbeef")
    _write(trace_engine, org_id=org, request_id="b", conversation_key="deadbeefdeadbeef")
    _write(trace_engine, org_id=org, request_id="c", conversation_key="0000111122223333")
    rows = list_requests(trace_engine, org_id=org, conversation_key="deadbeefdeadbeef")
    assert len(rows) == 2
    assert all(r["conversation_key"] == "deadbeefdeadbeef" for r in rows)


def test_limit_offset_pagination(trace_engine):
    org = "o_" + uuid.uuid4().hex
    for h in range(5):
        _write(trace_engine, org_id=org, ts_start=f"2026-07-08T1{h}:00:00+00:00", model=f"m{h}")
    page1 = list_requests(trace_engine, org_id=org, limit=2, offset=0)
    page2 = list_requests(trace_engine, org_id=org, limit=2, offset=2)
    assert [r["model"] for r in page1] == ["m4", "m3"]  # newest first
    assert [r["model"] for r in page2] == ["m2", "m1"]


def test_user_scope_filters_to_one_user(trace_engine):
    org = "o_" + uuid.uuid4().hex
    _write(trace_engine, org_id=org, user_id="u1", model="a")
    _write(trace_engine, org_id=org, user_id="u2", model="b")
    rows = list_requests(trace_engine, org_id=org, user_id="u1")
    assert [r["user_id"] for r in rows] == ["u1"]


# --- HTTP surface: require_auth (not admin-only) + role-scoping ---------------------------------


@pytest.fixture()
def activity_app(tmp_path):
    """A full app whose gateway writes traces to a SQL sink, so /v1/admin/requests can read them."""
    from fastapi.testclient import TestClient

    from toto_gateway.app import create_app
    from toto_gateway.config import Settings
    from toto_gateway.trace import sql_engine

    trace_db = f"sqlite:///{tmp_path}/trace.db"
    settings = Settings(catalog="catalog.yaml", trace_jsonl="", trace_db=trace_db,
                        trace_stdout=False, auth_token="test-operator-token", db=":memory:",
                        fake_exec=True)
    app = create_app(settings=settings)
    with TestClient(app) as client:
        engine = sql_engine(app.state.gateway.writer)
        yield client, app, engine


def _seed(engine, **kw):
    row = dict(request_id="r", ts_start="2026-07-08T10:00:00+00:00", lane="frontier",
               runner_id="openrouter", model="or-sonnet", residency_class="cloud", status="ok")
    row.update(kw)
    with Session(engine) as s:
        s.add(TraceRow(**row))
        s.commit()


def _as(app, **kw):
    from toto_gateway.routes.deps import Identity, require_auth
    app.dependency_overrides[require_auth] = lambda: Identity(authenticated=True, **kw)


def test_unauthenticated_is_401(activity_app):
    client, _app, _engine = activity_app
    # No credential at all (strip the operator bearer the TestClient would otherwise send).
    assert client.get("/v1/admin/requests", headers={"authorization": ""}).status_code == 401


def test_member_sees_only_own_rows(activity_app):
    client, app, engine = activity_app
    _seed(engine, org_id="o_a", user_id="u1", model="mine")
    _seed(engine, org_id="o_a", user_id="u2", model="theirs")
    _as(app, user_id="u1", org_id="o_a", role="member")
    body = client.get("/v1/admin/requests").json()
    assert {r["model"] for r in body["requests"]} == {"mine"}       # never u2's row
    assert all(r["user_id"] == "u1" for r in body["requests"])
    app.dependency_overrides.clear()


def test_member_user_filter_to_another_user_is_ignored(activity_app):
    client, app, engine = activity_app
    _seed(engine, org_id="o_a", user_id="u1", model="mine")
    _seed(engine, org_id="o_a", user_id="u2", model="theirs")
    _as(app, user_id="u1", org_id="o_a", role="member")
    # A member trying to pivot to u2 still gets only their own rows (IDOR floor).
    body = client.get("/v1/admin/requests", params={"user": "u2"}).json()
    assert {r["model"] for r in body["requests"]} == {"mine"}
    app.dependency_overrides.clear()


def test_admin_sees_whole_org(activity_app):
    client, app, engine = activity_app
    _seed(engine, org_id="o_a", user_id="u1", model="a1")
    _seed(engine, org_id="o_a", user_id="u2", model="a2")
    _seed(engine, org_id="o_b", user_id="u3", model="b1")  # another org — must not appear
    _as(app, user_id="admin1", org_id="o_a", role="admin")
    body = client.get("/v1/admin/requests").json()
    assert {r["model"] for r in body["requests"]} == {"a1", "a2"}   # whole org, not org b
    # admin may narrow to one user
    narrowed = client.get("/v1/admin/requests", params={"user": "u2"}).json()
    assert {r["model"] for r in narrowed["requests"]} == {"a2"}
    app.dependency_overrides.clear()


def test_cross_org_never_leaks(activity_app):
    client, app, engine = activity_app
    _seed(engine, org_id="o_a", user_id="u1", model="a1")
    _seed(engine, org_id="o_b", user_id="u9", model="SECRET_B")
    # Admin of org A, even naming org B's user, never sees org B's rows.
    _as(app, user_id="admin1", org_id="o_a", role="admin")
    body = client.get("/v1/admin/requests", params={"user": "u9"}).json()
    assert body["requests"] == []                                  # u9 is not in org A
    assert "SECRET_B" not in {r["model"] for r in
                              client.get("/v1/admin/requests").json()["requests"]}
    app.dependency_overrides.clear()


def test_operator_sees_all_orgs(activity_app):
    client, app, engine = activity_app  # default client carries the operator bearer
    _seed(engine, org_id="o_a", user_id="u1", model="a1")
    _seed(engine, org_id="o_b", user_id="u9", model="b1")
    body = client.get("/v1/admin/requests").json()
    assert {r["model"] for r in body["requests"]} == {"a1", "b1"}  # unrestricted


def test_pagination_next_offset(activity_app):
    client, app, engine = activity_app
    for h in range(3):
        _seed(engine, org_id="o_a", user_id="u1", ts_start=f"2026-07-08T1{h}:00:00+00:00")
    _as(app, user_id="u1", org_id="o_a", role="member")
    first = client.get("/v1/admin/requests", params={"limit": 2}).json()
    assert len(first["requests"]) == 2 and first["next_offset"] == 2
    second = client.get("/v1/admin/requests", params={"limit": 2, "offset": 2}).json()
    assert len(second["requests"]) == 1 and second["next_offset"] is None  # last page
    app.dependency_overrides.clear()


def test_response_never_carries_content(activity_app):
    client, app, engine = activity_app
    _seed(engine, org_id="o_a", user_id="u1")
    _as(app, user_id="u1", org_id="o_a", role="member")
    [row] = client.get("/v1/admin/requests").json()["requests"]
    assert not (_CONTENT_KEYS & set(row))  # metadata only — no prompt/response fields


def test_list_row_carries_stable_id(activity_app):
    client, app, engine = activity_app
    _seed(engine, org_id="o_a", user_id="u1")
    _as(app, user_id="u1", org_id="o_a", role="member")
    [row] = client.get("/v1/admin/requests").json()["requests"]
    assert isinstance(row["id"], int)  # the key the detail endpoint opens
    app.dependency_overrides.clear()


# --- content-capture (TOTO_GW_LOG_CONTENT): detail endpoint + retention -------------------------
# The activity DRILL-DOWN: GET /v1/admin/requests/{id} → {request(metadata), prompt, response,
# content_available}. Captured at the gateway trace-finalize choke point when the flag is on; the
# detail is scoped exactly like the list (member=own, admin=org, operator=all — a foreign id 404s).


def _content_app(tmp_path, *, log_content: bool):
    """A full fake-exec app with content-capture on/off, so a real completion round-trips through
    the gateway and the detail endpoint reads back what was (or wasn't) captured."""
    from fastapi.testclient import TestClient

    from toto_gateway.app import create_app
    from toto_gateway.config import Settings
    from toto_gateway.trace import sql_engine

    trace_db = f"sqlite:///{tmp_path}/trace.db"
    settings = Settings(catalog="catalog.yaml", trace_jsonl="", trace_db=trace_db,
                        trace_stdout=False, auth_token="test-operator-token", db=":memory:",
                        fake_exec=True, log_content=log_content)
    app = create_app(settings=settings)
    client = TestClient(app)
    client.__enter__()
    engine = sql_engine(app.state.gateway.writer)
    return client, app, engine


def _seed_content(engine, *, request_id, prompt, response, **kw) -> int:
    """Seed a gateway_events row + its request_content, return the row id (the detail key)."""
    from toto_gateway.trace import RequestContent

    row = dict(request_id=request_id, ts_start="2026-07-08T10:00:00+00:00", lane="frontier",
               runner_id="openrouter", model="or-sonnet", residency_class="cloud", status="ok")
    row.update(kw)
    tr = TraceRow(**row)
    with Session(engine) as s:
        s.add(tr)
        s.add(RequestContent(request_id=request_id, prompt=prompt, response=response,
                             created_ts=1_800_000_000.0))
        s.commit()
        s.refresh(tr)
        return tr.id


def test_detail_returns_captured_content_when_flag_on(tmp_path):
    client, app, engine = _content_app(tmp_path, log_content=True)
    r = client.post("/v1/chat/completions", json={
        "model": "echo-cloud", "messages": [{"role": "user", "content": "hello toto"}]})
    assert r.status_code == 200
    [row] = client.get("/v1/admin/requests").json()["requests"]
    detail = client.get(f"/v1/admin/requests/{row['id']}").json()
    assert detail["content_available"] is True
    assert detail["prompt"] == [{"role": "user", "content": "hello toto"}]
    assert "hello toto" in detail["response"]              # fake runner echoes the prompt
    assert detail["request"]["id"] == row["id"]            # metadata trail travels with it
    client.__exit__(None, None, None)


def test_detail_no_content_when_flag_off(tmp_path):
    from sqlmodel import select

    from toto_gateway.trace import RequestContent

    client, app, engine = _content_app(tmp_path, log_content=False)
    r = client.post("/v1/chat/completions", json={
        "model": "echo-cloud", "messages": [{"role": "user", "content": "secret prompt"}]})
    assert r.status_code == 200
    [row] = client.get("/v1/admin/requests").json()["requests"]
    detail = client.get(f"/v1/admin/requests/{row['id']}").json()
    assert detail["content_available"] is False
    assert "prompt" not in detail and "response" not in detail
    assert detail["request"]["model"] == "echo-cloud"   # metadata still returned
    with Session(engine) as s:                             # nothing was written
        assert s.exec(select(RequestContent)).first() is None
    client.__exit__(None, None, None)


def test_streamed_content_is_captured(tmp_path):
    client, app, engine = _content_app(tmp_path, log_content=True)
    r = client.post("/v1/chat/completions", json={
        "model": "echo-cloud", "stream": True,
        "messages": [{"role": "user", "content": "stream me"}]})
    assert r.status_code == 200
    _ = r.text  # drain the SSE so the stream generator's finally captures the joined text
    [row] = client.get("/v1/admin/requests").json()["requests"]
    detail = client.get(f"/v1/admin/requests/{row['id']}").json()
    assert detail["content_available"] is True
    assert detail["prompt"] == [{"role": "user", "content": "stream me"}]
    assert "stream me" in detail["response"]
    client.__exit__(None, None, None)


def test_detail_scoping_member_own_only(tmp_path):
    client, app, engine = _content_app(tmp_path, log_content=True)
    mine = _seed_content(engine, request_id="req-mine", org_id="o_a", user_id="u1",
                         prompt='[{"role":"user","content":"mine"}]', response="r1")
    theirs = _seed_content(engine, request_id="req-theirs", org_id="o_a", user_id="u2",
                           prompt='[{"role":"user","content":"theirs"}]', response="r2")
    _as(app, user_id="u1", org_id="o_a", role="member")
    own = client.get(f"/v1/admin/requests/{mine}")
    assert own.status_code == 200 and own.json()["content_available"] is True
    # another user's id in the SAME org → 404, indistinguishable from a genuinely absent id
    assert client.get(f"/v1/admin/requests/{theirs}").status_code == 404
    # a definitely-absent id → the SAME 404 (no existence leak)
    assert client.get("/v1/admin/requests/999999").status_code == 404
    app.dependency_overrides.clear()
    client.__exit__(None, None, None)


def test_detail_cross_org_404(tmp_path):
    client, app, engine = _content_app(tmp_path, log_content=True)
    b = _seed_content(engine, request_id="req-b", org_id="o_b", user_id="u9",
                      prompt='[{"role":"user","content":"SECRET_B"}]', response="secret")
    _as(app, user_id="admin1", org_id="o_a", role="admin")  # admin of org A
    assert client.get(f"/v1/admin/requests/{b}").status_code == 404  # org B never leaks
    app.dependency_overrides.clear()
    client.__exit__(None, None, None)


def test_prune_request_content_ages_out(trace_engine):
    import time

    from sqlmodel import select

    from toto_gateway.trace import RequestContent, prune_request_content

    now = time.time()
    with Session(trace_engine) as s:
        s.add(RequestContent(request_id="old", prompt="[]", response="x",
                             created_ts=now - 40 * 86400))
        s.add(RequestContent(request_id="new", prompt="[]", response="y",
                             created_ts=now - 1 * 86400))
        s.commit()
    assert prune_request_content(trace_engine, 30) == 1   # only the 40-day-old row
    assert prune_request_content(trace_engine, 0) == 0    # 0 = keep forever (no-op)
    with Session(trace_engine) as s:
        assert [r.request_id for r in s.exec(select(RequestContent))] == ["new"]
