"""The StateBackend contract and the code both backends share (M5a I4a; docs/v2/M5A_BUILD_PLAN.md section 3).

Money is an integer number of MICRO-dollars everywhere in this package, so a daily total can never drift by a float
rounding. :func:`usd_to_micro` is the one place a float price becomes micro-dollars (round half up on the micro).

Clocks: ``now_wall`` / ``sweep(now)`` / ``renew(now_wall)`` are wall-clock epoch seconds (the durable ``lease_until``
on a ledger row is wall clock so another machine can read it); ``now_mono`` and the kill-level staleness are monotonic.

Every backend method is a plain synchronous function. The async caller runs each that touches the store on a worker
thread, holding one token of ``limiters.state`` (``stream_runtime.slot_call``), with NO caller-side timeout once it
holds the token: abandoning a thread that a shielded reserve is running in would let the reserve complete after its
caller gave up. (Only the wait for a free token is bounded, by ``state_op_timeout_s``.) The bound lives in the work: the
state driver's per-attempt timeouts and short retry window (``graph.client.make_state_driver``) and the server-side
transaction timeout set by :mod:`.ledger`. Any driver error becomes :class:`StateUnavailable`; a call that takes over
one second is logged ``state_slow``.

``kill_level``, ``hold_kill_level`` and ``mark_started`` are the exceptions, and the protocol REQUIRES them to be
memory-only on every backend (no driver, no ledger, no wait on anything that does I/O, and never the lock that serialises
the kill-level writes): the async callers take them on the event loop, with no thread hop and no slot, so that a database
that has gone silent, and the slots its stuck calls hold, can neither delay a kill-level read, an emergency kill, nor cost
a stream its registration. Both backends keep them to a lock held only for memory operations.

What the bound is. Measured on 2026-10-06 (Windows; neo4j driver 6.2.0, and 6.3.0 as a spot check; Neo4j Community
2026.07.1; 20 runs per cell; the production settings: a 1 s operation budget and a 0.5 s configured acquisition
timeout, which gives 0.47 s per connection attempt):

- A DEAD server (a closed port, a listener that accepts and never answers, a listener that is never accepted from):
  every connection attempt is cut at 0.47 s. An auto-commit read (``cache_get``, the kill-level read) is one attempt:
  median 0.47 s, worst 0.49 s. A managed transaction (reserve, renew, sweep, snapshot, settle) always makes two, because
  the driver starts its retry timer after the first failed attempt and checks it only after the second, with the 0.05 s
  first retry delay between them: median 1.00 s, worst 1.03 s. Before the fix: 0.50 s and 1.8-2.2 s (the driver's own
  1 s retry delay between two 0.5 s attempts). On Linux a closed port is refused at once and fails faster still.
- A LOCK held by another transaction: the server cuts the transaction at its 1 s timeout, but only when its transaction
  monitor next looks, every ``db.transaction.monitor.check.interval``. ``deploy/neo4j/fly.toml`` sets 100 ms for the
  semigraph-neo4j app (the default is 2 s): median 1.04 s, worst 1.09 s (the cut is the non-retryable
  ``Neo.ClientError.Transaction.LockClientStopped``, so there is no second attempt). With the default interval it was
  1.99 s median and 2.01 s worst. The margin to 1.1 s is about 15 ms: a loaded server can exceed it. The server timeout
  could be asked for as ``state_op_timeout_s`` minus one monitor period to widen it; that couples this code to the
  server's setting and is not done.
- A POOLED connection whose server goes silent (a frozen machine, a lost route; nothing is closed): once the handshake
  is done the driver reads with no deadline of ours, only the server's receive-timeout hint (120 s). Measured through a
  forwarder that stops moving bytes: WITHOUT a liveness check the first operation on such a connection was still
  blocked after 20 s (until the hint); WITH ``liveness_check_timeout=0`` (``make_state_driver``) each acquire proves the
  connection alive inside the attempt timeout and drops it: a read fails in 0.47 s, a managed transaction in 1.0 s.
- NOT covered: an operation that is already IN FLIGHT when the server goes silent. Measured: a reserve blocked on a
  held lock, the forwarder frozen 0.3 s into it, was still blocked after 25 s (the wait was not run longer). The socket
  carries a 120 s read timeout from the server's hint, so it should end then, or sooner if the connection is reset.
  Such an operation holds a state-limiter slot (4 on live) and its caller, with no caller-side timeout by design, for
  that long. The driver has no read-timeout setting of its own; shortening the server's hint is a server setting that
  was not measured or changed here.
- A failed SETTLE is not waited for at all: ``reconcile`` returns after its first attempt (see :mod:`.settle_queue`).
  Before, it slept 2 s three times while holding a state-limiter slot (about 14 s per reconcile during an outage).

Every case in the first three bullets fails closed in a measured 1.0-1.1 s; the in-flight case does not. A caller must
not size a queue on a flat 1 s.

``store`` (the answer cache and the policy flag) is imported inside the methods that use it: ``store`` pulls in the
answerer, and a later increment may make ``store`` import this package.
"""

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, NamedTuple, Protocol

