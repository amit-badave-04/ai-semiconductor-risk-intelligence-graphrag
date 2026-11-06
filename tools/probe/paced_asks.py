"""The ask workload of window W1c: how many asks (derived), when each goes out (a pure decision) and the sender that keeps the plan.

Council 8 (docs/v2/research/m5-councils/council8/verdict.md, "The Recommendation" 2, "W1c"): window W1c is window W1b with ONE change,
the asks sent while the max-size upload embeds. W1b's saturating load (two asks in flight at all times, about 150 asks) starved the
upload past its 1,200 s embed timeout on the throttled machine. W1c sends a paced load anchored to the live day cap instead:

* the COUNT is ``floor(production max_spend_usd_per_day / the live hybrid estimate)``, worked out in whole micro-dollars by
  :func:`derive_ask_count` from ``semigraph.config`` and ``semigraph.serve.estimate`` for the models in ``fly.toml``
  (10,000,000 // 709,573 = 14), never written down here; ``deploy/staging/windows.json`` registers the same number and the driver
  refuses to run when the two differ;
* the SCHEDULE is one ask every ``interval_s`` seconds from the upload's acceptance (the first ask AT the acceptance), anchored to that
  moment and never re-anchored, each ask from its own client address, at most ``max_in_flight`` in flight at once: when that many
  are still running at the moment the next is due, the next WAITS (the delay is recorded, the ask is not skipped);
* sending STOPS at the job's end plus the tail (:meth:`PacedSender.stop_sending_at`): the count is a cap, not a target, so an upload
  that is ready early gets fewer asks and one that takes longer than the schedule gets no more after the last.

:func:`next_step` is the whole pacing decision as a pure function of the clock, so the tests drive it on a fake one; :class:`PacedSender`
runs it on threads. Standard library only at module level (``semigraph`` is imported inside :func:`derive_ask_count`); nothing here
imports the driver, so ``tools/probe/w1b.py`` can import it.
"""

from __future__ import annotations

import sys
import threading
import tomllib
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
LIVE_TOML = ROOT / "fly.toml"

PACED_MODE = "paced"
STOP_CAP, STOP_JOB = "cap", "job_ready_plus_tail"
POLL_S = 0.05                  # while an ask waits for a slot: how often the slots are looked at again
MAX_WAIT_S = 0.25              # no sleep is longer, so a stop (or a due ask) is noticed within a quarter of a second
JOIN_S = 190.0                 # how long finish() waits for an ask in flight (the driver's ask cap, 180 s, and a margin)
# Every field the estimate reads, as ``tests/test_serve_estimate.live_settings`` builds them: fly.toml's value, else the code default.
ESTIMATE_FIELDS = ("answer_model", "escalation_model", "agent_planner_model", "llm_answer_max_tokens", "llm_input_price_per_mtok",
                   "llm_output_price_per_mtok", "agent_max_model_calls", "agent_max_tool_calls", "max_question_chars")
FORMULA = "floor(day_cap_micro / hybrid_estimate_micro)"


# ---- the count ---------------------------------------------------------------------------------------------------------------------

