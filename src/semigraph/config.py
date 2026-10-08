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

from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .llm_shape import MOCK_MODEL_PREFIX

MIN_PRODUCTION_PEPPER_BYTES = 32
MIN_PRODUCTION_ADMIN_TOKEN_CHARS = 32       # secrets.token_urlsafe(32) is 43; a typed word is not a credential
# What a live deployment may run with (docs/v2/M5_DECISIONS.md 2.2 and decision 11): a raised or zeroed cap refuses to
# boot instead of widening what a stolen key or a bot can spend. 0 means "off" for every cap outside production.
PRODUCTION_CLIENT_IP_HEADER = "fly-client-ip"
PRODUCTION_MAX_SPEND_USD_PER_DAY = 10.0
# One address's share of that day's spend (docs/v2/research/m5-councils/council4/verdict.md); 0 means "off". It must meet
# council 4's two requirements, applied to the corrected estimates (hybrid $0.709573, vector $0.265873, agent $1.015669,
# workspace $0.734632; serve/estimate.py, per-model characters per token):
#   (i') pausing LIVE ASKS needs at least eight addresses: after seven addresses have each spent a full share, the rest of
#        the day still admits a hybrid ask, max_spend_usd_per_day - 7 x share >= hybrid estimate
#        (10 - 9.24 = 0.76 >= 0.709573), so share <= (10 - 0.709573) / 7 = $1.3272. (The first form of this rule, only
#        7 x share < 10, was met by $1.40 with $0.20 left, less than any live ask: seven addresses could stop live asks
#        for the day, because the day cap is judged on estimates.)
#   (ii) the buyer demo from one office (council test (b), tests/test_state_contract.py) is admitted whole: 20 asks including
#        3 escalations and 2 agent asks, the second agent ask arriving with $0.30 settled,
#        share >= 0.30 + 1.015669 = $1.3157.
# The window is $1.3157 to $1.3272; $1.32 is inside it (margins: 4,331 and 7,203 micro-dollars). The old $1.25 ($10 / 8) fails
# (ii). tests/test_serve_config_production.py and tests/test_state_contract.py compute both rules from the live estimates and
# the production caps: a changed estimate or cap fails them instead of leaving the number quietly wrong.
# What $1.32 means for one address (a running ask counts at its estimate): two concurrent hybrid asks do not fit (2 x
# 0.709573 > 1.32), so the second is refused with the busy text while the first runs; an agent ask is admitted while the
# address's settled spend is at most about $0.30 (1.32 - 1.015669), a hybrid ask while it is at most about $0.61
# (1.32 - 0.709573), a workspace ask about $0.59, a vector ask about $1.05.
PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD = 1.32
# Counts a live deployment may run at most (each 1 or more). The windows are "not looser than live": fly.toml [env] sets
# none of the last five, so what runs today is the code default, which is the owner-approved value. The Settings class
# default and this table must agree (tests/test_serve_config_production.py pins both against fly.toml).
PRODUCTION_COUNT_CEILINGS = {
    "max_queries_per_day": 150, "paid_per_ip_per_day": 20, "max_concurrent_answers": 4,
    "rate_limit_questions": 5, "free_rate_limit_questions": 30, "read_rate_limit_per_minute": 120,
    "workspace_create_per_day": 3, "uploads_per_hour": 10,
}
# The per-address windows hold events for this long (RateLimiter: a window of 0 s never holds one, a short one lets the
# counts above through faster than they are meant to).
PRODUCTION_MIN_RATE_LIMIT_WINDOW_S = 600
# Plan values (docs/v2/M5_DECISIONS.md 2.2): the answer-cache read budget is 10 reads per second for the whole process,
# before the bot check, and a kill level not re-read for 30 s reads as "on". A larger budget opens that unauthenticated
# read to more load; a longer staleness lets a cached "off" outlive a database that can no longer confirm it.
PRODUCTION_MAX_CACHE_READ_BUDGET_PER_S = 10
PRODUCTION_MAX_KILL_SWITCH_STALE_S = 30
# The Fly app each environment runs on. FLY_APP_NAME is set by Fly on every machine, so a machine that is the live app
# cannot be started as anything but production (an unset or mistyped ENVIRONMENT would leave every validator off).
# The staging window (deploy/staging, docs/v2/M5_DECISIONS.md 2.1) is the API and the apps around it that run this code's
# Python: the mock LLM, the S7 replay machine and the load generators. Its database app (semigraph-neo4j-stg) runs no
# Python of ours and is deliberately absent: only a host name of it is allowed (STAGING_NEO4J_HOST).
FLY_APP_ENVIRONMENTS = {"semigraph": "production", "semigraph-stg": "staging", "semigraph-mockllm": "staging",
                        "semigraph-tools-stg": "staging", "semigraph-loadgen-stg": "staging"}
