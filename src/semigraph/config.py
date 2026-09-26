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
