"""Runs the upload parser in a sandboxed subprocess and returns its result (M4_PLAN.md 4.2, 5, 14.2).

``parse_document`` launches ``python -m semigraph.uploads.parse_worker <kind> <max_pages>``, writes the raw
document bytes to its stdin from a background thread (so a large upload cannot deadlock against an un-drained
stdout pipe), reads its stdout capped at :data:`MAX_OUTPUT_BYTES`, and enforces the wall timeout itself (the
worker sets its OWN ``RLIMIT_AS`` / ``RLIMIT_CPU`` on Linux, before importing any parser — see
``parse_worker.py``). The worker's stdout is parsed with ``json.loads`` only and every field is validated before
it becomes a :class:`Block`; malformed output is never trusted, it becomes ``ParseError("parse_failed")``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import semigraph

PARSE_ERROR_CODES = ("parse_failed", "timeout", "too_large", "scanned", "empty", "too_many_pages", "active_content")
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_S = 90
DEFAULT_MAX_PAGES = 30
_ALLOWED_KIND_HINTS = frozenset({"heading_style", "heading_md", "heading_html", "table", "paragraph"})
_READ_CHUNK = 65536
_STDIN_WRITE_JOIN_S = 5
_PROC_WAIT_JOIN_S = 5
# exception classes serious enough (a broken native dependency, an exhausted resource) to log at ERROR rather than
# WARNING: a parser RCE / broken build is otherwise indistinguishable from an ordinary bad upload (finding #28).
_SEVERE_EXC_TYPES = frozenset({"ImportError", "ModuleNotFoundError", "OSError", "MemoryError"})

logger = logging.getLogger("semigraph.uploads.parse")


class ParseError(Exception):
    """Raised by ``parse_document``; ``code`` is one of :data:`PARSE_ERROR_CODES`.

    ``exc_type`` (finding #28): the CLASS NAME of the exception the sandboxed child hit, when the failure was an
    unexpected one (``code == "parse_failed"`` from the child's catch-all handler) rather than one of its own
    recognised codes — never the exception's message, which can quote document text (M4_PLAN.md 5)."""

    def __init__(self, code: str, message: str = "", *, exc_type: str | None = None) -> None:
        if code not in PARSE_ERROR_CODES:
            raise ValueError(f"unknown parse error code {code!r}; expected one of {PARSE_ERROR_CODES}")
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.exc_type = exc_type


@dataclass(frozen=True)
class Block:
    """One structural block of a parsed document (module docstring; see also ``uploads.units``)."""

    text: str
    page: int
    size: float
    bold: bool
    kind_hint: str          # one of: heading_style, heading_md, heading_html, table, paragraph


@dataclass(frozen=True)
class ParsedDoc:
    method: str
    pages: int
    blocks: tuple[Block, ...]
    chars_per_page: float
    warnings: tuple[str, ...]


def _worker_command(kind: str, max_pages: int) -> list[str]:
    """The child argv; a module-level function so tests can monkeypatch it to a fixture worker (e.g. one that
    sleeps, to exercise the timeout path) without an importable fake module."""
    return [sys.executable, "-m", "semigraph.uploads.parse_worker", kind, str(max_pages)]


_WINDOWS_CHILD_ENV_VARS = ("SYSTEMROOT", "TEMP", "TMP")      # only when the child needs them to start at all


def _child_env() -> dict[str, str]:
    """An ALLOWLISTED environment for the child, never a copy of the parent's (finding #6, M4_PLAN.md 15.7): a
    parser RCE on untrusted bytes must not be able to read ``ANTHROPIC_API_KEY`` / ``ADMIN_TOKEN`` /
    ``TURNSTILE_SECRET_KEY`` / the Neo4j password, or reach the Neo4j network with them. Only ``PATH`` (to find the
    interpreter's own shared libraries), ``PYTHONPATH`` (the same ``semigraph`` package the parent runs, derived
    from ``semigraph.__file__``, not hard-coded), ``MALLOC_ARENA_MAX=2``, ``PYTHONDONTWRITEBYTECODE=1`` and
    ``LANG=C.UTF-8`` are passed, plus ``SYSTEMROOT`` / ``TEMP`` / ``TMP`` on Windows (the interpreter and its native
    extensions need them to start at all there) — and even those only when actually set in the parent."""
    src_dir = str(Path(semigraph.__file__).resolve().parent.parent)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": src_dir,
        "MALLOC_ARENA_MAX": "2",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }
    if sys.platform.startswith("win"):
        for name in _WINDOWS_CHILD_ENV_VARS:
            value = os.environ.get(name)
            if value:
                env[name] = value
    return env


def _write_stdin(proc: subprocess.Popen, data: bytes) -> None:
    """Runs on its own thread: a large upload (up to 15 MiB) would otherwise block on the pipe buffer while
    nothing drains stdout, deadlocking the parent against its own child."""
    try:
        proc.stdin.write(data)
    except (BrokenPipeError, OSError):
        pass          # the child died or closed stdin early (e.g. it rejected the kind before reading); harmless
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


def _validate_block(raw: object) -> Block:
    if not isinstance(raw, dict):
        raise ValueError(f"block must be an object, got {type(raw).__name__}")
    kind_hint = raw.get("kind_hint")
    if kind_hint not in _ALLOWED_KIND_HINTS:
        raise ValueError(f"unknown block kind_hint {kind_hint!r}")
    return Block(text=str(raw["text"]), page=int(raw["page"]), size=float(raw["size"]),
                bold=bool(raw["bold"]), kind_hint=kind_hint)


def _validate_parsed_doc(obj: object) -> ParsedDoc:
    if not isinstance(obj, dict):
        raise ValueError("worker output was not a JSON object")
    try:
        blocks = tuple(_validate_block(b) for b in obj["blocks"])
        return ParsedDoc(
            method=str(obj["method"]), pages=int(obj["pages"]), blocks=blocks,
            chars_per_page=float(obj["chars_per_page"]),
            warnings=tuple(str(w) for w in obj.get("warnings", ())),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError("parse_failed", f"malformed worker output: {exc}") from exc


def _decode_result(stdout_bytes: bytes) -> ParsedDoc:
    try:
        obj = json.loads(stdout_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise ParseError("parse_failed", f"worker produced no valid JSON: {exc}") from exc
    if isinstance(obj, dict) and "error" in obj:
        code = obj["error"] if obj["error"] in PARSE_ERROR_CODES else "parse_failed"
        exc_type = obj.get("exc_type")
        exc_type = exc_type if isinstance(exc_type, str) else None
        raise ParseError(code, f"parse worker reported {code!r}", exc_type=exc_type)
    return _validate_parsed_doc(obj)


def _log_parse_failure(exc: ParseError, returncode: int | None) -> None:
    """Logs ``code`` and ``exc_type`` (the child's exception CLASS NAME) only — never :attr:`ParseError.message`,
    which can quote parser output derived from the uploaded document's own bytes (finding #28, M4_PLAN.md 5)."""
    severe = exc.exc_type in _SEVERE_EXC_TYPES or (returncode is not None and returncode < 0)
    logger.log(logging.ERROR if severe else logging.WARNING,
              "parse subprocess failed code=%s exc_type=%s returncode=%s", exc.code, exc.exc_type, returncode)


def parse_document(data: bytes, kind: str, *, timeout_s: int = DEFAULT_TIMEOUT_S,
                   max_pages: int = DEFAULT_MAX_PAGES) -> ParsedDoc:
    """Parse ``data`` (already gate-checked, of kind ``kind``) in a sandboxed subprocess.

    Raises :class:`ParseError` for every failure mode: a bad or oversized worker result, a wall-clock timeout
    (the process is killed), or a worker-reported code (``scanned`` / ``empty`` / ``too_many_pages`` / ...).
    """
    proc = subprocess.Popen(
        _worker_command(kind, max_pages), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=_child_env(),
    )
    writer = threading.Thread(target=_write_stdin, args=(proc, data), daemon=True)
    writer.start()

    outcome: dict[str, bytes | None] = {}

    def _read() -> None:
        outcome["stdout"] = _read_stdout_capped(proc, MAX_OUTPUT_BYTES)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout_s)
    if reader.is_alive():
        _kill(proc)
        reader.join(_PROC_WAIT_JOIN_S)
        # returncode is intentionally NOT passed here: we killed the process ourselves (Linux: SIGKILL, a negative
        # returncode), so a negative code means nothing about severity for this path and would otherwise always
        # escalate an ordinary slow parse to ERROR.
        exc = ParseError("timeout", f"parse subprocess exceeded {timeout_s}s")
        _log_parse_failure(exc, None)
        raise exc

    stdout_bytes = outcome.get("stdout")
    if stdout_bytes is None:
        _kill(proc)                                             # see the timeout branch above: returncode omitted
        exc = ParseError("too_large", f"parse subprocess output exceeded {MAX_OUTPUT_BYTES} bytes")
        _log_parse_failure(exc, None)
        raise exc

    try:
        proc.wait(timeout=_PROC_WAIT_JOIN_S)
    except subprocess.TimeoutExpired:
        _kill(proc)
    writer.join(timeout=_STDIN_WRITE_JOIN_S)
    try:
        return _decode_result(stdout_bytes)
    except ParseError as exc:
        _log_parse_failure(exc, proc.returncode)
        raise