# What a staging process may run with (docs/v2/M5_DECISIONS.md 2.1 "Staging rules"): an ALLOWLIST, so a new way to reach
# the live database or a provider is refused until it is added here on purpose. Production refuses the same switches.
STAGING_NEO4J_HOST = "semigraph-neo4j-stg.internal"
STAGING_CLIENT_IP_HEADERS = frozenset({"x-test-client-ip", PRODUCTION_CLIENT_IP_HEADER})
STAGING_INTERNAL_HOST_SUFFIX = ".internal"       # Fly's private DNS: the mock LLM is reached by its private name only
STAGING_MOCK_KEY_PREFIX = "mock-"
MIN_ORIGIN_AUTH_SECRET_BYTES = 32
# Every LLM model string a setting holds (production refuses a mock in any of them).
LLM_MODEL_SETTINGS = ("llm_model", "answer_model", "escalation_model", "critic_model", "adjudication_model",
                      "agent_planner_model")
# The models the serve path runs, the only ones staging pins to the mock.
SERVE_MODEL_SETTINGS = ("answer_model", "escalation_model", "agent_planner_model")
# drain.py: the drain, then the post-drain window (at least MIN_SHUTDOWN_S), then the lifespan shutdown budget, then the
# margin must fit Fly's kill_timeout. 300 - 10 (SHUTDOWN_MARGIN_S) - 35 (LIFESPAN_SHUTDOWN_BUDGET_S) - 1 (MIN_SHUTDOWN_S);
# tests/test_serve_drain.py derives the same number from those constants and pins the two together.
DRAIN_TIMEOUT_CEILING_S = 254
# Every setting a refusal of Settings can name, in the order the pre-deploy check (scripts/check_env_fly.py) lists them.
CHECKED_SETTINGS = (
    "ENVIRONMENT", "FLY_APP_NAME", "IP_HASH_PEPPER", "ADMIN_TOKEN", "TURNSTILE_REQUIRED", "TURNSTILE_SECRET_KEY",
    "CLIENT_IP_HEADER", *(name.upper() for name in PRODUCTION_COUNT_CEILINGS), "MAX_SPEND_USD_PER_DAY",
    "PAID_SPEND_SHARE_PER_IP_USD", "RATE_LIMIT_WINDOW_SECONDS", "CACHE_READ_BUDGET_PER_S", "KILL_SWITCH_STALE_S",
    "KILL_SWITCH_REFRESH_S", "LEASE_TTL_S", "LEASE_RENEW_S", "DRAIN_TIMEOUT_S",
    # the staging switches production refuses (a live deployment must not run any of them)
    "TURNSTILE_STUB", "OPENAI_API_BASE", "OPENAI_BASE_URL", "ANTHROPIC_API_BASE", "ANTHROPIC_BASE_URL", "ORIGIN_AUTH_SECRET",
    "NEO4J_URI", *(name.upper() for name in LLM_MODEL_SETTINGS))
