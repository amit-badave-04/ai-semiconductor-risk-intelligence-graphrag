"""A sandboxed upload worker can start no process or thread, gains no added privilege on exec, and its process group is cleaned up
(Opus security review of the H1 diff, 2026-11-07, and the round-5 follow-ups).

The layers, in the order ``apply_sandbox_limits`` sets them:

- ``prctl(PR_SET_NO_NEW_PRIVS, 1)``, first and fail closed: no exec adds privilege,
  so the limits below mean what they say.
- ``prctl(PR_SET_DUMPABLE, 0)``, fail closed: no core dump and no ptrace attach, as the API process is already marked.
- ``RLIMIT_NPROC`` (0, 0): every later ``fork`` or ``clone`` fails with ``EAGAIN``, threads included, and the hard limit is lowered
  too, so the worker cannot raise it. Linux exempts only ``root`` and ``CAP_SYS_RESOURCE`` / ``CAP_SYS_ADMIN``; the image runs as ``app``.
- The hard limits of ``RLIMIT_AS`` and ``RLIMIT_CPU`` come down to their caps, so the worker cannot raise those either.
- Cleanup: the worker starts in its own session; its process group is killed on the timeout, output-overflow, wait-timeout and
  clean-exit paths and when a stdin or stdout thread cannot start; an ``atexit`` handler kills the groups of the workers still
  running when the server process exits.

What runs where: the argument checks (fake ``resource``, fake ``libc``, recorded ``Popen`` and ``killpg``, a fake ``os.waitid``) run on
every platform; the tests that need the kernel to enforce a limit or to deliver a group kill are Linux-only, and the ones that fork
are skipped when the process is exempt from ``RLIMIT_NPROC`` (root, or holding ``CAP_SYS_RESOURCE`` / ``CAP_SYS_ADMIN``). CI runs the
Linux-only ones in a step that fails on any skip (``.github/workflows/tests.yml``, job ``serve-shipped``).
"""

from __future__ import annotations

import errno
import io
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import upload_fixtures as fx  # noqa: E402

from semigraph.uploads import compare, parse, sandbox  # noqa: E402
from semigraph.uploads import units as U  # noqa: E402

SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")
TESTS_DIR = str(Path(__file__).resolve().parent)
KILL = 9                    # what signal.SIGKILL is on Linux; the Windows runs stand it in so the call can be checked there too
CAP_SYS_ADMIN, CAP_SYS_RESOURCE = 21, 24


def capability_mask(field: str) -> int:
    """``CapEff`` / ``CapBnd`` of this process as an int; 0 where /proc is absent."""
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
    except OSError:
        return 0
    return next((int(line.split()[1], 16) for line in status.splitlines() if line.startswith(field + ":")), 0)


def holds_resource_capability(mask: int) -> bool:
    return bool(mask >> CAP_SYS_ADMIN & 1 or mask >> CAP_SYS_RESOURCE & 1)


def exempt_from_nproc() -> bool:
    """True where RLIMIT_NPROC is not enforced: root, or an effective CAP_SYS_RESOURCE / CAP_SYS_ADMIN."""
    return os.geteuid() == 0 or holds_resource_capability(capability_mask("CapEff"))


LINUX = sys.platform.startswith("linux")
needs_enforced_nproc = pytest.mark.skipif(
    not LINUX or (LINUX and exempt_from_nproc()),
    reason="RLIMIT_NPROC is enforced by the Linux kernel for an unprivileged user only (root and CAP_SYS_RESOURCE are exempt)")
needs_linux = pytest.mark.skipif(not LINUX, reason="process groups and /proc are Linux-only here")


# ------------------------------------------------------------------------------------------ recorded Popen and killpg

@pytest.fixture
def popen_spy(monkeypatch):
    """Real children, but every ``Popen`` keeps the keyword arguments it was given."""
    spy = types.SimpleNamespace(procs=[])
    real = subprocess.Popen

    class Spy(real):
        def __init__(self, *args, **kwargs):
            self.requested = kwargs
            super().__init__(*args, **kwargs)
            spy.procs.append(self)

    monkeypatch.setattr(subprocess, "Popen", Spy)
    return spy


