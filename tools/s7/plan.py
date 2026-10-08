"""What one S7 level is asked to do: its configuration and its inputs (no Settings is ever built here).

The sizes of the thread gates and the time bounds are the SERVICE's own constants and defaults, imported, not typed: the state
limiter (``serve.limiters.STATE_THREADS``), the graph-read limiter (the default of ``Settings.db_thread_limit``), the bound on a
state operation (``graph.client.STATE_OP_TIMEOUT_DEFAULT_S``) and the lease timings (the defaults of the ``Settings`` fields).
``Settings.model_fields`` is class metadata: reading a default builds nothing, so the production validators (which refuse an
unknown ``FLY_APP_NAME``) are never run by this tool.

Salt: a live ask is the pool question plus ``" (ref 7NNNNNN)"``, one worker digit and a six-digit zero-padded counter. The token is
ONE run of seven digits, so the retriever's and the router's year detectors never see a ``20xx`` in it
(``tools/loadtest/salt.py`` explains and tests the same format); it changes the answer-cache key, so the ask is a miss.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace

# Before the first semigraph import that can load LiteLLM (the estimate reads the retriever's anchor cap): it downloads its
# model-price table when imported unless told to use its bundled copy, and the tools machine makes no call it was not asked to.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from semigraph.config import Settings  # noqa: E402
from semigraph.graph.client import STATE_OP_TIMEOUT_DEFAULT_S  # noqa: E402
from semigraph.serve import estimate  # noqa: E402
from semigraph.serve.limiters import STATE_THREADS  # noqa: E402

SALT_WORKER = 7
SALT_WIDTH = 6
MAX_COUNTER = 10**SALT_WIDTH - 1
STRATEGY = "hybrid"
IP_POOL = 1000                                    # distinct client addresses, as many as the gate's virtual users
# The live models and answer budget the replay's estimate is priced on: fly.toml [env] (a test pins them to it), the one place
# the service's production shape is written down. Every other field the estimate reads is the code default.
LIVE_ANSWER_MODEL = "openai/gpt-6-luna"
LIVE_ESCALATION_MODEL = "anthropic/claude-sonnet-5"
LIVE_ANSWER_MAX_TOKENS = 2400
_ESTIMATE_FIELDS = ("agent_planner_model", "agent_max_model_calls", "max_question_chars", "llm_input_price_per_mtok",
                    "llm_output_price_per_mtok")
COST_MICRO = 1_500                                # a typical live answer ($0.0015, the ledger's range is $0.001-0.002)
COST_USD = COST_MICRO / 1_000_000
USAGE = {"prompt_tokens": 6000, "completion_tokens": 400}
ANSWER = "S7 replay answer."
THREADPOOL_TOKENS = 40                            # anyio's default worker-thread pool: what ``run_in_threadpool`` used before M5a
DEFAULT_WORKERS = 128                             # driver threads: more than the gates, so the gates are what limits


def _default(name: str):
    return Settings.model_fields[name].default


def live_hybrid_estimate_micro() -> int:
    """The worst-case estimate of one live hybrid ask, by the service's own arithmetic (``serve/estimate``): the micro-dollars
    its lease holds between the reserve and the settle, and what the per-address share and the day cap count it at. Read off
    the estimate, never typed, so the replay admits what the service admits. The estimate takes any object with the fields it
    reads, so a plain namespace of the live models and the code defaults is enough: no ``Settings`` is built, not even without
    its validators."""
    live = SimpleNamespace(answer_model=LIVE_ANSWER_MODEL, escalation_model=LIVE_ESCALATION_MODEL,
                           llm_answer_max_tokens=LIVE_ANSWER_MAX_TOKENS, **{name: _default(name) for name in _ESTIMATE_FIELDS})
    return estimate.estimate_micro(STRATEGY, live)


# 709,573 at the time of writing (tests/test_tools_s7.py pins it). The replay runs with every cap and the per-address share OFF
# (replay._settings), so this is the estimate each lease row carries, not a number an admission is decided on.
ESTIMATE_MICRO = live_hybrid_estimate_micro()


def salted(question: str, counter: int) -> str:
    """The live-ask text: ``question`` and the fixed-width salt of ``counter`` (``ValueError`` past the width)."""
    if type(counter) is not int or not 0 <= counter <= MAX_COUNTER:
        raise ValueError(f"the salt counter must be an int in 0..{MAX_COUNTER}, got {counter!r}")
    return f"{question} (ref {SALT_WORKER}{counter:0{SALT_WIDTH}d})"


@dataclass(frozen=True)
class LevelConfig:
    """One phase run. ``duration_s`` is the arrival window; the asks that arrive in it are drained afterwards (``drain_s``).

    ``hold_s`` is how long a live ask holds its lease between the reserve and the settle: the stream. The default is the
    gate's own figure, at least 40 concurrent live streams at 2.6 live asks/s, i.e. 40 / 2.6 = 15.4 s per stream."""

    phase: str
    mix: str
    live_rate: float
    duration_s: float
    judged_s: float | None
    run_id: str
    backend: str = "inprocess"
    hold_s: float = 15.0
    drain_s: float = 120.0
    probe_every_s: float = 5.0
    workers: int = DEFAULT_WORKERS
    seed: int = 1
    salt_start: int = 0
    kill_refresh_s: float = field(default_factory=lambda: _default("kill_switch_refresh_s"))
    kill_stale_s: float = field(default_factory=lambda: _default("kill_switch_stale_s"))
    lease_renew_s: float = field(default_factory=lambda: _default("lease_renew_s"))
    lease_ttl_s: float = field(default_factory=lambda: _default("lease_ttl_s"))
    state_op_timeout_s: float = STATE_OP_TIMEOUT_DEFAULT_S
    state_slots: int = STATE_THREADS
    db_slots: int = field(default_factory=lambda: _default("db_thread_limit"))
    pool_slots: int = THREADPOOL_TOKENS
    max_inflight: int = 100_000
    cache_ttl_hours: int = field(default_factory=lambda: _default("answer_cache_ttl_hours"))

    def __post_init__(self) -> None:
        if self.mix not in ("m5a", "today"):
            raise ValueError(f"unknown mix {self.mix!r}")
        if self.backend not in ("inprocess", "neo4j"):
            raise ValueError(f"unknown backend {self.backend!r}")
        for name in ("live_rate", "duration_s", "hold_s", "drain_s"):
            if not getattr(self, name) >= 0:
                raise ValueError(f"{name} must not be negative")
        for name in ("probe_every_s", "kill_refresh_s", "lease_renew_s", "lease_ttl_s", "state_op_timeout_s"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        if self.workers < 1 or self.state_slots < 1 or self.db_slots < 1 or self.pool_slots < 1:
            raise ValueError("workers and every gate need at least one token")


@dataclass(frozen=True)
class ReplayInputs:
    """``pool``: ``[{"id", "q"}]`` (the live questions); ``vectors``: ``{id: vector}``; ``examples``: ``[{"question"}]`` (the
    cached ones); ``snapshot_id`` and ``template``: what the cache keys are made of."""

    pool: Sequence[Mapping]
    vectors: Mapping[str, Sequence[float]]
    examples: Sequence[Mapping]
    snapshot_id: str
    template: str

    def __post_init__(self) -> None:
        if not self.pool or not self.examples:
            raise ValueError("a level needs at least one pool question and one cached example")
        missing = [item["id"] for item in self.pool if item["id"] not in self.vectors]
        if missing:
            raise ValueError(f"{len(missing)} pool question(s) have no vector, e.g. {missing[:3]}")
