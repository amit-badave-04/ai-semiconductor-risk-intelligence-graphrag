"""One full-core loop, measured every second, with the steal time of ``/proc/stat`` (M5a I5, the fresh-machine throttle probe).

Why: ``scripts/ops.ps1 start`` creates a NEW machine, and the documentation's burst figures (a few seconds of balance on a fresh
shared-cpu machine, about 500 s to refill) are not an observation. This loop is the observation. It does fixed chunks of pure integer
work and counts them; a second later the iterations per second say how much CPU the machine really gave this process, and the steal
column of ``/proc/stat`` (the hypervisor taking the core away) says why. After the burn an idle stretch with a two-second pulse every
minute shows how fast the balance comes back.

Output (one directory): ``samples.jsonl``, one JSON object per second (flushed as it is written, so a killed run keeps what it
measured) and ``meta.json`` (machine id, region, cpu count, the arguments; the environment is NOT copied). ``report.py`` analyses them.

Stdlib only. Clock, work, ``/proc/stat`` reader and sleep are parameters so the tests run in microseconds and on any platform.
"""

import argparse
import json
import os
import platform
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PROC_STAT = Path("/proc/stat")
DEFAULT_CHUNK = 20_000
DEFAULT_PULSE_EVERY_S = 60
DEFAULT_PULSE_S = 2
STAT_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")     # guest time is inside user / nice


@dataclass(frozen=True)
class StatSnapshot:
    """``/proc/stat`` at one instant: the aggregate line and one dict per CPU, each with the eight fields and ``total``."""

    total: dict
    cpus: tuple

    @property
    def total_jiffies(self) -> int:
        return self.total["total"]


def _fields(parts: list[str]) -> dict | None:
    try:
        numbers = [int(p) for p in parts[1:1 + len(STAT_FIELDS)]]
    except ValueError:
        return None
    if len(numbers) < 4:
        return None
    numbers += [0] * (len(STAT_FIELDS) - len(numbers))                       # older kernels have fewer columns (no steal)
    row = dict(zip(STAT_FIELDS, numbers, strict=True))
    row["total"] = sum(numbers)
    return row


def parse_proc_stat(text: str) -> StatSnapshot | None:
    """The aggregate ``cpu`` line and the ``cpuN`` lines of ``/proc/stat``; None when the text has no readable aggregate."""
    total, cpus = None, []
    for line in text.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("cpu"):
            continue
        row = _fields(parts)
        if row is None:
            continue
        if parts[0] == "cpu":
            total = row
        else:
            cpus.append(row)
    return StatSnapshot(total, tuple(cpus)) if total is not None else None


def read_proc_stat() -> StatSnapshot | None:
    try:
        return parse_proc_stat(PROC_STAT.read_text(encoding="ascii"))
    except OSError:
        return None


def steal_fraction(before: StatSnapshot | None, after: StatSnapshot | None) -> tuple[float | None, list | None]:
    """Steal jiffies over all jiffies between two snapshots: ``(whole machine, [per cpu])``; ``(None, None)`` when either
    snapshot is missing or no time passed."""
    if before is None or after is None:
        return None, None
    elapsed = after.total_jiffies - before.total_jiffies
    if elapsed <= 0:
        return None, None
    whole = (after.total["steal"] - before.total["steal"]) / elapsed
    by_cpu = []
    for a, b in zip(before.cpus, after.cpus, strict=False):
        span = b["total"] - a["total"]
        by_cpu.append((b["steal"] - a["steal"]) / span if span > 0 else None)
    return whole, by_cpu or None


def _spin(n: int) -> int:
    x = 0
    for i in range(n):
        x = (x * 31 + i) & 0xFFFFFFFF
    return x


def _check(duration_s: float, chunk: int, minimum: float = 1.0) -> None:
    if not duration_s >= minimum:
        raise ValueError(f"the duration must be at least {minimum:g} s, got {duration_s!r}")
    if not chunk >= 1:
        raise ValueError(f"chunk must be 1 or more, got {chunk!r}")


def _sample(phase: str, t: int, *, wall: float, elapsed: float, iterations: int, cpu_seconds: float,
            before: StatSnapshot | None, after: StatSnapshot | None) -> dict:
    steal, by_cpu = steal_fraction(before, after)
    return {"t": t, "phase": phase, "wall": wall, "elapsed_s": elapsed, "iterations": iterations,
            "iter_per_s": iterations / elapsed, "cpu_share": cpu_seconds / elapsed, "steal_frac": steal,
            "steal_by_cpu": by_cpu}


