"""The asks of an S7 level: what the service does with the database for one question, with the service's own functions.

Nothing is reimplemented. The state backend (``serve.state``: reserve, reconcile, renew, sweep, the kill-level read, the answer
cache), the ledger rows (``serve.store.log_query``), the answer cache (``store.get_answer`` / ``put_answer``) and the graph reads
(``retrieval.retriever.hybrid_retrieve``, with the question's vector handed in and an embedder that fails if it is ever called)
are called as the routes call them, under the same thread gates (``tools.s7.gates``), with the same bounded wait for a state token.

The M5a flow of a LIVE ask (``stream_runtime`` / ``routes``): cache read, reserve, [stream: retrieval, then ``hold_s``], settle,
cache put. A CACHED ask: cache read, a ledger row. Today's flow (the control; the code before ``bfd7b71``): cache read, the
kill-switch policy read, the day's paid count, retrieval, [stream], a ledger row, cache put; no lease. The control measures what
the old flow cost the DATABASE; it does not hold a thread of the 40-thread pool for the whole stream, as the old sync stream
did (that was a thread-pool problem, not a database one, and the async rewrite removed it).

Every operation is timed from the moment it was requested (see :mod:`tools.s7.record`) and its outcome is one of ``ok``,
``err:<Class>``, ``denied:<reason>`` or ``noslot``.
"""

import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import Any, NamedTuple

from semigraph.retrieval.retriever import hybrid_retrieve
from semigraph.serve import store
from semigraph.serve.state.backend import BoundedDriver, Denied

from tools.s7 import plan
from tools.s7.counting import Tally
from tools.s7.gates import Gate
from tools.s7.record import Recorder
from tools.s7.schedule import Dispatcher, Job

IP_HASH_VERSION = 1


class NoEmbedder:
    """The replay never embeds: the vector is handed to ``hybrid_retrieve``. Reaching the embedder is a bug in the replay."""

    def encode_query(self, text: str):
        raise AssertionError("the S7 replay must not embed a question: hand the vector to hybrid_retrieve")


SERVER_CODE = re.compile(r"[A-Za-z0-9_.]{1,120}")


def failure_status(exc: BaseException) -> str:
    """``err:<Class>``, plus the SERVER's own code when the exception (or the driver error it was raised from) carries one:
    ``err:StateUnavailable:Neo.TransientError.General.MemoryPoolOutOfMemoryError``. The report reads a memory error from it; the
    state package wraps every driver error in ``StateUnavailable`` and a retrieval error reaches the replay unwrapped, so the
    code is the only place both say what the server said. Never the message: a driver quotes the statement's parameters."""
    status = f"err:{type(exc).__name__}"
    for source in (exc, exc.__cause__):
        code = getattr(source, "code", None)
        if isinstance(code, str) and SERVER_CODE.fullmatch(code):
            return f"{status}:{code}"
    return status


class Outcome(NamedTuple):
    status: str
    value: Any


@dataclass(frozen=True)
class Gates:
    state: Gate
    db: Gate
    pool: Gate


@dataclass(frozen=True)
class Ask:
    kind: str                       # "live" | "cached"
    index: int
    question: str                   # the salted text for a live ask
    key: str                        # its answer-cache key
    vector: Any
    ip: str


def ip_prefix(run_id: str) -> str:
    return f"s7:{run_id}"


