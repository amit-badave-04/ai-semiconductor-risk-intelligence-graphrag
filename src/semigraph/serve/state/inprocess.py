"""The in-process backend (the live default; docs/v2/M5A_BUILD_PLAN.md section 3).

The counters (paid asks, spend, per-IP asks, leases in flight) are memory under ONE ``threading.Lock``, keyed by UTC
day; the durable ledger row of every ask is written to Neo4j. That is correct on one machine, which is what live is, and
it makes the admission check a memory operation: Neo4j is touched once per ask to write a row, never to decide.

``reserve`` mutates the counters and inserts the lease under the lock, releases the lock, and only then writes the row
(a synchronous call the CALLER hops to a worker thread, under the state limiter). A failed row write rolls the counters
back under the lock and denies as UNAVAILABLE, so a lease that was never durable can never exist. The one gap: a write
that timed out but did commit leaves a ``reserved`` row no lease owns; the next boot closes it (``abandoned_restart``)
and charges its estimate.

A day's counters are kept while any lease references the day: a stream that started at 23:59 holds its slot and settles
into day D, while day D+1 starts at zero.
"""

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from .backend import (
    BOOT_TIMEOUT_S,
    KILL_OFF,
    Denied,
    Lease,
    RebuildReport,
    StateConfig,
    StateCore,
    StateDrivers,
    StateUnavailable,
    day_of,
)

logger = logging.getLogger("semigraph.serve.state")


@dataclass
class _DayCounters:
    paid: int = 0
    spend_micro: int = 0
    per_ip: dict[str, int] = field(default_factory=dict)


@dataclass
class _Entry:
    lease: Lease
    until_wall: float               # wall-clock expiry: the clock ``sweep(now)`` and the durable row use