def run_burn(duration_s: float, *, chunk: int = DEFAULT_CHUNK, work: Callable[[], object] | None = None,
             clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
             cpu_time: Callable[[], float] = time.process_time,
             read_stat: Callable[[], StatSnapshot | None] = read_proc_stat, sink: Callable[[dict], object]) -> int:
    """Spin for ``int(duration_s)`` seconds, giving ``sink`` one sample per second (``t`` = 1, 2, ...). Returns how many.
    A second's bucket ends at the first chunk that completes at or after it, so ``elapsed_s`` is the real length."""
    _check(duration_s, chunk)
    work = work if work is not None else (lambda: _spin(chunk))
    seconds = int(duration_s)
    bucket_start, snap, cpu = clock(), read_stat(), cpu_time()
    count, t = 0, 0
    while t < seconds:
        work()
        count += chunk
        now = clock()
        if now - bucket_start < 1.0:
            continue
        t += 1
        after, cpu_now = read_stat(), cpu_time()
        sink(_sample("burn", t, wall=wall(), elapsed=now - bucket_start, iterations=count, cpu_seconds=cpu_now - cpu,
                     before=snap, after=after))
        bucket_start, snap, cpu, count = now, after, cpu_now, 0
    return t


def run_pulses(idle_s: float, *, every_s: float = DEFAULT_PULSE_EVERY_S, pulse_s: float = DEFAULT_PULSE_S,
               chunk: int = DEFAULT_CHUNK, work: Callable[[], object] | None = None,
               clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
               cpu_time: Callable[[], float] = time.process_time,
               read_stat: Callable[[], StatSnapshot | None] = read_proc_stat,
               sleep: Callable[[float], object] = time.sleep, sink: Callable[[dict], object]) -> int:
    """The idle stretch: every ``every_s`` seconds a ``pulse_s`` burst whose iterations per second say how much CPU a rested
    machine gives. Sleeps between pulses (nothing else runs). ``t`` is the seconds since the stretch began, at the pulse's end."""
    _check(idle_s, chunk, minimum=every_s)
    if not 0 < pulse_s < every_s:
        raise ValueError("the pulse must be shorter than the period")
    work = work if work is not None else (lambda: _spin(chunk))
    began, pulses = clock(), 0
    for _ in range(int(idle_s // every_s)):
        sleep(every_s - pulse_s)
        start, snap, cpu, count = clock(), read_stat(), cpu_time(), 0
        while clock() - start < pulse_s:
            work()
            count += chunk
        end = clock()
        sink(_sample("pulse", round(end - began), wall=wall(), elapsed=end - start, iterations=count,
                     cpu_seconds=cpu_time() - cpu, before=snap, after=read_stat()))
        pulses += 1
    return pulses


def _meta(args: argparse.Namespace) -> dict:
    return {"machine_id": os.environ.get("FLY_MACHINE_ID") or socket.gethostname(), "region": os.environ.get("FLY_REGION", ""),
            "cpu_count": os.cpu_count() or 1, "python": platform.python_version(), "platform": sys.platform,
            "started_at": time.time(), "burn_s": args.burn_s, "idle_s": args.idle_s, "pulse_every_s": args.pulse_every_s,
            "pulse_s": args.pulse_s, "chunk": args.chunk}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tools.probe.burn", description=__doc__.splitlines()[0])
    parser.add_argument("--burn-s", type=int, default=3600, help="seconds of full-core load (default 3600)")
    parser.add_argument("--idle-s", type=int, default=0, help="seconds of idle with a pulse each period after the burn")
    parser.add_argument("--pulse-every-s", type=int, default=DEFAULT_PULSE_EVERY_S)
    parser.add_argument("--pulse-s", type=int, default=DEFAULT_PULSE_S)
    parser.add_argument("--chunk", type=int, default=DEFAULT_CHUNK, help="iterations per unit of work")
    parser.add_argument("--out", type=Path, default=Path(os.environ.get("PROBE_OUT_DIR", "probe_out")))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "meta.json").write_text(json.dumps(_meta(args), indent=1), encoding="utf-8")
    with (args.out / "samples.jsonl").open("w", encoding="utf-8", buffering=1) as handle:
        def sink(row: dict) -> None:
            handle.write(json.dumps(row) + "\n")
            handle.flush()

        run_burn(args.burn_s, chunk=args.chunk, sink=sink)
        if args.idle_s:
            run_pulses(args.idle_s, every_s=args.pulse_every_s, pulse_s=args.pulse_s, chunk=args.chunk, sink=sink)
    return 0


if __name__ == "__main__":
    sys.exit(main())
