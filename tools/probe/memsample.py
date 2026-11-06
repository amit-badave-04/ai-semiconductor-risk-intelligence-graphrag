"""The memory sampler of window W1b: once a second, one JSON line of what /proc says about the serving process and the machine.

Runs ON the Fly machine, next to the real service, and reads nothing but ``/proc``. Standard library only, no import from
``tools`` or ``semigraph`` and no use of ``__file__``: the driver (``tools/probe/w1b.py``) ships this file's text inside a
``python -c`` command over ``flyctl ssh console`` (the runtime image holds the installed package, not the repository), so it
must work as text run by ``exec``.

    python memsample.py --out /tmp/memsample.jsonl --duration-s 1500 --daemon

The first line is a header (``{"header": {...}}``: MemTotal, SwapTotal, CPUs, the interval). Every line after it is one sample::

    {"t": <epoch s>, "mem_available_kb": ..., "swap_free_kb": ..., "swap_total_kb": ..., "uvicorn_pid": ..., "vmrss_kb": ...,
     "vmhwm_kb": ..., "oom_kill": ...}

* ``uvicorn_pid`` is the serving process: the one whose argument list holds ``-m semigraph.serve.drain`` (the image's CMD). The
  scan runs again on EVERY sample, so a process that was replaced shows as a changed pid (restart evidence), and a process that
  is gone shows as ``null``. If several processes match, the one whose parent is not itself a match is taken (a forked child
  inherits its parent's argument list). The sampler's own pid is never taken. A pid that vanishes between the scan and the read
  is skipped, not an error.
* ``vmhwm_kb`` is the kernel's own high-water mark of that process (``VmHWM`` of ``/proc/<pid>/status``), so a peak between two
  samples is not lost.
* ``oom_kill`` is the machine's cumulative OOM-kill counter (``/proc/vmstat``), ``null`` where the kernel has none.
* Each line is flushed as it is written, and the sampler ends by itself after ``--duration-s``, at SIGTERM, or when
  ``--stop-file`` exists: an ssh session that drops must not leave it running for good.
* ``--daemon`` detaches (double fork, new session) so the ssh command that started it can return.
"""

import argparse
import json
import os
import signal
import sys
import time

SERVER_ARGV = ("-m", "semigraph.serve.drain")
DEFAULT_INTERVAL_S = 1.0
DEFAULT_DURATION_S = 1800.0
MEMINFO_KEYS = {"MemTotal": "mem_total_kb", "MemAvailable": "mem_available_kb", "SwapTotal": "swap_total_kb", "SwapFree": "swap_free_kb"}


def read_text(path):
    """The text of ``path``, or None when it cannot be read (a process that exited, a file the kernel does not have)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def parse_kb_lines(text):
    """``{name: kB}`` of the ``Name:  123 kB`` lines of /proc/meminfo or /proc/<pid>/status (lines that are not a number of kB are
    left out)."""
    values = {}
    for line in (text or "").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit() and (len(parts) == 1 or parts[1] == "kB"):
            values[name.strip()] = int(parts[0])
    return values


def parse_oom_kill(text):
    """The ``oom_kill`` counter of /proc/vmstat, or None."""
    for line in (text or "").splitlines():
        name, _, value = line.partition(" ")
        if name == "oom_kill" and value.strip().isdigit():
            return int(value)
    return None


def parse_ppid(status_text):
    for line in (status_text or "").splitlines():
        if line.startswith("PPid:"):
            value = line.split(":", 1)[1].strip()
            return int(value) if value.isdigit() else None
    return None


def argv_of(path):
    """The argument list in /proc/<pid>/cmdline (NUL-separated), or [] when unreadable."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def is_server_argv(argv):
    """True when the arguments hold the service's module run: ``-m semigraph.serve.drain`` as two neighbouring words."""
    n = len(SERVER_ARGV)
    return any(tuple(argv[i:i + n]) == SERVER_ARGV for i in range(len(argv) - n + 1))