@pytest.fixture
def group_kills(monkeypatch, popen_spy):
    """Replaces ``os.killpg`` (absent on Windows) with a recorder: ``(group, signal, pid of the worker, its returncode at that moment)``.
    The recorded returncode tells the ORDER: ``None`` means the worker had not been reaped yet."""
    calls: list[tuple[int, int, int, int | None]] = []

    def fake_killpg(group, sig):
        proc = popen_spy.procs[-1]
        calls.append((group, sig, proc.pid, proc.returncode))

    monkeypatch.setattr(os, "killpg", fake_killpg, raising=False)
    monkeypatch.setattr(signal, "SIGKILL", KILL, raising=False)
    return calls


@pytest.fixture
def waitid_branch(monkeypatch, group_kills):
    """The Linux clean-exit branch on every platform: ``os.waitid`` is replaced by a recorder that answers "the worker has exited and
    is not reaped yet" (what ``WNOWAIT`` gives). Each call is recorded with the number of group kills made before it."""
    for name, value in (("P_PID", 1), ("WEXITED", 4), ("WNOWAIT", 0x01000000), ("WNOHANG", 1)):
        if not hasattr(os, name):
            monkeypatch.setattr(os, name, value, raising=False)
    calls: list[tuple[int, int, int, int]] = []
    answers: list[object] = []

    def fake_waitid(idtype, pid, options):
        calls.append((idtype, pid, options, len(group_kills)))
        return answers[0] if answers else object()

    monkeypatch.setattr(os, "waitid", fake_waitid, raising=False)
    monkeypatch.setattr(sandbox, "_WAITID_BEFORE_REAP", True)
    return types.SimpleNamespace(calls=calls, answers=answers)


def run(cmd: list[str], *, timeout_s: int = 30, max_output_bytes: int = 4096) -> sandbox.SandboxResult:
    return sandbox.run_sandboxed_command(cmd, b"", timeout_s=timeout_s, max_output_bytes=max_output_bytes)


ANSWERS = [sys.executable, "-c", "import sys; sys.stdout.write('{}')"]
SLEEPS = [sys.executable, "-c", "import time; time.sleep(60)"]
FLOODS = [sys.executable, "-c", "import sys, time; sys.stdout.write('x' * 200000); sys.stdout.flush(); time.sleep(60)"]
# more than one 64 KiB read chunk: the runner's buffered read waits for a full chunk (or EOF) before it can see the overflow


def test_the_worker_is_started_in_its_own_session(popen_spy):
    run(ANSWERS)
    assert popen_spy.procs[-1].requested.get("start_new_session") is True


def test_the_exit_wait_branch_is_on_where_the_kernel_has_waitid():
    assert sandbox._WAITID_BEFORE_REAP is (LINUX and hasattr(os, "waitid"))


def test_without_waitid_a_clean_exit_kills_the_group_after_the_worker_is_reaped_so_its_exit_code_is_kept(
        monkeypatch, group_kills, popen_spy):
    """Once, with the group id equal to the worker's pid (its own session), and AFTER ``wait``: the returncode is already 0.
    Killing first would turn a clean exit into -9 and escalate ``parse._log_parse_failure`` for every upload."""
    monkeypatch.setattr(sandbox, "_WAITID_BEFORE_REAP", False)
    result = run(ANSWERS)
    proc = popen_spy.procs[-1]
    assert result.returncode == 0 and result.stdout == b"{}"
    assert group_kills == [(proc.pid, KILL, proc.pid, 0)]


def test_with_waitid_a_clean_exit_kills_the_group_while_the_worker_is_still_unreaped_and_keeps_its_exit_code(
        waitid_branch, group_kills, popen_spy):
    """``waitid(P_PID, pid, WEXITED | WNOWAIT | WNOHANG)`` first: the exited worker stays an unreaped zombie, which holds its pid as
    the group id, so the group kill cannot reach a different group. Then ``wait`` reaps it: the exit code is the worker's own (0)."""
    result = run(ANSWERS)
    proc = popen_spy.procs[-1]
    assert result.returncode == 0 and result.stdout == b"{}"
    assert waitid_branch.calls == [(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG, 0)]
    assert group_kills == [(proc.pid, KILL, proc.pid, None)]