from neo4j import Query
from neo4j.exceptions import DriverError, Neo4jError

from .settle_queue import DRAIN_PER_TICK, QUEUE_MAX, PendingSettle, SettleQueue

logger = logging.getLogger("semigraph.serve.state")

MICRO = 1_000_000
SLOW_OP_S = 1.0                    # a state call slower than this is logged `state_slow`
BOOT_TIMEOUT_S = 15.0              # server-side transaction timeout of the boot rebuild (not on the serving path)
KILL_POLICY_KEY = "kill_switch"
KILL_OFF, KILL_RETRIEVAL_ONLY, KILL_ON = "off", "retrieval_only", "on"
KILL_LEVELS = (KILL_OFF, KILL_RETRIEVAL_ONLY, KILL_ON)      # least to most restrictive
# what a state call may fail with (a TimeoutError is an OSError)
_DRIVER_ERRORS = (Neo4jError, DriverError, OSError)


class Denied(Enum):
    KILL = "kill"
    DAILY_COUNT = "daily_count"
    DAILY_SPEND = "daily_spend"
    IP_DAILY = "ip_daily"
    INFLIGHT = "inflight"
    UNAVAILABLE = "unavailable"


class Lease(NamedTuple):
    lease_id: str
    day: str
    ip_hash: str
    strategy: str
    workspace: bool
    estimate_micro: int
    until_mono: float
    machine_id: str


class StateUnavailable(RuntimeError):
    """The state store failed or was too slow: the caller fails closed (a paid ask is refused, never let through)."""


class KillNotStored(StateUnavailable):
    """``set_kill_level``: the database write failed. ``held`` says whether the level nonetheless holds on this machine
    and its write is queued for the maintenance thread (a tightening, or the same level again): then the caller answers
    that it is in force and will be stored. When it is False nothing changed and nothing is queued (a relaxation)."""

    def __init__(self, message: str, *, held: bool):
        super().__init__(message)
        self.held = held