def derive_ask_count(live_toml: Path = LIVE_TOML) -> dict:
    """The asks the live day cap admits at the live hybrid estimate, with every input of the sum. ``asks`` is
    ``day_cap_micro // hybrid_estimate_micro`` (whole micro-dollars). ``semigraph`` is imported here, not at module level: the
    estimate imports the retriever (and litellm) when the anchor cap is read. Raises ``ValueError`` for a cap that admits no ask."""
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from semigraph import config
    from semigraph.config import Settings
    from semigraph.serve import estimate

    env = tomllib.loads(Path(live_toml).read_text(encoding="utf-8"))["env"]
    settings = Settings(_env_file=None, **{name: env.get(name.upper(), Settings.model_fields[name].default) for name in ESTIMATE_FIELDS})
    cap_usd = config.PRODUCTION_MAX_SPEND_USD_PER_DAY
    cap_micro, hybrid_micro = round(cap_usd * 1_000_000), estimate.estimate_micro("hybrid", settings)
    asks = cap_micro // hybrid_micro
    if asks < 1:
        raise ValueError(f"the production day cap of ${cap_usd:g} admits no hybrid ask at the live estimate of ${hybrid_micro / 1_000_000:.6f}")
    return {"asks": asks, "formula": FORMULA, "unrounded": round(cap_micro / hybrid_micro, 4),
            "day_cap_usd": cap_usd, "day_cap_micro": cap_micro,
            "hybrid_estimate_usd": hybrid_micro / 1_000_000, "hybrid_estimate_micro": hybrid_micro,
            "models": {"answer": settings.answer_model, "escalation": settings.escalation_model},
            "inputs": "semigraph.config.PRODUCTION_MAX_SPEND_USD_PER_DAY; semigraph.serve.estimate.estimate_micro('hybrid') on the "
                      "models and token cap of fly.toml [env]"}


# ---- the schedule: a pure decision ---------------------------------------------------------------------------------------------------

def planned_offsets(count: int, interval_s: float) -> list[float]:
    """Seconds after the upload's acceptance at which each ask is due: ``0, interval, 2 x interval, ...`` (14 asks at 30 s span 390 s)."""
    return [round(interval_s * n, 3) for n in range(count)]


class Step(NamedTuple):
    action: str                # "send" | "wait" | "stop"
    wait_s: float = 0.0        # for "wait": how long until the decision is worth taking again
    reason: str = ""           # for "stop": STOP_CAP or STOP_JOB; for "wait": not_due or slots_full


def next_step(*, now: float, t0: float, interval_s: float, count: int, sent: int, in_flight: int, max_in_flight: int,
              stop_at: float | None = None) -> Step:
    """What the sender does next. Ask ``n`` is due at ``t0 + n x interval_s`` whatever happened to the asks before it (the schedule is
    anchored, never shifted by a wait). Order: all ``count`` sent, then stop; ``now`` at or past ``stop_at`` (exclusive boundary: an ask
    due at that very moment is not sent), then stop; not yet due, wait; every slot taken, wait for one; else send."""
    if sent >= count:
        return Step("stop", reason=STOP_CAP)
    if stop_at is not None and now >= stop_at:
        return Step("stop", reason=STOP_JOB)
    due = t0 + interval_s * sent
    if now < due:
        return Step("wait", (due if stop_at is None else min(due, stop_at)) - now, "not_due")
    if in_flight >= max_in_flight:
        return Step("wait", POLL_S if stop_at is None else min(POLL_S, stop_at - now), "slots_full")
    return Step("send")


def summarize_records(records: list[dict], job_start: float, job_end: float) -> dict:
    """The counts of a list of ``{start, end, outcome}`` ask records: how many were sent, finished (``done``), were cached answers, ended
    in each kind of refusal or drop, and how many ``done`` asks overlapped the job's run (the verdict needs at least one)."""
    outcomes = Counter(r["outcome"] for r in records)
    overlap = sum(1 for r in records if r["outcome"] == "done" and r["start"] < job_end and r["end"] > job_start)
    return {"sent": len(records), "done": outcomes["done"], "cached": outcomes["done_cached"], "done_overlapping_job": overlap,
            "errors": {k: v for k, v in sorted(outcomes.items()) if k not in ("done", "done_cached")}}


# ---- the sender ------------------------------------------------------------------------------------------------------------------------