def test_a_worker_that_never_reports_its_exit_is_killed_after_the_bounded_wait(monkeypatch, waitid_branch, group_kills, popen_spy):
    """``waitid`` with ``WNOHANG`` answers ``None`` while the worker is still running: the wait is bounded, then the worker is killed."""
    monkeypatch.setattr(sandbox, "_PROC_WAIT_JOIN_S", 0.3)
    waitid_branch.answers.append(None)
    start = time.monotonic()
    result = run(ANSWERS)
    assert time.monotonic() - start < 20
    proc = popen_spy.procs[-1]
    assert result.stdout == b"{}"
    assert len(waitid_branch.calls) >= 2          # it polled, it did not give up on the first answer
    assert group_kills == [(proc.pid, KILL, proc.pid, None)]


def test_a_timeout_kills_the_group_before_the_worker_is_reaped(group_kills, popen_spy):
    start = time.monotonic()
    with pytest.raises(sandbox.SandboxTimeout):
        run(SLEEPS, timeout_s=1)
    proc = popen_spy.procs[-1]
    assert time.monotonic() - start < 30, "the worker was not killed at the timeout"
    assert group_kills == [(proc.pid, KILL, proc.pid, None)]


def test_an_output_overflow_kills_the_group_before_the_worker_is_reaped(group_kills, popen_spy):
    start = time.monotonic()
    with pytest.raises(sandbox.SandboxOutputTooLarge):
        run(FLOODS, max_output_bytes=100)
    proc = popen_spy.procs[-1]
    assert time.monotonic() - start < 30, "the worker was not killed when its output overflowed"
    assert group_kills == [(proc.pid, KILL, proc.pid, None)]


@pytest.mark.parametrize("error", [ProcessLookupError, PermissionError, OSError])
def test_a_failing_group_kill_changes_nothing_the_caller_sees(monkeypatch, popen_spy, error):
    """The group is usually already empty (ESRCH) when the worker exited normally; that must not surface."""
    def refuses(group, sig):
        raise error()

    monkeypatch.setattr(os, "killpg", refuses, raising=False)
    monkeypatch.setattr(signal, "SIGKILL", KILL, raising=False)
    assert run(ANSWERS).stdout == b"{}"
    with pytest.raises(sandbox.SandboxTimeout):
        run(SLEEPS, timeout_s=1)               # the leader is still killed on its own when the group kill fails


def kill_group_with(monkeypatch, error):
    def refuses(group, sig):
        raise error

    monkeypatch.setattr(os, "killpg", refuses, raising=False)
    monkeypatch.setattr(signal, "SIGKILL", KILL, raising=False)
    sandbox._kill_group(types.SimpleNamespace(pid=4242))


def test_a_group_that_is_already_empty_is_not_logged(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG, logger="semigraph.uploads.sandbox"):
        kill_group_with(monkeypatch, ProcessLookupError(errno.ESRCH, "No such process"))
    assert caplog.records == []


@pytest.mark.parametrize("code", [errno.EPERM, errno.EINVAL])
def test_any_other_group_kill_error_is_logged_at_warning_with_its_errno_and_nothing_else(monkeypatch, caplog, code):
    with caplog.at_level(logging.DEBUG, logger="semigraph.uploads.sandbox"):
        kill_group_with(monkeypatch, OSError(code, "DOCUMENT-TEXT-MUST-NOT-APPEAR"))
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert f"errno={code}" in record.getMessage()
    assert "DOCUMENT-TEXT" not in record.getMessage() and record.exc_info is None


def test_a_platform_without_process_groups_still_runs_and_kills(monkeypatch, popen_spy):
    monkeypatch.delattr(os, "killpg", raising=False)
    assert run(ANSWERS).stdout == b"{}"
    with pytest.raises(sandbox.SandboxTimeout):
        run(SLEEPS, timeout_s=1)


# ------------------------------------------- a stdin or stdout thread that cannot start, and the registry of live workers

@pytest.fixture
def thread_start_fails(monkeypatch):
    """Makes ``Thread.start`` raise for the sandbox's own thread whose target is named ``target_name`` (and only that one)."""
    real_start = threading.Thread.start

    def install(target_name: str) -> None:
        def start(self):
            if getattr(self, "_target", None) is not None and self._target.__name__ == target_name:
                raise RuntimeError("can't start new thread")
            return real_start(self)

        monkeypatch.setattr(threading.Thread, "start", start)

    return install