# Every setting a STAGING refusal can name. Not part of the pre-deploy check above, which reads the live app: it would
# print PASS for settings that production does not validate.
STAGING_CHECKED_SETTINGS = (
    "ENVIRONMENT", "FLY_APP_NAME", "NEO4J_URI", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE",
    *(name.upper() for name in SERVE_MODEL_SETTINGS), "ORIGIN_AUTH_SECRET", "FRESHNESS_ENABLED", "CLIENT_IP_HEADER")


def _host_of(url: str) -> str | None:
    """The host of ``url`` by the standard parser (lower-cased), or None when it has none, when the text carries a space
    or a control character, or when the authority has user information (``name@host`` is how a look-alike host is written
    behind an allowed one). Never raises."""
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 for char in url):
        return None
    try:
        parts = urlsplit(url)
        return None if "@" in parts.netloc else parts.hostname
    except ValueError:
        return None


def _is_internal_http_base(url: str) -> bool:
    """An ``http(s)`` URL on a Fly private (``.internal``) host: how staging names the mock LLM."""
    host = _host_of(url)
    if host is None:
        return False
    return (urlsplit(url).scheme in ("http", "https") and host.endswith(STAGING_INTERNAL_HOST_SUFFIX)
            and len(host) > len(STAGING_INTERNAL_HOST_SUFFIX))


