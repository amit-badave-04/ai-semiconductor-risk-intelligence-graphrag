"""Generic sandboxed-subprocess machinery shared by the parse and comparison workers (M4_PLAN.md 4.2, 5, 14.2;
extended for the "comparing" stage, section 1/16). Stdlib only at import: this module is imported by the API
process itself (via ``uploads.parse`` and ``uploads.compare``), so it must never pull in a parser or the aligner.

Three things live here:

- :func:`child_env` — the ALLOWLISTED child environment (never a copy of the parent's, finding #6): only
  ``PATH``, ``PYTHONPATH`` (the same ``semigraph`` package the parent runs), ``MALLOC_ARENA_MAX``,
  ``PYTHONDONTWRITEBYTECODE``, ``LANG``, plus ``SYSTEMROOT``/``TEMP``/``TMP`` on Windows (only when actually set in
  the parent) - and, when the caller passes ``extra`` (both workers pass ``SCIPY_BLAS_THREAD_ENV``), those names too.
- :func:`run_sandboxed_command` — the actual Popen/timeout/kill/output-cap machinery, taking an already-built
  ``cmd`` (so a test can substitute an arbitrary command, e.g. a fixture worker that sleeps, without needing a
  second importable module) — and :func:`run_sandboxed`, a convenience wrapper that builds
  ``python -s -P -m <module> <args...>`` (:func:`module_command`: ``-P`` keeps the working directory and ``-s`` the user site
  off ``sys.path``).
- :func:`apply_sandbox_limits` - moved here from ``parse_worker.py`` (docs/v2/M4_PLAN.md 14.2) and extended (2026-11-07): for THIS
  process on Linux only, no added privilege on exec (``PR_SET_NO_NEW_PRIVS``), non-dumpable, ``RLIMIT_NPROC`` (0, 0) so the worker
  can start no process or thread, and ``RLIMIT_AS`` (1 GiB) and ``RLIMIT_CPU`` (120 s), the hard limits too. Called by a worker's
  ``__main__`` guard as its FIRST statement - before importing any parser or the aligner and before reading stdin - and NEVER via
  ``preexec_fn`` (unsafe in a multi-threaded parent: the child can deadlock before exec). Importing this module never changes
  the importing process's own limits; only calling the function does.

The worker also starts as the leader of its own session, and :func:`run_sandboxed_command` cleans up its process group: SIGKILL
on the timeout, output-overflow, wait-timeout and clean-exit paths and when a stdin or stdout thread cannot start, and an
``atexit`` handler does the same for the workers still running when the server process exits.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import semigraph

_RLIMIT_AS_BYTES = 1 * 1024 * 1024 * 1024        # 1 GiB
_RLIMIT_CPU_SECONDS = 120

_READ_CHUNK = 65536
_STDIN_WRITE_JOIN_S = 5
_PROC_WAIT_JOIN_S = 5
_EXIT_POLL_S = 0.005

_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38

# On Linux the clean-exit path waits for the worker WITHOUT reaping it (waitid with WNOWAIT), cleans its group while the exited
# worker is still a zombie that holds its pid as the group id, and only then reaps it. Module-level so a test can run the branch
# anywhere with a fake ``os.waitid``.
_WAITID_BEFORE_REAP = sys.platform.startswith("linux") and hasattr(os, "waitid")

logger = logging.getLogger("semigraph.uploads.sandbox")

_WINDOWS_CHILD_ENV_VARS = ("SYSTEMROOT", "TEMP", "TMP")      # only when the child needs them to start at all

# Added to BOTH workers' env: scipy/OpenBLAS otherwise spins up one thread pool per
# available core, each reserving its own native stack/arena, which can push virtual memory past RLIMIT_AS 1 GiB before a single
# line of alignment code runs, and under RLIMIT_NPROC (0, 0) a pool cannot start at all. Pinning every BLAS backend to one thread
# keeps the subprocess's own footprint small and deterministic. The parse worker imports no numpy today; the pin is the second line
# of defence if a parser dependency ever starts to.
SCIPY_BLAS_THREAD_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


class SandboxTimeout(Exception):
    """The child did not finish within ``timeout_s`` and was killed (returncode is therefore meaningless: SIGKILL
    always looks the same whether the process was slow or wedged)."""


class SandboxOutputTooLarge(Exception):
    """The child's stdout exceeded ``max_output_bytes``; it was killed before being fully drained."""


