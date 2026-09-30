"""Generic sandboxed-subprocess machinery shared by the parse and comparison workers (M4_PLAN.md 4.2, 5, 14.2;
extended for the "comparing" stage, section 1/16). Stdlib only at import: this module is imported by the API
process itself (via ``uploads.parse`` and ``uploads.compare``), so it must never pull in a parser or the aligner.

Three things live here:

- :func:`child_env` — the ALLOWLISTED child environment (never a copy of the parent's, finding #6): only
  ``PATH``, ``PYTHONPATH`` (the same ``semigraph`` package the parent runs), ``MALLOC_ARENA_MAX``,
  ``PYTHONDONTWRITEBYTECODE``, ``LANG``, plus ``SYSTEMROOT``/``TEMP``/``TMP`` on Windows (only when actually set in
  the parent) — and, when the caller passes ``extra`` (the comparison worker's ``SCIPY_BLAS_THREAD_ENV``), those
  names too. The base allowlist is unchanged from the pre-extraction ``uploads.parse._child_env`` so every existing
  parse test (which asserts the exact allowed set) keeps passing unchanged.
- :func:`run_sandboxed_command` — the actual Popen/timeout/kill/output-cap machinery, taking an already-built
  ``cmd`` (so a test can substitute an arbitrary command, e.g. a fixture worker that sleeps, without needing a
  second importable module) — and :func:`run_sandboxed`, a convenience wrapper that builds
  ``python -m <module> <args...>``.
- :func:`apply_sandbox_limits` — moved here from ``parse_worker.py`` unchanged (docs/v2/M4_PLAN.md 14.2): lowers
  ``RLIMIT_AS`` (1 GiB) and ``RLIMIT_CPU`` (120 s) for THIS process on Linux only, called by a worker's ``__main__``
  guard as its FIRST statement — before importing any parser or the aligner and before reading stdin — and NEVER
  via ``preexec_fn`` (unsafe in a multi-threaded parent: the child can deadlock before exec). Importing this module
  never changes the importing process's own limits; only calling the function does.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import semigraph

_RLIMIT_AS_BYTES = 1 * 1024 * 1024 * 1024        # 1 GiB
_RLIMIT_CPU_SECONDS = 120

_READ_CHUNK = 65536
_STDIN_WRITE_JOIN_S = 5
_PROC_WAIT_JOIN_S = 5

_WINDOWS_CHILD_ENV_VARS = ("SYSTEMROOT", "TEMP", "TMP")      # only when the child needs them to start at all

# Added to the comparison worker's env only (never the parse worker's, section 1): scipy/OpenBLAS otherwise spins up
# one thread pool per available core, each reserving its own native stack/arena, which can push virtual memory past
# RLIMIT_AS 1 GiB before a single line of alignment code runs. Pinning every BLAS backend to one thread keeps the
# comparison subprocess's own footprint small and deterministic.
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
    """An ALLOWLISTED environment for a sandboxed child, never a copy of the parent's (finding #6): a parser or
    aligner RCE on untrusted bytes must not be able to read ``ANTHROPIC_API_KEY`` / ``ADMIN_TOKEN`` /
    ``TURNSTILE_SECRET_KEY`` / the Neo4j password, or reach the Neo4j network with them. ``extra`` (e.g.
    :data:`SCIPY_BLAS_THREAD_ENV`) is merged in on top of the base allowlist for callers that need additional,
    still-fixed, non-secret variables."""
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


def _kill(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=_PROC_WAIT_JOIN_S)
    except Exception:
        pass


def run_sandboxed_command(cmd: list[str], stdin: bytes, *, timeout_s: int, max_output_bytes: int,
                          env: dict[str, str] | None = None) -> SandboxResult:
    """Runs ``cmd`` as a child process, feeding it ``stdin`` from a background thread and reading its stdout capped
    at ``max_output_bytes``, enforcing ``timeout_s`` itself (the child is killed on expiry). ``env`` defaults to
    :func:`child_env` with no extras. Raises :class:`SandboxTimeout` or :class:`SandboxOutputTooLarge` instead of
    ever returning a partial/oversized result; every other outcome (including a non-zero exit or malformed output)
    is returned as a plain :class:`SandboxResult` for the caller to interpret."""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env if env is not None else child_env(),
    )
    writer = threading.Thread(target=_write_stdin, args=(proc, stdin), daemon=True)
    writer.start()

    outcome: dict[str, bytes | None] = {}

    def _read() -> None:
        outcome["stdout"] = _read_stdout_capped(proc, max_output_bytes)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
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

    try:
        proc.wait(timeout=_PROC_WAIT_JOIN_S)
    except subprocess.TimeoutExpired:
        _kill(proc)
    writer.join(timeout=_STDIN_WRITE_JOIN_S)
    return SandboxResult(stdout=stdout_bytes, returncode=proc.returncode)


def run_sandboxed(module: str, args: list[str], stdin: bytes, *, timeout_s: int, max_output_bytes: int,
                  env_extra: dict[str, str] | None = None) -> SandboxResult:
    """Convenience wrapper: runs ``python -m <module> <args...>`` with the allowlisted child environment
    (:func:`child_env`, plus ``env_extra`` when given)."""
    cmd = [sys.executable, "-m", module, *args]
    return run_sandboxed_command(cmd, stdin, timeout_s=timeout_s, max_output_bytes=max_output_bytes,
                                 env=child_env(env_extra))


def _lower_rlimit(kind: int, soft: int, resource_mod) -> None:
    _, hard = resource_mod.getrlimit(kind)
    cap = soft if hard == resource_mod.RLIM_INFINITY else min(soft, hard)
    resource_mod.setrlimit(kind, (cap, hard))


def apply_sandbox_limits(platform: str = sys.platform, resource_mod=None) -> bool:
    """Lower RLIMIT_AS (1 GiB) and RLIMIT_CPU (120 s) for THIS process on Linux; False (nothing done) elsewhere.
    Called only by a sandboxed worker's ``__main__`` entry point (``parse_worker.py``, ``compare_worker.py``), as
    its first statement, before importing anything heavy and before reading stdin."""
    if not platform.startswith("linux"):
        return False
    if resource_mod is None:
        import resource as resource_mod
    _lower_rlimit(resource_mod.RLIMIT_AS, _RLIMIT_AS_BYTES, resource_mod)
    _lower_rlimit(resource_mod.RLIMIT_CPU, _RLIMIT_CPU_SECONDS, resource_mod)
    return True
