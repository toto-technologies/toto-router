"""The trace / provenance record — the most important artifact in Phase 0 (context doc §3).

Designed to be deployment-independent (guardrail #2) and the seed of the Phase-2 chain-of-custody.
One record per upstream call. `residency_class` and `request_id` exist from request #1 because
they are cheap now and a nightmare to retrofit. `cost_estimated` (Gary fold G2) makes it explicit
whenever we had to estimate tokens instead of using the upstream's reported usage, so the
north-star cost metric never silently lies.

Three sinks, behind one `TraceWriter`:
  - JSONL   : append-only file; the spike's demo artifact.
  - SQLModel: a row in Postgres/SQLite; inherits Toto's deploy/DB story for free.
  - stdout  : one OTel-style structured JSON line; cheap observability today, Langfuse later.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _label_from_reason(reason: str | None) -> str | None:
    """The task-type label embedded in a route_reason, or None.

    Smart/driver label routes stamp `label:<l>` | `label:<l>:team` | `label:<l>:fallback` → `<l>`.
    Every other reason carries no task type → None: `catalog` | `cache` | `guard:*` | `policy:*`
    | `smart:classify_failed` (unclassified) | a fallback string | None.

    The single label-parse — the trace writer (finalize) AND the analytics backfill both call this,
    so gateway_events.label and any retro backfill agree by construction.
    """
    if not reason or not reason.startswith("label:"):
        return None
    return reason.split(":", 2)[1] or None  # 'label:x' / 'label:x:team' / 'label:x:fallback' → x


class TraceRecord(BaseModel):
    """The provenance/audit record. Every field the Phase-2 rail will render is captured here."""

    request_id: str  # chain-of-custody anchor; stable across retries/escalation
    # Groups multi-turn requests into one conversation: 16-hex prefix of sha256 over
    # (system text) + NUL + (first user text) — stable across turns of the same chat. NULL for
    # non-chat traffic or a request with no user message. Additive; migrated by _ensure_columns.
    conversation_key: str | None = None
    ts_start: str
    ts_end: str | None = None
    lane: str  # TIER: economy | frontier | fake
    runner_id: str  # which box/provider answered
    model: str  # catalog id (never a hard-coded "smart model")
    residency_class: str  # LOCATION: in_perimeter (green) | cloud (indigo)
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_cached: int = 0  # prompt tokens the PROVIDER served from cache (context-caching P0)
    tokens_cache_write: int = 0  # prompt tokens the provider WROTE to cache (write-ledger; caching P&L)
    cost_usd: float = 0.0
    cost_estimated: bool = False  # True when token usage was estimated, not upstream-reported
    frontier_baseline_usd: float | None = None  # what frontier would have cost (savings denom)
    latency_ms_total: int | None = None
    latency_ms_gateway_overhead: int | None = None  # routing-tax baseline (guardrail #3)
    stream: bool = False
    status: str = "ok"  # ok | error
    error: str | None = None
    harness: str | None = None  # pi | opencode | raw
    task_id: str | None = None  # groups calls into a task for cost-per-task (§13)
    # Tenant identity (control-plane C1/metering-C4): who this call belongs to, so per-org/team
    # cost & usage rollups read straight off the trace/gateway_events. Deliberately NOT on the
    # Prometheus gw_* labels (cardinality — control-surface §5.8); the DB row carries it instead.
    # None for identity-thin callers (operator, driver-internal) — additive, changes no existing field.
    org_id: str | None = None
    team_id: str | None = None
    # Analytics dimensions (A1): the task TYPE and the USER, first-class so rollups can answer
    # "what kinds of work, how often, by whom" without parsing route_reason at query time.
    # `user_id` is stamped from the resolved identity (same seam as org/team). `label` is derived
    # from route_reason at finalize (see _label_from_reason) — one classified task type, or NULL
    # for unclassified / non-label routes (catalog | cache | guard:* | smart:classify_failed).
    # None on old rows → "unattributed" downstream; additive, changes no existing field.
    label: str | None = None
    user_id: str | None = None
    # W2-C7: the org data-classification the taxonomy classifier assigned this request (e.g.
    # "restricted"), NULL when the org configured no taxonomy. The constraint it triggered rides
    # guard_action (downgrade_local for local_only) / status (denied for deny), not a duplicate
    # column. Additive; _ensure_columns migrates it onto pre-existing tables.
    data_label: str | None = None
    # Dynamic provider-offer provenance. Static catalog calls leave these NULL.
    identity_id: str | None = None
    offer_id: str | None = None
    provider: str | None = None
    upstream_model: str | None = None
    credential_scope: str | None = None
    # The totoshape classifier's captured metadata (component/files/keywords/scope/intent) as a
    # JSON string, or NULL when the request wasn't classified in totoshape shape / had no metadata.
    # The queryable substrate for the org work-map (routes/admin_workmap). Additive → _ensure_columns
    # migrates it onto pre-existing tables; changes no existing field.
    label_metadata: str | None = None
    # --- Phase 1: the routing decision, logged so the rail can show WHY a lane was chosen ---
    route_reason: str | None = None  # human-readable route reason (catalog | exemplar:… | guard:…)
    # Shadow-mode trajectory signals (agentic turns only; None for plain chat): the run-stage score
    # this turn would have routed on if trajectory routing were live. Computed, never acted on.
    trajectory_score: float | None = None
    trajectory_confidence: float | None = None
    trajectory_top: str | None = None
    cache_hit: bool = False
    guard_action: str | None = None  # allow | downgrade_local | block
    signal_intent: str | None = None
    signal_complexity: str | None = None
    # W1-C1: the degradation reason when this request was served (or 503'd) by a failure floor
    # rather than the intended smart-routing intelligence — "classify_failed" | "policy_error" |
    # "breaker_open". None on normal requests. Additive; _ensure_columns migrates it onto old tables.
    degraded_mode: str | None = None
    # W1-C2: per-stage latency decomposition, so gateway_overhead (= total − upstream) is
    # explainable in SQL rather than one opaque number. Individual nullable columns (NOT a JSON blob)
    # so analytics aggregates them. `classify_ms` is the smart-router classify call (float ms straight
    # off SmartResult) — NULL/0 means NO classifier call ran this request (the FAST PATH: an
    # explicit-model/catalog request, a sticky-session memo hit, or a labels-off deploy). `plan_ms`
    # is the decision pipeline (policy resolution + guard + route + eligibility). `upstream_ms` is the
    # upstream provider wall (overhead ≈ total − upstream). All additive; _ensure_columns migrates.
    classify_ms: float | None = None
    plan_ms: int | None = None
    upstream_ms: int | None = None
    # W2-C5: how a team/org monthly budget touched this request — "over" (served past 100% under the
    # observe action), "downgraded" (forced onto the cheapest eligible model), or "rejected" (402'd,
    # trace still written). None on every request under budget or with no budget. Additive;
    # _ensure_columns migrates it onto old tables.
    budget_state: str | None = None
    # W3-C3: the request_id this call was escalated FROM — the routing-dissatisfaction signal a
    # "retry on frontier" carries. Stamped at the gateway's _begin_trace chokepoint from the
    # x-toto-escalated-from header (via obs.escalated_from_var), so passthrough, driver and companion
    # all record it. NULL on a first-attempt request. Additive; _ensure_columns migrates it on.
    escalated_from: str | None = None

    @classmethod
    def begin(
        cls,
        *,
        request_id: str,
        conversation_key: str | None = None,
        lane: str,
        runner_id: str,
        model: str,
        residency_class: str,
        stream: bool,
        harness: str | None,
        task_id: str | None,
        org_id: str | None = None,
        team_id: str | None = None,
        user_id: str | None = None,
        identity_id: str | None = None,
        offer_id: str | None = None,
        provider: str | None = None,
        upstream_model: str | None = None,
        credential_scope: str | None = None,
    ) -> "TraceRecord":
        return cls(
            request_id=request_id,
            conversation_key=conversation_key,
            ts_start=_utc_now_iso(),
            lane=lane,
            runner_id=runner_id,
            model=model,
            residency_class=residency_class,
            stream=stream,
            harness=harness,
            task_id=task_id,
            org_id=org_id,
            team_id=team_id,
            user_id=user_id,
            identity_id=identity_id,
            offer_id=offer_id,
            provider=provider,
            upstream_model=upstream_model,
            credential_scope=credential_scope,
        )

    def finish(self) -> "TraceRecord":
        if self.ts_end is None:
            self.ts_end = _utc_now_iso()
        # Derive the analytics label from the (now-final) route_reason. One parse, so cache-hit,
        # smart-route, driver, fallback + backfill all agree on what the task type was.
        if self.label is None:
            self.label = _label_from_reason(self.route_reason)
        return self


# --- SQLModel table ----------------------------------------------------------


class TraceRow(SQLModel, table=True):
    __tablename__ = "gateway_events"

    id: int | None = SQLField(default=None, primary_key=True)
    request_id: str = SQLField(index=True)
    conversation_key: str | None = SQLField(default=None, index=True)  # multi-turn grouping
    ts_start: str
    ts_end: str | None = None
    lane: str = SQLField(index=True)
    runner_id: str
    model: str = SQLField(index=True)
    residency_class: str = SQLField(index=True)
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_cached: int = 0
    tokens_cache_write: int = 0
    cost_usd: float = 0.0
    cost_estimated: bool = False
    frontier_baseline_usd: float | None = None
    latency_ms_total: int | None = None
    latency_ms_gateway_overhead: int | None = None
    stream: bool = False
    status: str = "ok"
    error: str | None = None
    harness: str | None = None
    task_id: str | None = SQLField(default=None, index=True)
    org_id: str | None = SQLField(default=None, index=True)   # tenant dimension (metering C4)
    team_id: str | None = SQLField(default=None, index=True)
    label: str | None = SQLField(default=None, index=True)     # task type (analytics A1)
    user_id: str | None = SQLField(default=None, index=True)   # who called (analytics A1)
    data_label: str | None = SQLField(default=None, index=True)  # W2-C7 data classification
    identity_id: str | None = SQLField(default=None, index=True)
    offer_id: str | None = SQLField(default=None, index=True)
    provider: str | None = SQLField(default=None, index=True)
    upstream_model: str | None = None
    credential_scope: str | None = SQLField(default=None, index=True)
    label_metadata: str | None = None                          # JSON: totoshape metadata (work-map)
    route_reason: str | None = None
    trajectory_score: float | None = None       # shadow-mode run-stage score (None = non-agentic)
    trajectory_confidence: float | None = None
    trajectory_top: str | None = None           # top-contributing dimension name
    cache_hit: bool = False
    guard_action: str | None = None
    signal_intent: str | None = None
    signal_complexity: str | None = None
    degraded_mode: str | None = SQLField(default=None, index=True)  # W1-C1 failure-floor reason
    classify_ms: float | None = None          # W1-C2: classify wall (NULL/0 = fast path, no classifier)
    plan_ms: int | None = None                # W1-C2: decision-pipeline wall (policy/guard/route)
    upstream_ms: int | None = None            # W1-C2: upstream provider wall (overhead = total − this)
    budget_state: str | None = None           # W2-C5: over | downgraded | rejected (NULL = under/no budget)
    escalated_from: str | None = SQLField(default=None, index=True)  # W3-C3: retried-from request id


class RequestContent(SQLModel, table=True):
    """Observability content-capture (Alex 2026-07-08): the actual prompt + response per request,
    keyed by `request_id` — a sibling of `gateway_events` so content ages out independently and the
    metadata table stays lean. Written ONLY when TOTO_GW_LOG_CONTENT is on, at the gateway's
    trace-finalize choke point. Read scope is the caller's (routes/admin_requests detail), same
    fail-closed org/user rule as the activity list. Dual-dialect: TEXT + REAL, no dialect specifics.

    ponytail: keyed by request_id (the chain-of-custody anchor), written once at the served turn.
    A fallback request writes several gateway_events rows but one content row — all its rows' detail
    views resolve to the same prompt+response, which is correct (it was one prompt, one served answer).
    """

    __tablename__ = "request_content"

    id: int | None = SQLField(default=None, primary_key=True)
    request_id: str = SQLField(index=True)
    prompt: str  # JSON array of the resolved request messages
    response: str  # the served assistant text (streamed text is joined)
    created_ts: float


def write_request_content(engine: Any, request_id: str, prompt: str, response: str) -> None:
    """Persist one request's prompt+response. Caller gates on the flag; this just writes."""
    from sqlmodel import Session

    with Session(engine) as session:
        session.add(RequestContent(request_id=request_id, prompt=prompt, response=response,
                                   created_ts=datetime.now(timezone.utc).timestamp()))
        session.commit()


