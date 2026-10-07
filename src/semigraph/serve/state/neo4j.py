"""The neo4j backend: the counters live in Neo4j, in the same transaction as the ledger row (docs/v2/M5A_BUILD_PLAN.md
section 3). It is the rollback for the in-process backend and the only choice when more than one machine shares a
database: every cap is applied inside ONE write transaction that holds the day counter's lock, so two machines cannot
both take the last slot.

The process keeps no lease table: the ledger is the truth. What it does keep is the registry of leases whose stream has
started (``mark_started``), which the maintenance thread renews and the sweep skips, and the queue of settles that are
waiting for a retry (also skipped by the sweep). All Cypher is in :mod:`.ledger`.

``reserve`` is one managed transaction (counters, in-flight, caps, increments and the ``reserved`` row together), so a
failure leaves nothing to roll back. ``reconcile`` settles the row and adjusts its day's counter in one transaction, and
a second call matches nothing. ``sweep`` closes this machine's expired, unregistered leases (their estimate is already
in the counters). A lease of another machine is never touched here: its owner renews or sweeps it, and a dead machine's
are closed by the next boot (``abandoned_restart``).
"""

import logging
import uuid
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

_DENIALS = {"daily_count": Denied.DAILY_COUNT, "daily_spend": Denied.DAILY_SPEND, "ip_daily": Denied.IP_DAILY,
            "inflight": Denied.INFLIGHT}


class Neo4jBackend(StateCore):
    def __init__(self, config: StateConfig, drivers: StateDrivers, *, ledger: Any = None, **clocks: Any):
        if ledger is None:
            from . import ledger as default_ledger
            ledger = default_ledger
        super().__init__(config, drivers, ledger, **clocks)
        self._caps = ledger.Caps(max_count=config.max_queries_per_day, max_spend_micro=config.max_spend_micro,
                                 max_per_ip=config.paid_per_ip_per_day, max_inflight=config.max_concurrent_answers)

    def reserve(self, *, ip_hash: str, strategy: str, workspace: bool, estimate_micro: int, now_wall: float,
                now_mono: float) -> Lease | Denied:
        self._check_estimate(estimate_micro)
        if self.kill_level() != KILL_OFF:
            return Denied.KILL
        cfg, day = self._cfg, day_of(now_wall)
        lease_id = str(uuid.uuid4())
        try:
            outcome = self._call("reserve", self._ledger.reserve_counted, self._driver, lease_id=lease_id, day=day,
                                 ip_hash=ip_hash, ip_hash_v=cfg.ip_hash_version, strategy=strategy,
                                 workspace=workspace, estimate_micro=estimate_micro, now_wall=now_wall,
                                 lease_until=now_wall + cfg.lease_ttl_s, machine_id=cfg.machine_id, caps=self._caps,
                                 timeout_s=cfg.state_op_timeout_s)
        except StateUnavailable:
            return Denied.UNAVAILABLE
        if not outcome.granted:
            return _DENIALS.get(outcome.reason, Denied.UNAVAILABLE)
        return Lease(lease_id, day, ip_hash, strategy, workspace, estimate_micro, now_mono + cfg.lease_ttl_s,
                     cfg.machine_id)

    def mark_started(self, lease_id: str) -> None:
        """Memory only (the protocol requires it: the async caller takes it on the event loop)."""
        self.registry.add(lease_id)

    def renew(self, lease_id: str, now_wall: float) -> bool:
        return bool(self._call("renew", self._ledger.renew_row, self._driver, lease_id,
                               lease_until=now_wall + self._cfg.lease_ttl_s, timeout_s=self._cfg.state_op_timeout_s))

    def reconcile(self, lease_id: str, *, outcome: str, usage: dict | None, cost_micro: int | None) -> bool:
        """Settle the row and adjust its day counter by ``cost - estimate`` in one transaction; an unknown cost or an
        abandoned ask keeps the estimate. True iff this call charged. If the transaction fails nothing was charged (it
        rolled back): the call returns False at once and the settle is queued and retried by the maintenance thread
        (see :mod:`.settle_queue`); until it lands, the row stays reserved and counts in-flight, and the sweep leaves
        it alone. A settle that is given up is charged its estimate by the sweep once its lease runs out, or by the
        next boot."""
        self.registry.discard(lease_id)
        charge = self._charge_micro(outcome, cost_micro)
        done, charged = self._settle_or_queue("settle", lease_id, lambda: self._ledger.settle_counted(
            self._driver, lease_id, outcome=outcome, usage=usage, cost_micro=charge, now_wall=self._wall(),
            timeout_s=self._cfg.state_op_timeout_s))
        return bool(done and charged)

    def sweep(self, now: float) -> int:
        """Close this machine's expired leases that were never started, charging each its estimate (already in the
        counters, so only the row changes). Returns how many this call closed; a batch at a time."""
        ids = self._call("sweep_scan", self._ledger.expired_lease_ids, self._driver, machine_id=self._cfg.machine_id,
                         now_wall=now, skip=[*self.registry.active(), *self._settles.lease_ids()],
                         limit=self._ledger.SWEEP_BATCH,
                         timeout_s=self._cfg.state_op_timeout_s)
        closed = 0
        for lease_id in ids:
            if self._call("sweep_settle", self._ledger.settle_counted, self._driver, lease_id, outcome="abandoned",
                          usage=None, cost_micro=None, now_wall=now, timeout_s=self._cfg.state_op_timeout_s):
                closed += 1
        return closed

    def rebuild_from_ledger(self) -> RebuildReport:
        """Boot only: close what a dead process left reserved, then set the day counters to the ledger's sums in one
        transaction, unless another machine holds an unexpired lease (it has charged its own; the difference is
        logged)."""
        if len(self.registry):
            raise RuntimeError("rebuild_from_ledger is a boot operation: leases are in flight")
        cfg = self._cfg
        now_wall, now_mono = self._wall(), self._clock()
        day = day_of(now_wall)
        expired = self._boot_expire(now_wall)
        result = self._call("sync_counters", self._ledger.sync_day_counters, self._driver, day=day, now_wall=now_wall,
                            machine_id=cfg.machine_id, ip_hash_v=cfg.ip_hash_version, timeout_s=BOOT_TIMEOUT_S)
        if not result.synced:
            logger.warning("state_rebuild_delta day=%s foreign_leases=%d delta=%s: counters left as they are",
                           day, result.sums.foreign_leases, dict(result.delta or {}))
        return self._boot_report(day=day, now_wall=now_wall, now_mono=now_mono, expired=expired, sums=result.sums,
                                 synced=result.synced, delta=result.delta)

    def snapshot(self) -> dict:
        day = day_of(self._wall())
        counted = self._call("snapshot", self._ledger.snapshot_counted, self._driver, day=day,
                             now_wall=self._wall(), timeout_s=self._cfg.state_op_timeout_s)
        started = set(self.registry.active())
        leases = [{"lease_id": row["id"], "day": row["day"], "strategy": row["strategy"],
                   "workspace": row["workspace"], "estimate_micro": row["estimate_micro"],
                   "machine_id": row["machine_id"], "lease_until": row["lease_until"],
                   "started": row["id"] in started} for row in counted["leases"]]
        return {"backend": "neo4j", "day": day, "paid": counted["paid"], "spend_micro": counted["spend_micro"],
                "inflight": counted["inflight"], "per_ip_max": counted["per_ip_max"], "kill": self.kill_level(),
                "kill_age_s": self.kill_age_s(), "leases": leases}
