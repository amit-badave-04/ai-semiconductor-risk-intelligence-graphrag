"""Open-loop dispatch of an S7 level: the arrivals fire at their scheduled times whatever the database is doing (stdlib only).

A closed loop (the next ask waits for the previous one) hides queueing: a slow database would slow the offered load down and
look fine. Here a seeded arrival process (``mix.arrivals``) is scheduled in advance on a monotonic clock; a dispatcher thread
hands each job to a worker pool at its time, and a job may schedule a later one (the settle of a lease, ``hold_s`` after its
reserve) with :meth:`Dispatcher.at`. A job receives the time it was DUE, so the latency of its first operation is measured
from there and a pool that fell behind shows up as ``lag`` (the time between due and the moment a worker started it).
"""

import heapq
import itertools
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

Job = Callable[[float], None]                     # called with the monotonic time the job was due


class Dispatcher:
    def __init__(self, workers: int, clock: Callable[[], float] = time.perf_counter):
        self._clock = clock
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="s7-worker")
        self._cv = threading.Condition()
        self._heap: list[tuple[float, int, Job]] = []
        self._seq = itertools.count()
        self._running = 0                          # jobs handed to the pool and not finished
        self.lags: list[float] = []
        self.crashes: list[str] = []                # the class of every exception a job let escape (a bug of the replay)

    def at(self, due: float, job: Job) -> None:
        """Run ``job(due)`` once ``due`` (on this dispatcher's clock) has passed. Safe to call from a job."""
        with self._cv:
            heapq.heappush(self._heap, (due, next(self._seq), job))
            self._cv.notify()

    def _submit(self, due: float, job: Job) -> None:
        with self._cv:
            self._running += 1
        self._pool.submit(self._run, due, job)

    def _run(self, due: float, job: Job) -> None:
        try:
            lag = max(0.0, self._clock() - due)
            with self._cv:
                self.lags.append(lag)
            try:
                job(due)
            except Exception as exc:                                  # noqa: BLE001 - recorded, and it voids the level
                with self._cv:
                    self.crashes.append(type(exc).__name__)
        finally:
            with self._cv:
                self._running -= 1
                self._cv.notify()

    def drive(self, arrivals: Iterator[tuple[float, Job]], start: float, give_up_at: float) -> int:
        """Fire the arrivals (``(seconds from start, job)``) and every job they schedule, until none is left or the clock reaches
        ``give_up_at``. Returns how many jobs were still waiting or running then (0 for a clean drain)."""
        upcoming = next(arrivals, None)
        while True:
            with self._cv:
                now = self._clock()
                due_arrival = None if upcoming is None else start + upcoming[0]
                due_job = self._heap[0][0] if self._heap else None
                if due_arrival is None and due_job is None and self._running == 0:
                    return 0
                if now >= give_up_at:
                    return self._running + len(self._heap) + (0 if upcoming is None else 1)
                candidates = [t for t in (due_arrival, due_job) if t is not None]
                if not candidates:                 # nothing is scheduled, but a running job may still schedule its settle
                    self._cv.wait(0.05)
                    continue
                soonest = min(candidates)
                if soonest > now:
                    self._cv.wait(min(soonest - now, 0.05))
                    continue
                if due_job is not None and (due_arrival is None or due_job <= due_arrival):
                    due, _, job = heapq.heappop(self._heap)
                else:
                    due, job = due_arrival, upcoming[1]
                    upcoming = next(arrivals, None)
            self._submit(due, job)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