@dataclass(frozen=True)
class SandboxResult:
    stdout: bytes
    returncode: int | None


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """An ALLOWLISTED environment for a sandboxed child, never a copy of the parent's (finding #6): a worker
    handling untrusted bytes gets only the variables it needs and none of ``ANTHROPIC_API_KEY`` / ``ADMIN_TOKEN`` /
    ``TURNSTILE_SECRET_KEY`` / the Neo4j password (least privilege). ``extra`` (e.g.
    :data:`SCIPY_BLAS_THREAD_ENV`, which both workers pass) is merged in on top of the base allowlist for callers that need
    additional, still-fixed, non-secret variables."""
    src_dir = str(Path(semigraph.__file__).resolve().parent.parent)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": src_dir,
        "MALLOC_ARENA_MAX": "2",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }
    if extra:
        env.update(extra)
    if sys.platform.startswith("win"):
        for name in _WINDOWS_CHILD_ENV_VARS:
            value = os.environ.get(name)
            if value:
                env[name] = value
    return env


def _write_stdin(proc: subprocess.Popen, data: bytes) -> None:
    """Runs on its own thread: a large payload would otherwise block on the pipe buffer while nothing drains
    stdout, deadlocking the parent against its own child."""
    try:
        proc.stdin.write(data)
    except (BrokenPipeError, OSError):
        pass          # the child died or closed stdin early; harmless
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _read_stdout_capped(proc: subprocess.Popen, limit: int) -> bytes | None:
    """stdout up to ``limit`` bytes; ``None`` (never more than ``limit + 1`` bytes read) once it overflows."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = proc.stdout.read(_READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            return None


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL to the worker's whole process group: the worker is started as the leader of its own session
    (``start_new_session``), so the group id is its pid and everything it started in that group dies with it.
    Hygiene: the barrier is ``apply_sandbox_limits`` (no new process or thread, no added privilege on exec).
    Nothing on Windows (no ``os.killpg``, no ``signal.SIGKILL``). ``ProcessLookupError`` (an empty group, ESRCH) is the usual
    answer after a clean exit and is swallowed; any other ``OSError`` is logged at WARNING with its errno only (never document
    data) and does not reach the caller. Always called while the leader is still an unreaped worker or zombie, so its pid still
    names the group: before ``wait`` where the job is being cut short, and on Linux between the exit and the reap on a clean exit
    (see ``_WAITID_BEFORE_REAP``). Off Linux a clean exit calls it after ``wait`` (so the exit code is the worker's own, not -9);
    that call has a theoretical window, a pid reused as a new group leader between the reap and the signal."""
    killpg, sigkill = getattr(os, "killpg", None), getattr(signal, "SIGKILL", None)
    if killpg is None or sigkill is None:
        return
    try:
        killpg(proc.pid, sigkill)
    except ProcessLookupError:
        pass
    except OSError as exc:
        logger.warning("sandbox worker process-group kill failed errno=%s", exc.errno)


def _kill(proc: subprocess.Popen) -> None:
    _kill_group(proc)           # the group first, while the leader is still unreaped; the leader below is the fallback
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=_PROC_WAIT_JOIN_S)
    except Exception:
        pass


# Every worker between its spawn and the end of its job, so the ``atexit`` handler below can reach the ones a server exit would leave.
_LIVE_WORKERS: set[subprocess.Popen] = set()
_LIVE_LOCK = threading.Lock()


def _kill_live_workers_at_exit() -> None:
    """Registered with ``atexit``: the job threads are daemons, so a server that exits during a job (a second signal after the drain
    timeout, for one) must still clean up the worker's process group. A worker already
    reaped (``returncode`` set) is skipped."""
    with _LIVE_LOCK:
        live = list(_LIVE_WORKERS)
    for proc in live:
        if proc.returncode is None:
            _kill_group(proc)
            try:
                proc.kill()
            except OSError:
                pass


atexit.register(_kill_live_workers_at_exit)


def _wait_until_exited(proc: subprocess.Popen) -> None:
    """Returns once the worker has exited WITHOUT reaping it (``waitid`` with ``WNOWAIT``), so it stays a zombie that holds its pid as
    the group id. Bounded by ``_PROC_WAIT_JOIN_S``: raises ``subprocess.TimeoutExpired`` after it."""
    flags = os.WEXITED | os.WNOWAIT | os.WNOHANG
    deadline = time.monotonic() + _PROC_WAIT_JOIN_S
    try:
        while os.waitid(os.P_PID, proc.pid, flags) is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(proc.args, _PROC_WAIT_JOIN_S)
            time.sleep(_EXIT_POLL_S)
    except ChildProcessError:
        return          # already reaped elsewhere: ``wait`` below reports its status


