"""The StateBackend contract and the code both backends share (M5a I4a; docs/v2/M5A_BUILD_PLAN.md section 3).

Money is an integer number of MICRO-dollars everywhere in this package, so a daily total can never drift by a float
rounding. :func:`usd_to_micro` is the one place a float price becomes micro-dollars (round half up on the micro).

Clocks: ``now_wall`` / ``sweep(now)`` / ``renew(now_wall)`` are wall-clock epoch seconds (the durable ``lease_until``
on a ledger row is wall clock so another machine can read it); ``now_mono`` and the kill-level staleness are monotonic.

Every backend method is a plain synchronous function. The async caller runs each as ``await anyio.to_thread.run_sync(fn,
limiter=limiters.state)`` with NO caller-side timeout: the bound lives in the work (the state driver's acquisition and
connect timeouts, and the server-side transaction timeout set by :mod:`.ledger`). Any driver error becomes
:class:`StateUnavailable`; a call that takes over one second is logged ``state_slow``.

What the bound really is. It is NOT 1 s. Measured on 2026-10-06 with the production settings (1 s operation timeout,
0.5 s pool acquisition, 1 s connect; neo4j driver 6.2.0 and 6.3.0, Neo4j Community 2026.07.1):

- the server enforces a transaction timeout when its transaction monitor next looks, every
  ``db.transaction.monitor.check.interval`` (2 s by default): an operation blocked on a held lock came back unavailable
  after up to 2.0 s with the default interval and after 1.06 s with it set to 100 ms;
- an unreachable server: an auto-commit read (``cache_get``, the kill-level read) gives up in about 0.5 s (the
  acquisition timeout). A managed transaction (reserve, renew, sweep, snapshot, settle) does not stop at
  ``max_transaction_retry_time``: the driver starts that timer after the FIRST failed attempt, so it always retries
  at least once, after about 1 s (then about 2 s more if the failures were instant). Against a listener that closes at
  once: 1.0-1.2 s half the time, 2.4-3.4 s the other half; against a black hole or a closed port on Windows 1.8-2.2 s.
  ``max_transaction_retry_time=0.2`` with ``initial_retry_delay=0.05`` measured 0.35-0.44 s and 1.05-1.07 s on the
  same targets (not applied here: the plan fixes ``max_transaction_retry_time = state_op_timeout_s``).

Both are bounded and both fail closed; neither is a hang. Callers must not size a queue on the 1 s figure.

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

logger = logging.getLogger("semigraph.serve.state")

MICRO = 1_000_000
SLOW_OP_S = 1.0                    # a state call slower than this is logged `state_slow`
BOOT_TIMEOUT_S = 15.0              # server-side transaction timeout of the boot rebuild (not on the serving path)
SETTLE_RETRIES = 3                 # extra attempts after a failed settle of a ledger row ...
SETTLE_RETRY_DELAY_S = 2.0         # ... this far apart; then the boot charges the estimate
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

    def mark_started(self, lease_id: str) -> None: ...        # registry entry: the maintenance thread renews it

    def renew(self, lease_id: str, now_wall: float) -> bool: ...                       # writes lease_until on the row

    def reconcile(self, lease_id: str, *, outcome: str, usage: dict | None, cost_micro: int | None) -> bool: ...

    def sweep(self, now: float) -> int: ...                   # unregistered, expired leases: charge the estimate

    def rebuild_from_ledger(self) -> RebuildReport: ...

    def kill_level(self) -> str: ...                          # 'on' | 'retrieval_only' | 'off'

    def set_kill_level(self, level: str) -> None: ...

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
                 perf: Callable[[], float] = time.perf_counter, sleep: Callable[[float], None] = time.sleep):
        self._cfg, self._driver, self._ledger = config, drivers.state, ledger
        self._wall, self._clock, self._perf, self._sleep = wall, clock, perf, sleep
        self._store_driver = BoundedDriver(self._driver, config.state_op_timeout_s)
        self.registry = LeaseRegistry()
        self._kill_lock = threading.Lock()
        self._kill_level: str | None = None          # None: never read
        self._kill_read_at: float | None = None
        self._pending_kill: str | None = None        # a tightening applied in memory whose DB write failed

    # ---- the error and timing wrapper -------------------------------------------------------------------------

    def _call(self, op: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        started = self._perf()
        try:
            return fn(*args, **kwargs)
        except _DRIVER_ERRORS as exc:
            # a server error is logged by its code: its message can quote the properties of a node (an address hash)
            detail = exc.code if isinstance(exc, Neo4jError) else str(exc)
            logger.warning("state_unavailable op=%s error=%s: %.200s", op, type(exc).__name__, detail)
            raise StateUnavailable(f"state operation {op} failed: {type(exc).__name__}") from exc
        finally:
            elapsed = self._perf() - started
            if elapsed > SLOW_OP_S:
                logger.warning("state_slow op=%s elapsed_ms=%d", op, round(elapsed * 1000))

    def _retry_write(self, op: str, fn: Callable[[], Any]) -> tuple[bool, Any]:
        """``fn`` with up to SETTLE_RETRIES retries SETTLE_RETRY_DELAY_S apart; ``(done, result)``."""
        for attempt in range(1 + SETTLE_RETRIES):
            try:
                return True, self._call(op, fn)
            except StateUnavailable:
                if attempt < SETTLE_RETRIES:
                    self._sleep(SETTLE_RETRY_DELAY_S)
        return False, None

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
        Raises StateUnavailable when the database cannot be reached; the cache then simply ages towards ``on``."""
        from .. import store

        with self._kill_lock:
            pending = self._pending_kill
        if pending is not None:
            self._persist_kill(pending)
        stored = self._call("kill_read", store.get_policy, self._store_driver, KILL_POLICY_KEY)
        level = self._parse_policy(stored)
        with self._kill_lock:
            if self._pending_kill is None:
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

    def _persist_kill(self, level: str) -> None:
        from .. import store

        self._call("kill_write", store.set_policy, self._store_driver, KILL_POLICY_KEY, level)
        with self._kill_lock:
            if self._pending_kill == level:
                self._pending_kill = None

    def set_kill_level(self, level: str) -> None:
        """Set the level. A TIGHTENING is applied in memory first, so it holds even if the database write then fails (it
        raises StateUnavailable, and the maintenance thread retries the write). A relaxing level is written first and
        applied only once stored: an unstored relaxation must never open the gate."""
        if level not in KILL_LEVELS:
            raise ValueError(f"kill level must be one of {KILL_LEVELS}, got {level!r}")
        if _kill_rank(level) > _kill_rank(self._cached_level()):
            with self._kill_lock:
                self._kill_level, self._kill_read_at, self._pending_kill = level, self._clock(), level
            self._persist_kill(level)
            return
        self._persist_kill(level)
        with self._kill_lock:
            self._kill_level, self._kill_read_at, self._pending_kill = level, self._clock(), None

    def _cached_level(self) -> str:
        with self._kill_lock:
            level, read_at = self._kill_level, self._kill_read_at
        if level is None or read_at is None or self._clock() - read_at > self._cfg.kill_switch_stale_s:
            return KILL_ON
        return level

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

    def _log_settle_failed(self, lease_id: str) -> None:
        logger.error("state_settle_failed lease=%s: the row stays reserved and the next boot charges its estimate",
                     lease_id)


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
