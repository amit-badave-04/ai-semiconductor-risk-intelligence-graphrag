"""The raw sample file of an S7 level: one compact JSON line per timed operation (stdlib only).

A line is ``[t, op, total_ms, exec_ms, status]``:

* ``t``: seconds from the level's start to the moment the operation was REQUESTED (its scheduled start for the first operation of
  an ask, the end of the previous operation for the rest);
* ``total_ms``: from that moment to the end of the operation, so a driver that fell behind its schedule shows up here;
* ``exec_ms``: from the moment a worker began it (the wait for a thread gate token is inside it, because the route waits too);
* ``status``: ``ok``, ``err:<ExceptionClass>``, ``denied:<reason>`` or ``noslot`` (no state token within the bound).

The file is flushed on every line, so a killed run keeps what it measured; a torn last line is skipped on read.
"""

import json
import threading
from collections.abc import Iterator
from pathlib import Path

Sample = tuple[float, str, float, float, str]


class Recorder:
    """Thread-safe appender: ``add`` from any thread, ``close`` once."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "w", encoding="utf-8")        # noqa: SIM115 - closed by close()
        self._lock = threading.Lock()
        self.n = 0

    def add(self, t: float, op: str, total_ms: float, exec_ms: float, status: str) -> None:
        line = json.dumps([round(t, 3), op, round(total_ms, 3), round(exec_ms, 3), status], separators=(",", ":"))
        with self._lock:
            if self._handle.closed:
                return
            self._handle.write(line + "\n")
            self._handle.flush()
            self.n += 1

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()


def iter_samples(path: Path) -> Iterator[Sample]:
    """The samples of ``path`` in file order; a line that does not parse (the last one of a killed run) is skipped."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                yield float(row[0]), str(row[1]), float(row[2]), float(row[3]), str(row[4])
            except (ValueError, IndexError, TypeError):
                continue


def read_samples(path: Path) -> list[Sample]:
    return list(iter_samples(path))
