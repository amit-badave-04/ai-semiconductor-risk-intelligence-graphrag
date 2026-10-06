"""Central configuration — pydantic-settings over .env.

Every module takes a Settings instance (or calls get_settings()) instead of
reading os.environ directly; the notebooks' PROJECT_ROOT convention becomes
`settings.data_dir` (default: ./data relative to the current working dir).
"""

import os
import socket
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_PRODUCTION_PEPPER_BYTES = 32
# What a live deployment may run with (docs/v2/M5_DECISIONS.md 2.2 and decision 11): a raised or zeroed cap refuses to
# boot instead of widening what a stolen key or a bot can spend. 0 means "off" for every cap outside production.
PRODUCTION_CLIENT_IP_HEADER = "fly-client-ip"
PRODUCTION_MAX_QUERIES_PER_DAY = 150
PRODUCTION_MAX_SPEND_USD_PER_DAY = 10.0
PRODUCTION_PAID_PER_IP_PER_DAY = 20
PRODUCTION_MAX_CONCURRENT_ANSWERS = 4
DRAIN_TIMEOUT_CEILING_S = 289       # drain.py: the drain plus its shutdown margin must fit Fly's kill_timeout of 300 s


def _default_machine_id() -> str:
    """The id this process writes on every ledger row it owns: Fly's machine id, else the host name."""
    return os.environ.get("FLY_MACHINE_ID") or socket.gethostname()


