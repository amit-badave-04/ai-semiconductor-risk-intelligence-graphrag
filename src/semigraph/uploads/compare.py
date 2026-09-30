"""Runs the "what changed" comparison in a sandboxed subprocess (M4_PLAN.md 4.2, section 1/16 extension).

The upload job's "comparing" stage used to call ``changes.compare_versions`` directly, IN the API process, on the
job thread: it runs the frozen SEC aligner (``graph/alignment.py``, ``graph/passages.py``) plus the negation check,
pure Python (``difflib.SequenceMatcher`` etc.), so it held the GIL against every request thread, and it had no
wall-clock bound (a pathological in-cap document measured 50s+ in the aligner alone). :func:`compare_in_subprocess`
sends ``{"older": view, "newer": view}`` (the JSON-serializable ``VersionView`` shape ``uploads.jobs`` already
builds for ``repo.put_version`` / ``repo.version_view``) to ``python -m semigraph.uploads.compare_worker`` over
stdin, through the SAME sandbox runner the parse subprocess uses (:mod:`semigraph.uploads.sandbox`), and parses its
stdout as the ``ChangeReport`` dict.

This module is stdlib-only at import — the aligner (``graph.alignment`` -> scipy, ``graph.passages`` -> rapidfuzz)
is imported only inside the sandboxed CHILD, never here, so the API process never loads it and a pathological
in-cap document's O(n^2) alignment work can never hold the API's own GIL.

On a timeout or a crash/malformed output, this returns a well-formed "not compared" report with the SAME key set
``changes.compare_versions`` always returns (``minor_rewordings: []``, ``negation_check_skipped: 0``, ...) — a
caller (``uploads.jobs``) never needs a special case for "the comparison subprocess failed" versus any other
``not_compared_reason``. Only ``not_compared_reason`` and the child's exception CLASS NAME are ever logged, never
its message (which can quote uploaded text, M4_PLAN.md 5).
"""

from __future__ import annotations

import json
import logging

from . import sandbox

logger = logging.getLogger("semigraph.uploads.compare")

MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_MODULE = "semigraph.uploads.compare_worker"

# The same key set changes.compare_versions() always returns (module docstring): a "not compared" report built here
# must never be a different shape from one built by the real, in-subprocess compare_versions() call.
_REQUIRED_REPORT_KEYS = ("items_compared", "not_compared_reason", "added", "removed", "changed",
                        "minor_rewordings", "unchanged_count", "negation_check_skipped")


def _not_compared(reason: str, older: dict) -> dict:
    return {"items_compared": False, "not_compared_reason": reason, "added": [], "removed": [], "changed": [],
           "minor_rewordings": [], "unchanged_count": len(older.get("units") or []), "negation_check_skipped": 0}


def _looks_like_change_report(obj: object) -> bool:
    return isinstance(obj, dict) and all(k in obj for k in _REQUIRED_REPORT_KEYS)


def compare_in_subprocess(older: dict, newer: dict, *, timeout_s: int) -> dict:
    """The ``ChangeReport`` dict between two consecutive versions, computed in a sandboxed subprocess. ``older`` /
    ``newer`` are the JSON-serializable ``VersionView`` dicts (``text``, ``units``, ``chunk_spans``, ``method``,
    ``chars_per_page``)."""
    payload = json.dumps({"older": older, "newer": newer}).encode("utf-8")
    try:
        result = sandbox.run_sandboxed(_MODULE, [], payload, timeout_s=timeout_s, max_output_bytes=MAX_OUTPUT_BYTES,
                                       env_extra=sandbox.SCIPY_BLAS_THREAD_ENV)
    except sandbox.SandboxTimeout:
        _log_not_compared("comparison_timeout", exc_type=None, returncode=None)
        return _not_compared("comparison_timeout", older)
    except sandbox.SandboxOutputTooLarge:
        _log_not_compared("comparison_failed", exc_type="SandboxOutputTooLarge", returncode=None)
        return _not_compared("comparison_failed", older)

    try:
        obj = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        _log_not_compared("comparison_failed", exc_type=type(exc).__name__, returncode=result.returncode)
        return _not_compared("comparison_failed", older)

    if isinstance(obj, dict) and "error" in obj:
        exc_type = obj.get("exc_type")
        _log_not_compared("comparison_failed", exc_type=exc_type if isinstance(exc_type, str) else None,
                          returncode=result.returncode)
        return _not_compared("comparison_failed", older)

    if not _looks_like_change_report(obj):
        _log_not_compared("comparison_failed", exc_type=None, returncode=result.returncode)
        return _not_compared("comparison_failed", older)

    return obj


def _log_not_compared(code: str, *, exc_type: str | None, returncode: int | None) -> None:
    """Logs ``code`` and ``exc_type`` (the sandboxed child's own exception CLASS NAME, when known) only — never any
    message text, which can quote uploaded document content (M4_PLAN.md 5, finding #28's same reasoning)."""
    logger.warning("comparison subprocess not_compared_reason=%s exc_type=%s returncode=%s", code, exc_type,
                   returncode)
