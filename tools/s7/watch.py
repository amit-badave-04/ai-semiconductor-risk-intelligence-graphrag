"""What the replay watches besides the asks: the database's own uptime, the driver's CPU, and the ledger afterwards.

* :class:`ServerProbe`: every few seconds one cheap statement; ``server.jsonl`` gets ``{"t", "ok", "ms", "uptime_ms"}``. The uptime
  comes from the JVM's runtime bean (``dbms.queryJmx``); if the server refuses that procedure the probe falls back to
  ``RETURN 1`` once and records ``uptime_ms: null`` from then on (the report then cannot call a restart from the probes alone and
  says so). UNVERIFIED on Neo4j 2026.07.1 Community until the opt-in test in ``tests/test_tools_s7.py`` has run against a
  throwaway server.
* :class:`CpuSampler`: the driver's own CPU over each second, as a share of one core (``driver.jsonl``). A driver above 70 % of
  its core voids the level (``limits.DRIVER_CPU_MAX``).
* :func:`verify_ledger`: after the drain, the ledger rows this run made, found by the address prefix every ask carries.
* :func:`table_counts`: the size of the ledger and of the answer cache before and after a level.
"""

import json
import math
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from neo4j import Query
from neo4j.exceptions import ClientError

from semigraph.serve.state.backend import day_of

UPTIME_QUERY = ("CALL dbms.queryJmx('java.lang:type=Runtime') YIELD attributes "
                "RETURN attributes.Uptime.value AS up")
PING_QUERY = "RETURN 1 AS ok"
PROBE_TIMEOUT_S = 5.0
COUNT_QUERIES = {"svc_query": "MATCH (q:SvcQuery) RETURN count(q) AS n", "svc_answer": "MATCH (a:SvcAnswer) RETURN count(a) AS n"}
VERIFY_QUERY = ("MATCH (q:SvcQuery) WHERE q.day IN $days AND q.ip_hash STARTS WITH $prefix "
                "RETURN q.status AS status, q.cached AS cached, count(q) AS n")


class ServerProbe(threading.Thread):
    def __init__(self, driver: Any, every_s: float, path: Path, *, t0: float | None = None,
                 clock: Callable[[], float] = time.perf_counter):
        super().__init__(name="s7-probe", daemon=True)
        self._driver, self._every_s, self._path, self._clock = driver, every_s, Path(path), clock
        self._stop_event = threading.Event()
        self._t0 = clock() if t0 is None else t0                    # the level's start, so `t` agrees with samples.jsonl
        self._uptime_supported = True
        self.count = self.failures = 0

    def _query(self) -> int | None:
        text = UPTIME_QUERY if self._uptime_supported else PING_QUERY
        with self._driver.session() as session:
            rows = list(session.run(Query(text, timeout=PROBE_TIMEOUT_S)))
        if not self._uptime_supported or not rows:
            return None
        return int(dict(rows[0])["up"])

    def probe_once(self) -> dict:
        began = self._clock()
        try:
            try:
                uptime = self._query()
            except ClientError:                            # the server answered and refused: the procedure is not there
                if not self._uptime_supported:
                    raise
                self._uptime_supported = False
                uptime = self._query()
            row = {"t": round(began - self._t0, 3), "ok": True, "ms": round((self._clock() - began) * 1000, 3),
                   "uptime_ms": uptime}
        except Exception as exc:                           # noqa: BLE001 - a failed probe is the finding
            self.failures += 1
            row = {"t": round(began - self._t0, 3), "ok": False, "ms": round((self._clock() - began) * 1000, 3),
                   "uptime_ms": None, "error": type(exc).__name__}
        self.count += 1
        return row

    def run(self) -> None:
        with open(self._path, "w", encoding="utf-8") as handle:
            while True:
                handle.write(json.dumps(self.probe_once(), separators=(",", ":")) + "\n")
                handle.flush()
                if self._stop_event.wait(self._every_s):
                    break

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=PROBE_TIMEOUT_S + 2)


class CpuSampler(threading.Thread):
    """Process CPU seconds per wall second, once a second."""

    def __init__(self, path: Path, interval_s: float = 1.0, clock: Callable[[], float] = time.perf_counter):
        super().__init__(name="s7-cpu", daemon=True)
        self._path, self._interval_s, self._clock = Path(path), interval_s, clock
        self._stop_event = threading.Event()
        self.shares: list[float] = []

    def run(self) -> None:
        t0 = last_wall = self._clock()
        last_cpu = time.process_time()
        with open(self._path, "w", encoding="utf-8") as handle:
            while not self._stop_event.wait(self._interval_s):
                wall, cpu = self._clock(), time.process_time()
                share = (cpu - last_cpu) / max(wall - last_wall, 1e-6)
                last_wall, last_cpu = wall, cpu
                self.shares.append(share)
                handle.write(json.dumps({"t": round(wall - t0, 3), "cpu": round(share, 4)}, separators=(",", ":")) + "\n")
                handle.flush()

    def stop(self) -> None:
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=self._interval_s + 2)

    def summary(self) -> dict:
        ordered = sorted(self.shares)
        if not ordered:
            return {"cpu_max": 0.0, "cpu_p95": 0.0, "samples": 0}
        p95 = ordered[max(1, math.ceil(0.95 * len(ordered))) - 1]
        return {"cpu_max": round(ordered[-1], 4), "cpu_p95": round(p95, 4), "samples": len(ordered)}


def verify_ledger(driver: Any, prefix: str, wall_start: float, wall_end: float) -> dict:
    """The ledger rows whose address starts with ``prefix``, counted by what they are:

    ``paid_rows`` settled live asks (or, in today's flow, the plain paid rows with no status), ``reserved_rows`` leases never
    settled, ``cached_rows`` the rows of cached asks."""
    days = sorted({day_of(wall_start), day_of(wall_end)})
    with driver.session() as session:
        rows = [dict(r) for r in session.run(Query(VERIFY_QUERY, timeout=60.0), days=days, prefix=prefix)]
    out = {"prefix": prefix, "days": days, "paid_rows": 0, "reserved_rows": 0, "cached_rows": 0, "other_rows": 0}
    for row in rows:
        n = int(row["n"])
        if row["status"] == "reserved":
            out["reserved_rows"] += n
        elif row["cached"]:
            out["cached_rows"] += n
        elif row["status"] in ("settled", None):
            out["paid_rows"] += n
        else:
            out["other_rows"] += n
    return out


def table_counts(driver: Any) -> dict:
    """How many ledger rows and cached answers the database holds right now. Every level runs on what the earlier ones left
    (the pre-registration is silent on resetting between levels), so each level records its start and end counts. A failed
    count is recorded as ``None``: it never fails a run."""
    out: dict = {}
    for name, text in COUNT_QUERIES.items():
        try:
            with driver.session() as session:
                out[name] = int(next(iter(session.run(Query(text, timeout=60.0))))["n"])
        except Exception:                                  # noqa: BLE001 - information, not a gate
            out[name] = None
    return out
