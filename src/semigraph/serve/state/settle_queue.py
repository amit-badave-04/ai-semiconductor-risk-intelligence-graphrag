"""The retry queue of failed settles (M5a I4a; docs/v2/M5_DECISIONS.md 2.2: "failed reconciles are retried, else charged
their estimate at boot").

A settle writes the final cost of an ask onto its ledger row. When that write fails, the caller must not wait for the
database: it would hold a state-limiter slot while the database is down, and a handful of those slots is all there is.
The first attempt is made inline; if it fails, the settle is queued here and the call returns. The maintenance thread
retries the queue on every tick (:meth:`SettleQueue.drain`), and a shutdown flushes what it can within a bound
(:meth:`SettleQueue.flush`).

An entry is given up after ``MAX_ATTEMPTS`` attempts or ``MAX_AGE_S`` seconds, with an ERROR line; the row then stays
``reserved`` and the next boot (or, on the neo4j backend, the sweep) charges its estimate. Nothing is dropped
silently: a refused (queue full) and an abandoned entry are both logged with the lease id.

One attempt is one call of ``run``. The caller's ``attempt`` function turns it into a yes/no: False when the store is
down, True when ``run`` returned. A settle that matches nothing (the row was already settled, or a sweep closed it
first) is finished, not failed: the statements are one-winner by design.
"""

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger("semigraph.serve.state")

QUEUE_MAX = 500            # entries; a full queue refuses a new entry (an ERROR line), it never evicts an old one
MAX_ATTEMPTS = 10          # the inline attempt plus nine retries ...
MAX_AGE_S = 120.0          # ... or this long after the first failure, whichever comes first
RETRY_INTERVAL_S = 2.0     # the maintenance thread comes back this soon while anything is queued
DRAIN_PER_TICK = 10        # entries one tick retries at most: a tick must leave time for the kill-level refresh


@dataclass(frozen=True)
class PendingSettle:
    op: str                         # the operation name the error wrapper logs
    lease_id: str
    run: Callable[[], Any]
    queued_at: float                # monotonic seconds of the first failure
    attempts: int = 1               # including the inline one


class SettleQueue:
    """``attempt(entry)`` makes one try and says whether the store answered (False: it is down); ``clock`` is
    monotonic."""

    def __init__(self, attempt: Callable[[PendingSettle], bool], clock: Callable[[], float], *,
                 max_size: int = QUEUE_MAX):
        self._attempt, self._clock, self._max_size = attempt, clock, max_size
        self._lock = threading.Lock()
        self._entries: deque[PendingSettle] = deque()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def lease_ids(self) -> list[str]:
        """The leases with a queued settle: a sweep must leave them to their settle."""
        with self._lock:
            return [entry.lease_id for entry in self._entries]

    def offer(self, op: str, lease_id: str, run: Callable[[], Any]) -> bool:
        """Queue a settle whose inline attempt failed. False, with an ERROR line, when the queue is full."""
        entry = PendingSettle(op, lease_id, run, queued_at=self._clock())
        with self._lock:
            accepted = len(self._entries) < self._max_size
            if accepted:
                self._entries.append(entry)
        if accepted:
            logger.warning("state_settle_queued lease=%s op=%s: the row write is retried by the maintenance thread",
                           lease_id, op)
        else:
            logger.error("state_settle_dropped lease=%s op=%s reason=queue_full: the row stays reserved and the next "
                         "boot charges its estimate", lease_id, op)
        return accepted

    def drain(self, limit: int = DRAIN_PER_TICK) -> int:
        """Retry up to ``limit`` entries, oldest first; returns how many finished. It stops at the first failed attempt:
        the store is down, and trying the rest would only cost one bounded operation each."""
        finished = 0
        for _ in range(limit):
            entry = self._take()
            if entry is None:
                break
            step = self._step(entry)
            if step == "failed":
                break
            finished += step == "done"
        return finished

    def flush(self, budget_s: float) -> int:
        """Shutdown: one attempt per entry, oldest first, until ``budget_s`` seconds are spent or one attempt fails.
        The call is bounded by the budget plus one state operation. Whatever is left is logged, not lost."""
        started, finished = self._clock(), 0
        while self._clock() - started < budget_s:
            entry = self._take()
            if entry is None:
                break
            step = self._step(entry)
            if step == "failed":
                break
            finished += step == "done"
        left = len(self)
        if left:
            logger.error("state_settle_unflushed count=%d: the rows stay reserved and the next boot charges their "
                         "estimate", left)
        return finished

    def _take(self) -> PendingSettle | None:
        with self._lock:
            return self._entries.popleft() if self._entries else None

    def _put_back(self, entry: PendingSettle) -> None:
        with self._lock:
            self._entries.appendleft(entry)

    def _step(self, entry: PendingSettle) -> str:
        """One entry: ``done`` (finished), ``dropped`` (given up) or ``failed`` (the store is down; it stays queued)."""
        if self._clock() - entry.queued_at > MAX_AGE_S:
            self._give_up(entry, "age")
            return "dropped"
        if self._attempt(entry):
            return "done"
        tried = replace(entry, attempts=entry.attempts + 1)
        if tried.attempts >= MAX_ATTEMPTS:
            self._give_up(tried, "attempts")
        else:
            self._put_back(tried)
        return "failed"

    def _give_up(self, entry: PendingSettle, reason: str) -> None:
        logger.error("state_settle_abandoned lease=%s op=%s reason=%s attempts=%d age_s=%d: the row stays reserved and "
                     "the next boot charges its estimate", entry.lease_id, entry.op, reason, entry.attempts,
                     round(self._clock() - entry.queued_at))