@pytest.mark.parametrize("target_name", ["_write_stdin", "_read"])
def test_a_thread_that_cannot_start_after_the_worker_is_spawned_kills_the_worker_group(
        group_kills, popen_spy, thread_start_fails, target_name):
    thread_start_fails(target_name)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        run(SLEEPS, timeout_s=30)
    proc = popen_spy.procs[-1]
    try:
        assert group_kills == [(proc.pid, KILL, proc.pid, None)]
        assert proc.poll() is not None, "the worker was left running"
        assert proc not in sandbox._LIVE_WORKERS
    finally:
        proc.kill()


@pytest.mark.parametrize("command,outcome", [(ANSWERS, None), (SLEEPS, sandbox.SandboxTimeout), (FLOODS, sandbox.SandboxOutputTooLarge)])
def test_a_worker_is_registered_only_while_its_job_runs(monkeypatch, popen_spy, command, outcome):
    seen: list[bool] = []
    real_write = sandbox._write_stdin

    def capture(proc, data):
        seen.append(proc in sandbox._LIVE_WORKERS)
        real_write(proc, data)

    monkeypatch.setattr(sandbox, "_write_stdin", capture)
    if outcome is None:
        run(command)
    else:
        with pytest.raises(outcome):
            run(command, timeout_s=1, max_output_bytes=100)
    assert seen == [True]
    assert popen_spy.procs[-1] not in sandbox._LIVE_WORKERS


class _FakeWorker:
    def __init__(self, pid: int, returncode: int | None):
        self.pid, self.returncode = pid, returncode


def test_the_exit_handler_kills_the_group_of_every_running_worker_and_leaves_finished_ones_alone(monkeypatch):
    kills: list[tuple[int, int]] = []
    running, finished = _FakeWorker(111, None), _FakeWorker(222, 0)
    monkeypatch.setattr(sandbox, "_LIVE_WORKERS", {running, finished})
    monkeypatch.setattr(os, "killpg", lambda group, sig: kills.append((group, sig)), raising=False)
    monkeypatch.setattr(signal, "SIGKILL", KILL, raising=False)
    monkeypatch.setattr(_FakeWorker, "kill", lambda self: None, raising=False)
    sandbox._kill_live_workers_at_exit()
    assert kills == [(111, KILL)]