def usd_to_micro(usd: float | int | Decimal) -> int:
    """Whole micro-dollars for a price in dollars: round HALF UP on the micro (0.0000005 -> 1, 0.0000004 -> 0).

    A float is read through its shortest decimal form (``0.06`` is 60000, not 59999.99999999999). Negative, NaN and
    infinite prices raise ValueError. An unknown cost has no micro value: callers keep the lease's ESTIMATE instead."""
    if isinstance(usd, bool) or not isinstance(usd, int | float | Decimal):
        raise TypeError(f"a price in dollars must be a number, got {type(usd).__name__}")
    value = Decimal(repr(float(usd))) if isinstance(usd, float) else Decimal(usd)
    if not value.is_finite() or value < 0:
        raise ValueError(f"a price must be a finite number of dollars >= 0, got {usd!r}")
    return int((value * MICRO).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def micro_to_usd(micro: int) -> float:
    """Dollars for whole micro-dollars (exact to the micro: six decimals)."""
    return round(micro / MICRO, 6)


def day_of(epoch_s: float) -> str:
    """The UTC calendar day (``YYYY-MM-DD``) of an epoch second: what the ledger keys a day by."""
    return datetime.fromtimestamp(epoch_s, UTC).date().isoformat()


@dataclass(frozen=True)
class RebuildReport:
    """What the boot rebuild found. ``ip_events`` carries the timestamps for the paid per-IP rate-limiter windows: the
    wiring code seeds ``RateLimiter`` from :meth:`window_events` (guard.py has no seed method; this package never
    edits it)."""

    day: str
    paid: int                                   # paid asks today (settled rows plus live reserved ones)
    spend_micro: int                            # their spend (a reserved row counts its estimate)
    per_ip: Mapping[str, int]
    expired: int                                # reserved rows closed as ``abandoned_restart``
    foreign_leases: int                         # live leases of OTHER machines found (they were left alone)
    counters_synced: bool | None                # neo4j backend: the counters were set to the sums (None: n/a)
    delta: Mapping[str, int] | None             # neo4j backend, not synced: sums minus the stored counter
    ip_events: tuple[tuple[str, float], ...]    # (ip_hash, wall-clock time) of today's rows that carry ip_hash_v
    now_wall: float
    now_mono: float

    def window_events(self, window_s: float) -> dict[str, list[float]]:
        """``{ip_hash: [monotonic times]}`` of the events inside the last ``window_s`` seconds, in the clock
        ``RateLimiter`` uses (``time.monotonic``), ready for a seed."""
        events: dict[str, list[float]] = {}
        for ip_hash, ts in self.ip_events:
            age = max(0.0, self.now_wall - ts)
            if age <= window_s:
                events.setdefault(ip_hash, []).append(self.now_mono - age)
        return {ip: sorted(times) for ip, times in events.items()}


class StateBackend(Protocol):
    def reserve(self, *, ip_hash: str, strategy: str, workspace: bool, estimate_micro: int,
                now_wall: float, now_mono: float) -> Lease | Denied: ...

    def mark_started(self, lease_id: str) -> None: ...        # registry entry the maintenance thread renews; MEMORY ONLY

    def renew(self, lease_id: str, now_wall: float) -> bool: ...                       # writes lease_until on the row

    def reconcile(self, lease_id: str, *, outcome: str, usage: dict | None, cost_micro: int | None) -> bool: ...

    def sweep(self, now: float) -> int: ...                   # unregistered, expired leases: charge the estimate

    def rebuild_from_ledger(self) -> RebuildReport: ...

    def kill_level(self) -> str: ...                          # 'on' | 'retrieval_only' | 'off'; MEMORY ONLY

    # MEMORY ONLY (event loop, no worker thread, no limiter token): apply a tightening, queue its write; True if it holds.
    # The admin route calls it before it waits for the admin limiter, when the backend has it (a double need not).
    def hold_kill_level(self, level: str) -> bool: ...

    def set_kill_level(self, level: str) -> None: ...    # a failed write raises KillNotStored (a StateUnavailable)

    def cache_get(self, key: str, ttl_hours: int) -> dict | None: ...                  # raises StateUnavailable

    def cache_put(self, **kw: Any) -> None: ...                                        # best effort

    def snapshot(self) -> dict: ...


def _need(settings: Any, name: str) -> Any:
    try:
        return getattr(settings, name)
    except AttributeError:
        raise ValueError(f"the state backend needs settings.{name}") from None


@dataclass(frozen=True)
class StateConfig:
    """The settings the state package reads, validated once. Caps of 0 mean off, EXCEPT ``max_concurrent_answers`` where
    0 means nothing is allowed."""

    max_queries_per_day: int
    max_spend_micro: int
    paid_per_ip_per_day: int
    max_concurrent_answers: int
    kill_switch: bool
    kill_switch_refresh_s: float
    kill_switch_stale_s: float
    state_op_timeout_s: float
    lease_ttl_s: float
    lease_renew_s: float
    machine_id: str
    ip_hash_version: int | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> "StateConfig":
        cfg = cls(
            max_queries_per_day=int(_need(settings, "max_queries_per_day")),
            max_spend_micro=usd_to_micro(_need(settings, "max_spend_usd_per_day")),
            paid_per_ip_per_day=int(_need(settings, "paid_per_ip_per_day")),
            max_concurrent_answers=int(_need(settings, "max_concurrent_answers")),
            kill_switch=bool(_need(settings, "kill_switch")),
            kill_switch_refresh_s=float(_need(settings, "kill_switch_refresh_s")),
            kill_switch_stale_s=float(_need(settings, "kill_switch_stale_s")),
            state_op_timeout_s=float(_need(settings, "state_op_timeout_s")),
            lease_ttl_s=float(_need(settings, "lease_ttl_s")),
            lease_renew_s=float(_need(settings, "lease_renew_s")),
            machine_id=str(_need(settings, "machine_id")),
            ip_hash_version=getattr(settings, "ip_hash_version", None),   # absent before the pepper cutover
        )
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        for name in ("max_queries_per_day", "max_spend_micro", "paid_per_ip_per_day", "max_concurrent_answers"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be 0 or more, got {getattr(self, name)}")
        for name in ("kill_switch_refresh_s", "kill_switch_stale_s", "state_op_timeout_s", "lease_ttl_s",
                     "lease_renew_s"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive, got {getattr(self, name)}")
        if not self.machine_id:
            raise ValueError("machine_id must not be empty")


class StateDrivers(NamedTuple):
    """The drivers the backend uses. ``state`` is the bounded state driver (``graph.client.make_state_driver``)."""

    state: Any


class LeaseRegistry:
    """Leases whose stream has started: the maintenance thread renews exactly these, and a sweep leaves them alone."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: set[str] = set()

    def add(self, lease_id: str) -> None:
        with self._lock:
            self._ids.add(lease_id)

    def discard(self, lease_id: str) -> None:
        with self._lock:
            self._ids.discard(lease_id)

    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._ids)

    def __contains__(self, lease_id: object) -> bool:
        with self._lock:
            return lease_id in self._ids

    def __len__(self) -> int:
        with self._lock:
            return len(self._ids)


class BoundedDriver:
    """A driver whose ``session().run`` carries a server-side transaction timeout.

    ``store.get_answer`` / ``put_answer`` / ``get_policy`` / ``set_policy`` run through ``run_cypher`` (auto-commit
    ``session.run`` with no timeout of their own). Wrapping the state driver here reuses them unchanged and still bounds
    each statement on the server (``neo4j.Query(timeout=...)``), next to the driver's acquisition and connect limits."""

    def __init__(self, driver: Any, timeout_s: float):
        self._driver, self._timeout_s = driver, timeout_s

    def session(self, **config: Any) -> "_BoundedSession":
        return _BoundedSession(self._driver.session(**config), self._timeout_s)


class _BoundedSession:
    def __init__(self, session: Any, timeout_s: float):
        self._session, self._timeout_s = session, timeout_s

    def run(self, query: Any, **params: Any) -> Any:
        bounded = Query(query, timeout=self._timeout_s) if isinstance(query, str) else query
        return self._session.run(bounded, **params)

    def __enter__(self) -> "_BoundedSession":
        self._session.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        return self._session.__exit__(*exc_info)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


def _kill_rank(level: str) -> int:
    return KILL_LEVELS.index(level)


class StateCore:
    """What the two backends share: configuration, clocks, the error and timing wrapper, the kill-level cache and the
    answer cache. Both backends add the counters (in memory, or in Neo4j) and the lease bookkeeping."""

    def __init__(self, config: StateConfig, drivers: StateDrivers, ledger: Any, *,
                 wall: Callable[[], float] = time.time, clock: Callable[[], float] = time.monotonic,
                 perf: Callable[[], float] = time.perf_counter, settle_queue_max: int = QUEUE_MAX):
        self._cfg, self._driver, self._ledger = config, drivers.state, ledger
        self._wall, self._clock, self._perf = wall, clock, perf
        self._store_driver = BoundedDriver(self._driver, config.state_op_timeout_s)
        self.registry = LeaseRegistry()
        self._settles = SettleQueue(self._attempt_settle, clock, max_size=settle_queue_max)
        self._kill_lock = threading.Lock()           # guards the cache below: the hot path (kill_level) takes only this
        self._writer_lock = threading.Lock()         # serialises the database writes of the level; never taken to read
        self._kill_level: str | None = None          # None: never read
        self._kill_read_at: float | None = None
        self._pending_kill: str | None = None        # a tightening applied in memory whose DB write failed
        self._kill_gen = 0                           # bumped when a set begins (its identity, see set_kill_level)
        self._kill_done = 0                          # bumped when a set has applied its level; see refresh_kill_level

    # ---- the error and timing wrapper -------------------------------------------------------------------------

    def _call(self, op: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        started = self._perf()
        try:
            return fn(*args, **kwargs)
        except _DRIVER_ERRORS as exc:
            # the class and the server's code only, never the message: a driver quotes what the statement carried (a
            # node's properties such as an address hash, or the text of a question)
            code = exc.code if isinstance(exc, Neo4jError) else None
            logger.warning("state_unavailable op=%s error=%s code=%s", op, type(exc).__name__, code)
            raise StateUnavailable(f"state operation {op} failed: {type(exc).__name__}") from exc
        finally:
            elapsed = self._perf() - started
            if elapsed > SLOW_OP_S:
                logger.warning("state_slow op=%s elapsed_ms=%d", op, round(elapsed * 1000))

    # ---- settles that failed: the retry queue -----------------------------------------------------------------

    def _settle_or_queue(self, op: str, lease_id: str, run: Callable[[], Any]) -> tuple[bool, Any]:
        """One attempt now; ``(True, result)``. If the store fails the settle goes to the retry queue and the call
        returns ``(False, None)`` at once: nothing here waits, so a limiter slot is never held through an outage."""
        try:
            return True, self._call(op, run)
        except StateUnavailable:
            self._settles.offer(op, lease_id, run)
            return False, None

    def _attempt_settle(self, entry: PendingSettle) -> bool:
        try:
            self._call(entry.op, entry.run)
        except StateUnavailable:
            return False
        return True

    def pending_settles(self) -> int:
        """How many settles wait for a retry."""
        return len(self._settles)

    def drain_settles(self, limit: int = DRAIN_PER_TICK) -> int:
        """Retry the queued settles, oldest first (the maintenance thread does this on every tick); returns how many
        finished. Stops at the first failure: the store is down. Safe to call from any thread."""
        return self._settles.drain(limit)

    def flush_settles(self, budget_s: float) -> int:
        """Shutdown: try each queued settle once, until ``budget_s`` seconds are spent or one fails (bounded by the
        budget plus one state operation). What is left is logged and stays for the next boot to charge."""
        return self._settles.flush(budget_s)

    # ---- the kill level ---------------------------------------------------------------------------------------

    def kill_level(self) -> str:
        """``on`` (paid asks off) | ``retrieval_only`` | ``off`` (normal). Read from a memory cache the maintenance
        thread refreshes: ``on`` when never read, when the cache is older than ``kill_switch_stale_s``, or when the
        ``KILL_SWITCH`` env override is set.

        The stored policy value is the ``SvcPolicy`` ``kill_switch`` node: ``on`` / ``retrieval_only`` / ``off``. A
        pre-M5 image reads only ``on`` as stopped (``store.kill_switch_on``), so it treats ``retrieval_only`` as
        ``off``: set ``on`` or ``off`` before rolling back to such an image (docs/v2/M5A_BUILD_PLAN.md section 1, I4
        rollback)."""
        if self._cfg.kill_switch:
            return KILL_ON
        with self._kill_lock:
            return self._level_locked()

    def _level_locked(self) -> str:
        """The cached level, or ``on`` when it was never read or is stale. The caller holds ``_kill_lock``."""
        level, read_at = self._kill_level, self._kill_read_at
        if level is None or read_at is None or self._clock() - read_at > self._cfg.kill_switch_stale_s:
            return KILL_ON
        return level

    def kill_age_s(self) -> float | None:
        with self._kill_lock:
            read_at = self._kill_read_at
        return None if read_at is None else round(self._clock() - read_at, 3)

    def refresh_kill_level(self) -> str:
        """Read the policy into the cache (the maintenance thread calls this every ``kill_switch_refresh_s``). A
        tightening whose database write failed is written first, and is not overwritten by an older stored value.
        Raises StateUnavailable when the database cannot be reached; the cache then simply ages towards ``on``.

        What the refresh read is applied only if no :meth:`set_kill_level` began or finished since the refresh did (the
        two counters): otherwise a refresh that read ``off`` just before an admin set ``on`` would write ``off`` back,
        and paid asks would reopen against a database that says ``on``. The same goes for a read that was taken before
        a relaxation was stored and lands after it was applied."""
        from .. import store

        with self._kill_lock:
            started = (self._kill_gen, self._kill_done)
        self._flush_pending_kill()
        stored = self._call("kill_read", store.get_policy, self._store_driver, KILL_POLICY_KEY)
        level = self._parse_policy(stored)
        with self._kill_lock:
            if self._pending_kill is None and (self._kill_gen, self._kill_done) == started:
                self._kill_level, self._kill_read_at = level, self._clock()
        return self.kill_level()

    @staticmethod
    def _parse_policy(stored: str | None) -> str:
        if stored is None:
            return KILL_OFF                             # never set
        if stored in KILL_LEVELS:
            return stored
        logger.error("unknown kill_switch policy value %r: failing closed (on)", stored)
        return KILL_ON

    def _flush_pending_kill(self) -> None:
        """Write a tightening whose database write failed earlier. It runs under the writer lock, so it cannot
        interleave with a set; a set that begins meanwhile supersedes it (the pending mark is then left to that set)."""
        from .. import store

        with self._kill_lock:
            if self._pending_kill is None:
                return                                      # the usual case: nothing to write, no lock taken
        with self._writer_lock:
            with self._kill_lock:
                pending, gen = self._pending_kill, self._kill_gen
            if pending is None:
                return
            self._call("kill_write", store.set_policy, self._store_driver, KILL_POLICY_KEY, pending)
            with self._kill_lock:
                if self._kill_gen == gen and self._pending_kill == pending:
                    self._pending_kill = None

    def _holds_locked(self, level: str) -> bool:
        """Whether ``level`` can be held in memory with its write queued, before any database write has succeeded: it is
        not ``off`` and it is at least as tight as the level the gates apply NOW (``_level_locked``: the cached level, or
        ``on`` when that is stale or was never read; the ``KILL_SWITCH`` env override is a separate layer and plays no part).
        The caller holds ``_kill_lock``.

        Two corners decide this. (1) Against the effective level, not the raw cached one: a cache that reads ``on`` only
        by age (or was never read) makes ``retrieval_only`` a relaxation of ``on``, which needs a stored write: held, it
        would lower the level the gates apply on a failed write and queue ``retrieval_only`` over a stored ``on``. A set
        of ``on`` against such a cache is not a relaxation, so it holds (the raw cached level may be ``off``: a set of
        ``on`` is a tightening of it, and a set equal to it is queued all the same). (2) ``off`` never holds, not even
        against a cache that reads ``off``: it relaxes whatever the database says, and a queued ``off`` would be written
        over a level another machine stored, with nobody having confirmed it."""
        return level != KILL_OFF and _kill_rank(level) >= _kill_rank(self._level_locked())

    def _apply_hold_locked(self, level: str) -> None:
        """Apply a level :meth:`_holds_locked` accepted, and queue its write. A level tighter than the raw cached one
        becomes the cached level (read just now); the same level only gets its write queued. The caller holds
        ``_kill_lock``."""
        if _kill_rank(level) > _kill_rank(self._kill_level or KILL_OFF):
            self._kill_level, self._kill_read_at = level, self._clock()
        self._pending_kill = level

    def hold_kill_level(self, level: str) -> bool:
        """MEMORY ONLY: apply ``level`` now if it can be held (see :meth:`_holds_locked`) and queue its database write
        for the maintenance thread; True when it holds, False (and nothing changed) when it would relax. It takes only
        the memory lock, never the lock that serialises the database writes, so an emergency kill is in force at once
        even while an earlier write sits on a silent connection (up to 120 s). It does not write anything: the caller
        goes on to :meth:`set_kill_level` for the write. A held level takes a generation number, like a set, so a refresh
        that read before it cannot undo it."""
        if level not in KILL_LEVELS:
            raise ValueError(f"kill level must be one of {KILL_LEVELS}, got {level!r}")
        with self._kill_lock:
            if not self._holds_locked(level):
                return False
            self._kill_gen += 1
            self._apply_hold_locked(level)
            return True

    def set_kill_level(self, level: str) -> None:
        """Set the level. A level that holds (:meth:`_holds_locked`: a tightening, or the same level again) is applied in
        memory first, so it holds even if the database write then fails: that raises :class:`KillNotStored` with
        ``held`` True, and the maintenance thread retries the write. A relaxing level is written first and applied only
        once stored: an unstored relaxation must never open the gate (``KillNotStored``, ``held`` False, nothing
        changed, nothing queued).

        A failed write never lowers the level the gates apply, whatever the cache's age: what holds is judged against
        that level (which reads ``on`` for a cache that is stale or was never read), and what does not hold is not
        applied. Whether a set tightens the CACHE is judged against the cached level as it was last read or set, however
        old it is (never read counts as ``off``): judged against :meth:`kill_level` instead, a set of ``on`` during an
        outage would look like 'already on', nothing would be queued, and the refresh after the recovery would read the
        stored ``off`` back and reopen paid asks.

        Every set takes a generation number and decides, in the same critical section, whether it holds. The database
        writes are serialised, and a set that a later one has superseded by the time its turn comes skips its write: the
        later set owns the level, in memory and in the database. Two concurrent sets therefore cannot leave the two
        disagreeing."""
        from .. import store

        if level not in KILL_LEVELS:
            raise ValueError(f"kill level must be one of {KILL_LEVELS}, got {level!r}")
        with self._kill_lock:
            self._kill_gen += 1
            gen = self._kill_gen
            held = self._holds_locked(level)
            if held:
                self._apply_hold_locked(level)
        with self._writer_lock:
            with self._kill_lock:
                if self._kill_gen != gen:
                    return
            try:
                self._call("kill_write", store.set_policy, self._store_driver, KILL_POLICY_KEY, level)
            except StateUnavailable as exc:
                raise KillNotStored(str(exc), held=held) from exc
            with self._kill_lock:
                if self._kill_gen == gen:
                    self._kill_level, self._kill_read_at, self._pending_kill = level, self._clock(), None
                    self._kill_done += 1

    def _forget_kill_level(self) -> None:
        with self._kill_lock:
            self._kill_level, self._kill_read_at, self._pending_kill = None, None, None

    # ---- the answer cache -------------------------------------------------------------------------------------

    def cache_get(self, key: str, ttl_hours: int) -> dict | None:
        """``store.get_answer`` under the state bound. Raises StateUnavailable on any driver error (the caller fails
        closed: a cached answer is never replaced by a paid call because the cache could not be read)."""
        from .. import store

        return self._call("cache_get", store.get_answer, self._store_driver, key, ttl_hours)

    def cache_put(self, **kw: Any) -> None:
        """``store.put_answer`` under the state bound; best effort (a failure only means the answer is not cached)."""
        from .. import store

        try:
            self._call("cache_put", store.put_answer, self._store_driver, **kw)
        except Exception as exc:  # noqa: BLE001 - never raise into the finalize path
            logger.warning("state cache_put failed (%s): the answer is not cached", type(exc).__name__)

    # ---- helpers for the subclasses ---------------------------------------------------------------------------

    @staticmethod
    def _check_estimate(estimate_micro: int) -> int:
        if type(estimate_micro) is not int or estimate_micro < 0:
            raise ValueError(f"estimate_micro must be a whole number of micro-dollars >= 0, got {estimate_micro!r}")
        return estimate_micro

    @staticmethod
    def _charge_micro(outcome: str, cost_micro: int | None) -> int | None:
        """What a settled ask is charged, or None for 'the estimate': an unknown cost and an abandoned ask both keep
        the estimate; a known cost is floored at zero."""
        if outcome == "abandoned" or cost_micro is None:
            return None
        return max(0, int(cost_micro))

    def _actual_micro(self, estimate_micro: int, outcome: str, cost_micro: int | None) -> int:
        charge = self._charge_micro(outcome, cost_micro)
        return estimate_micro if charge is None else charge

    def _boot_expire(self, now_wall: float) -> int:
        """Boot step 1 (both backends): close this machine's reserved rows and the long-expired ones, charged their
        estimate. The grace is twice the renewal period: a live lease is renewed at least that often."""
        return self._call("boot_expire", self._ledger.expire_reserved, self._driver, now_wall=now_wall,
                          grace_s=2 * self._cfg.lease_renew_s, machine_id=self._cfg.machine_id,
                          timeout_s=BOOT_TIMEOUT_S)

    def _boot_report(self, *, day: str, now_wall: float, now_mono: float, expired: int, sums: Any,
                     synced: bool | None, delta: Mapping[str, int] | None) -> RebuildReport:
        """Boot steps 2-4 ended: the report, with the kill cache forgotten (serving starts closed until the first
        read) and one log line."""
        self._forget_kill_level()
        report = RebuildReport(day=day, paid=sums.paid, spend_micro=sums.spend_micro, per_ip=dict(sums.per_ip),
                               expired=expired, foreign_leases=sums.foreign_leases, counters_synced=synced,
                               delta=delta, ip_events=tuple(sums.ip_events), now_wall=now_wall, now_mono=now_mono)
        logger.info("state_rebuilt day=%s paid=%d spend_micro=%d ips=%d expired=%d foreign=%d synced=%s delta=%s",
                    day, report.paid, report.spend_micro, len(report.per_ip), expired, report.foreign_leases, synced,
                    dict(delta) if delta else None)
        return report


def make_backend(settings: Any, drivers: StateDrivers, **kwargs: Any) -> StateBackend:
    """The backend ``settings.state_backend`` names: ``inprocess`` (default; counters in memory, durable rows in Neo4j)
    or ``neo4j`` (counters in Neo4j too: the rollback, and any multi-machine setup). ``kwargs`` (clocks, ``ledger``)
    are for tests."""
    kind = getattr(settings, "state_backend", "inprocess")
    config = StateConfig.from_settings(settings)
    if kind == "inprocess":
        from .inprocess import InProcessBackend

        return InProcessBackend(config, drivers, **kwargs)
    if kind == "neo4j":
        from .neo4j import Neo4jBackend

        return Neo4jBackend(config, drivers, **kwargs)
    raise ValueError(f"unknown state_backend {kind!r}: expected 'inprocess' or 'neo4j'")