def _is_staging_mock_model(model: str) -> bool:
    """``openai/mock-<something>``, spelled exactly (an allowlist: another case or prefix is not the mock)."""
    return model.startswith(MOCK_MODEL_PREFIX) and len(model) > len(MOCK_MODEL_PREFIX)


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
    # LiteLLM reads OPENAI_API_KEY from the environment itself; it is a field only so the staging validators can refuse a
    # provider's key and production can leave it alone. Nothing in the service reads this value.
    openai_api_key: str = Field("", repr=False)
    # STAGING ONLY (production refuses any value): where every ``openai/`` model call goes instead of OpenAI, the mock LLM's
    # private address (llm_shape.provider_kwargs). Empty = the provider's own endpoint.
    openai_api_base: str = ""
    # The other spelling of the same switch, and the one LiteLLM reads FIRST: ``OPENAI_BASE_URL`` from the process environment
    # beats ``OPENAI_API_BASE`` (litellm/main.py), and a live call passes no ``api_base`` of its own, so a production process
    # holding it would send every ``openai/`` call (the cheap drafts) to that address. Nothing in the service reads this
    # value: it is a field so production can refuse it and the pre-deploy check can report it. Hidden from the repr, since
    # a base URL may carry credentials.
    openai_base_url: str = Field("", repr=False)
    # The same class for the Sonnet route: LiteLLM 1.100.0 reads ``ANTHROPIC_API_BASE`` and ``ANTHROPIC_BASE_URL`` from the
    # process environment for an ``anthropic/`` call (litellm/main.py), and a live call passes no ``api_base`` of its own, so a
    # production process holding either would send every escalation to that address. Fields only so production can refuse
    # them and the pre-deploy check can report them; nothing in the service reads the values. Hidden from the repr (a base URL
    # may carry credentials). Development may set them (a local gateway); staging has no provider key and ignores them.
    anthropic_api_base: str = Field("", repr=False)
    anthropic_base_url: str = Field("", repr=False)
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
    environment: str = "development"          # "production" on Fly: stricter defaults; stripped and lower-cased
    fly_app_name: str = ""                    # set by Fly on every machine; it fixes ENVIRONMENT (FLY_APP_ENVIRONMENTS); empty = local
    app_base_url: str = "http://localhost:8080"
    admin_token: str = Field("", repr=False)  # X-Admin-Token for /api/admin/* (kill switch)
    kill_switch: bool = False                 # env override; the persisted flag lives in Neo4j
    max_queries_per_day: int = 150            # global paid-answer ceiling (0 = unlimited)
    rate_limit_questions: int = 5             # per client IP ...
    rate_limit_window_seconds: int = 600      # ... per window
    max_concurrent_answers: int = 2           # LLM calls in flight on the single machine
    max_question_chars: int = 500
    client_ip_header: str = ""                # header set by a TRUSTED proxy (Fly: fly-client-ip); empty = socket address; stripped, lower-cased
    free_rate_limit_questions: int = 30       # cached/benchmark answers per address per window
    stats_cache_seconds: int = 30             # /api/stats ledger aggregation cache
    llm_request_timeout_s: int = 90
    llm_answer_max_tokens: int = 1200         # streamed answers cannot regenerate on truncation
    answer_cache_ttl_hours: int = 24
    turnstile_site_key: str = ""              # Cloudflare Turnstile (optional bot gate)
    turnstile_secret_key: str = Field("", repr=False)
    turnstile_required: bool = False          # true = fail CLOSED for live questions when unconfigured/invalid
    # STAGING ONLY (production refuses True): the bot check accepts any non-empty token without calling Cloudflare, so a load
    # generator can pass it. A missing token is still refused.
    turnstile_stub: bool = False
    # STAGING ONLY (production refuses a value): the secret every request must carry in X-Origin-Auth (serve/asgi_middleware.py,
    # wired in main.create_app only when set; /healthz is exempt). At least 32 bytes in staging.
    origin_auth_secret: str = Field("", repr=False)
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
    # One address's share of the day's spend, in dollars (0 = off): an ask is admitted only while that address's settled
    # spend plus its running asks' estimates plus this ask's estimate stay within it (council 4, option C). Production
    # allows 0 < x <= PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD; a refusal never echoes the value.
    paid_spend_share_per_ip_usd: float = Field(PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD, ge=0, allow_inf_nan=False)
    # Every state operation is bounded by this (the server-side transaction timeout); past it the ask fails closed.
    state_op_timeout_s: float = Field(1.0, ge=0.1, le=10)
    state_connection_acquisition_s: float = Field(0.5, ge=0.05, le=10)   # a ceiling on one connection attempt
    kill_switch_refresh_s: float = Field(10, ge=1)      # the maintenance thread re-reads the kill level this often
    kill_switch_stale_s: float = Field(30, ge=1)        # a kill level older than this reads as "on"
    # answer-cache reads per second, process-wide, before the bot check
    cache_read_budget_per_s: float = Field(10, ge=1)
    lease_ttl_s: float = Field(60, ge=1)                # a lease nobody renews expires after this
    lease_renew_s: float = Field(15, ge=1)              # renewal and sweep period of the maintenance thread
    # How long a SIGTERM drain waits for running streams and uploads (env DRAIN_TIMEOUT_S). drain.main() reads THIS field:
    # one source, validated here, so a value the shutdown cannot fit inside Fly's kill_timeout refuses to start.
    drain_timeout_s: float = Field(240, ge=1, le=DRAIN_TIMEOUT_CEILING_S, allow_inf_nan=False)
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
        return self.environment == "production"

    @field_validator("environment", "client_ip_header")
    @classmethod
    def _normalized(cls, value: str) -> str:
        """Stripped and lower-cased ONCE, here, and every reader uses the result: "production " must not skip the
        validators, and a header with a stray space must not pass them while the guard fails to find it on the request
        (every visitor would then share the proxy's address)."""
        return value.strip().lower()

    @model_validator(mode="after")
    def _refuse_a_setup_that_cannot_work(self) -> "Settings":
        """One error naming EVERY problem (pydantic stops at the first validator that raises, and the pre-deploy check
        prints the whole list), each by SETTING and never by value or length."""
        problems = [*self._timing_problems(), *self._fly_app_problems()]
        if self.is_production:
            problems += [*self._production_secret_problems(), *self._production_limit_problems(),
                         *self._production_staging_switch_problems()]
        elif self.environment == "staging":
            problems += self._staging_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _timing_problems(self) -> list[str]:
        """A kill level that goes stale before its next refresh would read "on" part of the time, a lease that expires
        before its renewal would be swept while its stream runs."""
        problems = []
        if self.kill_switch_stale_s <= self.kill_switch_refresh_s:
            problems.append("KILL_SWITCH_STALE_S must be larger than KILL_SWITCH_REFRESH_S")
        if self.lease_renew_s >= self.lease_ttl_s:
            problems.append("LEASE_RENEW_S must be smaller than LEASE_TTL_S")
        return problems

    def _fly_app_problems(self) -> list[str]:
        """On Fly the environment is the app's, not a free choice: the live app is production, the staging app is
        staging, an app this build does not know is refused (its caps would be off), and no Fly app name is a local
        process. The refusal names the app only when it is one of the known ones."""
        if not self.fly_app_name:
            return []
        expected = FLY_APP_ENVIRONMENTS.get(self.fly_app_name)
        if expected is None:
            return [f"FLY_APP_NAME is not an app this build knows ({', '.join(FLY_APP_ENVIRONMENTS)})"]
        if self.environment != expected:
            return [f"ENVIRONMENT must be {expected} on the Fly app {self.fly_app_name}"]
        return []

    def _production_secret_problems(self) -> list[str]:
        """The credentials and the bot check: a pepper and an admin token that are not guessable, the Turnstile gate
        on and able to verify, and the one client-address header Fly's proxy sets."""
        problems = []
        if len(self.ip_hash_pepper.encode("utf-8")) < MIN_PRODUCTION_PEPPER_BYTES:
            problems.append(f"IP_HASH_PEPPER must be set to at least {MIN_PRODUCTION_PEPPER_BYTES} bytes in production "
                            "(generate one: python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
        # An empty token keeps every /api/admin route off (routes._check_admin answers 404): safe, so only a weak one is refused.
        if self.admin_token and len(self.admin_token.strip()) < MIN_PRODUCTION_ADMIN_TOKEN_CHARS:
            problems.append("ADMIN_TOKEN must be empty (the admin routes stay off) or at least "
                            f"{MIN_PRODUCTION_ADMIN_TOKEN_CHARS} characters in production")
        if not self.turnstile_required:
            problems.append("TURNSTILE_REQUIRED must be true in production")
        if not self.turnstile_secret_key:
            problems.append("TURNSTILE_SECRET_KEY must be set in production")
        if self.client_ip_header != PRODUCTION_CLIENT_IP_HEADER:
            problems.append(f"CLIENT_IP_HEADER must be {PRODUCTION_CLIENT_IP_HEADER} in production")
        return problems

    def _production_staging_switch_problems(self) -> list[str]:
        """The staging switches, each refused outright: a stubbed bot check, a model call diverted to another base, a mock
        model, the staging database and the origin-header secret (which would make the live site answer 403 to every
        browser). Each is refused whatever its value, and no value is named."""
        problems = []
        if self.turnstile_stub:
            problems.append("TURNSTILE_STUB must be false in production (the bot check must call Cloudflare)")
        if self.openai_api_base:
            problems.append("OPENAI_API_BASE must be empty in production (live model calls go to the providers)")
        if self.openai_base_url:
            problems.append("OPENAI_BASE_URL must be empty in production (LiteLLM reads it first, so it would divert "
                            "live model calls from the providers)")
        if self.anthropic_api_base:
            problems.append("ANTHROPIC_API_BASE must be empty in production (LiteLLM reads it for the Anthropic route, so "
                            "it would divert the live Sonnet calls from the provider)")
        if self.anthropic_base_url:
            problems.append("ANTHROPIC_BASE_URL must be empty in production (LiteLLM reads it for the Anthropic route, so "
                            "it would divert the live Sonnet calls from the provider)")
        problems += [f"{name.upper()} must not be a mock model in production"
                     for name in LLM_MODEL_SETTINGS if getattr(self, name).strip().lower().startswith(MOCK_MODEL_PREFIX)]
        if _host_of(self.neo4j_uri) == STAGING_NEO4J_HOST:
            problems.append("NEO4J_URI must not be the staging database in production")
        if self.origin_auth_secret:
            problems.append("ORIGIN_AUTH_SECRET must be empty in production (it would refuse every browser)")
        return problems

    def _staging_problems(self) -> list[str]:
        """A staging process touches no live system: only the staging database, no provider key, the mock LLM on its
        private name, the mock models, a secret origin header, no outside call by the freshness monitor, and a client
        address from the header the load generator sets (or Fly's). An ALLOWLIST for every address: a look-alike host is
        refused. The texts name the setting and the rule, never the value."""
        problems = []
        if _host_of(self.neo4j_uri) != STAGING_NEO4J_HOST:
            problems.append(f"NEO4J_URI must be on the staging database host ({STAGING_NEO4J_HOST}) in staging")
        if self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY must be empty in staging (no provider key reaches a staging machine)")
        if not self.openai_api_key.startswith(STAGING_MOCK_KEY_PREFIX):
            problems.append(f"OPENAI_API_KEY must start with {STAGING_MOCK_KEY_PREFIX} in staging (the mock's key)")
        if not _is_internal_http_base(self.openai_api_base):
            problems.append(f"OPENAI_API_BASE must be an http(s) URL on a {STAGING_INTERNAL_HOST_SUFFIX} host in staging")
        problems += [f"{name.upper()} must be an {MOCK_MODEL_PREFIX}* model in staging"
                     for name in SERVE_MODEL_SETTINGS if not _is_staging_mock_model(getattr(self, name))]
        if len(self.origin_auth_secret.encode("utf-8")) < MIN_ORIGIN_AUTH_SECRET_BYTES:
            problems.append(f"ORIGIN_AUTH_SECRET must be set to at least {MIN_ORIGIN_AUTH_SECRET_BYTES} bytes in staging")
        if self.freshness_enabled:
            problems.append("FRESHNESS_ENABLED must be false in staging (the monitor calls EDGAR and the Federal Register)")
        if self.client_ip_header not in STAGING_CLIENT_IP_HEADERS:
            problems.append(f"CLIENT_IP_HEADER must be one of {', '.join(sorted(STAGING_CLIENT_IP_HEADERS))} in staging")
        return problems

    def _production_limit_problems(self) -> list[str]:
        """Every cap and window at or under what the live machine runs (a raised one, or a 0, which means "off"
        elsewhere, would widen what a stolen key or a bot can spend or reach)."""
        problems = [f"{name.upper()} must be 1 to {ceiling} in production"
                    for name, ceiling in PRODUCTION_COUNT_CEILINGS.items() if not 1 <= getattr(self, name) <= ceiling]
        if not 0 < self.max_spend_usd_per_day <= PRODUCTION_MAX_SPEND_USD_PER_DAY:
            problems.append(f"MAX_SPEND_USD_PER_DAY must be above 0 and at most {PRODUCTION_MAX_SPEND_USD_PER_DAY:g} "
                            "in production")
        if not 0 < self.paid_spend_share_per_ip_usd <= PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD:
            problems.append("PAID_SPEND_SHARE_PER_IP_USD must be above 0 and at most "
                            f"{PRODUCTION_PAID_SPEND_SHARE_PER_IP_USD:g} in production")
        if self.rate_limit_window_seconds < PRODUCTION_MIN_RATE_LIMIT_WINDOW_S:
            problems.append(f"RATE_LIMIT_WINDOW_SECONDS must be at least {PRODUCTION_MIN_RATE_LIMIT_WINDOW_S} in production")
        if self.cache_read_budget_per_s > PRODUCTION_MAX_CACHE_READ_BUDGET_PER_S:
            problems.append(f"CACHE_READ_BUDGET_PER_S must be at most {PRODUCTION_MAX_CACHE_READ_BUDGET_PER_S} in production")
        if self.kill_switch_stale_s > PRODUCTION_MAX_KILL_SWITCH_STALE_S:
            problems.append(f"KILL_SWITCH_STALE_S must be at most {PRODUCTION_MAX_KILL_SWITCH_STALE_S} in production")
        return problems

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