def test_the_exit_handler_is_registered_with_atexit_when_the_module_is_imported():
    code = ("import atexit, json, sys; seen = []; atexit.register = lambda f, *a, **k: seen.append(f.__name__) or f; "
            "sys.path.insert(0, sys.argv[1]); import semigraph.uploads.sandbox; print(json.dumps(seen))")
    done = subprocess.run([sys.executable, "-I", "-c", code, SRC_DIR], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert "_kill_live_workers_at_exit" in json.loads(done.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- a real group kill (Linux)

LEAVES_A_CHILD = textwrap.dedent("""
    import os, sys, time
    pidfile, mode = sys.argv[1], sys.argv[2]
    if os.fork() == 0:
        null = os.open(os.devnull, os.O_RDWR)          # a leftover that lets go of the worker's pipes, so the job sees EOF
        os.dup2(null, 0)
        os.dup2(null, 1)
        with open(pidfile, "w") as f:
            f.write(str(os.getpid()))
        time.sleep(120)
        os._exit(0)
    while not (os.path.exists(pidfile) and open(pidfile).read()):
        time.sleep(0.01)
    if mode == "hang":
        time.sleep(120)
    sys.stdout.write("{}")
""")


def is_running(pid: int) -> bool:
    """Alive and not a zombie (a killed orphan stays a zombie until its new parent reaps it, and ``kill(pid, 0)`` still succeeds)."""
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return False
    return stat_line.rsplit(")", 1)[1].split()[0] != "Z"


@needs_linux
@pytest.mark.parametrize("mode,outcome", [("exit", None), ("hang", sandbox.SandboxTimeout)])
def test_a_process_left_in_the_workers_group_ends_with_the_job(tmp_path, mode, outcome):
    """No monkeypatching: the worker forks a child that sleeps for two minutes, then either exits normally or hangs until the
    timeout. The job's own cleanup takes the child with it (the clean-exit path here is the real ``waitid`` branch)."""
    pidfile = tmp_path / "child.pid"
    cmd = [sys.executable, "-c", LEAVES_A_CHILD, str(pidfile), mode]
    if outcome is None:
        assert run(cmd).stdout == b"{}"
    else:
        with pytest.raises(outcome):
            run(cmd, timeout_s=5)           # long enough for a slow runner to get the forked child to write its pid file
    child = int(pidfile.read_text(encoding="ascii"))
    deadline = time.monotonic() + 10
    while is_running(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not is_running(child), "the child the worker forked was still running after the job's cleanup"


@needs_linux
def test_a_worker_still_running_when_the_server_process_exits_is_killed_by_the_exit_handler():
    """A fresh interpreter registers a sleeping worker the way ``run_sandboxed_command`` does and exits normally."""
    code = textwrap.dedent("""
        import subprocess, sys
        sys.path.insert(0, sys.argv[1])
        from semigraph.uploads import sandbox
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
        sandbox._LIVE_WORKERS.add(worker)
        print(worker.pid, flush=True)
    """)
    done = subprocess.run([sys.executable, "-I", "-c", code, SRC_DIR], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    pid = int(done.stdout.strip().splitlines()[-1])
    deadline = time.monotonic() + 10
    while is_running(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        assert not is_running(pid), "the worker was still running after the process that started it exited"
    finally:
        if is_running(pid):
            os.kill(pid, KILL)


# --------------------------------------------------------------- the kernel enforces the limits (Linux, unprivileged)

PROBE = textwrap.dedent("""
    import ctypes, errno, json, os, resource, sys, threading
    sys.path.insert(0, sys.argv[1])
    from semigraph.uploads.sandbox import apply_sandbox_limits
    if sys.argv[2] == "limited":
        assert apply_sandbox_limits() is True
    out = {}
    try:
        pid = os.fork()
    except BlockingIOError as exc:
        out["fork"] = errno.errorcode[exc.errno]
    else:
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        out["fork"] = "allowed"
    try:
        threading.Thread(target=lambda: None).start()
    except RuntimeError:
        out["thread"] = "refused"
    else:
        out["thread"] = "allowed"
    out["limits"] = {name: list(resource.getrlimit(getattr(resource, name))) for name in ("RLIMIT_NPROC", "RLIMIT_AS", "RLIMIT_CPU")}
    wider = {"RLIMIT_NPROC": (10**6, 10**6), "RLIMIT_AS": (2 * 1024**3, 2 * 1024**3), "RLIMIT_CPU": (10**6, 10**6)}
    for name, limits in wider.items():
        try:
            resource.setrlimit(getattr(resource, name), limits)
        except (ValueError, OSError):
            out["raise_" + name] = "refused"
        else:
            out["raise_" + name] = "raised"
    with open("/proc/self/status", encoding="ascii") as f:
        out["no_new_privs"] = next(line.split()[1] for line in f if line.startswith("NoNewPrivs:"))
    out["dumpable"] = ctypes.CDLL(None).prctl(3, 0, 0, 0, 0)          # PR_GET_DUMPABLE
    print(json.dumps(out))
""")


def probe(mode: str) -> dict:
    done = subprocess.run([sys.executable, "-I", "-c", PROBE, SRC_DIR, mode], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@needs_enforced_nproc
def test_after_the_limits_a_worker_can_start_no_process_and_no_thread_and_cannot_raise_a_limit():
    seen = probe("limited")
    assert seen["fork"] == "EAGAIN" and seen["thread"] == "refused"
    assert seen["limits"] == {"RLIMIT_NPROC": [0, 0], "RLIMIT_AS": [1024 ** 3, 1024 ** 3], "RLIMIT_CPU": [120, 120]}
    assert {k: v for k, v in seen.items() if k.startswith("raise_")} == {
        "raise_RLIMIT_NPROC": "refused", "raise_RLIMIT_AS": "refused", "raise_RLIMIT_CPU": "refused"}


@needs_linux
def test_after_the_limits_a_worker_gains_no_privilege_on_exec_and_is_not_dumpable():
    """``/proc/self/status`` shows ``NoNewPrivs: 1`` and ``PR_GET_DUMPABLE`` answers 0 inside a worker that has applied its limits."""
    seen = probe("limited")
    assert seen["no_new_privs"] == "1"
    assert seen["dumpable"] == 0


@needs_enforced_nproc
def test_the_control_without_the_limits_the_same_probe_forks_and_starts_threads():
    """So the test above fails for the reason it claims to: this account can fork and start threads when nothing stops it."""
    seen = probe("unlimited")
    assert seen["fork"] == "allowed" and seen["thread"] == "allowed"


# ------------------------------------- a worker gains no added privilege on exec, through the system sg helper (Linux, unprivileged)

SG = shutil.which("sg") or "/usr/bin/sg"


def is_privileged_helper(path: str) -> bool:
    try:
        info = os.stat(os.path.realpath(path))
    except OSError:
        return False
    return info.st_uid == 0 and bool(info.st_mode & stat.S_ISUID)


needs_privileged_sg = pytest.mark.skipif(
    not LINUX or exempt_from_nproc() or not holds_resource_capability(capability_mask("CapBnd")) or not is_privileged_helper(SG),
    reason="Linux, unprivileged, CAP_SYS_RESOURCE or CAP_SYS_ADMIN in the bounding set, and a root-owned privileged sg")


@needs_privileged_sg
def test_the_control_sg_runs_a_command_for_this_account_without_a_password():
    """So the test below cannot pass merely because ``sg`` refused to run at all."""
    import grp

    group = grp.getgrgid(os.getgid()).gr_name
    done = subprocess.run([SG, group, "-c", "echo ok"], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and done.stdout.strip() == "ok", (done.returncode, done.stdout, done.stderr)


@needs_privileged_sg
def test_a_limited_worker_gains_no_added_privilege_on_exec_through_sg(tmp_path):
    """The worker applies its limits and replaces itself with ``sg``, which runs a command. With no added privilege on exec the
    command runs in the worker's own process (same pid) and ``NoNewPrivs`` was 1 before the exec. The command MUST run: a missing
    pid file fails the test (it is never a silent pass), and nothing is left running when the job ends."""
    worker_pid_file, command_pid_file = tmp_path / "worker.pid", tmp_path / "command.pid"
    status_file = tmp_path / "status.txt"
    inner = tmp_path / "inner.py"
    inner.write_text(f"import os\nopen({str(command_pid_file)!r}, 'w').write(str(os.getpid()))\n", encoding="utf-8")
    worker = textwrap.dedent("""
        import grp, os, sys
        sys.path.insert(0, sys.argv[1])
        from semigraph.uploads.sandbox import apply_sandbox_limits
        assert apply_sandbox_limits() is True
        open(sys.argv[4], "w").write(str(os.getpid()))
        open(sys.argv[5], "w").write(next(l for l in open("/proc/self/status") if l.startswith("NoNewPrivs")))
        group = grp.getgrgid(os.getgid()).gr_name
        os.execv(sys.argv[2], [sys.argv[2], group, "-c", f"exec {sys.executable} -I {sys.argv[3]}"])
    """)
    sandbox.run_sandboxed_command([sys.executable, "-I", "-c", worker, SRC_DIR, SG, str(inner), str(worker_pid_file), str(status_file)], b"",
                                  timeout_s=20, max_output_bytes=4096)
    deadline = time.monotonic() + 10
    while not command_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert status_file.read_text().split()[-1] == "1"
    assert command_pid_file.exists(), "sg did not run the command, so nothing was proven"
    assert command_pid_file.read_text() == worker_pid_file.read_text(), "the command ran in a process of its own"
    assert not is_running(int(command_pid_file.read_text()))


# ------------------------------------------------------ the premise: the real workers need no process and no thread

def view_dict(text: str) -> dict:
    """The ``VersionView`` shape ``uploads.jobs`` sends the comparison worker, built through the real parse and unit code."""
    doc = parse.parse_document(text.encode("utf-8"), "md")
    unit_list = U.detect_units(doc.blocks, "md")
    canonical = U.canonical_text(doc.blocks)
    chunks = U.chunk_units(canonical, unit_list, count_tokens=lambda t: max(1, len(t) // 3), max_tokens=512,
                           target_chars=1200, max_chars=1800)
    return {"text": canonical,
            "units": [{"unit_id": u.unit_id, "kind": u.kind, "headline": u.headline,
                      "char_start": u.char_start, "char_end": u.char_end} for u in unit_list],
            "chunk_spans": [[f"c{c.seq}", c.char_start, c.char_end] for c in chunks],
            "method": doc.method, "chars_per_page": doc.chars_per_page}


def test_a_real_parse_of_each_binary_kind_and_a_real_comparison_succeed_under_the_limits():
    """Whole pipeline through the real ``python -s -P -m`` children, whose ``__main__`` guards apply the limits first. On Linux, as
    an unprivileged user, that is the proof the workers need no new process or thread; elsewhere it shows the new session flag
    does not break them. (The 30-page and max-size runs under the limits are the two RLIMIT_AS tests in ``test_serve_upload_*``.)"""
    pdf = parse.parse_document(fx.simple_pdf(), "pdf")
    docx = parse.parse_document(fx.docx_with_table_and_bold_heading(), "docx")
    assert pdf.blocks and docx.blocks
    report = compare.compare_in_subprocess(view_dict(fx.MD_V1), view_dict(fx.MD_V2), timeout_s=60)
    assert report["items_compared"] is True, report["not_compared_reason"]


def test_the_parse_worker_loads_no_numpy_and_starts_no_thread():
    """The premise of ``RLIMIT_NPROC`` (0, 0) for the parse worker: nothing on any of its paths may import numpy, whose OpenBLAS would
    try to start a thread pool. Every path is run: the PDF text layer, the PDF fallback (a sparse and a blank PDF, which end in
    ``scanned`` and ``empty``), DOCX, HTML, Markdown and plain text. The Python thread count is checked everywhere, and on Linux the
    kernel's own task count for the process too (it sees a native thread that ``threading`` does not). The parse worker's environment
    pins every BLAS backend to one thread as well (``parse._child_env``), the second line of defence."""
    script = textwrap.dedent("""
        import io, json, os, sys, threading, types
        sys.path[:0] = [sys.argv[1], sys.argv[2]]
        import upload_fixtures as fx
        from semigraph.uploads import parse_worker
        samples = (("pdf", fx.simple_pdf(), None), ("docx", fx.docx_with_table_and_bold_heading(), None),
                   ("pdf", fx.sparse_pdf(3), "scanned"), ("pdf", fx.blank_pdf(2), "empty"),
                   ("html", fx.HTML_SAMPLE.encode("utf-8"), None), ("md", fx.MD_V1.encode("utf-8"), None),
                   ("txt", b"A plain text document. " * 40, None))
        for kind, data, expected in samples:
            real_in, real_out = sys.stdin, sys.stdout
            out = io.BytesIO()
            sys.stdin, sys.stdout = types.SimpleNamespace(buffer=io.BytesIO(data)), types.SimpleNamespace(buffer=out)
            try:
                rc = parse_worker.main(["worker", kind, "30"])
            finally:
                sys.stdin, sys.stdout = real_in, real_out
            reply = json.loads(out.getvalue())
            assert reply.get("error") == expected, (kind, reply.get("error"))
            assert rc == (0 if expected is None else 1), (kind, rc)
        tasks = len(os.listdir("/proc/self/task")) if sys.platform.startswith("linux") else None
        print(json.dumps({"numpy": "numpy" in sys.modules, "threads": threading.active_count(), "tasks": tasks}))
    """)
    done = subprocess.run([sys.executable, "-I", "-c", script, SRC_DIR, TESTS_DIR], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"numpy": False, "threads": 1, "tasks": 1 if LINUX else None}


# --------------------------------------------------- the comparison worker: diagnosable refusals, and the wall timeout margin

def test_the_comparison_worker_reports_an_interrupt_by_class_name_and_lets_a_deliberate_exit_through(monkeypatch):
    """Same rule as the parse worker: ``KeyboardInterrupt`` (what a refused thread start inside a native library can end in) is
    reported as ``comparison_failed`` with its class name; ``SystemExit`` is not swallowed."""
    from semigraph.uploads import compare_worker

    def run_main(failure):
        def raises(view):
            raise failure

        monkeypatch.setattr(compare_worker, "_version_view_from_dict", raises)
        monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b'{"older": {}, "newer": {}}')))
        out = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=out))
        return compare_worker.main(["compare_worker"]), out.getvalue()

    rc, written = run_main(KeyboardInterrupt())
    assert rc == 1 and json.loads(written) == {"error": "comparison_failed", "exc_type": "KeyboardInterrupt"}
    with pytest.raises(SystemExit):
        run_main(SystemExit(3))


def test_the_comparison_wall_timeout_is_below_the_cpu_cap_by_a_margin():
    """One thread cannot use more CPU seconds than wall seconds, so at equal values the CPU cap's SIGKILL and the wall timeout race and a
    CPU-bound comparison reports a failure instead of a timeout. A margin of seconds keeps the wall timeout first."""
    from semigraph.config import Settings

    wall = Settings.model_fields["upload_compare_timeout_s"].default
    assert sandbox._RLIMIT_CPU_SECONDS - wall >= 5