def _finish_after_clean_output(proc: subprocess.Popen) -> None:
    """The worker closed stdout within its limits. Wait for it, clean its group, keep ITS exit code (never -9)."""
    try:
        if _WAITID_BEFORE_REAP:
            _wait_until_exited(proc)
            _kill_group(proc)           # the leader is an unreaped zombie: its pid still names the group
            proc.wait(timeout=_PROC_WAIT_JOIN_S)
        else:
            proc.wait(timeout=_PROC_WAIT_JOIN_S)
            _kill_group(proc)           # the worker is gone and its code is recorded: nothing in its group may remain
    except subprocess.TimeoutExpired:
        _kill(proc)


def _supervise(proc: subprocess.Popen, stdin: bytes, *, timeout_s: int, max_output_bytes: int) -> SandboxResult:
    writer = threading.Thread(target=_write_stdin, args=(proc, stdin), daemon=True)
    outcome: dict[str, bytes | None] = {}

    def _read() -> None:
        outcome["stdout"] = _read_stdout_capped(proc, max_output_bytes)

    reader = threading.Thread(target=_read, daemon=True)
    try:
        writer.start()
        reader.start()
    except BaseException:
        _kill(proc)             # a worker already spawned and no thread to feed or drain it: take its group down, then re-raise
        raise
    reader.join(timeout_s)
    if reader.is_alive():
        _kill(proc)
        reader.join(_PROC_WAIT_JOIN_S)
        # returncode is intentionally not surfaced here: we killed the process ourselves, so it means nothing about
        # severity (see SandboxTimeout's docstring).
        raise SandboxTimeout(f"sandboxed subprocess exceeded {timeout_s}s")

    stdout_bytes = outcome.get("stdout")
    if stdout_bytes is None:
        _kill(proc)
        raise SandboxOutputTooLarge(f"sandboxed subprocess output exceeded {max_output_bytes} bytes")

    _finish_after_clean_output(proc)
    writer.join(timeout=_STDIN_WRITE_JOIN_S)
    return SandboxResult(stdout=stdout_bytes, returncode=proc.returncode)


def run_sandboxed_command(cmd: list[str], stdin: bytes, *, timeout_s: int, max_output_bytes: int,
                          env: dict[str, str] | None = None) -> SandboxResult:
    """Runs ``cmd`` as a child process, feeding it ``stdin`` from a background thread and reading its stdout capped
    at ``max_output_bytes``, enforcing ``timeout_s`` itself (the child is killed on expiry). ``env`` defaults to
    :func:`child_env` with no extras. Raises :class:`SandboxTimeout` or :class:`SandboxOutputTooLarge` instead of
    ever returning a partial/oversized result; every other outcome (including a non-zero exit or malformed output)
    is returned as a plain :class:`SandboxResult` for the caller to interpret. The worker's process group is killed on every
    way out of this function, and the worker is registered for the ``atexit`` handler while the job runs."""
    # start_new_session: setsid() in the child before exec (done in C, not a preexec_fn, so the multi-threaded-parent hazard in
    # this module's docstring does not apply). The worker leads its own process group, which every exit path kills (_kill_group).
    # CPython ignores the flag on Windows.
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env if env is not None else child_env(), start_new_session=True,
    )
    with _LIVE_LOCK:
        _LIVE_WORKERS.add(proc)
    try:
        return _supervise(proc, stdin, timeout_s=timeout_s, max_output_bytes=max_output_bytes)
    finally:
        with _LIVE_LOCK:
            _LIVE_WORKERS.discard(proc)


def module_command(module: str, *args: str) -> list[str]:
    """The argv of a sandboxed worker: ``python -s -P -m <module> <args...>``. EVERY worker command is built here (the parse
    worker's in ``uploads.parse`` too). A worker imports only from the image's own install and ``PYTHONPATH``: ``-P`` (Python
    3.11+) keeps the working directory off ``sys.path`` (``-m`` would otherwise put it first) and ``-s`` keeps the user site
    directory off it (it is found through the passwd entry even when the child's environment has no ``HOME``). They are argv flags
    and not ``PYTHONSAFEPATH`` / ``PYTHONNOUSERSITE`` in :func:`child_env`, because that environment is an exact allowlist and a flag
    does not depend on what the parent's environment holds. Not ``-I``: it implies ``-E``, which would drop the ``PYTHONPATH`` the
    child needs."""
    return [sys.executable, "-s", "-P", "-m", module, *args]


