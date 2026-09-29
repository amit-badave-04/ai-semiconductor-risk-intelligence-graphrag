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
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import semigraph

PARSE_ERROR_CODES = ("parse_failed", "timeout", "too_large", "scanned", "empty", "too_many_pages")
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_S = 90
DEFAULT_MAX_PAGES = 30
_ALLOWED_KIND_HINTS = frozenset({"heading_style", "heading_md", "heading_html", "table", "paragraph"})
_READ_CHUNK = 65536
_STDIN_WRITE_JOIN_S = 5
_PROC_WAIT_JOIN_S = 5


class ParseError(Exception):
    """Raised by ``parse_document``; ``code`` is one of :data:`PARSE_ERROR_CODES`."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in PARSE_ERROR_CODES:
            raise ValueError(f"unknown parse error code {code!r}; expected one of {PARSE_ERROR_CODES}")
        super().__init__(message or code)
        self.code = code
        self.message = message or code


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


def _child_env() -> dict[str, str]:
    """A COPY of the parent environment plus ``MALLOC_ARENA_MAX=2`` and a ``PYTHONPATH`` that puts the same
    ``semigraph`` package the parent runs on the child's import path (derived from ``semigraph.__file__``, not
    hard-coded, so it works whether that package is installed or run from a checkout)."""
    env = dict(os.environ)
    env["MALLOC_ARENA_MAX"] = "2"
    src_dir = str(Path(semigraph.__file__).resolve().parent.parent)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_dir if not existing else f"{src_dir}{os.pathsep}{existing}"
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
        raise ParseError(code, f"parse worker reported {code!r}")
    return _validate_parsed_doc(obj)


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
        raise ParseError("timeout", f"parse subprocess exceeded {timeout_s}s")

    stdout_bytes = outcome.get("stdout")
    if stdout_bytes is None:
        _kill(proc)
        raise ParseError("too_large", f"parse subprocess output exceeded {MAX_OUTPUT_BYTES} bytes")

    try:
        proc.wait(timeout=_PROC_WAIT_JOIN_S)
    except subprocess.TimeoutExpired:
        _kill(proc)
    writer.join(timeout=_STDIN_WRITE_JOIN_S)
    return _decode_result(stdout_bytes)