class Runtime:
    """Everything an ask needs. ``backend`` is None for today's flow."""

    def __init__(self, *, cfg: plan.LevelConfig, inputs: plan.ReplayInputs, rec: Recorder, tally: Tally, gates: Gates,
                 read_driver: Any, store_driver: BoundedDriver | None, backend: Any, dispatcher: Dispatcher, t0: float,
                 clock: Callable[[], float] = time.perf_counter):
        self.cfg, self.inputs, self.rec, self.tally, self.gates = cfg, inputs, rec, tally, gates
        self.read_driver, self.store_driver, self.backend, self.dispatcher = read_driver, store_driver, backend, dispatcher
        self.t0, self.clock = t0, clock
        self.embedder = NoEmbedder()
        self._order = list(range(len(inputs.pool)))
        random.Random(f"{cfg.seed}:pool").shuffle(self._order)
        self._examples = list(inputs.examples)
        self._wait = cfg.state_op_timeout_s
        self._m5a = cfg.mix == "m5a"

    # ---- one timed operation ---------------------------------------------------------------------------------------

    def op(self, name: str, fn: Callable[[], Any], *, gate: Gate | None = None, wait: float | None = None,
           due: float | None = None, classify: Callable[[Any], str] | None = None, reraise: bool = False) -> Outcome:
        requested = due if due is not None else self.clock()
        began = self.clock()
        status, value, failure = "ok", None, None
        with self.tally.operation(name):
            if gate is not None and not gate.acquire(wait):
                status = "noslot"
            else:
                try:
                    value = fn()
                    status = classify(value) if classify is not None else "ok"
                except Exception as exc:                                  # noqa: BLE001 - the failure is the finding
                    status, failure = failure_status(exc), exc
                finally:
                    if gate is not None:
                        gate.release()
        end = self.clock()
        self.rec.add(requested - self.t0, name, (end - requested) * 1000.0, (end - began) * 1000.0, status)
        self.tally.called(name)
        if status != "ok":
            self.tally.add("errors")
            if status.startswith("denied:"):
                self.tally.add("denied")
        if failure is not None and reraise:
            raise failure
        return Outcome(status, value)

    # ---- the asks ---------------------------------------------------------------------------------------------------

    def make_live(self, index: int) -> Ask:
        item = self.inputs.pool[self._order[index % len(self._order)]]
        question = plan.salted(item["q"], self.cfg.salt_start + index)
        key = store.cache_key(question, plan.STRATEGY, self.inputs.snapshot_id, self.inputs.template)
        return Ask("live", index, question, key, self.inputs.vectors[item["id"]],
                   f"{ip_prefix(self.cfg.run_id)}:{index % plan.IP_POOL:04d}")

    def make_cached(self, index: int) -> Ask:
        example = self._examples[index % len(self._examples)]
        key = store.cache_key(example["question"], plan.STRATEGY, self.inputs.snapshot_id, self.inputs.template)
        return Ask("cached", index, example["question"], key, None,
                   f"{ip_prefix(self.cfg.run_id)}:{(index + plan.IP_POOL // 2) % plan.IP_POOL:04d}")

    def arrivals(self, events: Iterator[tuple[float, str]]) -> Iterator[tuple[float, Job]]:
        live = cached = 0
        for t, kind in events:
            if kind == "live":
                yield t, partial(self.live, self.make_live(live))
                live += 1
            else:
                yield t, partial(self.cached, self.make_cached(cached))
                cached += 1

    def live(self, ask: Ask, due: float) -> None:
        self.tally.add("live_started")
        (self._live_m5a if self._m5a else self._live_today)(ask, due)

    def cached(self, ask: Ask, due: float) -> None:
        self.tally.add("cached_started")
        (self._cached_m5a if self._m5a else self._cached_today)(ask, due)

    def _retrieve(self, ask: Ask, gate: Gate) -> Outcome:
        return self.op("retrieval", lambda: hybrid_retrieve(ask.question, self.read_driver, self.embedder, query_vec=ask.vector),
                       gate=gate, wait=None)

    @staticmethod
    def _unexpected_hit(value: Any) -> str:
        return "err:CacheHit" if value else "ok"                         # a salted question must always miss

    @staticmethod
    def _unexpected_miss(value: Any) -> str:
        return "ok" if value else "err:CacheMiss"                        # a seeded example must always hit

    # ---- M5a: cache, lease, stream, settle ----------------------------------------------------------------------------

    def _live_m5a(self, ask: Ask, due: float) -> None:
        backend, state, ttl = self.backend, self.gates.state, self.cfg.cache_ttl_hours
        if self.op("cache_read", lambda: backend.cache_get(ask.key, ttl), gate=state, wait=self._wait, due=due,
                   classify=self._unexpected_hit).status != "ok":
            return
        reserved = self.op("reserve", lambda: backend.reserve(
            ip_hash=ask.ip, strategy=plan.STRATEGY, workspace=False, estimate_micro=plan.ESTIMATE_MICRO,
            now_wall=time.time(), now_mono=time.monotonic()), gate=state, wait=self._wait,
            classify=lambda v: f"denied:{v.value}" if isinstance(v, Denied) else "ok")
        if reserved.status != "ok":
            return
        lease = reserved.value
        self.tally.add("granted")
        backend.mark_started(lease.lease_id)                              # memory only, as the route's stream does
        retrieved = self._retrieve(ask, self.gates.db)
        ok = retrieved.status == "ok"
        hold = self.cfg.hold_s if ok else 0.0                              # a failed retrieval ends the stream at once
        self.dispatcher.at(self.clock() + hold, partial(self._settle_m5a, ask, lease, "ok" if ok else "error"))

    def _settle_m5a(self, ask: Ask, lease: Any, outcome: str, due: float) -> None:
        backend, state = self.backend, self.gates.state
        settled = self.op("settle", lambda: backend.reconcile(lease.lease_id, outcome=outcome, usage=plan.USAGE,
                                                              cost_micro=plan.COST_MICRO), gate=state, wait=None, due=due)
        if settled.status == "ok" and settled.value is not True:
            self.tally.add("settle_not_charged")
        if outcome == "ok":
            self.op("cache_put", lambda: backend.cache_put(
                question=ask.question, strategy=plan.STRATEGY, answer=plan.ANSWER, citations=[], hallucinated=[],
                usage=plan.USAGE, cost_usd=plan.COST_USD, snapshot_id=self.inputs.snapshot_id), gate=state, wait=self._wait)

    def _cached_m5a(self, ask: Ask, due: float) -> None:
        backend, state = self.backend, self.gates.state
        hit = self.op("cache_read", lambda: backend.cache_get(ask.key, self.cfg.cache_ttl_hours), gate=state, wait=self._wait,
                      due=due, classify=self._unexpected_miss)
        if hit.status != "ok":
            return
        logged = self.op("cached_log", lambda: store.log_query(
            self.store_driver, ip_hash=ask.ip, strategy=plan.STRATEGY, cached=True, ip_hash_v=IP_HASH_VERSION),
            gate=state, wait=self._wait)
        if logged.status == "ok":
            self.tally.add("cached_logged")

    # ---- today's flow (the control) -----------------------------------------------------------------------------------

    def _live_today(self, ask: Ask, due: float) -> None:
        driver, pool, ttl = self.read_driver, self.gates.pool, self.cfg.cache_ttl_hours
        if self.op("cache_read", lambda: store.get_answer(driver, ask.key, ttl), gate=pool, wait=None, due=due,
                   classify=self._unexpected_hit).status != "ok":
            return
        if self.op("policy_read", lambda: store.kill_switch_on(driver, False), gate=pool, wait=None).status != "ok":
            return
        if self.op("count_read", lambda: store.paid_queries_today(driver), gate=pool, wait=None).status != "ok":
            return
        retrieved = self._retrieve(ask, pool)
        if retrieved.status != "ok":
            return                                                         # the old flow wrote no row for a failed ask
        self.dispatcher.at(self.clock() + self.cfg.hold_s, partial(self._post_today, ask))

    def _post_today(self, ask: Ask, due: float) -> None:
        driver, pool = self.read_driver, self.gates.pool
        logged = self.op("paid_log", lambda: store.log_query(
            driver, ip_hash=ask.ip, strategy=plan.STRATEGY, cached=False, usage=plan.USAGE, cost_usd=plan.COST_USD,
            ip_hash_v=IP_HASH_VERSION), gate=pool, wait=None, due=due)
        if logged.status == "ok":
            self.tally.add("paid_logged")
        self.op("cache_put", lambda: store.put_answer(
            driver, question=ask.question, strategy=plan.STRATEGY, answer=plan.ANSWER, citations=[], hallucinated=[],
            usage=plan.USAGE, cost_usd=plan.COST_USD, snapshot_id=self.inputs.snapshot_id), gate=pool, wait=None)

    def _cached_today(self, ask: Ask, due: float) -> None:
        driver, pool = self.read_driver, self.gates.pool
        hit = self.op("cache_read", lambda: store.get_answer(driver, ask.key, self.cfg.cache_ttl_hours), gate=pool, wait=None,
                      due=due, classify=self._unexpected_miss)
        if hit.status != "ok":
            return
        logged = self.op("cached_log", lambda: store.log_query(
            driver, ip_hash=ask.ip, strategy=plan.STRATEGY, cached=True, ip_hash_v=IP_HASH_VERSION), gate=pool, wait=None)
        if logged.status == "ok":
            self.tally.add("cached_logged")


class MeteredBackend:
    """What the real ``MaintenanceThread`` is given instead of the backend: the same methods, each timed under the name the
    report knows (``kill_read``, ``renew``, ``sweep``). The thread, its schedule and its error handling are the service's own."""

    def __init__(self, backend: Any, runtime: Runtime):
        self._backend, self._runtime = backend, runtime
        self.registry = backend.registry

    def refresh_kill_level(self) -> str:
        return self._runtime.op("kill_read", self._backend.refresh_kill_level, reraise=True).value

    def renew(self, lease_id: str, now_wall: float) -> bool:
        return self._runtime.op("renew", lambda: self._backend.renew(lease_id, now_wall), reraise=True).value

    def sweep(self, now: float) -> int:
        return self._runtime.op("sweep", lambda: self._backend.sweep(now), reraise=True).value

    def drain_settles(self, *args: Any, **kwargs: Any) -> int:
        return self._backend.drain_settles(*args, **kwargs)

    def pending_settles(self) -> int:
        return self._backend.pending_settles()

    def flush_settles(self, budget_s: float) -> int:
        return self._backend.flush_settles(budget_s)
