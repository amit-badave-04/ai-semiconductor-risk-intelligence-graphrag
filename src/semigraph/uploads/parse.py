"""Runs the upload parser in a sandboxed subprocess and returns its result (M4_PLAN.md 4.2, 5, 14.2).

``parse_document`` launches ``python -m semigraph.uploads.parse_worker <kind> <max_pages>`` through the SHARED
sandbox runner (:mod:`semigraph.uploads.sandbox`, extracted for reuse by the comparison worker — section 1 of the
plan's revision 6), writes the raw document bytes to its stdin, reads its stdout capped at :data:`MAX_OUTPUT_BYTES`,
and enforces the wall timeout (the worker sets its OWN ``RLIMIT_AS`` / ``RLIMIT_CPU`` on Linux, before importing any
parser — see ``parse_worker.py``). The worker's stdout is parsed with ``json.loads`` only and every field is
validated before it becomes a :class:`Block`; malformed output is never trusted, it becomes
``ParseError("parse_failed")``. Behavior is UNCHANGED from before the extraction — every test in
``tests/test_serve_upload_parse.py`` and ``tests/test_serve_upload_gate.py`` still passes unmodified.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass

from . import sandbox

PARSE_ERROR_CODES = ("parse_failed", "timeout", "too_large", "scanned", "empty", "too_many_pages", "active_content")
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_S = 90
DEFAULT_MAX_PAGES = 30
_ALLOWED_KIND_HINTS = frozenset({"heading_style", "heading_md", "heading_html", "table", "paragraph"})
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


def _child_env() -> dict[str, str]:
    """The allowlisted child environment (finding #6, M4_PLAN.md 15.7), unchanged from before the extraction into
    :mod:`semigraph.uploads.sandbox`: no BLAS-thread additions here (those are for the comparison worker only —
    parsing never touches scipy/rapidfuzz)."""
    return sandbox.child_env()


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
    cmd = _worker_command(kind, max_pages)
    try:
        result = sandbox.run_sandboxed_command(cmd, data, timeout_s=timeout_s, max_output_bytes=MAX_OUTPUT_BYTES,
                                               env=_child_env())
    except sandbox.SandboxTimeout:
        # returncode is intentionally NOT passed here: we killed the process ourselves (Linux: SIGKILL, a negative
        # returncode), so a negative code means nothing about severity for this path and would otherwise always
        # escalate an ordinary slow parse to ERROR.
        exc = ParseError("timeout", f"parse subprocess exceeded {timeout_s}s")
        _log_parse_failure(exc, None)
        raise exc from None
    except sandbox.SandboxOutputTooLarge:
        exc = ParseError("too_large", f"parse subprocess output exceeded {MAX_OUTPUT_BYTES} bytes")
        _log_parse_failure(exc, None)
        raise exc from None

    try:
        return _decode_result(result.stdout)
    except ParseError as exc:
        _log_parse_failure(exc, result.returncode)
        raise
