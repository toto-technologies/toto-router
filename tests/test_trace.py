"""Tests for toto_gateway.trace — TraceRecord, writers, and MultiTraceWriter resilience."""

from __future__ import annotations

import json
import time

import pytest

from toto_gateway.trace import (
    JsonlTraceWriter,
    MemoryTraceWriter,
    MultiTraceWriter,
    SqlModelTraceWriter,
    TraceRecord,
)


# --- TraceRecord.begin / finish ---


def test_begin_sets_ts_start():
    """begin() populates ts_start with a non-empty ISO timestamp."""
    rec = TraceRecord.begin(
        request_id="req-abc",
        lane="fake",
        runner_id="fake-echo-local",
        model="echo-local",
        residency_class="in_perimeter",
        stream=False,
        harness=None,
        task_id=None,
    )
    assert rec.ts_start
    assert "T" in rec.ts_start  # ISO 8601 separator


def test_begin_ts_end_is_none():
    """begin() leaves ts_end as None — the call is still in flight."""
    rec = TraceRecord.begin(
        request_id="req-1",
        lane="fake",
        runner_id="fake",
        model="echo-local",
        residency_class="in_perimeter",
        stream=False,
        harness=None,
        task_id=None,
    )
    assert rec.ts_end is None


def test_finish_sets_ts_end():
    """finish() populates ts_end."""
    rec = TraceRecord.begin(
        request_id="req-2",
        lane="fake",
        runner_id="fake",
        model="echo-local",
        residency_class="in_perimeter",
        stream=False,
        harness=None,
        task_id=None,
    )
    rec.finish()
    assert rec.ts_end is not None
    assert "T" in rec.ts_end


def test_finish_is_idempotent():
    """Calling finish() twice does not change ts_end (set-once semantics)."""
    rec = TraceRecord.begin(
        request_id="req-3",
        lane="fake",
        runner_id="fake",
        model="echo-local",
        residency_class="in_perimeter",
        stream=False,
        harness=None,
        task_id=None,
    )
    rec.finish()
    first_end = rec.ts_end
    time.sleep(0.001)  # tiny pause to let the clock tick
    rec.finish()
    assert rec.ts_end == first_end


def test_begin_carries_request_id():
    """begin() stores the request_id exactly."""
    rec = TraceRecord.begin(
        request_id="req-xyz-123",
        lane="frontier",
        runner_id="anthropic",
        model="claude-sonnet-4.6",
        residency_class="cloud",
        stream=True,
        harness="pi",
        task_id="task-001",
    )
    assert rec.request_id == "req-xyz-123"
    assert rec.lane == "frontier"
    assert rec.residency_class == "cloud"
    assert rec.harness == "pi"
    assert rec.task_id == "task-001"
    assert rec.stream is True


def test_default_status_is_ok():
    """TraceRecord defaults to status='ok'."""
    rec = TraceRecord.begin(
        request_id="r",
        lane="fake",
        runner_id="fake",
        model="echo-local",
        residency_class="in_perimeter",
        stream=False,
        harness=None,
        task_id=None,
    )
    assert rec.status == "ok"


# --- analytics dimensions (A1): label + user_id ---


@pytest.mark.parametrize("reason,expected", [
    ("label:code_generation", "code_generation"),
    ("label:code_generation:team", "code_generation"),
    ("label:code_generation:fallback", "code_generation"),
    ("label:legal_review", "legal_review"),
    ("catalog", None),
    ("cache", None),
    ("smart:classify_failed", None),
    ("guard:downgrade_local", None),
    ("policy:model_not_permitted", None),
    ("label:", None),
    (None, None),
])
def test_label_from_reason(reason, expected):
    from toto_gateway.trace import _label_from_reason

    assert _label_from_reason(reason) == expected


def test_finish_derives_label_from_route_reason():
    """finish() stamps the analytics label off the (final) route_reason — the one parse path."""
    rec = _make_record()
    rec.route_reason = "label:code_generation:team"
    rec.finish()
    assert rec.label == "code_generation"