class Settings(BaseSettings):
    # hide_input_in_errors: a refused value must never be echoed. A model-level error ends with the input it rejected,
    # which for a settings object is the whole environment (the pepper, the database password, the API keys).
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    # --- LLM (LiteLLM model strings; Sonnet extracts/answers, Haiku critiques) ---
    anthropic_api_key: str = Field("", repr=False)      # secrets carry repr=False: a failing test prints the Settings object
    llm_model: str = "anthropic/claude-sonnet-5"      # extraction, judges: schema-sensitive, never moves with answering
    # The model that DRAFTS answers (production sets ANSWER_MODEL; the default keeps local runs on Sonnet).
    answer_model: str = "anthropic/claude-sonnet-5"
    # Stronger model a rejected cheap draft escalates to (empty = no escalation: answer_model streams live).
    escalation_model: str = ""
    critic_model: str = "anthropic/claude-haiku-4-5"
    # Cheap model that settles the risk items the aligner cannot (``semigraph align-items --adjudicate``; env ADJUDICATION_MODEL).
    adjudication_model: str = "openai/gpt-6-luna"

    # --- Neo4j Desktop local instance ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = Field("neo4j", repr=False)
    neo4j_database: str = "neo4j"  # target database; empty = the server's home database

    # --- SEC EDGAR: declared identity, required by SEC fair-access policy ---
    sec_user_agent: str = ""

    # --- Embeddings: 1024-dim, schema-locked to the graph's vector indexes ---
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    # Backend: "local" (sentence-transformers + torch, the pipeline default),
    # "onnx" (torch-free onnxruntime, what the web service ships), or
    # "remote" (any OpenAI-compatible /embeddings endpoint serving the SAME model).
    embedding_backend: str = "local"
    onnx_model_path: Path | None = None       # built by scripts/build_onnx_embedder.py
    onnx_tokenizer_path: Path | None = None   # defaults to tokenizer.json next to the model
    onnx_threads: int = 0                     # 0 = onnxruntime default
    embedding_api_base: str = ""              # e.g. https://api.deepinfra.com/v1/openai
    embedding_api_key: str = Field("", repr=False)
    embedding_api_model: str = "Qwen/Qwen3-Embedding-0.6B"

    # --- LLM cost accounting (USD per million tokens; Claude Sonnet 5 list price) ---
    llm_input_price_per_mtok: float = 2.0
    llm_output_price_per_mtok: float = 10.0
    # extraction critic (``critic_model``; Claude Haiku 4.5 list price)
    critic_input_price_per_mtok: float = 1.0
    critic_output_price_per_mtok: float = 5.0

    # --- Web service (semigraph.serve) ---
    environment: str = "development"          # "production" on Fly: stricter defaults
    app_base_url: str = "http://localhost:8080"
    admin_token: str = Field("", repr=False)  # X-Admin-Token for /api/admin/* (kill switch)
    kill_switch: bool = False                 # env override; the persisted flag lives in Neo4j
    max_queries_per_day: int = 150            # global paid-answer ceiling (0 = unlimited)
    rate_limit_questions: int = 5             # per client IP ...
    rate_limit_window_seconds: int = 600      # ... per window
    max_concurrent_answers: int = 2           # LLM calls in flight on the single machine
    max_question_chars: int = 500
    client_ip_header: str = ""                # header set by a TRUSTED proxy (Fly: fly-client-ip); empty = socket address
    free_rate_limit_questions: int = 30       # cached/benchmark answers per address per window
    stats_cache_seconds: int = 30             # /api/stats ledger aggregation cache
    llm_request_timeout_s: int = 90
    llm_answer_max_tokens: int = 1200         # streamed answers cannot regenerate on truncation
    answer_cache_ttl_hours: int = 24
    turnstile_site_key: str = ""              # Cloudflare Turnstile (optional bot gate)
    turnstile_secret_key: str = Field("", repr=False)
    turnstile_required: bool = False          # true = fail CLOSED for live questions when unconfigured/invalid
    read_rate_limit_per_minute: int = 120     # per address, free read endpoints (stats/evidence/examples)
    # Async answer path (M5a I2, docs/v2/M5A_BUILD_PLAN.md section 2): every blocking hop runs on a worker thread under a
    # named limiter, so a stream holds no thread while it waits for the model.
    # Each is at least 1: a 0 send timeout drops every paid stream after retrieval, a 0 limiter can never be taken.
    embed_slots: int = Field(1, ge=1)         # concurrent query embeddings (CPU-bound; the machine's cores bound it)
    db_thread_limit: int = Field(32, ge=1)    # threads for graph reads/writes made on behalf of answer streams
    send_timeout_s: int = Field(30, ge=1)     # a client that stops reading an SSE stream is dropped after this
    loop_lag_warn_ms: int = Field(100, ge=1)  # the event-loop monitor logs a stall longer than this
    # Address hashing (M5a I3, docs/v2/M5_DECISIONS.md decisions 6 and 12): HMAC-SHA-256 under a Fly secret.
    # Production refuses to start without a pepper of at least 32 bytes; elsewhere an empty one means a random pepper
    # per process. Rotate only on exposure (windows reset, older rows stop correlating) and bump the version with it:
    # each ledger row stores the version of the pepper that made its hash (0 is reserved for rows whose legacy hash
    # was nulled).
    ip_hash_pepper: str = Field("", repr=False)
    ip_hash_version: int = Field(2, ge=1)
    # Paid-ask admission state (M5a I4, docs/v2/M5_DECISIONS.md 2.2). Counters live in memory on the one live machine
    # ("inprocess") or in Neo4j ("neo4j": the rollback, and any multi-machine setup); a durable ledger row is written
    # either way.
    state_backend: Literal["inprocess", "neo4j"] = "inprocess"
    max_spend_usd_per_day: float = Field(10.0, ge=0)    # estimate-based daily cap beside max_queries_per_day (0 = off)
    paid_per_ip_per_day: int = Field(20, ge=0)          # paid asks per address hash per UTC day (0 = off)
    # Every state operation is bounded by this (the server-side transaction timeout); past it the ask fails closed.
    state_op_timeout_s: float = Field(1.0, ge=0.1, le=10)
    state_connection_acquisition_s: float = Field(0.5, ge=0.05, le=10)   # a ceiling on one connection attempt
    kill_switch_refresh_s: float = Field(10, ge=1)      # the maintenance thread re-reads the kill level this often
    kill_switch_stale_s: float = Field(30, ge=1)        # a kill level older than this reads as "on"
    # answer-cache reads per second, process-wide, before the bot check
    cache_read_budget_per_s: float = Field(10, ge=1)
    lease_ttl_s: float = Field(60, ge=1)                # a lease nobody renews expires after this
    lease_renew_s: float = Field(15, ge=1)              # renewal and sweep period of the maintenance thread
    # How long a SIGTERM drain waits for running streams and uploads (env DRAIN_TIMEOUT_S; drain.py reads the same one).
    drain_timeout_s: float = Field(240, ge=1, le=DRAIN_TIMEOUT_CEILING_S)
    machine_id: str = Field(default_factory=_default_machine_id, min_length=1)

    # --- Agent (semigraph.agent, docs/v2/M3_AGENT_PLAN.md): OPT-IN retrieval planner, strategy=agent; off = never imported by serve ---
    agent_enabled: bool = False
    agent_planner_model: str = "openai/gpt-6-luna"   # plans the tool calls only; the answer still goes through answer_model/escalation
    agent_max_tool_calls: int = 4
    agent_max_model_calls: int = 3
    agent_time_budget_s: int = 25             # wall clock for the whole plan; on expiry the plain hybrid retrieval answers

    # --- Langfuse tracing (semigraph.serve.tracing): sampled, fail-open; no key = no tracing ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = Field("", repr=False)
    langfuse_host: str = ""
    langfuse_sample_rate: float = 0.1
    langfuse_hash_salt: str = Field("", repr=False)   # empty = a random salt per process (question hashes group within one process only)

    # --- Freshness monitor (semigraph.serve.monitor, docs/v2/M4_PLAN.md 4.1): detects and surfaces, never ingests ---
    freshness_enabled: bool = False
    freshness_poll_hours: int = 6             # EDGAR submissions + Federal Register count, one machine at a time (lease)
    freshness_boot_delay_s: int = 300         # first check after warm-up, and only when the last one is older than the poll

    # --- Upload workspaces (M4_PLAN.md 3 + 4.2; caps re-registered for shared-cpu-2x / 4 GB, owner decision 2026-09-29) ---
    uploads_enabled: bool = False             # false = every workspace route answers 503
    workspace_ttl_hours: int = 24             # a workspace and everything in it is deleted after this
    upload_max_bytes: int = 15 * 1024 * 1024
    upload_max_pages: int = 30                # per version, checked after parsing
    upload_max_tokens: int = 16000            # per version, whole text, the embedder's own tokenizer
    upload_max_chunk_tokens: int = 512        # per embedded chunk (memory stays flat below ~1k tokens, S1b)
    upload_max_chunks: int = 120              # per version
    upload_max_documents: int = 3             # per workspace
    upload_max_versions: int = 5              # per document
    upload_max_workspace_pages: int = 120
    upload_max_workspace_tokens: int = 48000  # tokens actually embedded (after hash-keyed reuse), per workspace
    upload_parse_timeout_s: int = 90          # the parse subprocess is killed after this
    upload_compare_timeout_s: int = 120       # the what-changed comparison runs in a subprocess killed after this
    upload_embed_timeout_s: int = 1200        # generous: a CPU-throttled job finishes slowly instead of failing (risk 13)
    max_uploads_per_day: int = 40             # global; 40 x ~190 CPU-s stays under the shared-cpu-2x baseline
    workspace_create_per_day: int = 3         # per client IP
    uploads_per_hour: int = 10                # per client IP

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    @model_validator(mode="after")
    def _require_a_production_pepper(self) -> "Settings":
        # The message names the setting, never the value (nor its length).
        if self.is_production and len(self.ip_hash_pepper.encode("utf-8")) < MIN_PRODUCTION_PEPPER_BYTES:
            raise ValueError(f"IP_HASH_PEPPER must be set to at least {MIN_PRODUCTION_PEPPER_BYTES} bytes "
                             "in production (generate one: "
                             "python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
        return self

    @model_validator(mode="after")
    def _require_timings_that_can_work(self) -> "Settings":
        """A kill level that goes stale before its next refresh would read "on" part of the time, a lease that expires
        before its renewal would be swept while its stream runs."""
        if self.kill_switch_stale_s <= self.kill_switch_refresh_s:
            raise ValueError("KILL_SWITCH_STALE_S must be larger than KILL_SWITCH_REFRESH_S")
        if self.lease_renew_s >= self.lease_ttl_s:
            raise ValueError("LEASE_RENEW_S must be smaller than LEASE_TTL_S")
        return self

    @model_validator(mode="after")
    def _refuse_a_live_deployment_that_is_open_or_raised(self) -> "Settings":
        """Production refuses to boot with the bot check off, an untrusted client-address header, or any cap raised past
        the owner-approved ones (or zeroed: 0 means off elsewhere). Every problem is named, by SETTING: never a
        value."""
        if not self.is_production:
            return self
        problems = []
        if not self.turnstile_required:
            problems.append("TURNSTILE_REQUIRED must be true in production")
        if not self.turnstile_secret_key:
            problems.append("TURNSTILE_SECRET_KEY must be set in production")
        if self.client_ip_header.strip().lower() != PRODUCTION_CLIENT_IP_HEADER:
            problems.append(f"CLIENT_IP_HEADER must be {PRODUCTION_CLIENT_IP_HEADER} in production")
        if not 1 <= self.max_queries_per_day <= PRODUCTION_MAX_QUERIES_PER_DAY:
            problems.append(f"MAX_QUERIES_PER_DAY must be 1 to {PRODUCTION_MAX_QUERIES_PER_DAY} in production")
        if not 0 < self.max_spend_usd_per_day <= PRODUCTION_MAX_SPEND_USD_PER_DAY:
            problems.append(f"MAX_SPEND_USD_PER_DAY must be above 0 and at most {PRODUCTION_MAX_SPEND_USD_PER_DAY:g} "
                            "in production")
        if not 1 <= self.paid_per_ip_per_day <= PRODUCTION_PAID_PER_IP_PER_DAY:
            problems.append(f"PAID_PER_IP_PER_DAY must be 1 to {PRODUCTION_PAID_PER_IP_PER_DAY} in production")
        if not 1 <= self.max_concurrent_answers <= PRODUCTION_MAX_CONCURRENT_ANSWERS:
            problems.append(f"MAX_CONCURRENT_ANSWERS must be 1 to {PRODUCTION_MAX_CONCURRENT_ANSWERS} in production")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # --- Data lake root (git-ignored, rebuildable) ---
    data_dir: Path = Path("data")

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def interim_dir(self) -> Path:
        return self.data_dir / "interim"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def chunks_dir(self) -> Path:
        return self.processed_dir / "chunks"

    @property
    def extractions_dir(self) -> Path:
        return self.processed_dir / "extractions"

    @property
    def embeddings_dir(self) -> Path:
        return self.processed_dir / "embeddings"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
