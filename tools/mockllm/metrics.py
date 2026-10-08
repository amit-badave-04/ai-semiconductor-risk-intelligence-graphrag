"""Counters of the mock and the contract of ``GET /metrics`` (what the load test reads; contract version 1).

``GET /metrics`` returns this JSON (``?format=prometheus`` returns the numeric fields as ``mockllm_<name> <value>`` lines):

* ``process_cpu_seconds`` and ``uptime_s`` and ``cpu_count``: CPU use over a window is
  ``(cpu2 - cpu1) / (t2 - t1) / cpu_count`` from two scrapes (``cpu_source`` says whether it came from psutil or from
  ``time.process_time``, which are the same quantity: user plus system time of this process);
* ``inflight`` / ``inflight_max``: requests being served now / the most at once since start;
* ``requests_total``: every POST to chat completions, including the 429s and the 400s (``bad_requests_total`` counts the 400s);
  ``requests_by_model`` counts the ones whose body parsed;
* ``rate_limited_total``, ``invalid_id_injected_total``, ``slow_ttft_injected_total``: what the knobs did.
  ``rate_limited_by_role`` splits the 429s by the role of the request (draft / strong / planner), which is what a fault-phase
  reconciliation needs, because the callers react differently: the service's draft call and its planner call run with
  ``num_retries=0``, so an injected 429 there is one failed draft (the service escalates) or one planner fallback to the plain
  retrieval. The strong call runs with ``num_retries=0`` too, whichever way it is reached (the escalation, a question routed
  straight to the strong model, or the sole answer model of the rollback configuration): the paid-call meter counts every
  provider call, so the provider SDK may not make hidden ones. The stream retries a transient error itself after its own
  backoff (5 s, one more attempt), each attempt is one request here, and a second 429 reaches the service as an error event;
* ``responses_by_role`` (draft / strong / planner), ``requests_by_model``, ``responses_by_source`` (excerpts / risk_lines /
  template / refusal / tool_call / final): what was answered;
* ``streams_started`` / ``streams_completed`` / ``streams_aborted`` (the client left before the end);
* ``prompt_tokens_total`` / ``completion_tokens_total``: the usage the mock reported;
* ``knobs``: the knobs in force now.
"""

import os
import time
from collections import Counter
from dataclasses import dataclass, field

from .knobs import Knobs
from .reply import Reply

CONTRACT = 1
MAX_MODEL_KEYS = 50

try:                                           # psutil is optional: the image does not install it
    import psutil
    _PROCESS = psutil.Process()
except Exception:  # noqa: BLE001 - any import or permission problem means "use the stdlib"
    _PROCESS = None


def process_cpu_seconds() -> tuple[float, str]:
    """(user + system CPU seconds of this process, the source of the figure)."""
    if _PROCESS is not None:
        try:
            times = _PROCESS.cpu_times()
            return times.user + times.system, "psutil"
        except Exception:  # noqa: BLE001
            pass
    return time.process_time(), "process_time"


@dataclass
class Metrics:
    started_wall: float
    started_mono: float
    requests_total: int = 0
    bad_requests_total: int = 0
    rate_limited_total: int = 0
    invalid_id_injected_total: int = 0
    slow_ttft_injected_total: int = 0
    streams_started: int = 0
    streams_completed: int = 0
    streams_aborted: int = 0
    prompt_tokens_total: int = 0
    completion_tokens_total: int = 0
    inflight: int = 0
    inflight_max: int = 0
    requests_by_model: Counter = field(default_factory=Counter)
    responses_by_role: Counter = field(default_factory=Counter)
    responses_by_source: Counter = field(default_factory=Counter)
    rate_limited_by_role: Counter = field(default_factory=Counter)

    def count_model(self, model: str) -> None:
        """Count a parsed request by model id; a client that invents model ids cannot grow the table past ``MAX_MODEL_KEYS``."""
        key = model if model in self.requests_by_model or len(self.requests_by_model) < MAX_MODEL_KEYS else "other"
        self.requests_by_model[key[:80]] += 1

    def enter(self) -> None:
        self.inflight += 1
        self.inflight_max = max(self.inflight_max, self.inflight)

    def leave(self) -> None:
        self.inflight -= 1

    def record_reply(self, reply: Reply) -> None:
        self.responses_by_role[reply.role] += 1
        self.responses_by_source[reply.source] += 1
        self.prompt_tokens_total += reply.prompt_tokens
        self.completion_tokens_total += reply.completion_tokens
        self.invalid_id_injected_total += int(reply.invalid_injected)
        self.slow_ttft_injected_total += int(reply.slow)

    def snapshot(self, knobs: Knobs, now_mono: float) -> dict:
        cpu, source = process_cpu_seconds()
        return {"contract": CONTRACT, "started_at": self.started_wall, "uptime_s": round(now_mono - self.started_mono, 3),
                "cpu_count": os.cpu_count() or 1, "process_cpu_seconds": round(cpu, 4), "cpu_source": source,
                "inflight": self.inflight, "inflight_max": self.inflight_max, "requests_total": self.requests_total,
                "bad_requests_total": self.bad_requests_total, "rate_limited_total": self.rate_limited_total,
                "rate_limited_by_role": dict(self.rate_limited_by_role), "invalid_id_injected_total": self.invalid_id_injected_total,
                "slow_ttft_injected_total": self.slow_ttft_injected_total, "streams_started": self.streams_started,
                "streams_completed": self.streams_completed, "streams_aborted": self.streams_aborted,
                "prompt_tokens_total": self.prompt_tokens_total, "completion_tokens_total": self.completion_tokens_total,
                "requests_by_model": dict(self.requests_by_model), "responses_by_role": dict(self.responses_by_role),
                "responses_by_source": dict(self.responses_by_source), "knobs": knobs.as_dict()}


def to_prometheus(snapshot: dict) -> str:
    """The numeric fields of ``snapshot``, one ``mockllm_<name> <value>`` line each; the per-key counters get a label."""
    lines = []
    for name, value in snapshot.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            lines.append(f"mockllm_{name} {value}")
        elif isinstance(value, dict) and name != "knobs":
            label = {"requests_by_model": "model", "responses_by_role": "role", "responses_by_source": "source",
                     "rate_limited_by_role": "role"}[name]
            lines += [f'mockllm_{name}{{{label}="{_label_value(key)}"}} {count}' for key, count in sorted(value.items())]
    return "\n".join(lines) + "\n"


def _label_value(raw: str) -> str:
    return raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