def get_request_content(engine: Any, request_id: str) -> dict | None:
    """The captured prompt+response for a request_id, or None (flag was off / not captured / aged
    out). Newest row wins if a request_id somehow has several."""
    from sqlmodel import Session, select

    with Session(engine) as session:
        row = session.exec(
            select(RequestContent).where(RequestContent.request_id == request_id)
            .order_by(RequestContent.id.desc())
        ).first()
        return {"prompt": row.prompt, "response": row.response} if row else None


def prune_request_content(engine: Any, retention_days: int) -> int:
    """Age out request_content older than `retention_days` (content-retention reaper, sibling of
    delta retention). Returns rows deleted. retention_days <= 0 disables pruning (keep forever)."""
    if retention_days <= 0:
        return 0
    from sqlalchemy import delete, func, select as sa_select
    from sqlmodel import Session

    cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
    with Session(engine) as session:
        n = session.execute(
            sa_select(func.count()).select_from(RequestContent)
            .where(RequestContent.created_ts < cutoff)
        ).scalar_one()
        session.execute(delete(RequestContent).where(RequestContent.created_ts < cutoff))
        session.commit()
        return int(n)


class LabelVerdict(SQLModel, table=True):
    """Human routing-verdict on one real request (router-eval chunk 5): a judge tags a captured
    routing decision good/bad and optionally supplies the correct label. Sibling of `gateway_events`
    — one row per (request_id, judge). `query_text` / `predicted_label` / `model_served` are
    DENORMALIZED copies taken at verdict time on purpose: `request_content` ages out (30d), but a
    verdict is gold that must survive to feed the label eval set forever. Read/write scope is the
    caller's org, fail-closed (routes/admin_labeling), same IDOR floor as the activity list.

    ponytail: no DB unique constraint — the upsert reads-then-writes per (request_id, judge). Two
    concurrent verdicts on the same request by the same judge is not a real race (one human, one
    tab); add a UniqueConstraint if that ever stops being true.
    """

    __tablename__ = "label_verdicts"

    id: int | None = SQLField(default=None, primary_key=True)
    request_id: str = SQLField(index=True)
    judge_user_id: str = SQLField(index=True)   # unique-together with request_id (app-enforced)
    org_id: str | None = SQLField(default=None, index=True)
    verdict: str                                 # "good" | "bad"
    corrected_label: str | None = None           # the right label (bad verdicts); vocab-validated
    query_text: str                              # denormalized user text — survives content aging
    predicted_label: str | None = None           # gateway_events.label at verdict time
    model_served: str | None = None              # gateway_events.model at verdict time
    created_ts: float


