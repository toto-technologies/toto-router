"""Centralized env-driven configuration (mirrors Toto's app/config.py convention)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOTO_GW_", env_file=".env", extra="ignore")

    # Server
    host: str = "127.0.0.1"
    port: int = 8080

    # --- DB connection pool (psycopg async pool; PG mode only — SQLite ignores these) ---
    # pool_max is the REAL per-replica concurrency ceiling. On acquire-exhaustion the pool waits
    # pool_timeout seconds then raises PoolTimeout, which app.create_app maps to a clean 503
    # capacity_error (never a 500). The admission valve (max_concurrent_runs) is sized to trip the
    # clean 429 BEFORE this ceiling — see that field's inequality.
    # ponytail: pool_max=20 is a default that must be tuned to the Railway PG plan's
    # max_connections ÷ replicas — leave headroom for the reaper, the dedicated LISTEN conn, and
    # manual psql. Env: TOTO_GW_POOL_MIN / TOTO_GW_POOL_MAX / TOTO_GW_POOL_TIMEOUT.
    pool_min: int = 2
    pool_max: int = 20
    pool_timeout: float = 10.0

    # Catalog. Empty (unset) resolves at validation: catalog.openrouter.yaml when
    # OPENROUTER_API_KEY is in the environment (so a fresh clone with just that key gets working
    # smart routing — the shipped labels.yaml binds OpenRouter entries), else catalog.yaml.
    # An explicit TOTO_GW_CATALOG always wins. os.environ is the right lookup: runners read the
    # provider key from there too (never from .env), so the default tracks what dispatch can use.
    catalog: str = ""
    # Offline leaderboard scores keyed by upstream model (refreshed by
    # scripts/fetch_benchmarks.py). Missing file is fine — routing degrades gracefully.
    benchmarks: str = "benchmarks.yaml"
    # Optional Artificial Analysis free-tier key for the benchmarking ingest. Empty → the AA
    # connector is skipped silently. AA data is INTERNAL-ONLY (redistributable=0); the key is never
    # logged. Env: TOTO_GW_AA_API_KEY.
    aa_api_key: str = ""
    # Scheduled benchmark refresh cadence in HOURS. 0 = off (default) → refresh only via the
    # admin endpoint. > 0 → a lifespan task runs the same ingest+overlay hot-swap every N hours
    # (first run delayed N hours; boot already overlays). A failed tick logs once and keeps ticking.
    benchmark_refresh_hours: float = 0.0
    # Provider inventory discovery is out-of-band. Fireworks always includes public serverless
    # models; an account id adds account-owned models, and deployments remain an explicit opt-in.
    fireworks_account_id: str = ""
    fireworks_discover_deployments: bool = False
    # Freshness and refresh cadence in hours. 0 refresh cadence means explicit refresh only.
    inventory_max_staleness_hours: float = 24.0
    inventory_refresh_hours: float = 0.0

    # Trace sinks
    trace_jsonl: str = "traces.jsonl"
    trace_db: str = ""
    trace_stdout: bool = True
    # Observability content capture: store the actual prompt (request messages) + served response
    # text per request in the `request_content` sibling table, so the activity log's drill-down
    # shows the real work, not just the decision trail. ON by default.
    # Access-scoped on read (member=own, org admin=org, operator=all); ages out (below). Off → the
    # gateway writes no content and the detail endpoint reports content_available: false.
    log_content: bool = True
    # Content retention: the reaper ages out request_content rows older than this (sibling of
    # delta_retention_days). 0 = keep forever. Requires trace_db (the content table's home).
    content_retention_days: int = 30
    # Audit-export scheduler tick: how often the exporter wakes to check each org's cadence.
    # 0 = disable the scheduled task entirely (manual POST .../run still works). Cadence is per-org.
    audit_export_tick_seconds: int = 3600
    # Content-plane retention sweep: how often the sweeper ages out product storage per each
    # org's retention policy. 0 = disable the scheduled task (manual POST .../retention/run still
    # works). retention_batch_limit bounds the rows deleted per (user, sink) each tick so a large
    # backlog drains over successive ticks instead of one long lock.
    retention_sweep_tick_seconds: int = 3600
    retention_batch_limit: int = 1000

    # Operator service credential. Empty means auth is open (single-operator dev posture).
    auth_token: str = ""

    # BYOK at-rest encryption secret (toto_gateway/credentials.py). Derives the Fernet key that
    # encrypts users' own provider API keys. Empty → the /v1/credentials write path fails closed
    # (503) rather than storing plaintext. Set a long random value in prod; rotating it makes
    # already-stored keys undecryptable (they degrade to the platform key — users re-enter them).
    credentials_secret: str = ""
    # Optional PREVIOUS at-rest secret, kept as a decrypt-only fallback during a zero-downtime
    # rotation window (credentials.py MultiFernet). Empty (default) → single-key, unchanged. Set it
    # to the old secret while credentials_secret holds the new one; new writes use the new key and a
    # lazy re-encrypt on read drains rows off the old, then drop this.
    credentials_secret_old: str = ""

    # At-rest KMS provider — WHERE credentials.py's Fernet key material comes from.
    # "env" (default) → the credentials_secret[/_old] fields above, byte-for-byte UNCHANGED. "vault"
    # → read the SAME key material from HashiCorp Vault KV v2 via hvac (the KV secret carries fields
    # `credentials_secret` + optional `credentials_secret_old`). FAIL-CLOSED: with provider=vault an
    # unreachable Vault or a missing primary key RAISES at startup — never a silent fall back to a
    # weak/empty key. MultiFernet dual-key rotation works identically for both sources.
    kms_provider: str = "env"   # env | vault
    vault_addr: str = ""        # e.g. https://vault.internal:8200
    # A Vault token. ponytail: token-auth only; AppRole/role login is a bigger surface — a platform
    # sidecar (vault-agent) that renews a token into TOTO_GW_VAULT_TOKEN covers the "or role" path
    # with zero code here. Add an in-process AppRole login only if that sidecar isn't available.
    vault_token: str = ""
    vault_kv_path: str = ""     # KV v2 secret path, e.g. "toto/credentials"

    # Routing brain. Default off so the offline fake-lane quickstart still works
    # (routing targets real local/frontier lanes — see appliance/bring_up.py). Flip on once a
    # real box backs the local lane. Cache is independently safe to enable.
    routing: bool = False
    cache: bool = False
    # Live-demo mode: real router/guards/cache/trace, but every lane executes as a FakeRunner
    # (offline, no keys, no GPU). Routing + provenance are real; answer text is a stub.
    fake_exec: bool = False
    # Permissive CORS for the local UI prototype to call the gateway cross-origin (dev only).
    cors: bool = False

    # Driver layer (the Sonnet-class agent that decomposes a request into Toto tasks and routes
    # each task by its metadata). Off by default → the raw /v1/chat/completions passthrough is
    # unaffected. When on, POST /v1/route is served.
    driver: bool = False
    # The driver's own reasoning model — a *catalog entry id*, swappable, never welded to one
    # lab. Point it anywhere in the catalog via TOTO_GW_DRIVER_MODEL.
    driver_model: str = "or-sonnet-5"
    # A cheap model for the triage classifier node (trivial-vs-multistep). Catalog entry id.
    triage_model: str = "or-qwen3-coder-flash"
    # Model that writes activity-analytics insights (governance summary over aggregate numbers,
    # never content). Catalog entry id; empty → driver_model. Env TOTO_GW_ANALYTICS_INSIGHTS_MODEL.
    analytics_insights_model: str = ""

    # Per-role output caps (max_tokens) on the driver's own LLM calls. Wall-time is ~linear in
    # output length, so these are the single biggest latency lever. Env-tunable per role
    # (TOTO_GW_MAX_TOKENS_DISPATCH, ...). Defaults sized to real task shapes; raise if answers
    # truncate. ponytail: five ints, not a nested config object — YAGNI until a 6th role appears.
    max_tokens_triage: int = 200
    max_tokens_answer: int = 1200
    # decompose emits STRUCTURED JSON (≤4 rich tasks); a truncated cap cuts it mid-object →
    # unparseable → single-task fallback. ~4 rich tasks need ~1000-1400 tokens; 2000 gives
    # real headroom.
    max_tokens_decompose: int = 2000
    max_tokens_dispatch: int = 1500
    # The final answer is the product surface — lower caps clip multi-part answers.
    max_tokens_synthesize: int = 2500

    # --- Subagent runners ---
    # Live pi / claude_code harness adapters: a task authored with metadata.requires.runner
    # ("pi" | "claude_code") executes as a REAL agentic subagent subprocess instead of a one-shot
    # gateway completion. Off (default) → byte-identical pre-runner behavior: parse strips the
    # runner pin and only the gateway adapter is registered. On → pi runs headless pointed BACK
    # at this gateway (inner calls routed/traced/guarded by us); claude_code runs `claude -p`
    # on the customer's own claude auth (NEVER for in-perimeter-pinned tasks — the driver
    # refuses pre-spawn). A pinned task whose binary is missing FAILS loudly, no downgrade.
    # Env: TOTO_GW_SUBAGENT_RUNNERS.
    subagent_runners: bool = False
    # Wall-clock budget (seconds) per subagent run. On expiry the whole process GROUP is
    # SIGKILLed (no orphans) and the task fails as a timeout. Env: TOTO_GW_SUBAGENT_TIMEOUT.
    subagent_timeout: int = 300

    # Provider resilience: on a transient upstream failure (429 / 5xx / timeout / connection),
    # retry the SAME model this many times with exponential backoff + jitter, then fall back
    # across catalog entries within the same residency boundary. See driver/core.Driver._call.
    provider_retries: int = 2
    provider_backoff_base: float = 0.5
    # Cap the per-attempt backoff sleep (also the ceiling on an honored upstream Retry-After) so a
    # bogus `Retry-After: 9999` header can't wedge a worker for hours. See resilience.backoff.
    provider_backoff_cap: float = 30.0

    # Per-provider I/O timeouts. The OpenAI/Anthropic SDKs default to a 600s read timeout — one
    # slow/degraded provider then pins an event-loop task + a concurrency slot for 10 minutes.
    # We pass an explicit httpx.Timeout instead. connect is a fast fail on a dead host;
    # read/write/pool track provider_read_timeout. Local (MLX/LM Studio) generation is
    # genuinely slow, so it keeps a longer, independently-tunable read budget. All env-tunable
    # (TOTO_GW_PROVIDER_READ_TIMEOUT, ...). See Settings.provider_timeout + the runners.
    provider_connect_timeout: float = 5.0
    provider_read_timeout: float = 60.0
    provider_read_timeout_local: float = 300.0

    # Per-provider circuit breaker. A provider (keyed by base_url host) that fails N
    # times in a row is marked OPEN — subsequent calls skip straight to fallback / fast-503 for
    # reset_seconds, then a single HALF_OPEN trial closes or re-opens it. In-process per replica.
    breaker_fail_threshold: int = 5
    breaker_reset_seconds: float = 30.0

    # Stream stall / first-token timeout. A provider that opens the SSE then goes silent
    # would otherwise hold the slot for the full read timeout. Abandon a stream whose next chunk
    # doesn't arrive within this inter-chunk deadline; the trace finalizes as error=stream_stall.
    stream_stall_timeout: float = 30.0

    # Multi-turn conversations: how many chars of prior (query, answer) history to feed a
    # follow-up turn. Past the cap, whole turns are block-evicted oldest-first down to HALF
    # the cap (hysteresis — keeps the prompt prefix byte-stable for provider caching).
    # 16000 (~4k tokens) bets on follow-ups landing inside the 5-min provider cache TTL;
    # tune down via TOTO_GW_HISTORY_CHARS if cache-miss cost dominates.
    history_chars: int = 16000

    # Anthropic auto-inject prompt caching. Most OpenAI-shaped clients never send `cache_control`,
    # so a continuing Anthropic conversation pays full input price every turn. When on and the
    # request carries NO client breakpoint AND looks continuous (has tools, or >= min_messages), the
    # frontier runner adds Anthropic's top-level automatic `cache_control` so the prefix caches. A
    # one-shot is left alone — it would only eat the 1.25x write premium with nothing to reuse.
    # Env: TOTO_GW_ANTHROPIC_AUTO_CACHE / TOTO_GW_ANTHROPIC_AUTO_CACHE_MIN_MESSAGES.
    anthropic_auto_cache: bool = True
    anthropic_auto_cache_min_messages: int = 3

    # Embedding-powered routing. Off by default: skill inference falls back to (and defaults to)
    # the keyword classifier. On → skill via nearest-centroid over exemplar embeddings, keyword on
    # timeout/error. Corpus logging is separate and default-on (feeds the experience-kNN corpus) —
    # it degrades silently with no key.
    embed_routing: bool = False
    embed_model: str = "openai/text-embedding-3-small"  # via OpenRouter (existing OPENROUTER_API_KEY)
    embed_timeout_ms: int = 500
    embed_corpus: bool = True

    # Experience-kNN model proposer. Dark by default — the corpus needs ~200 labeled tasks
    # before it beats the benchmark prior; flip on once task_embeddings + feedback are dense.
    # Precedence: privacy/guard > pins > kNN (flag on + >= knn_k neighbors) > benchmarks.
    experience_knn: bool = False
    knn_k: int = 3                # min similar neighbors (>= threshold) before proposing
    knn_sim: float = 0.75         # cosine similarity floor for a neighbor
    knn_refresh_seconds: int = 300  # in-memory corpus reload cadence
    knn_max_rows: int = 5000      # cap the brute-force corpus scan (newest-first)
    knn_cost_coeff: float = 0.0   # >0 nudges toward cheaper models among similar performers

    # Label routing (default ON — it owns dispatch). A haiku-class classifier labels each
    # sub-task against the closed vocab in routing/labels.yaml; the binding deterministically
    # picks the model, displacing the benchmark argmax. Any miss — pinned local, parse failure,
    # unknown/unbound label, classifier down — falls back to classify(). The optimize knob
    # applies only on that fallback path. Classifier model absent from the catalog → label
    # routing soft-disables at build (loud log), so plain-catalog dev boots keep working.
    label_routing: bool = True    # kill switch; off → pre-label routing, byte-identical
    label_classifier_model: str = "or-haiku-4.5"  # catalog entry id the classifier call runs on
    label_bindings: str = ""      # bindings path override; "" → toto_gateway/routing/labels.yaml
    label_timeout_ms: int = 10000  # hard cap on the classifier call; timeout → fallback ladder
    # Classifier prompt variant (driver/prompts.LABEL_PROMPT_VARIANTS), applied to BOTH the
    # driver's label node and the /v1 smart route. "fewshot" (default) outperforms "baseline";
    # "baseline" restores the pre-variant prompt byte-identically. Unknown value fails at boot.
    label_prompt_variant: str = "fewshot"
    # Fast tagging model for the /v1 smart route. Empty → the smart path reuses
    # label_classifier_model (driver + smart share one classifier). Set (env
    # TOTO_GW_SMART_CLASSIFIER_MODEL) to point ONLY the /v1/chat/completions smart-route
    # tagging at a different catalog entry (e.g. or-gemini-2.5-flash) without touching the driver's classifier.
    # A configured id absent from the catalog degrades gracefully (smart:classify_failed), never 500.
    smart_classifier_model: str = ""
    # Global default per-task-type memo holds for LabelAwareTTL: a JSON object {label: seconds}
    # e.g. {"code_generation": 3600, "classification": 120}. Empty → every label falls to the flat
    # 900s hold (identical to SlidingTTL). A team/org routing-policy `stick_ttls` overrides per label.
    stick_ttls: str = ""
    # TTL-aware incumbent hold: while a conversation's warm model still has a live upstream
    # prefix cache (inside the provider cache TTL), the smart route keeps it over a freshly-resolved
    # model rather than forfeiting the ~90% cache-read discount. Off (TOTO_GW_WARMTH_ROUTING=0) →
    # pure fresh resolution every turn. Bindings still win (they re-classify via the memo key);
    # warmth only holds against benchmark/optimize drift on an unchanged binding.
    warmth_routing: bool = True

    # --- Companion ---
    # The persistent partner agent — state in the gateway DB, woken per message, LangGraph loop.
    # Rides the driver plane (needs TOTO_GW_DRIVER=1). Every turn runs on one model.
    companion_model: str = "or-sonnet-5"
    # Voice turns run on a faster edge-class model (first-token latency dominates voice UX). Empty
    # = use companion_model (no behavior change until set). Budget degrade still overrides this
    # to triage_model past companion_daily_usd.
    companion_voice_model: str = ""
    # Per-user daily chat spend (USD). Past it, companion turns run on the economy model
    # (triage_model) — the partner goes terse, never mute. 0 = no budget.
    companion_daily_usd: float = 1.0

    # --- Companion voice / TTS ---
    # Speech-OUT leg: POST /v1/companion/speak proxies ElevenLabs Flash streaming audio. Off by
    # default — the frontend voice toggle only calls /speak when this is on. The provider key is
    # read straight from ELEVENLABS_API_KEY (provider convention, runners/openai.py), NOT the
    # TOTO_GW_ prefix; absent → clean 503 and the client degrades to text-only.
    companion_tts: bool = False
    companion_tts_model: str = "eleven_flash_v2_5"
    # Default voice + audition set — warm "confident coworker friend" ElevenLabs library voices.
    # Named ids (change the default with TOTO_GW_COMPANION_TTS_VOICE to any id from the candidates):
    #   Rachel   (21m00Tcm4TlvDq8ikWAM) — calm, warm, even — the safe default
    #   Charlotte(XB0fDUnXU5powFXDhCwa) — natural, friendly, a touch brighter
    #   Callum   (N2lVS1w4EtoT3dr4eOWO) — grounded, warm male coworker
    #   Alice    (Xb7hH8MSUJpSbSDYk0k2) — crisp, upbeat, confident female
    #   Daniel   (onwK4e9ZLuTAKqWW03F9) — deep, measured male coworker
    #   Adam     (pNInz6obpgDQGcFmaJgB) — resonant, authoritative male
    # Served to the client at GET /v1/companion (tts_voices) — that's the source of truth for
    # which ids are pickable; the client only keeps a display-name map for these, never the list.
    companion_tts_voice: str = "21m00Tcm4TlvDq8ikWAM"
    companion_tts_candidates: str = (
        "21m00Tcm4TlvDq8ikWAM,XB0fDUnXU5powFXDhCwa,N2lVS1w4EtoT3dr4eOWO,Xb7hH8MSUJpSbSDYk0k2,"
        "onwK4e9ZLuTAKqWW03F9,pNInz6obpgDQGcFmaJgB"
    )
    # Per-user/day TTS spend cap (USD). Past it → text-only degrade with one plain notice.
    # 0 = no budget. Separate from companion_daily_usd (the chat-LLM budget).
    companion_tts_daily_usd: float = 1.0
    # Per-VOICE-SESSION spend cap (USD) — sustained conversation is a metered premium tier.
    # A "voice session" is toggle-on → toggle-off/idle-timeout, tracked CLIENT-side (the burn
    # meter); this budget is handed to the client at bootstrap and enforced there (over it →
    # one spoken+shown notice, text-only continues). The daily cap (companion_tts_daily_usd)
    # still applies underneath. 0 = no per-session budget.
    companion_voice_session_usd: float = 3.0
    # Billing rate: USD per 1k characters synthesized — the calibration knob. ElevenLabs bills by
    # credits/plan, so tune this to the real invoice; Flash v2.5 ≈ $0.04/1k chars at list.
    companion_tts_usd_per_1k_chars: float = 0.04

    # --- memory recall plane (in-Postgres hybrid retrieval) ---
    # The companion's long-term memory: hybrid pgvector-cosine + tsvector-keyword retrieval over
    # past conversations / session outcomes / captures, and over the content plane's brain/note
    # documents. user_memory stays the DECLARED plane (visible/erasable); this is the RECALL
    # plane. It rides entirely in the content-plane Postgres — no subprocess, no second runtime.
    # Off by default → the companion works exactly as today (degrade-to-off is still LAW: a
    # missing embedding key drops to keyword-only, never fatal). Flip on with TOTO_GW_MEMORY=1.
    memory: bool = False
    # Recall injected at wake: top-k hits for the incoming message, char-capped so it can't blow
    # the prompt budget. Rendered AFTER the declared memory block.
    memory_recall_k: int = 5
    memory_recall_chars: int = 2000
    # Fusion weights: vector and keyword result lists are combined by reciprocal-rank fusion
    # (RRF: score = Σ weight/(60 + rank)) — robust, no score-scale tuning. Vector leads.
    memory_vec_weight: float = 0.7
    memory_kw_weight: float = 0.3
    # Rerank stage (the wake path's relevance filter). Retrieval fuses top-N candidates, then an
    # LLM reorders + cuts to k. Default on when memory is on; degrades to fused order on
    # timeout/error (NEVER fails recall). Uses the economy model via our OWN gateway
    # complete() — no external reranker vendor.
    memory_rerank: bool = True
    memory_rerank_model: str = ""       # empty → triage_model (economy class)
    memory_rerank_budget_ms: int = 600  # per-call ceiling; over it → fused order, no rerank
    memory_rerank_candidates: int = 20  # top-N fused candidates fed to the reranker

    # --- memory lifecycle: extraction ---
    # A post-capture async distiller turns raw captures into short typed facts written through the
    # SAME path as the companion's memory_write (caps/eviction/injection intact). Off by default →
    # captures still happen, just no auto-distillation. Needs the recall plane on (memory=1).
    memory_extract: bool = False
    memory_extract_model: str = ""        # empty → triage_model (economy/Haiku class)
    memory_extract_every: int = 6         # conversation cadence: distil every Nth chat turn
    memory_extract_daily_usd: float = 0.25  # per-user/day cap; past it extraction no-ops that day
    memory_extract_dedupe_sim: float = 0.85  # token-overlap floor above which a candidate is a dup

    # --- memory lifecycle: dreams (nightly consolidation) ---
    # One in-process pass per active tenant per night (sibling of the reaper), leader-guarded by a
    # per-(tenant,date) claim row. Merges near-duplicate captures + soft-archives stale ones (never
    # hard-deletes), writes a digest brain doc. Off by default. Needs memory=1.
    memory_dreams: bool = False
    memory_dream_hour: int = 3            # UTC hour the nightly pass fires
    memory_dream_daily_usd: float = 0.10  # per-tenant/night budget cap; a leg stops when spent
    memory_dream_stale_days: int = 30    # captures older than this with no activity get soft-archived
    memory_dream_merge_sim: float = 0.90  # token-overlap floor to cluster two captures for merge

    # --- calendar ICS sync ---
    # A periodic in-process job (sibling of the reaper/dreamer), leader-guarded by a pg advisory
    # lock. Each tick fetches every calendar object's subscriptions[].url, parses VEVENTs, and
    # REPLACES the events tagged with that source (never touching source:"toto" events). Off by
    # default; needs the driver on (the object store). No OAuth — ICS covers Google/Apple/Outlook.
    cal_sync: bool = False
    cal_sync_interval: int = 900          # seconds between sync ticks
    cal_sync_timeout: float = 10.0        # per-feed fetch timeout (s)
    cal_sync_max_events: int = 200        # per-source event clamp, so a huge feed can't blow the 32KB payload

    # --- Pipedream Connect ---
    # Flag-gated, read-only external calendar surface: a user logs into their existing Google
    # Calendar via Pipedream's hosted OAuth (Connect Link), and the _calsync tick pulls their
    # events through Pipedream's proxy. external_user_id = the toto user_id (Pipedream isolates per
    # that id). All off/unset → the endpoints 404 and the sync branch is skipped; nothing errors.
    # environment "development" is $0 (dev while piloting); flip to "production" for the live plan.
    pipedream: bool = False
    pipedream_client_id: str = ""
    pipedream_client_secret: str = ""
    pipedream_project_id: str = ""
    pipedream_environment: str = "development"   # development ($0) | production

    # --- Custom tools + module templates ---
    # Declarative user-authored tools (JSON specs composing canonical tools) + canvas templates.
    # Default off → the REST routes 404, the companion never registers create_tool/run_custom_tool/
    # instantiate_template/delete_tool, and CUSTOM stays out of every scope (the fail-closed
    # boundary all surfaces share). Flip on to expose the authoring/dispatch surface. Never in CORE.
    custom_tools: bool = False

    # Toto metadata plane. The driver persists task metadata + execution provenance here — NEVER
    # prompts/answers/content. Empty token → persistence is skipped gracefully (the driver still
    # routes + answers; it just doesn't write the task graph).
    toto_url: str = "https://toto.tech"
    toto_token: str = ""
    # Provision-on-signup: the shared secret for Toto's internal POST /api/internal/provision
    # endpoint. Set → a user who becomes real (email verified) is auto-provisioned a Toto-app
    # identity + API key, vaulted per-user (credentials.py "toto" slot) and used by the
    # Toto-writing surfaces instead of the shared toto_token. Empty (default) → no provisioning;
    # those surfaces fall back to toto_token exactly as before. FAIL-OPEN: a provision failure
    # never blocks signup or a request. Never logged.
    toto_provision_secret: str = ""

    # Driver observability: every graph node emits one span to this JSONL sink (always on — the
    # local provenance floor). LangSmith is opt-in and env-driven (LANGSMITH_TRACING=true +
    # LANGSMITH_API_KEY) — zero code, LangGraph traces nodes automatically when those are set.
    driver_spans_jsonl: str = "driver_spans.jsonl"

    # Live-routing plane (sessions/events/feedback/preferences) — SQLite file, ":memory:" in
    # tests. Only opened when the driver is on.
    db: str = "data/gateway.db"
    # Postgres cutover: empty → SQLite at `db` (exactly today). Set to a psycopg URL
    # (postgresql://...) → the stores run on Postgres.
    database_url: str = ""
    # Redis coordination tier. OPTIONAL and fail-open: set to a redis:// URL → cross-replica SSE
    # fan-out (RedisWakeBus) + shared circuit-breaker OPEN state. Empty (the default) → PG
    # LISTEN/NOTIFY fan-out + per-replica breaker, exactly as before. A Redis outage degrades to
    # per-replica behaviour, never crashes the request path.
    redis_url: str = ""
    # --- Object store (toto_gateway/storage.py) ---
    # Per-user key-scoped blob storage. No S3 endpoint → FilesystemBackend under storage_dir (dev
    # default, zero cloud). Set s3_endpoint → S3Backend via a thin httpx SigV4 signer (no boto3).
    # MinIO speaks path-style, so force_path_style defaults True — it also makes the compose MinIO
    # work with no extra env.
    # ponytail: path-style default; set TOTO_GW_S3_FORCE_PATH_STYLE=false for AWS virtual-host style.
    storage_dir: str = "data/objects"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret: str = ""
    s3_force_path_style: bool = True

    # Content plane: authored markdown (brain files, note bodies) + the memory recall plane
    # (captures + embeddings) live here. Resolution order (app.create_app): CONTENT_DATABASE_URL
    # if set (a dedicated Postgres for a sole-tenant customer — the registry code stays), ELSE the
    # primary DATABASE_URL under a `content` schema (the enterprise default: one Postgres, two
    # schemas), ELSE SQLite at `content_db` (dev only). Hard rule: when DATABASE_URL is set the
    # content plane NEVER falls back to ephemeral SQLite — that path is impossible on Railway.
    content_db: str = "data/content.db"
    content_database_url: str = ""
    # Postgres schema for the co-located content plane (the DATABASE_URL default above). Ignored
    # for a dedicated CONTENT_DATABASE_URL (that DB owns its whole namespace) and for SQLite.
    content_schema: str = "content"
    # Exact allowed browser origin for the frontend (credentialed CORS). Empty → no
    # cross-origin access. The permissive `cors` dev flag above is separate and dev-only.
    cors_origin: str = ""

    # --- User accounts & auth ---
    # Login is required everywhere except the public routes (/healthz, /svelte/*, /v1/auth/*): an
    # unauthenticated caller gets 401 (see routes/deps.require_auth). Not a flag — it's the posture.
    # auth_token above still configures the operator service credential.
    # Registration invite gate. Set → register requires a matching code (prod posture);
    # empty → open registration (dev).
    invite_code: str = ""
    # Email-verification gate on login. Off by default — verification emails/links still work and
    # mark the row, login just doesn't block on it. Re-arm with TOTO_GW_REQUIRE_EMAIL_VERIFY=true.
    require_email_verify: bool = False
    # Session cookie `Secure` flag. True in prod (HTTPS); flip off in local .env over http.
    cookie_secure: bool = True
    session_ttl_days: int = 30
    # SMTP. Unconfigured (empty host) → mailer prints the verify link to stdout (dev mode).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""
    # Google OAuth (placeholders — endpoints not built yet).
    google_client_id: str = ""
    google_client_secret: str = ""
    # Public origin of the app, used to build verification links. Empty → derived from request.
    public_url: str = ""

    # --- Ops / lifecycle ---
    log_level: str = "info"
    # Graceful drain on SIGTERM: seconds to let in-flight driver runs finish before failing them.
    drain_seconds: int = 30
    # Reaper: a run still 'running' with no event newer than this is failed as orphaned/hung.
    run_timeout: int = 600
    # Delta retention: the reaper prunes answer_delta events of terminal runs older than this
    # (deltas are ~90% of bytes, redundant once sessions.answer is stored; spans kept).
    # Daily per-user run quota (PG rate-limit table); 0 = unlimited.
    delta_retention_days: int = 7
    daily_run_quota: int = 0
    # Max bare items enriched per /v1/lists/{id}/enrich call (each item = one model call). Caps
    # per-request cost/fan-out. Tunable via TOTO_GW_ENRICH_MAX_ITEMS env var.
    enrich_max_items: int = 20

    # --- Prompt tuning ---
    # Optional JSON file {surface_name: text} overriding driver/prompts.py surfaces at load
    # (and live via PUT /v1/dev/prompts). Empty/missing file → byte-identical prompts;
    # malformed file → loud startup failure. See prompts.PROMPT_SURFACES for the names.
    prompts_file: str = ""
    # Optional JSON file {surface: {add: [...], remove: [...]}} overriding tool_scopes.SCOPES at
    # load (and live via the dashboard Scopes section). Empty/missing → code-default scopes;
    # malformed → loud startup failure. See tool_scopes.SCOPES for the surface names.
    scopes_file: str = ""
    # LangSmith prompt hub (the PULL side of the prompt platform — toto_gateway/prompt_hub.py).
    # Makes LangSmith the editor: edit+commit a surface in the playground, pull it here, and it
    # hot-applies through the same override seam as the dashboard. Off by default (the in-dashboard
    # editor is the default surface); flip on to expose per-surface "pull from LangSmith" + sync.
    prompt_hub: bool = False
    prompt_hub_prefix: str = "toto"       # hub prompt name = "<prefix>-<surface-lower-dashes>"
    prompt_hub_poll_seconds: int = 0      # 0 = manual pulls only (no background poller built yet)
    # Mounts /dev + /v1/dev/* (prompt editor, eval runner). Dev/sandbox ONLY — the routes
    # simply don't exist unless this is on (scripts/sandbox.sh sets it). NEVER set in prod.
    dev_dashboard: bool = False
    # Which product planes this deploy mounts. "gateway" = the pure API/gateway + driver +
    # gateway features (BYOK, tool contract) — ALWAYS mounted. "app" = the Toto product surface
    # (companion/canvas/tasks/objects/integrations) + the SPA. Default "gateway,app" = the full
    # behavior; set TOTO_GW_PLANES=gateway for a gateway-only deploy. Comma-separated; unknown
    # planes are ignored, dev routers are independent (dev_dashboard). See app.py plane_routers
    # for the map.
    planes: str = "gateway,app"
    # Which edition this deploy runs (open-core seam). "enterprise" (default) mounts the
    # org/enterprise admin routers on the gateway plane; "oss" leaves them unmounted, so every
    # org-scoped /v1/admin (+ /scim) route is a plain 404 (same pattern as planes/dev_dashboard).
    # Any value other than "oss" means enterprise — a typo must never strip a running enterprise
    # deploy of its org surface.
    edition: str = "oss"
    # QA harness target: the deployed TESTING env the /dev/qa fixtures land on. Token is an
    # operator bearer (keychain 'Toto Testing Auth Token' → sandbox.sh exports it). Empty
    # token → fixture generation returns 400; the catalog + checklists still render.
    # ponytail / portability: this is the ONE Railway string in the whole codebase and it is
    # DEV-HARNESS-ONLY — inert unless dev_dashboard=1 (never set in prod). Not a runtime
    # coupling; treated as cosmetic. Override with TOTO_GW_TESTING_URL to point the harness
    # anywhere.
    testing_url: str = ""
    testing_token: str = ""

    # --- Surge / incident ---
    # Error tracking. Empty DSN → Sentry never initializes (zero overhead). Content is never sent
    # (send_default_pii=False + include_local_variables=False + a scrubbing before_send).
    # traces_sample_rate 0 = errors only.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0
    # Deploy environment tag (testing|staging|prod) so on-call can split issues in the Sentry UI.
    sentry_environment: str = ""
    # SSE keep-alive comment cadence. Lower for flaky proxies, higher to cut noise.
    sse_heartbeat_seconds: int = 15
    # Backpressure for user surges: cap simultaneous driver runs PER REPLICA (0 = unlimited).
    # At the cap, run creation returns 429 + Retry-After instead of piling on and OOMing.
    # Safe default ON (backpressure out of the box). It is sized so the clean 429 trips BEFORE the
    # pool ceiling (pool_max) — the coherence inequality is:
    #     max_concurrent_runs × avg_conns_per_run  <  pool_max
    # AsyncStoreMixin acquires a conn per query and releases it immediately, so a run holds ~1–2
    # conns concurrently at peak (a delta publish overlapping a span write). With avg≈2 and
    # pool_max=20: 8 × 2 = 16 < 20, leaving headroom for the reaper, the LISTEN conn, and psql.
    # ponytail: 8 is a derived default — re-tune alongside pool_max per the PG plan using the
    # measured avg_conns_per_run.
    max_concurrent_runs: int = 8
    # Global valve on concurrent OUTBOUND LLM calls per replica (0 = unlimited) — bounds the
    # surge amplifier where N runs each fan out to ≤4 dispatch calls at once. Coordinate with
    # provider rate limits + retry/backoff (driver/core) so admission + retry don't fight.
    max_concurrent_llm_calls: int = 16
    # Streaming delta coalescing (flush a batch at whichever comes first). Tunable under load
    # without a code change; each flush is a DB row + a notify round-trip on the hot path.
    delta_flush_chars: int = 120
    delta_flush_ms: int = 200

    # --- Persona ---
    # The ONE brand-carrying surface. The routing engine (driver/prompts.py) carries zero brand;
    # the persona composes on top of the two user-facing answer prompts + the companion, resolved
    # by toto_gateway/persona.get_persona(). "toto" (default) → the shipped identity+voice.
    # "neutral" → a minimal brand-free assistant. Any other value is a file path (used verbatim
    # if it exists) or an inline system string. Set TOTO_GW_PERSONA.
    persona: str = "toto"

    # --- Egress allowlist (toto_gateway/egress.py) ---
    # One chokepoint over every outbound host. The allowed set is DERIVED from config (catalog
    # base_urls, provider hosts, toto_url, SMTP/S3/OTLP/Sentry/LangSmith, SSO issuers) — this is the
    # operator extension for anything derivation can't see (a corporate calendar/ICS feed host, a
    # private proxy). Comma-separated hosts. Enforce is OFF by default (observe-only: log + audit a
    # would-be violation so the feature can soak); flip TOTO_GW_EGRESS_ENFORCE=1 to refuse rogue egress.
    egress_extra: str = ""
    egress_enforce: bool = False

    # --- Distribution license (toto_gateway/license.py) ---
    # A compact Ed25519-signed token proving entitlement offline (no phone-home). Empty is fine for
    # dev/OSS. license_required arms the HARD-GATE-WITH-GRACE posture: a missing/invalid/past-grace
    # key then refuses chat-plane traffic (503) while /healthz, /statusz and /console stay up. The
    # in-perimeter bundle's compose sets TOTO_GW_LICENSE_REQUIRED=1; dev/OSS leaves it 0.
    license_key: str = ""
    license_required: bool = False

    # Passthrough (/v1/chat/completions) cross-provider fallback. ON by default so a pinned
    # model that is down still gets answered by a same-residency sibling; a caller who truly
    # pinned the model opts out per-request with the `x-toto-no-fallback` header. The served
    # model is always reported back in response.x_toto.model. Same-model retry is independent
    # of this flag.
    passthrough_fallback: bool = True

    @model_validator(mode="after")
    def _default_catalog(self) -> "Settings":
        """Resolve an unset catalog (see the `catalog` field comment). Runs after env/.env/kwargs,
        so every consumer reads the resolved path."""
        if not self.catalog:
            import os

            self.catalog = ("catalog.openrouter.yaml" if os.environ.get("OPENROUTER_API_KEY")
                            else "catalog.yaml")
        return self

    def provider_timeout(self, *, local: bool = False):
        """Explicit httpx.Timeout for a provider client — replaces the SDK's unbounded 600s read
        default. write/pool track read; connect is a separate fast-fail budget. local=True picks
        the longer on-box (MLX) read budget."""
        import httpx

        read = self.provider_read_timeout_local if local else self.provider_read_timeout
        return httpx.Timeout(connect=self.provider_connect_timeout, read=read, write=read, pool=read)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_token)

    @property
    def smtp_enabled(self) -> bool:
        return bool(self.smtp_host)

    @property
    def toto_enabled(self) -> bool:
        return bool(self.toto_token)

    @property
    def stick_ttls_map(self) -> dict[str, float]:
        """Parsed TOTO_GW_STICK_TTLS → {label: seconds}. A malformed value degrades to {} (flat
        holds) rather than blocking boot — stickiness is a tuning lever, never load-bearing."""
        if not self.stick_ttls:
            return {}
        import json

        try:
            data = json.loads(self.stick_ttls)
            return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    @property
    def plane_set(self) -> frozenset[str]:
        """Parsed TOTO_GW_PLANES. Gateway is always present (it's the core the deploy exists for)."""
        parsed = {p.strip().lower() for p in self.planes.split(",") if p.strip()}
        return frozenset(parsed | {"gateway"})


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: clear the cached Settings so env changes take effect."""
    get_settings.cache_clear()
