"""Named thread limiters and an event-loop lag monitor (M5a I2, docs/v2/M5A_BUILD_PLAN.md sections 2 and 4).

The async answer path holds no thread while it waits for the model; every blocking hop (embedding, graph reads and writes,
document checks) runs on a worker thread and is bounded by one of these ``anyio.CapacityLimiter`` pools, so a burst of
streams queues for a thread instead of starving the event loop, and one kind of work cannot take every thread from another:

* ``embed``  : query embeddings (CPU-bound; ``embed_slots``), taken together with the embedder's own semaphore;
* ``db``     : graph reads and writes made on behalf of answer streams (``db_thread_limit``);
* ``health`` : the ``/healthz`` ping (one thread, so a stuck database cannot hang the probe pool);
* ``state``  : the service-state operations (reserve / reconcile / cache reads; bounded and separate from ``db``). A
  caller waits for a slot at most ``state_op_timeout_s`` (``stream_runtime.state_call``);
* ``admin``  : the admin routes' state calls (a kill-level flip, the state report), one at a time and never queued
  behind the public traffic of ``state``. It is None in a test double that predates it: such a double serves no admin
  route.

A limiter must be created inside a running event loop, so :func:`make_limiters` is called from the lifespan.

:class:`LoopLagMonitor` measures how late a short timer fires. A synchronous call made on the loop shows up as lag; the
stream tests assert it stays silent.
"""

import logging
import time
from typing import NamedTuple

import anyio

logger = logging.getLogger("semigraph.serve.loop")

HEALTH_THREADS = 1
STATE_THREADS = 4
ADMIN_THREADS = 1
DEFAULT_MONITOR_INTERVAL_S = 0.05


class Limiters(NamedTuple):
    embed: anyio.CapacityLimiter
    db: anyio.CapacityLimiter
    health: anyio.CapacityLimiter
    state: anyio.CapacityLimiter
    admin: anyio.CapacityLimiter | None = None


def make_limiters(settings) -> Limiters:
    for name in ("embed_slots", "db_thread_limit"):
        if getattr(settings, name) < 1:
            raise ValueError(f"{name} must be 1 or more, got {getattr(settings, name)}")
    return Limiters(embed=anyio.CapacityLimiter(settings.embed_slots), db=anyio.CapacityLimiter(settings.db_thread_limit),
                    health=anyio.CapacityLimiter(HEALTH_THREADS), state=anyio.CapacityLimiter(STATE_THREADS),
                    admin=anyio.CapacityLimiter(ADMIN_THREADS))


class LoopLagMonitor:
    """Sleeps ``interval_s`` in a loop and records how much later than that it woke up."""

    def __init__(self, warn_ms: int, interval_s: float = DEFAULT_MONITOR_INTERVAL_S):
        if warn_ms < 1:
            raise ValueError(f"warn_ms must be 1 or more, got {warn_ms}")
        if interval_s <= 0:
            raise ValueError(f"interval_s must be positive, got {interval_s}")
        self.warn_ms, self.interval_s = warn_ms, interval_s
        self.max_lag_ms = 0.0
        self.warnings = 0

    async def run(self) -> None:
        """Runs until cancelled (start it in a task group or a task and cancel it at shutdown)."""
        while True:
            started = time.perf_counter()
            await anyio.sleep(self.interval_s)
            lag_ms = (time.perf_counter() - started - self.interval_s) * 1000.0
            self.max_lag_ms = max(self.max_lag_ms, lag_ms)
            if lag_ms > self.warn_ms:
                self.warnings += 1
                logger.warning("loop_lag_ms=%d (the event loop was blocked; threshold %d ms)", round(lag_ms), self.warn_ms)
