"""The generator's own clock: which pre-registered phase this process is in, and a 1 Hz sample of its open streams. Pure stdlib.

Locust's ``LoadTestShape`` runs on the master only and a worker is never told the phase, so each process keeps its own: it
starts when its first user is spawned (the ramp's first second) and reads :func:`tools.loadtest.model.phase_at` from a monotonic
clock. Two processes disagree by the seconds their first spawn messages took, which is why the report buckets by phase and
seconds INTO the phase, never by one absolute clock across machines. With ``shape=False`` (an ad-hoc or smoke run) the phase is a
fixed label.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from tools.loadtest import model
from tools.loadtest.client import StreamGauge
from tools.loadtest.records import RecordWriter

BEFORE, AFTER = "pre", "after"


class PhaseClock:
    def __init__(self, *, shape: bool = True, fixed_phase: str = "steady", clock: Callable[[], float] = time.monotonic) -> None:
        self.shape, self.fixed_phase, self._clock = shape, fixed_phase, clock
        self._t0: float | None = None

    def start(self) -> None:
        if self._t0 is None:
            self._t0 = self._clock()

    @property
    def started(self) -> bool:
        return self._t0 is not None

    def elapsed(self) -> float:
        return 0.0 if self._t0 is None else self._clock() - self._t0

    def phase(self) -> str:
        if self._t0 is None:
            return BEFORE
        if not self.shape:
            return self.fixed_phase
        return model.phase_at(self.elapsed()) or AFTER

    def t_phase(self) -> float:
        """Seconds since the current phase began."""
        if self._t0 is None:
            return 0.0
        if not self.shape:
            return self.elapsed()
        current = self.phase()
        for name, t0, _ in model.phase_schedule():
            if name == current:
                return self.elapsed() - t0
        return self.elapsed() - model.run_length_s()


class StreamSampler:
    """``tick()`` once a second: a ``streams`` record (open live / total streams, phase, seconds into the phase) and a
    ``phase`` record whenever the phase changes."""

    def __init__(self, gauge: StreamGauge, phases: PhaseClock, writer: RecordWriter, *, run_id: str, worker: int,
                 wall: Callable[[], float] = time.time) -> None:
        self.gauge, self.phases, self.writer, self.run_id, self.worker, self.wall = gauge, phases, writer, run_id, worker, wall
        self._last_phase: str | None = None

    def tick(self) -> None:
        if not self.phases.started:
            return
        phase = self.phases.phase()
        base = {"ts": round(self.wall(), 3), "run_id": self.run_id, "worker": self.worker}
        if phase != self._last_phase:
            self.writer.write({"kind": "phase", "name": phase, **base})
            self._last_phase = phase
        live, total = self.gauge.snapshot()
        self.writer.write({"kind": "streams", "phase": phase, "t_phase": int(self.phases.t_phase()), "live_in_flight": live,
                           "total_in_flight": total, **base})