class InProcessBackend(StateCore):
    def __init__(self, config: StateConfig, drivers: StateDrivers, *, ledger: Any = None, **clocks: Any):
        if ledger is None:
            from . import ledger as default_ledger
            ledger = default_ledger
        super().__init__(config, drivers, ledger, **clocks)
        self._lock = threading.Lock()
        self._counters: dict[str, _DayCounters] = {}
        self._leases: dict[str, _Entry] = {}

    # ---- reserve ----------------------------------------------------------------------------------------------

    def reserve(self, *, ip_hash: str, strategy: str, workspace: bool, estimate_micro: int, now_wall: float,
                now_mono: float) -> Lease | Denied:
        self._check_estimate(estimate_micro)
        if self.kill_level() != KILL_OFF:
            return Denied.KILL
        cfg, day = self._cfg, day_of(now_wall)
        lease = Lease(str(uuid.uuid4()), day, ip_hash, strategy, workspace, estimate_micro,
                      now_mono + cfg.lease_ttl_s, cfg.machine_id)
        until_wall = now_wall + cfg.lease_ttl_s
        with self._lock:
            denial = self._admit(day, ip_hash, estimate_micro)
            if denial is not None:
                return denial
            counters = self._counters.setdefault(day, _DayCounters())
            counters.paid += 1
            counters.spend_micro += estimate_micro
            counters.per_ip[ip_hash] = counters.per_ip.get(ip_hash, 0) + 1
            self._leases[lease.lease_id] = _Entry(lease, until_wall)
        try:
            self._call("reserve_row", self._ledger.reserve_row, self._driver, lease_id=lease.lease_id, day=day,
                       ip_hash=ip_hash, ip_hash_v=cfg.ip_hash_version, strategy=strategy, workspace=workspace,
                       estimate_micro=estimate_micro, now_wall=now_wall, lease_until=until_wall,
                       machine_id=cfg.machine_id, timeout_s=cfg.state_op_timeout_s)
        except StateUnavailable:
            self._roll_back(lease)
            return Denied.UNAVAILABLE
        except BaseException:
            self._roll_back(lease)
            raise
        return lease

    def _admit(self, day: str, ip_hash: str, estimate_micro: int) -> Denied | None:
        """The caps, in the documented order. The caller holds the lock. 0 is off, except for concurrency."""
        cfg = self._cfg
        counters = self._counters.get(day) or _DayCounters()
        if cfg.max_queries_per_day and counters.paid + 1 > cfg.max_queries_per_day:
            return Denied.DAILY_COUNT
        if cfg.max_spend_micro and counters.spend_micro + estimate_micro > cfg.max_spend_micro:
            return Denied.DAILY_SPEND
        if cfg.paid_per_ip_per_day and counters.per_ip.get(ip_hash, 0) + 1 > cfg.paid_per_ip_per_day:
            return Denied.IP_DAILY
        if len(self._leases) >= cfg.max_concurrent_answers:
            return Denied.INFLIGHT
        return None

    def _roll_back(self, lease: Lease) -> None:
        with self._lock:
            if self._leases.pop(lease.lease_id, None) is None:
                return
            counters = self._counters[lease.day]
            counters.paid -= 1
            counters.spend_micro -= lease.estimate_micro
            remaining = counters.per_ip.get(lease.ip_hash, 1) - 1
            if remaining > 0:
                counters.per_ip[lease.ip_hash] = remaining
            else:
                counters.per_ip.pop(lease.ip_hash, None)
            self._prune_days()

    def _prune_days(self) -> None:
        """Drop the counters of every day before today that no lease references. The caller holds the lock."""
        referenced = {entry.lease.day for entry in self._leases.values()}
        today = day_of(self._wall())
        for day in [d for d in self._counters if d < today and d not in referenced]:
            del self._counters[day]

    # ---- the lease life cycle ---------------------------------------------------------------------------------

    def mark_started(self, lease_id: str) -> None:
        with self._lock:
            if lease_id in self._leases:
                self.registry.add(lease_id)

    def renew(self, lease_id: str, now_wall: float) -> bool:
        until_wall = now_wall + self._cfg.lease_ttl_s
        with self._lock:
            entry = self._leases.get(lease_id)
            if entry is None:
                return False
            entry.until_wall = until_wall
        return bool(self._call("renew_row", self._ledger.renew_row, self._driver, lease_id, lease_until=until_wall,
                               timeout_s=self._cfg.state_op_timeout_s))

    def reconcile(self, lease_id: str, *, outcome: str, usage: dict | None, cost_micro: int | None) -> bool:
        """Charge the lease its actual cost (the estimate when unknown or abandoned) and settle its row. The lease is
        popped under the lock first, so a double reconcile, or a reconcile racing a sweep, charges exactly once. The
        counters are charged and the slot is free when this returns, even if the row write failed: that write is then
        queued, retried by the maintenance thread, and charged at its estimate by the next boot if it never lands (see
        :mod:`.settle_queue`). Nothing here waits for the database to recover. True iff the counters were charged."""
        with self._lock:
            entry = self._leases.pop(lease_id, None)
            if entry is None:
                return False
            self.registry.discard(lease_id)
            lease = entry.lease
            actual = self._actual_micro(lease.estimate_micro, outcome, cost_micro)
            counters = self._counters.get(lease.day)
            if counters is not None:
                counters.spend_micro = max(0, counters.spend_micro + actual - lease.estimate_micro)
            self._prune_days()
        self._settle_row(lease, outcome, usage, actual)
        return True

    def _settle_row(self, lease: Lease, outcome: str, usage: dict | None, actual_micro: int) -> None:
        self._settle_or_queue("settle_row", lease.lease_id, lambda: self._ledger.settle_row(
            self._driver, lease.lease_id, outcome=outcome, usage=usage, actual_micro=actual_micro,
            now_wall=self._wall(), timeout_s=self._cfg.state_op_timeout_s))

    def sweep(self, now: float) -> int:
        """Charge the estimate of every lease that expired (wall clock ``now``) and was never started: the stream never
        began or its task died. Started leases are renewed by the maintenance thread and left alone. Returns how
        many."""
        with self._lock:
            expired = [lid for lid, e in self._leases.items() if e.until_wall < now and lid not in self.registry]
            leases = [self._leases.pop(lid).lease for lid in expired]
            self._prune_days()
        for lease in leases:
            self._settle_row(lease, "abandoned", None, lease.estimate_micro)
        return len(leases)

    # ---- rebuild and snapshot ---------------------------------------------------------------------------------

    def rebuild_from_ledger(self) -> RebuildReport:
        """Boot only: close what a dead process left reserved, then rebuild today's counters from the ledger. Raises
        StateUnavailable when the database cannot be reached (the caller refuses to serve paid asks)."""
        with self._lock:
            if self._leases:
                raise RuntimeError("rebuild_from_ledger is a boot operation: leases are in flight")
        cfg = self._cfg
        now_wall, now_mono = self._wall(), self._clock()
        day = day_of(now_wall)
        expired = self._boot_expire(now_wall)
        sums = self._call("day_sums", self._ledger.day_sums, self._driver, day=day, now_wall=now_wall,
                          machine_id=cfg.machine_id, ip_hash_v=cfg.ip_hash_version, timeout_s=BOOT_TIMEOUT_S)
        with self._lock:
            self._counters = {day: _DayCounters(sums.paid, sums.spend_micro, dict(sums.per_ip))}
        return self._boot_report(day=day, now_wall=now_wall, now_mono=now_mono, expired=expired, sums=sums,
                                 synced=None, delta=None)

    def snapshot(self) -> dict:
        day = day_of(self._wall())
        with self._lock:
            counters = self._counters.get(day) or _DayCounters()
            leases = [{"lease_id": e.lease.lease_id, "day": e.lease.day, "strategy": e.lease.strategy,
                       "workspace": e.lease.workspace, "estimate_micro": e.lease.estimate_micro,
                       "machine_id": e.lease.machine_id, "lease_until": e.until_wall,
                       "started": e.lease.lease_id in self.registry} for e in self._leases.values()]
            paid, spend, per_ip_max = counters.paid, counters.spend_micro, max(counters.per_ip.values(), default=0)
        return {"backend": "inprocess", "day": day, "paid": paid, "spend_micro": spend, "inflight": len(leases),
                "per_ip_max": per_ip_max, "kill": self.kill_level(), "kill_age_s": self.kill_age_s(),
                "leases": sorted(leases, key=lambda lease: lease["lease_id"])}