def upsert_verdict(engine: Any, *, request_id: str, judge_user_id: str, org_id: str | None,
                   verdict: str, corrected_label: str | None, query_text: str,
                   predicted_label: str | None, model_served: str | None) -> dict:
    """Insert or update one judge's verdict on one request (idempotent per (request_id, judge) —
    re-judging overwrites). Returns the stored row as a dict. Caller validated the org + vocab."""
    from sqlmodel import Session, select

    with Session(engine) as s:
        row = s.exec(
            select(LabelVerdict).where(LabelVerdict.request_id == request_id,
                                       LabelVerdict.judge_user_id == judge_user_id)
        ).first()
        if row is None:
            row = LabelVerdict(request_id=request_id, judge_user_id=judge_user_id, org_id=org_id,
                               verdict=verdict, corrected_label=corrected_label,
                               query_text=query_text, predicted_label=predicted_label,
                               model_served=model_served,
                               created_ts=datetime.now(timezone.utc).timestamp())
        else:  # re-judge: overwrite the verdict, keep the original created_ts
            row.verdict = verdict
            row.corrected_label = corrected_label
            row.query_text = query_text
            row.predicted_label = predicted_label
            row.model_served = model_served
        s.add(row)
        s.commit()
        s.refresh(row)
        return {"request_id": row.request_id, "judge_user_id": row.judge_user_id,
                "org_id": row.org_id, "verdict": row.verdict,
                "corrected_label": row.corrected_label, "predicted_label": row.predicted_label,
                "model_served": row.model_served, "query_text": row.query_text,
                "created_ts": row.created_ts}


