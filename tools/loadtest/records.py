"""The generator's raw record: one JSON line per thing that happened. Pure stdlib.

``scripts/loadtest_report.py`` computes the verdict from these files and nothing else on the generator side: Locust's own
statistics are not used (with ``stream=True`` it stamps ``response_time`` when the HEADERS arrive, long before the answer is
done). Every line carries ``kind`` plus the common fields below; the kinds are documented where they are produced
(``client.py``):

    common   ts (epoch s, this machine's wall clock), run_id, worker, vu, phase
    ask      klass, strategy, question (the exact text sent), salt, status, outcome, ttfb_s, ttfe_s, first_delta_s, total_s,
             events (counts by name), pings, cached, escalated, answered_by, citations, detail, bytes, ip
    read     label, status, outcome, total_s, detail
    upload_step / upload   the HTTP calls of one upload cycle, and its summary (outcome ready | failed | ...)
    streams  1 Hz sample: live_in_flight, total_in_flight, phase, t_phase
    iteration  target_s, elapsed_s, overran
    phase    a phase boundary seen by this process
    meta     once per process: the parameters the model was run with
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class RecordWriter:
    """Appends JSON lines to ``path`` (line-buffered, so a killed process loses at most the line in flight) or, with no path,
    keeps them in ``self.records`` (tests)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._file = None
        if path is not None:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(target, "a", encoding="utf-8", buffering=1)      # noqa: SIM115 - closed in close()

    def write(self, record: dict[str, Any]) -> None:
        with self._lock:
            if self._file is None:
                self.records.append(record)
            else:
                self._file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()
                self._file = None


def read_records(path: Path | str) -> list[dict[str, Any]]:
    """Every record of one JSONL file; a torn last line (a killed process) is skipped, not fatal."""
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