def test_begin_carries_user_id():
    rec = TraceRecord.begin(
        request_id="r", lane="fake", runner_id="fake", model="echo-local",
        residency_class="in_perimeter", stream=False, harness=None, task_id=None,
        user_id="u_42",
    )
    assert rec.user_id == "u_42"


def test_gateway_events_has_label_and_user_columns():
    """Both analytics columns exist on the gateway_events table (this dialect)."""
    from toto_gateway.trace import TraceRow

    cols = set(TraceRow.__table__.c.keys())
    assert {"label", "user_id"} <= cols
    assert TraceRow.__table__.c["label"].index is True
    assert TraceRow.__table__.c["user_id"].index is True


def test_label_metadata_round_trips(tmp_path):
    """A record's label_metadata (JSON string) persists and reads back through the SQL sink."""
    from sqlmodel import Session, select

    from toto_gateway.trace import TraceRow

    w = SqlModelTraceWriter(f"sqlite:///{tmp_path}/t.db")
    rec = _make_record()
    rec.label_metadata = '{"component":"auth","scope":"backend"}'
    w.write(rec)
    with Session(w.engine) as s:
        row = s.exec(select(TraceRow)).first()
    assert json.loads(row.label_metadata) == {"component": "auth", "scope": "backend"}


def test_ensure_columns_adds_label_metadata_to_preexisting_table(tmp_path):
    """A gateway_events table created before label_metadata existed gets the column added
    (additive dual-dialect migration), so a select over the new model column can't fail."""
    from sqlalchemy import create_engine, inspect, text

    url = f"sqlite:///{tmp_path}/old.db"
    eng = create_engine(url)
    with eng.begin() as c:  # a pre-existing, pre-label_metadata table
        c.execute(text("CREATE TABLE gateway_events (id INTEGER PRIMARY KEY, request_id TEXT, "
                       "ts_start TEXT)"))
    w = SqlModelTraceWriter(url)  # __init__ runs _ensure_columns
    cols = {c["name"] for c in inspect(w.engine).get_columns("gateway_events")}
    assert "label_metadata" in cols


# --- JsonlTraceWriter ---


def _make_record(request_id: str = "req-test") -> TraceRecord:
    rec = TraceRecord.begin(
        request_id=request_id,
        lane="fake",
        runner_id="fake-echo-local",
        model="echo-local",
        residency_class="in_perimeter",
        stream=False,
        harness=None,
        task_id=None,
    )
    rec.status = "ok"
    rec.finish()
    return rec


def test_jsonl_writer_creates_file(tmp_path):
    """JsonlTraceWriter creates the file on first write."""
    path = str(tmp_path / "traces.jsonl")
    writer = JsonlTraceWriter(path)
    writer.write(_make_record())
    assert (tmp_path / "traces.jsonl").exists()


def test_jsonl_writer_unwritable_path_disables_with_one_warning(tmp_path, capsys):
    """A read-only location (the Railway container cwd) must cost ONE boot warning and a silent
    no-op sink — not a trace_sink_error on every request (the prod log noise this fixed)."""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)  # directory not writable → open(..., "a") raises at construction
    try:
        writer = JsonlTraceWriter(str(ro / "traces.jsonl"))
        assert writer._disabled is True
        warn = capsys.readouterr().err.strip().splitlines()
        assert len(warn) == 1 and "gateway.trace_jsonl_disabled" in warn[0]

        for i in range(3):  # subsequent writes: pure no-ops, zero further output
            writer.write(_make_record(f"req-{i}"))
        assert capsys.readouterr().err == ""
        assert not (ro / "traces.jsonl").exists()
    finally:
        ro.chmod(0o755)  # let pytest clean tmp_path up


def test_jsonl_writer_appends_valid_json(tmp_path):
    """Each write produces a valid JSON line."""
    path = str(tmp_path / "traces.jsonl")
    writer = JsonlTraceWriter(path)
    for i in range(3):
        writer.write(_make_record(f"req-{i}"))

    lines = (tmp_path / "traces.jsonl").read_text().strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        obj = json.loads(line)
        assert "request_id" in obj
        assert "ts_start" in obj