def run_sandboxed(module: str, args: list[str], stdin: bytes, *, timeout_s: int, max_output_bytes: int,
                  env_extra: dict[str, str] | None = None) -> SandboxResult:
    """Convenience wrapper: runs ``python -s -P -m <module> <args...>`` (:func:`module_command`) with the allowlisted child
    environment (:func:`child_env`, plus ``env_extra`` when given)."""
    cmd = module_command(module, *args)
    return run_sandboxed_command(cmd, stdin, timeout_s=timeout_s, max_output_bytes=max_output_bytes,
                                 env=child_env(env_extra))


def _lower_rlimit(kind: int, cap: int, resource_mod) -> None:
    """Soft AND hard limit both come down to ``cap`` (or to the current hard limit when that is already lower): with the hard
    limit lowered too, the worker cannot raise the soft limit again."""
    _, hard = resource_mod.getrlimit(kind)
    cap = cap if hard == resource_mod.RLIM_INFINITY else min(cap, hard)
    resource_mod.setrlimit(kind, (cap, cap))


def _prctl(libc, option: int, value: int, name: str) -> None:
    """``prctl(option, value, 0, 0, 0)``; raises ``OSError`` (fail closed) when the kernel refuses. Every argument is a ``c_ulong``: a
    plain int goes through a variadic call as 32 bits, and the kernel rejects ``PR_SET_NO_NEW_PRIVS`` unless the last three are zero."""
    args = [ctypes.c_ulong(v) for v in (option, value, 0, 0, 0)]
    if libc.prctl(*args) != 0:
        raise OSError(ctypes.get_errno(), f"prctl({name}) failed")


def apply_sandbox_limits(platform: str = sys.platform, resource_mod=None, libc=None) -> bool:
    """For THIS process on Linux (False, nothing done, elsewhere), in this order: ``prctl(PR_SET_NO_NEW_PRIVS, 1)``, then
    ``prctl(PR_SET_DUMPABLE, 0)``, then RLIMIT_NPROC (0, 0), RLIMIT_AS (1 GiB) and RLIMIT_CPU (120 s), each limit with its hard limit
    lowered to the value too so none of them can be raised again. Called only by a sandboxed worker's ``__main__`` entry point
    (``parse_worker.py``, ``compare_worker.py``), as its first statement, before importing anything heavy and before reading stdin.

    A sandboxed worker can start no process or thread and gains no added privilege on exec. ``NO_NEW_PRIVS`` is first so that no
    exec after it adds privilege, and the process limit below it holds for everything the worker later runs. ``PR_SET_DUMPABLE 0`` marks the worker non-dumpable (no core dump, no ptrace attach), as the
    API process is marked in ``serve.hardening``. Either ``prctl`` failing raises ``OSError``: the worker exits before it reads stdin
    and the job is recorded as failed (fail closed).

    ``RLIMIT_NPROC`` (0, 0): the kernel counts every process and thread of the user, and the worker is one of them, so with a
    limit of 0 every ``fork``, ``clone`` and thread start fails with ``EAGAIN``. The server and every other process of the user are
    unaffected: the limit binds the process that sets it and what it spawns. ``root`` and ``CAP_SYS_RESOURCE`` / ``CAP_SYS_ADMIN``
    are exempt; the image runs as ``app``. Neither worker may start a thread or a process: the parse worker imports no numpy, and
    both workers' environments pin every BLAS backend to one thread (:data:`SCIPY_BLAS_THREAD_ENV`), so OpenBLAS starts no pool.

    The hard CPU limit equal to the soft one means a runaway worker gets SIGKILL at 120 s instead of SIGXCPU; the wall-clock
    timeouts (90 s for a parse, 110 s for a comparison, ``config.upload_compare_timeout_s``) fire first, and either way the
    returncode is negative. ``libc`` is the ``ctypes`` handle for ``prctl`` (``CDLL(None)`` by default; a fake in tests)."""
    if not platform.startswith("linux"):
        return False
    if resource_mod is None:
        import resource as resource_mod
    if libc is None:
        libc = ctypes.CDLL(None, use_errno=True)
    _prctl(libc, _PR_SET_NO_NEW_PRIVS, 1, "PR_SET_NO_NEW_PRIVS")
    _prctl(libc, _PR_SET_DUMPABLE, 0, "PR_SET_DUMPABLE")
    resource_mod.setrlimit(resource_mod.RLIMIT_NPROC, (0, 0))
    _lower_rlimit(resource_mod.RLIMIT_AS, _RLIMIT_AS_BYTES, resource_mod)
    _lower_rlimit(resource_mod.RLIMIT_CPU, _RLIMIT_CPU_SECONDS, resource_mod)
    return True