# --- Writers -----------------------------------------------------------------


class TraceWriter(Protocol):
    def write(self, record: TraceRecord) -> None: ...


class JsonlTraceWriter:
    """Best-effort local file sink. In a container with a read-only cwd (Railway) the default
    relative path is unwritable — that must cost ONE boot-time warning and a disabled sink, not a
    trace_sink_error on every request (which is what it did until 2026-07-09)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._disabled = False
        try:  # probe writability once at construction — append mode, creates the file like write()
            with open(self.path, "a", encoding="utf-8"):
                pass
        except OSError as exc:
            self._disable(exc)

    def _disable(self, exc: OSError) -> None:
        self._disabled = True
        print(json.dumps({"event": "gateway.trace_jsonl_disabled",
                          "path": self.path, "error": str(exc),
                          "hint": "set TOTO_GW_TRACE_JSONL to a writable path, or '' to silence"}),
              file=sys.stderr, flush=True)

    def write(self, record: TraceRecord) -> None:
        if self._disabled:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(record.model_dump_json() + "\n")
        except OSError as exc:  # went unwritable after boot (fs remount, disk full) — same deal
            self._disable(exc)


class StdoutTraceWriter:
    """One OTel-style structured line per call. Stream is configurable for testing."""

    def __init__(self, stream: Any = None) -> None:
        self._stream = stream or sys.stdout

    def write(self, record: TraceRecord) -> None:
        line = json.dumps({"event": "gateway.call", **record.model_dump()}, separators=(",", ":"))
        print(line, file=self._stream, flush=True)


class SqlModelTraceWriter:
    def __init__(self, db_url: str) -> None:
        from sqlmodel import create_engine

        # Railway/Heroku hand out bare `postgres(ql)://` DSNs; SQLAlchemy maps those to the
        # psycopg2 dialect, which isn't installed (the app runs psycopg3). Pin the +psycopg driver
        # so TOTO_GW_TRACE_DB=${{Postgres.DATABASE_URL}} Just Works instead of crashing boot.
        if db_url.startswith("postgres://"):
            db_url = "postgresql+psycopg://" + db_url[len("postgres://"):]
        elif db_url.startswith("postgresql://"):
            db_url = "postgresql+psycopg://" + db_url[len("postgresql://"):]
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        self.engine = create_engine(db_url, connect_args=connect_args)
        SQLModel.metadata.create_all(self.engine)
        self._ensure_columns()
        self._backfill_legacy_model_ids()

    def _ensure_columns(self) -> None:
        """Additive migration for a PRE-EXISTING gateway_events table. `create_all` only CREATES
        missing tables — it never ALTERs an existing one to add new model columns (e.g. A1's
        label/user_id). So on an already-deployed DB the new columns are absent and any query
        selecting them fails. Inspect the live table and ADD any column the model has but the DB
        lacks — dual-dialect (both SQLite and Postgres accept `ALTER TABLE ADD COLUMN <name>
        <type>`; we only add genuinely-missing columns, so no IF-NOT-EXISTS needed). Idempotent."""
        from sqlalchemy import inspect, text

        insp = inspect(self.engine)
        if not insp.has_table("gateway_events"):
            return
        have = {c["name"] for c in insp.get_columns("gateway_events")}
        with self.engine.begin() as conn:
            for col in TraceRow.__table__.columns:
                if col.name not in have:
                    coltype = col.type.compile(self.engine.dialect)
                    conn.execute(text(f"ALTER TABLE gateway_events ADD COLUMN {col.name} {coltype}"))

    def _backfill_legacy_model_ids(self) -> None:
        """One-time data repair, idempotent at every boot: rows written before the 2026-07-09
        catalog rename carry retired tier-word ids (`or-economy`, …) in `model`. Rewrite them to
        the canonical ids so every reader (analytics, work map, metering) sees real ids without
        any runtime alias resolution — the alias mechanism itself is gone (see
        catalog.LEGACY_MODEL_IDS, a closed historical map). No-ops when nothing matches."""
        from sqlalchemy import inspect, text

        from .catalog import LEGACY_MODEL_IDS

        if not inspect(self.engine).has_table("gateway_events"):
            return
        with self.engine.begin() as conn:
            for legacy, canonical in LEGACY_MODEL_IDS.items():
                conn.execute(text("UPDATE gateway_events SET model = :c WHERE model = :l"),
                             {"c": canonical, "l": legacy})

    def write(self, record: TraceRecord) -> None:
        from sqlmodel import Session

        with Session(self.engine) as session:
            session.add(TraceRow(**record.model_dump()))
            session.commit()


def sql_engine(writer: Any):
    """The SQLAlchemy engine behind the trace's `gateway_events` sink, or None if no SQL sink is
    configured. The metering rollup (C4) reads `gateway_events` off this engine — the trace table
    IS the metering substrate, so the reader reuses the writer's engine rather than opening a
    second connection to the same DB. Walks a MultiTraceWriter's sinks; also accepts a bare
    SqlModelTraceWriter."""
    if isinstance(writer, SqlModelTraceWriter):
        return writer.engine
    for w in getattr(writer, "writers", []):
        if isinstance(w, SqlModelTraceWriter):
            return w.engine
    return None


class MultiTraceWriter:
    """Fan-out to every configured sink. A failing sink never blocks the response path."""

    def __init__(self, writers: list[TraceWriter]) -> None:
        self.writers = writers

    def write(self, record: TraceRecord) -> None:
        for w in self.writers:
            try:
                w.write(record)
            except Exception as exc:  # provenance must never break the request
                print(
                    json.dumps({"event": "gateway.trace_sink_error", "error": str(exc)}),
                    file=sys.stderr,
                    flush=True,
                )


class MemoryTraceWriter:
    """Test/inspection sink: keeps records in a list."""

    def __init__(self) -> None:
        self.records: list[TraceRecord] = []

    def write(self, record: TraceRecord) -> None:
        self.records.append(record)


class OTLPTraceWriter:
    """TraceWriter sink: export each upstream-call record as an OTel span via the OTLP exporter.

    Mirrors MetricsTraceWriter — a sink in the existing MultiTraceWriter, so it inherits the same
    fail-open fan-out (write errors never break the request). Gated on OTEL_EXPORTER_OTLP_ENDPOINT:
    unset → never constructed (build_writer_from_settings skips it), so opentelemetry is never
    imported and there is zero cost. All OTel imports are LAZY (in __init__), so an unset endpoint
    pays nothing at module load — same discipline as sentry-sdk.

    ponytail: reuses the TraceRecord seam (name + ts_start/ts_end + scalar attrs → one OTel span)
    rather than a parallel exporter path. The `exporter` param is a test seam (InMemorySpanExporter).
    """

    def __init__(self, endpoint: str, *, exporter: Any = None) -> None:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if exporter is None:  # real path: gRPC OTLP to the collector (endpoint 4317)
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=endpoint)
        self._provider = TracerProvider(resource=Resource.create({"service.name": "toto-gateway"}))
        self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("toto_gateway.trace")

    def write(self, record: TraceRecord) -> None:
        from opentelemetry.trace import Status, StatusCode

        start_ns = _iso_to_ns(record.ts_start)
        end_ns = _iso_to_ns(record.ts_end) if record.ts_end else start_ns
        span = self._tracer.start_span("gateway.call", start_time=start_ns)
        for k, v in record.model_dump().items():
            if isinstance(v, (str, bool, int, float)):  # OTel attrs: scalars only; drop None
                span.set_attribute(f"gw.{k}", v)
        if record.status == "error":
            span.set_status(Status(StatusCode.ERROR, record.error or "error"))
        span.end(end_time=end_ns)


def _iso_to_ns(iso: str) -> int:
    """ISO-8601 timestamp → epoch nanoseconds (OTel span time base)."""
    return int(datetime.fromisoformat(iso).timestamp() * 1_000_000_000)


def build_writer_from_settings(settings: Any) -> MultiTraceWriter:
    import os

    writers: list[TraceWriter] = []
    if settings.trace_jsonl:
        writers.append(JsonlTraceWriter(settings.trace_jsonl))
    if settings.trace_db:
        writers.append(SqlModelTraceWriter(settings.trace_db))
    if settings.trace_stdout:
        writers.append(StdoutTraceWriter())
    # Per-provider/model RED into the Prometheus registry — free (the record already exists) and
    # fail-open (MultiTraceWriter guards every sink). Always on; one registry, one /metrics.
    from .metrics import MetricsTraceWriter

    writers.append(MetricsTraceWriter())
    # OTLP span export (O5) — only when an endpoint is configured, else zero cost / no import.
    # Construction is guarded: a bad OTel install must not break app startup (fail-open).
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            writers.append(OTLPTraceWriter(endpoint))
        except Exception as exc:  # observability wiring never blocks boot
            print(
                json.dumps({"event": "gateway.otlp_init_error", "error": str(exc)}),
                file=sys.stderr, flush=True)
    return MultiTraceWriter(writers)