def test_jsonl_writer_appends_not_overwrites(tmp_path):
    """Multiple writes append — not overwrite."""
    path = str(tmp_path / "traces.jsonl")
    w = JsonlTraceWriter(path)
    w.write(_make_record("req-a"))
    w.write(_make_record("req-b"))
    lines = (tmp_path / "traces.jsonl").read_text().strip().split("\n")
    request_ids = [json.loads(ln)["request_id"] for ln in lines]
    assert "req-a" in request_ids
    assert "req-b" in request_ids


def test_jsonl_writer_record_has_all_required_fields(tmp_path):
    """Written JSON contains all important trace fields."""
    path = str(tmp_path / "traces.jsonl")
    w = JsonlTraceWriter(path)
    rec = _make_record()
    rec.tokens_prompt = 10
    rec.tokens_completion = 5
    rec.cost_usd = 0.001
    rec.cost_estimated = False
    w.write(rec)
    obj = json.loads((tmp_path / "traces.jsonl").read_text())
    for field in ("request_id", "ts_start", "ts_end", "lane", "model", "residency_class",
                  "tokens_prompt", "tokens_completion", "cost_usd", "cost_estimated", "status"):
        assert field in obj, f"missing field: {field}"


# --- MemoryTraceWriter ---


def test_memory_writer_collects_records():
    """MemoryTraceWriter accumulates records in order."""
    writer = MemoryTraceWriter()
    for i in range(4):
        writer.write(_make_record(f"req-mem-{i}"))
    assert len(writer.records) == 4
    assert [r.request_id for r in writer.records] == [f"req-mem-{i}" for i in range(4)]


def test_memory_writer_starts_empty():
    """MemoryTraceWriter starts with an empty records list."""
    writer = MemoryTraceWriter()
    assert writer.records == []


# --- MultiTraceWriter ---


def test_multi_writer_fans_out_to_all_sinks():
    """MultiTraceWriter delivers records to every sink."""
    a = MemoryTraceWriter()
    b = MemoryTraceWriter()
    multi = MultiTraceWriter([a, b])
    rec = _make_record()
    multi.write(rec)
    assert len(a.records) == 1
    assert len(b.records) == 1


def test_multi_writer_swallows_failing_sink():
    """A sink that raises must not prevent other sinks from receiving the record."""

    class BoomWriter:
        def write(self, record: TraceRecord) -> None:
            raise RuntimeError("intentional sink failure")

    good = MemoryTraceWriter()
    multi = MultiTraceWriter([BoomWriter(), good])
    rec = _make_record()
    # Must not raise
    multi.write(rec)
    # Good sink still received the record
    assert len(good.records) == 1


def test_multi_writer_swallows_multiple_failing_sinks():
    """Multiple failing sinks — all swallowed, the one good sink still writes."""

    class BoomWriter:
        def write(self, record: TraceRecord) -> None:
            raise ValueError("boom")

    good = MemoryTraceWriter()
    multi = MultiTraceWriter([BoomWriter(), BoomWriter(), good, BoomWriter()])
    multi.write(_make_record())
    assert len(good.records) == 1


def test_multi_writer_empty_sink_list():
    """MultiTraceWriter with no sinks silently succeeds."""
    multi = MultiTraceWriter([])
    multi.write(_make_record())  # should not raise


def test_multi_writer_all_sinks_receive_same_record():
    """All sinks receive the exact same TraceRecord object."""
    sinks = [MemoryTraceWriter() for _ in range(3)]
    multi = MultiTraceWriter(sinks)
    rec = _make_record("shared-req")
    multi.write(rec)
    for sink in sinks:
        assert sink.records[0].request_id == "shared-req"


# --- SqlModelTraceWriter ---


def test_sql_writer_inserts_row(tmp_path):
    """SqlModelTraceWriter writes a row that is queryable from the same DB."""
    db_path = str(tmp_path / "test_traces.db")
    db_url = f"sqlite:///{db_path}"
    writer = SqlModelTraceWriter(db_url)

    rec = _make_record("req-sql-001")
    rec.tokens_prompt = 50
    rec.tokens_completion = 30
    rec.cost_usd = 0.002
    rec.cost_estimated = False
    rec.status = "ok"
    writer.write(rec)

    # Query back using the same engine
    from sqlmodel import Session, select
    from toto_gateway.trace import TraceRow

    with Session(writer.engine) as session:
        rows = session.exec(select(TraceRow)).all()

    assert len(rows) == 1
    row = rows[0]
    assert row.request_id == "req-sql-001"
    assert row.tokens_prompt == 50
    assert row.tokens_completion == 30
    assert row.cost_usd == pytest.approx(0.002)
    assert row.cost_estimated is False
    assert row.status == "ok"


