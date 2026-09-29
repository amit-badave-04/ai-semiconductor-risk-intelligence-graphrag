"""Central configuration — pydantic-settings over .env.

Every module takes a Settings instance (or calls get_settings()) instead of
reading os.environ directly; the notebooks' PROJECT_ROOT convention becomes
`settings.data_dir` (default: ./data relative to the current working dir).
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
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
    upload_embed_timeout_s: int = 1200        # generous: a CPU-throttled job finishes slowly instead of failing (risk 13)
    max_uploads_per_day: int = 40             # global; 40 x ~190 CPU-s stays under the shared-cpu-2x baseline
    workspace_create_per_day: int = 3         # per client IP
    uploads_per_hour: int = 10                # per client IP

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

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
