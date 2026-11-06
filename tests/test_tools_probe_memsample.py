"""tools/probe/memsample.py: the /proc sampler that runs ON the W1b machine (council 7, decision 1, step 3).

Nothing here needs Linux: ``/proc`` is a fake tree in ``tmp_path`` (``--proc-root``), the clock and sleep are counters. What is
pinned: the /proc parsers; which process counts as the serving process (the exact ``-m semigraph.serve.drain`` words, a forked
child that inherits them is not taken, the sampler itself never, a pid that vanishes between the scan and the read is skipped);
the shape of the header and of every sample line (exactly the keys the driver reads); that the pid is resolved again on every
sample (a replaced process shows as a new pid, a dead one as null); that every line is flushed as it is written; the three ways
the sampler ends by itself (duration, stop file, SIGTERM flag); the double-fork detach; that the file runs as text under ``exec``
inside ``python -c`` (which is how the driver ships it) and imports the standard library only.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.probe import memsample  # noqa: E402

MODULE = ROOT / "tools" / "probe" / "memsample.py"
SERVER = ["python", "-m", "semigraph.serve.drain"]
MEMINFO = "MemTotal:        4000000 kB\nMemFree:          100000 kB\nMemAvailable:    3000000 kB\nSwapTotal:       1048576 kB\nSwapFree:        1000000 kB\n"
VMSTAT = "nr_free_pages 100\noom_kill 2\npgfault 5\n"
CPUINFO = "processor\t: 0\nmodel name\t: x\n\nprocessor\t: 1\nmodel name\t: x\n"


def status_text(hwm=1_500_000, rss=1_400_000, ppid=1):
    return f"Name:\tpython\nPPid:\t{ppid}\nVmHWM:\t {hwm} kB\nVmRSS:\t {rss} kB\nThreads:\t4\n"


def make_proc(root: Path, *, procs=None, meminfo=MEMINFO, vmstat=VMSTAT, cpuinfo=CPUINFO) -> Path:
    """``procs``: ``{pid: (argv, status text or None)}``; a None status leaves the file out (a process that just exited)."""
    root.mkdir(parents=True, exist_ok=True)
    for name, text in (("meminfo", meminfo), ("vmstat", vmstat), ("cpuinfo", cpuinfo)):
        if text is not None:
            (root / name).write_text(text, encoding="utf-8")
    for pid, (argv, status) in (procs or {}).items():
        folder = root / str(pid)
        folder.mkdir(exist_ok=True)
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        if status is not None:
            (folder / "status").write_text(status, encoding="utf-8")
    return root


# --- parsers ---------------------------------------------------------------------------------------------------------------------

def test_the_kb_parser_reads_meminfo_and_status_lines_and_skips_everything_else():
    values = memsample.parse_kb_lines(MEMINFO + "HugePages_Total:       0\nBogus: abc kB\n")
    assert values["MemAvailable"] == 3_000_000 and values["SwapTotal"] == 1_048_576 and values["HugePages_Total"] == 0
    assert "Bogus" not in values
    assert memsample.parse_kb_lines(status_text(hwm=7))["VmHWM"] == 7
    assert memsample.parse_kb_lines(None) == {} and memsample.parse_kb_lines("") == {}


def test_the_oom_counter_and_the_parent_pid_parsers():
    assert memsample.parse_oom_kill(VMSTAT) == 2 and memsample.parse_oom_kill("pgfault 5\n") is None
    assert memsample.parse_oom_kill(None) is None and memsample.parse_oom_kill("oom_kill x\n") is None
    assert memsample.parse_ppid(status_text(ppid=42)) == 42 and memsample.parse_ppid("Name: x\n") is None


@pytest.mark.parametrize("argv, expected", [
    (["python", "-m", "semigraph.serve.drain"], True),
    (["/usr/local/bin/python", "-u", "-m", "semigraph.serve.drain", "--port", "8080"], True),
    (["sh", "-c", "python -m semigraph.serve.drain"], False),          # one word: a shell wrapper, not the service
    (["python", "-m", "semigraph.serve.main"], False),
    (["python", "semigraph.serve.drain"], False),
    (["python", "-m"], False), ([], False)])
def test_only_the_service_s_module_run_counts_as_the_serving_process(argv, expected):
    assert memsample.is_server_argv(argv) is expected


# --- which process --------------------------------------------------------------------------------------------------------------

def test_the_serving_process_is_found_by_its_arguments_and_directories_that_are_not_pids_are_ignored(tmp_path):
    root = make_proc(tmp_path / "p", procs={10: (["sleep", "infinity"], status_text()), 20: (SERVER, status_text(hwm=1_600_000))})
    (root / "self").mkdir()
    (root / "self" / "cmdline").write_bytes(b"python\0-m\0semigraph.serve.drain\0")       # not a pid: never read
    assert memsample.find_server_pid(str(root)) == 20
    assert memsample.read_server(str(root)) == (20, memsample.parse_kb_lines(status_text(hwm=1_600_000)))


def test_a_forked_child_that_inherits_the_arguments_is_not_the_serving_process(tmp_path):
    root = make_proc(tmp_path / "p", procs={20: (SERVER, status_text(ppid=1)), 21: (SERVER, status_text(ppid=20)),
                                            22: (SERVER, status_text(ppid=21))})
    assert memsample.find_server_pid(str(root)) == 20


def test_the_sampler_never_takes_its_own_pid_and_no_match_is_none(tmp_path):
    root = make_proc(tmp_path / "p", procs={30: (SERVER, status_text())})
    assert memsample.find_server_pid(str(root), exclude={30}) is None
    assert memsample.find_server_pid(str(tmp_path / "missing")) is None


def test_a_pid_that_vanishes_between_the_scan_and_the_read_is_skipped_not_an_error(tmp_path):
    root = make_proc(tmp_path / "p", procs={40: (SERVER, None)})              # cmdline there, status gone
    assert memsample.find_server_pid(str(root)) == 40
    assert memsample.read_server(str(root)) == (None, {})
    line = memsample.sample(str(root), 5.0)
    assert line["uvicorn_pid"] is None and line["vmhwm_kb"] is None and line["mem_available_kb"] == 3_000_000


# --- the lines --------------------------------------------------------------------------------------------------------------------

SAMPLE_KEYS = {"t", "mem_available_kb", "swap_free_kb", "swap_total_kb", "uvicorn_pid", "vmrss_kb", "vmhwm_kb", "oom_kill"}


def test_a_sample_has_exactly_the_keys_the_driver_reads_and_the_values_of_the_tree(tmp_path):
    root = make_proc(tmp_path / "p", procs={20: (SERVER, status_text(hwm=1_700_000, rss=1_650_000))})
    line = memsample.sample(str(root), 1234.5678)
    assert set(line) == SAMPLE_KEYS
    assert line == {"t": 1234.568, "mem_available_kb": 3_000_000, "swap_free_kb": 1_000_000, "swap_total_kb": 1_048_576,
                    "uvicorn_pid": 20, "vmrss_kb": 1_650_000, "vmhwm_kb": 1_700_000, "oom_kill": 2}


def test_missing_kernel_files_give_nulls_and_never_raise(tmp_path):
    root = make_proc(tmp_path / "p", procs={}, meminfo=None, vmstat=None, cpuinfo=None)
    assert memsample.sample(str(root), 1.0) == {"t": 1.0, "mem_available_kb": None, "swap_free_kb": None, "swap_total_kb": None,
                                                 "uvicorn_pid": None, "vmrss_kb": None, "vmhwm_kb": None, "oom_kill": None}
    assert memsample.header(str(root), 1.0, 1.0)["header"]["cpus"] is None


def test_the_header_names_memory_swap_cpus_and_the_interval(tmp_path):
    root = make_proc(tmp_path / "p")
    head = memsample.header(str(root), 100.0, 1.0)["header"]
    assert (head["mem_total_kb"], head["swap_total_kb"], head["cpus"], head["interval_s"]) == (4_000_000, 1_048_576, 2, 1.0)
    assert head["sampler_pid"] == os.getpid()


# --- run, flushing, and the three ends -------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def read_lines(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_writes_a_header_then_one_sample_per_interval_until_the_duration_passed(tmp_path):
    root, out, clock = make_proc(tmp_path / "p", procs={20: (SERVER, status_text())}), tmp_path / "m.jsonl", Clock()
    written = memsample.run(str(out), interval_s=1.0, duration_s=3.0, proc_root=str(root), clock=clock, sleep=clock.sleep)
    lines = read_lines(out)
    assert written == 4 and "header" in lines[0] and len(lines) == 5          # samples at t = 0, 1, 2, 3 s
    assert [row["t"] for row in lines[1:]] == [1000.0, 1001.0, 1002.0, 1003.0]
    assert all(set(row) == SAMPLE_KEYS for row in lines[1:])


def test_every_line_is_flushed_when_it_is_written(tmp_path):
    root, out, clock = make_proc(tmp_path / "p"), tmp_path / "m.jsonl", Clock()
    seen = []

    def sleeping(seconds):
        seen.append(len(out.read_text(encoding="utf-8").splitlines()))          # what a reader sees mid-run
        clock.sleep(seconds)

    memsample.run(str(out), interval_s=1.0, duration_s=2.0, proc_root=str(root), clock=clock, sleep=sleeping)
    assert seen == [2, 3]                                                         # header + sample, then header + two samples


def test_run_ends_when_the_stop_file_appears(tmp_path):
    root, out, stop, clock = make_proc(tmp_path / "p"), tmp_path / "m.jsonl", tmp_path / "stop", Clock()

    def sleeping(seconds):
        clock.sleep(seconds)
        if clock() >= 1002.0:
            stop.write_text("", encoding="utf-8")

    written = memsample.run(str(out), interval_s=1.0, duration_s=1000.0, stop_file=str(stop), proc_root=str(root), clock=clock,
                            sleep=sleeping)
    assert written == 3 and len(read_lines(out)) == 4                           # the sample taken as the file appeared is the last


def test_run_ends_on_the_stop_flag_a_signal_sets(tmp_path):
    root, out, clock, flag = make_proc(tmp_path / "p"), tmp_path / "m.jsonl", Clock(), []
    written = memsample.run(str(out), interval_s=1.0, duration_s=1000.0, proc_root=str(root), clock=clock,
                            sleep=lambda s: (clock.sleep(s), flag.append(True) if clock() >= 1001.0 else None), should_stop=lambda: bool(flag))
    assert written == 2


def test_the_pid_is_resolved_on_every_sample_so_a_replacement_and_a_death_show(tmp_path):
    root, out, clock = make_proc(tmp_path / "p", procs={20: (SERVER, status_text(hwm=1_100_000))}), tmp_path / "m.jsonl", Clock()

    def sleeping(seconds):
        clock.sleep(seconds)
        if clock() == 1001.0:                                                    # the process is replaced
            for name in ("cmdline", "status"):
                (root / "20" / name).unlink()
            (root / "20").rmdir()
            make_proc(root, procs={21: (SERVER, status_text(hwm=900_000))})
        if clock() == 1003.0:                                                    # and then it is gone
            for name in ("cmdline", "status"):
                (root / "21" / name).unlink()
            (root / "21").rmdir()

    memsample.run(str(out), interval_s=1.0, duration_s=4.0, proc_root=str(root), clock=clock, sleep=sleeping)
    pids = [row["uvicorn_pid"] for row in read_lines(out)[1:]]
    assert pids == [20, 21, 21, None, None]


# --- main, the detach, the shipping -------------------------------------------------------------------------------------------------

def test_main_runs_with_real_time_and_rejects_nonsense_options(tmp_path):
    root, out = make_proc(tmp_path / "p", procs={20: (SERVER, status_text())}), tmp_path / "m.jsonl"
    assert memsample.main(["--out", str(out), "--proc-root", str(root), "--duration-s", "0.2", "--interval-s", "0.05"]) == 0
    assert len(read_lines(out)) >= 3
    for bad in (["--interval-s", "0"], ["--duration-s", "-1"]):
        with pytest.raises(SystemExit):
            memsample.main(["--out", str(out), *bad])


def test_daemon_mode_detaches_with_a_double_fork_and_a_new_session(tmp_path, monkeypatch):
    calls, forks = [], iter([1234, 0, 5678, 0])        # the first fork returns in the parent (exits), the child's returns 0, ...

    class Exited(BaseException):
        pass

    def fake_fork():
        pid = next(forks)
        calls.append(("fork", pid))
        return pid

    def fake_exit(code):
        calls.append(("exit", code))
        raise Exited()

    monkeypatch.setattr(os, "fork", fake_fork, raising=False)
    monkeypatch.setattr(os, "setsid", lambda: calls.append(("setsid",)), raising=False)
    monkeypatch.setattr(os, "dup2", lambda a, b: calls.append(("dup2", b)))
    monkeypatch.setattr(os, "_exit", fake_exit)
    with pytest.raises(Exited):                         # the first parent leaves at once: the ssh command can return
        memsample.daemonize()
    assert calls == [("fork", 1234), ("exit", 0)]
    calls.clear()
    forks = iter([0, 0])                                 # the child path: a new session, a second fork, streams on /dev/null
    memsample.daemonize()
    assert calls[:3] == [("fork", 0), ("setsid",), ("fork", 0)] and [c for c in calls if c[0] == "dup2"] == [("dup2", 0), ("dup2", 1), ("dup2", 2)]


def test_the_file_runs_as_text_under_exec_inside_python_dash_c_the_way_the_driver_ships_it(tmp_path):
    from tools.probe import w1b
    root, out = make_proc(tmp_path / "p", procs={20: (SERVER, status_text(hwm=1_234_567))}), tmp_path / "m.jsonl"
    words = __import__("shlex").split(w1b.sampler_command(w1b.memsample_source(), duration_s=1))
    program, blob = words[words.index("-c") + 1], words[words.index("-c") + 2]
    done = subprocess.run([sys.executable, "-c", program, blob, "--out", str(out), "--duration-s", "0.3", "--interval-s", "0.1",
                           "--proc-root", str(root), "--stop-file", str(tmp_path / "never")], capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr.decode()
    rows = read_lines(out)
    assert "header" in rows[0] and rows[1]["vmhwm_kb"] == 1_234_567 and rows[1]["uvicorn_pid"] == 20


def test_the_sampler_is_stdlib_only_and_never_reads_its_own_path():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= {"argparse", "json", "os", "signal", "sys", "time"}, imported
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "__file__" not in names                  # under exec in `python -c` there is no file to read