def find_server_pid(proc_root, exclude=()):
    """The pid of the serving process, or None. ``exclude`` are pids that are never taken (the sampler's own)."""
    matches = {}
    try:
        names = os.listdir(proc_root)
    except OSError:
        return None
    for name in names:
        if not name.isdigit() or int(name) in exclude:
            continue
        if is_server_argv(argv_of(os.path.join(proc_root, name, "cmdline"))):
            matches[int(name)] = parse_ppid(read_text(os.path.join(proc_root, name, "status")))
    roots = sorted(pid for pid, ppid in matches.items() if ppid not in matches)
    return roots[0] if roots else None


def read_server(proc_root, exclude=()):
    """``(pid, status kB values)`` of the serving process; ``(None, {})`` when there is none or it vanished while being read."""
    pid = find_server_pid(proc_root, exclude)
    if pid is None:
        return None, {}
    status = parse_kb_lines(read_text(os.path.join(proc_root, str(pid), "status")))
    if "VmHWM" not in status:
        return None, {}                                   # it exited between the scan and the read
    return pid, status


def sample(proc_root, now, exclude=()):
    meminfo = parse_kb_lines(read_text(os.path.join(proc_root, "meminfo")))
    pid, status = read_server(proc_root, exclude)
    return {"t": round(now, 3), "mem_available_kb": meminfo.get("MemAvailable"), "swap_free_kb": meminfo.get("SwapFree"),
            "swap_total_kb": meminfo.get("SwapTotal"), "uvicorn_pid": pid, "vmrss_kb": status.get("VmRSS"),
            "vmhwm_kb": status.get("VmHWM"), "oom_kill": parse_oom_kill(read_text(os.path.join(proc_root, "vmstat")))}


def header(proc_root, now, interval_s):
    meminfo = parse_kb_lines(read_text(os.path.join(proc_root, "meminfo")))
    cpuinfo = read_text(os.path.join(proc_root, "cpuinfo")) or ""
    cpus = sum(1 for line in cpuinfo.splitlines() if line.split(":")[0].strip() == "processor") or None
    return {"header": {"t": round(now, 3), "mem_total_kb": meminfo.get("MemTotal"), "swap_total_kb": meminfo.get("SwapTotal"),
                       "cpus": cpus, "interval_s": interval_s, "sampler_pid": os.getpid()}}


def run(out_path, *, interval_s=DEFAULT_INTERVAL_S, duration_s=DEFAULT_DURATION_S, stop_file=None, proc_root="/proc",
        clock=time.time, sleep=time.sleep, should_stop=lambda: False):
    """Append the header, then a sample every ``interval_s`` until ``duration_s`` passed, the stop file exists or
    ``should_stop()`` is true. Returns the number of samples written."""
    exclude = {os.getpid()}
    started, written = clock(), 0
    with open(out_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(header(proc_root, started, interval_s)) + "\n")
        handle.flush()
        while True:
            now = clock()
            handle.write(json.dumps(sample(proc_root, now, exclude)) + "\n")
            handle.flush()
            written += 1
            if now - started >= duration_s or should_stop() or (stop_file and os.path.exists(stop_file)):
                return written
            sleep(interval_s)


def daemonize():
    """Detach from the ssh session that started this process: double fork, new session, standard streams on /dev/null."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    null = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(null, descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description="One /proc sample per second of the serving process and the machine.")
    parser.add_argument("--out", default="/tmp/memsample.jsonl")
    parser.add_argument("--interval-s", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument("--duration-s", type=float, default=DEFAULT_DURATION_S)
    parser.add_argument("--stop-file", default=None, help="the sampler ends once this file exists")
    parser.add_argument("--proc-root", default="/proc")
    parser.add_argument("--daemon", action="store_true", help="detach (double fork) so the starting ssh command can return")
    args = parser.parse_args(argv)
    if args.interval_s <= 0 or args.duration_s <= 0:
        parser.error("--interval-s and --duration-s must be positive")
    if args.daemon:
        daemonize()
    stopping = []
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stopping.append(True))
    run(args.out, interval_s=args.interval_s, duration_s=args.duration_s, stop_file=args.stop_file, proc_root=args.proc_root,
        should_stop=lambda: bool(stopping))
    return 0


if __name__ == "__main__":
    sys.exit(main())