def test_sql_writer_multiple_rows(tmp_path):
    """Multiple writes produce multiple queryable rows."""
    db_path = str(tmp_path / "multi_traces.db")
    writer = SqlModelTraceWriter(f"sqlite:///{db_path}")

    for i in range(5):
        rec = _make_record(f"req-sql-{i}")
        writer.write(rec)

    from sqlmodel import Session, select
    from toto_gateway.trace import TraceRow

    with Session(writer.engine) as session:
        rows = session.exec(select(TraceRow)).all()

    assert len(rows) == 5
    request_ids = {r.request_id for r in rows}
    assert request_ids == {f"req-sql-{i}" for i in range(5)}


def test_sql_writer_task_id_indexed(tmp_path):
    """task_id is stored and queryable (it's an indexed field on TraceRow)."""
    db_path = str(tmp_path / "task_traces.db")
    writer = SqlModelTraceWriter(f"sqlite:///{db_path}")

    rec = _make_record("req-task")
    rec.task_id = "task-coding-001"
    writer.write(rec)

    from sqlmodel import Session, select
    from toto_gateway.trace import TraceRow

    with Session(writer.engine) as session:
        rows = session.exec(
            select(TraceRow).where(TraceRow.task_id == "task-coding-001")
        ).all()

    assert len(rows) == 1
    assert rows[0].task_id == "task-coding-001"


def test_trace_writer_adds_missing_columns_to_existing_table(tmp_path):
    """Regression: create_all never ALTERs an existing gateway_events; the writer's _ensure_columns
    must ADD model columns (label/user_id) that a pre-existing table lacks — else the activity
    query 500s on a deployed DB (bit us live 2026-07-08). SQLite proxy for the dialect-agnostic path."""
    import sqlite3
    from sqlalchemy import inspect
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE gateway_events (id INTEGER PRIMARY KEY, ts_start TEXT, model TEXT, lane TEXT)")
    con.commit(); con.close()
    from toto_gateway.trace import SqlModelTraceWriter
    w = SqlModelTraceWriter(f"sqlite:///{db}")
    cols = {c["name"] for c in inspect(w.engine).get_columns("gateway_events")}
    assert {"label", "user_id"}.issubset(cols), f"migration did not add columns: {sorted(cols)}"
    SqlModelTraceWriter(f"sqlite:///{db}")  # idempotent, no raise


def test_backfill_rewrites_legacy_model_ids(tmp_path):
    """Boot-time repair: pre-rename rows (`or-economy`, …) become canonical ids so no reader ever
    needs alias resolution. Idempotent — second boot no-ops."""
    from sqlalchemy import text

    from toto_gateway.trace import SqlModelTraceWriter

    db = f"sqlite:///{tmp_path}/t.db"
    w = SqlModelTraceWriter(db)
    with w.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO gateway_events (request_id, ts_start, ts_end, lane, runner_id, model, "
            "residency_class, status, tokens_prompt, tokens_completion, tokens_cached, "
            "tokens_cache_write, cost_usd, cost_estimated, frontier_baseline_usd, "
            "latency_ms_total, latency_ms_gateway_overhead, stream, cache_hit) VALUES "
            "('r1', '2026-07-08T00:00:00', '2026-07-08T00:00:01', 'economy', 'x', 'or-economy', "
            "'cloud', 'ok', 0, 0, 0, 0, 0.0, 0, 0.0, 0, 0, 0, 0)"))
    w2 = SqlModelTraceWriter(db)  # re-init runs the backfill on the existing table
    with w2.engine.begin() as conn:
        rows = conn.execute(text("SELECT model FROM gateway_events")).fetchall()
    assert [r[0] for r in rows] == ["or-qwen3-coder-flash"]
    SqlModelTraceWriter(db)  # idempotent
