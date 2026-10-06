"""The maintenance thread: the three periodic jobs the state package needs, whatever else the app is doing.

- the kill level is re-read every ``kill_switch_refresh_s`` (10 s), and ONCE before ``start()`` returns, so serving
never
  begins with an unread level (an unread level reads as ``on``);
- every lease whose stream has started (the registry) is renewed every ``lease_renew_s`` (15 s): ``renew`` writes
  ``lease_until`` on its row, so the lease TTL only has to cover a stream that never started;
- the sweep runs on the same period: it closes expired leases nobody renews (a stream that never began, a task that
died),
  charges each its estimate and frees its in-flight slot.

It is unconditional: not tied to the upload sweeper or to any route. A thread of its own, because the backend calls are
synchronous and bounded by the driver, and the event loop must never wait for them.

An exception in one task is logged (``state_maintenance_failed``) and the task is retried at its next tick; it never
ends the thread, so a Neo4j outage cannot stop the kill-level refresh from recovering afterwards. A state that cannot be
read simply ages: a kill level older than ``kill_switch_stale_s`` reads as ``on`` (see
``backend.StateCore.kill_level``).
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("semigraph.serve.state")

STOP_TIMEOUT_S = 5.0


def _interval(settings: Any, name: str) -> float:
    try:
        value = getattr(settings, name)
    except AttributeError:
        raise ValueError(f"the maintenance thread needs settings.{name}") from None
    if value is None or not value > 0:
        raise ValueError(f"{name} must be a positive number of seconds, got {value!r}")
    return float(value)


class MaintenanceThread(threading.Thread):
    """``MaintenanceThread(backend, settings, registry)``: ``backend`` needs ``refresh_kill_level()``, ``renew`` and
    ``sweep`` (both concrete backends have them); ``registry`` is the set of started leases (default: the backend's
    own).
    ``clock`` (monotonic, schedules the tasks) and ``wall`` (epoch seconds, handed to ``renew`` and ``sweep``) are
    injectable; so is ``wait(seconds) -> bool`` (True means stop), which defaults to waiting on the stop event."""

    def __init__(self, backend: Any, settings: Any, registry: Any = None, *,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 wait: Callable[[float], bool] | None = None):
        super().__init__(name="state-maintenance", daemon=True)
        self._backend = backend
        self._registry = registry if registry is not None else backend.registry
        self._kill_every = _interval(settings, "kill_switch_refresh_s")
        self._lease_every = _interval(settings, "lease_renew_s")
        self._clock, self._wall = clock, wall
        self._stop_event = threading.Event()
        self._wait = wait if wait is not None else self._stop_event.wait
        self._next_kill = self._next_lease = 0.0

    # ---- the tasks -----------------------------------------------------------------------------------------------

    def _guarded(self, task: str, fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - nothing a task raises may end the thread
            logger.warning("state_maintenance_failed task=%s error=%s: %.200s", task, type(exc).__name__, exc)
            return None

    def _refresh_kill(self) -> None:
        self._guarded("kill_refresh", self._backend.refresh_kill_level)

    def _renew_one(self, lease_id: str) -> None:
        renewed = self._guarded(f"renew lease={lease_id}", self._backend.renew, lease_id, self._wall())
        # the backend no longer knows the lease (reconciled or swept): stop renewing
        if renewed is False:
            self._registry.discard(lease_id)

    def _renew_leases(self) -> None:
        for lease_id in self._registry.active():
            self._renew_one(lease_id)

    def _sweep(self) -> None:
        self._guarded("sweep", self._backend.sweep, self._wall())

    # ---- the schedule --------------------------------------------------------------------------------------------

    def _first_read(self) -> None:
        """The read that must happen before serving: then the first refresh is one period away."""
        self._refresh_kill()
        now = self._clock()
        self._next_kill, self._next_lease = now + self._kill_every, now + self._lease_every

    def tick(self) -> float:
        """Run every task that is due; return the seconds until the next one is. A task that overran moves its own next
        due time forward from when it finished, so a stall never produces a burst of catch-up runs."""
        now = self._clock()
        if now >= self._next_kill:
            self._refresh_kill()
            self._next_kill = self._clock() + self._kill_every
        if now >= self._next_lease:
            self._renew_leases()
            self._sweep()
            self._next_lease = self._clock() + self._lease_every
        return max(0.0, min(self._next_kill, self._next_lease) - self._clock())

    def start(self) -> None:
        self._first_read()
        super().start()

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self._wait(self.tick()):
                break

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """Ask the thread to end and wait up to ``timeout`` seconds. True when it is no longer running; False (and a
        warning) when a task is still stuck in a call, which the driver's own timeouts will end."""
        self._stop_event.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)
        if self.is_alive():
            logger.warning("state maintenance thread did not stop within %.1f s", timeout)
            return False
        return True
