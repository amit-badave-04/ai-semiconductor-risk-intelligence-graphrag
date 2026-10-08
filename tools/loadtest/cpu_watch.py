"""A 1 Hz CPU sampler that writes JSONL, the single CPU format for the generators, the mock LLM and the API machine.

The report reads it for the pre-registered VOID clause "any generator CPU > 70 %" and for the server gate (p95 of
``cpu_pct_system`` against 70 %) and the validity rule (process CPU-seconds per live ask against the Fly per-embed cost, from
``cpu_time_s``). The report's "mock CPU > 50 %" rule reads it too, but that one is a harness rule, not pre-registered
(``scripts/loadtest_report.py`` tags it ``[harness-added]``). Run it as its OWN process on each machine, next to the thing it watches, never inside the Locust process it
would perturb:

    python -m tools.loadtest.cpu_watch --name gen-1 --role generator --match locust --out cpu/gen-1.jsonl
    python -m tools.loadtest.cpu_watch --name api --role server --match semigraph.serve --out cpu/api.jsonl
    python -m tools.loadtest.cpu_watch --name mock --role mock --match tools.mockllm --out cpu/mock.jsonl

A line (``t`` is this machine's epoch seconds):

    t, name, role, ncores, cpu_pct_system  (whole machine, 0-100)
    n_procs, proc_pct_core_max  (the busiest watched process, % of ONE core: a single-threaded Locust worker saturates at 100)
    proc_pct_core_sum, cpu_pct_procs  (the watched processes together, % of the whole machine)
    cpu_time_s  (cumulative user+system CPU seconds of the watched processes: differences give CPU-seconds per ask)
    rss_mb, rss_pct, mem_total_mb
    cpu_pct_gate  = max(cpu_pct_system, min(proc_pct_core_max, 100)): what the generator / mock VOID rules compare. A
                    one-core Locust worker pinned at 100 % is saturated even when a 2-core machine reads 50 %.

psutil's first ``cpu_percent`` reading is meaningless (0.0 since "the last call"), so every counter is primed and the first
line is written one interval later. The sampler matches on the command line text but never writes a command line or an
environment variable (a Locust or API command line can carry a secret): a line holds numbers and the sampler's own name.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

import psutil

REFRESH_PROCS_EVERY_S = 5.0
ROLES = ("generator", "mock", "server")


def gate_pct(system_pct: float, proc_core_max: float) -> float:
    return round(max(system_pct, min(proc_core_max, 100.0)), 2)


class CpuWatch:
    def __init__(self, name: str, role: str, *, pids: list[int] | None = None, match: str | None = None,
                 interval_s: float = 1.0, wall=time.time, sleep=time.sleep) -> None:
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {role!r}")
        self.name, self.role, self.interval_s = name, role, interval_s
        self.pids, self.match = list(pids or []), match
        self.wall, self.sleep = wall, sleep
        self.ncores = psutil.cpu_count(logical=True) or 1
        self._procs: dict[int, psutil.Process] = {}
        self._refreshed = float("-inf")
        self._self_pid = psutil.Process().pid

    # -- the watched set --------------------------------------------------------------------------------------------------

    def _discover(self) -> set[int]:
        found = set(self.pids)
        if self.match:
            for proc in psutil.process_iter(["pid", "cmdline"]):
                if proc.info["pid"] == self._self_pid:
                    continue
                if self.match in " ".join(proc.info.get("cmdline") or []):
                    found.add(proc.info["pid"])
        return found

    def _refresh(self, now: float) -> None:
        if now - self._refreshed < REFRESH_PROCS_EVERY_S:
            return
        self._refreshed = now
        wanted = self._discover()
        for pid in list(self._procs):
            if pid not in wanted:
                del self._procs[pid]
        for pid in wanted - set(self._procs):
            try:
                proc = psutil.Process(pid)
                proc.cpu_percent(None)                       # prime: the first reading of a process is meaningless
                self._procs[pid] = proc
            except psutil.Error:
                continue

    # -- sampling ---------------------------------------------------------------------------------------------------------

    def prime(self) -> None:
        psutil.cpu_percent(None)
        self._refresh(time.monotonic())

    def sample(self) -> dict:
        self._refresh(time.monotonic())
        system = psutil.cpu_percent(None)
        pct, cpu_time, rss = [], 0.0, 0
        for pid, proc in list(self._procs.items()):
            try:
                pct.append(proc.cpu_percent(None))
                times = proc.cpu_times()
                cpu_time += times.user + times.system
                rss += proc.memory_info().rss
            except psutil.Error:
                self._procs.pop(pid, None)
        total = psutil.virtual_memory().total
        busiest = max(pct, default=0.0)
        return {"t": round(self.wall(), 3), "name": self.name, "role": self.role, "ncores": self.ncores,
                "cpu_pct_system": round(system, 2), "n_procs": len(pct), "proc_pct_core_max": round(busiest, 2),
                "proc_pct_core_sum": round(sum(pct), 2), "cpu_pct_procs": round(sum(pct) / self.ncores, 2),
                "cpu_time_s": round(cpu_time, 3), "rss_mb": round(rss / 2**20, 1),
                "rss_pct": round(100.0 * rss / total, 2) if total else 0.0, "mem_total_mb": round(total / 2**20),
                "cpu_pct_gate": gate_pct(system, busiest)}

    def run(self, out: Path | str, *, duration_s: float | None = None, stop: threading.Event | None = None) -> int:
        """Write one line per interval until ``duration_s`` has passed or ``stop`` is set. Returns the number of lines."""
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        stop = stop or threading.Event()
        written, started = 0, time.monotonic()
        self.prime()
        with open(target, "a", encoding="utf-8", buffering=1) as handle:
            while not stop.is_set():
                if duration_s is not None and time.monotonic() - started >= duration_s:
                    break
                self.sleep(self.interval_s)
                handle.write(json.dumps(self.sample(), separators=(",", ":")) + "\n")
                written += 1
        return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="1 Hz CPU / memory sampler (JSONL).")
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--match", help="watch processes whose command line contains this text")
    parser.add_argument("--pid", type=int, action="append", default=[], help="watch this pid (repeatable)")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--duration", type=float, default=None, help="stop after this many seconds (default: until SIGTERM)")
    args = parser.parse_args(argv)
    stop = threading.Event()
    for signame in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signame):
            signal.signal(getattr(signal, signame), lambda *_: stop.set())
    watch = CpuWatch(args.name, args.role, pids=args.pid, match=args.match, interval_s=args.interval)
    lines = watch.run(args.out, duration_s=args.duration, stop=stop)
    print(f"cpu_watch {args.name}: {lines} samples -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