class PacedSender:
    """Keeps :func:`next_step`'s plan: one scheduler thread decides, one short-lived thread per ask runs ``ask(n)`` (which returns the
    ask's outcome). ``t0`` is the upload's acceptance. The records (planned, sent and end times, outcome) are kept for the report."""

    def __init__(self, *, ask: Callable[[int], str], now: Callable[[], float], sleep: Callable[[float], None], t0: float, count: int,
                 interval_s: float, max_in_flight: int):
        if count < 1 or interval_s <= 0 or max_in_flight < 1:
            raise ValueError(f"a paced load needs at least one ask, a positive interval and a slot, got {count}, {interval_s}, {max_in_flight}")
        self.ask, self.now, self.sleep, self.t0 = ask, now, sleep, t0
        self.count, self.interval_s, self.max_in_flight = count, interval_s, max_in_flight
        self.stopped_by: str | None = None
        self._lock, self._stop_at = threading.Lock(), None
        self._records: list[dict] = []
        self._started = self._in_flight = self._max_seen = 0
        self._scheduler: threading.Thread | None = None
        self._workers: list[threading.Thread] = []

    def start(self) -> None:
        self._scheduler = threading.Thread(target=self._run, name="w1c-scheduler", daemon=True)
        self._scheduler.start()

    def stop_sending_at(self, when: float) -> None:
        """No ask is started at or after ``when`` (the job's end plus the tail). The earliest time given wins."""
        with self._lock:
            self._stop_at = when if self._stop_at is None else min(self._stop_at, when)

    def wait(self, timeout: float) -> bool:
        """True once the scheduler is done (every ask sent, or sending stopped)."""
        if self._scheduler is not None:
            self._scheduler.join(timeout)
        return self._scheduler is None or not self._scheduler.is_alive()

    def finish(self, join_s: float = JOIN_S) -> None:
        """Stop sending now and wait for the asks still running."""
        self.stop_sending_at(self.now())
        self.wait(join_s)
        for worker in list(self._workers):
            worker.join(join_s)

    def _run(self) -> None:
        while True:
            with self._lock:
                sent, in_flight, stop_at = self._started, self._in_flight, self._stop_at
            step = next_step(now=self.now(), t0=self.t0, interval_s=self.interval_s, count=self.count, sent=sent, in_flight=in_flight,
                             max_in_flight=self.max_in_flight, stop_at=stop_at)
            if step.action == "stop":
                self.stopped_by = step.reason
                return
            if step.action == "wait":
                self.sleep(min(step.wait_s, MAX_WAIT_S))
            else:
                self._launch(sent)

    def _launch(self, n: int) -> None:
        started = self.now()
        with self._lock:
            self._started += 1
            self._in_flight += 1
            self._max_seen = max(self._max_seen, self._in_flight)
        worker = threading.Thread(target=self._work, args=(n, started), name=f"w1c-ask-{n}", daemon=True)
        self._workers.append(worker)
        worker.start()

    def _work(self, n: int, started: float) -> None:
        try:
            outcome = self.ask(n)
        except Exception:  # noqa: BLE001  an ask that blew up is recorded as one, never lost with its thread
            outcome = "exception"
        ended = self.now()
        with self._lock:
            self._records.append({"n": n, "start": started, "end": ended, "outcome": outcome})
            self._in_flight -= 1

    def summary(self, job_start: float, job_end: float) -> dict:
        """The counts of every ask sent, plus the schedule: the offsets planned for all ``count`` asks, and for each ask sent its planned
        and actual send offsets, its delay (a wait for a slot) and its end offset, all in seconds after ``t0``."""
        with self._lock:
            records, in_flight_max = sorted(self._records, key=lambda r: r["n"]), self._max_seen
        schedule = [{"n": r["n"], "planned_s": round(self.interval_s * r["n"], 3), "sent_s": round(r["start"] - self.t0, 3),
                     "delay_s": round(r["start"] - self.t0 - self.interval_s * r["n"], 3), "end_s": round(r["end"] - self.t0, 3),
                     "outcome": r["outcome"]} for r in records]
        return {**summarize_records(records, job_start, job_end), "planned_offsets_s": planned_offsets(self.count, self.interval_s),
                "schedule": schedule, "unsent": self.count - len(records), "stopped_by": self.stopped_by,
                "max_in_flight_seen": in_flight_max}
