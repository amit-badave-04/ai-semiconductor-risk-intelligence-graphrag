"""Comparison-subprocess entry point (M4_PLAN.md 4.2, section 1/16 extension), sandboxed by RLIMIT_AS / RLIMIT_CPU
on Linux exactly like ``parse_worker.py``.

Invoked by :mod:`semigraph.uploads.compare` as ``python -m semigraph.uploads.compare_worker`` with one JSON object
``{"older": view, "newer": view}`` on stdin (the same JSON-serializable ``VersionView`` shape ``uploads.jobs``
already builds: ``text``, ``units`` (a list of ``{unit_id, kind, headline, char_start, char_end}``), ``chunk_spans``
(a list of ``[chunk_id, char_start, char_end]``), ``method``, ``chars_per_page``). It rebuilds both sides into
``changes.VersionView``, runs ``changes.compare_versions``, and writes the resulting ``ChangeReport`` JSON to
stdout, exiting 0. On any exception it writes ``{"error": "comparison_failed", "exc_type": ClassName}` — the
exception's CLASS NAME only, NEVER its message, which can quote uploaded text (M4_PLAN.md 5, finding #28's same
reasoning) — and exits non-zero.

The rlimits are applied by the ``__main__`` guard at the bottom of this file, :func:`apply_sandbox_limits`
(:mod:`semigraph.uploads.sandbox`), as its FIRST statement — before ``changes`` (which imports
``graph.alignment`` -> scipy and ``graph.passages`` -> rapidfuzz) is ever imported and before stdin is read — and
never via ``preexec_fn`` (unsafe in a multi-threaded parent). Importing this module never changes the importing
process's own limits. Module-level imports are stdlib only; every parser/aligner import is lazy, inside a function,
so importing this module for tests never pulls in scipy or rapidfuzz.
"""

import json
import logging
import sys

from .sandbox import apply_sandbox_limits          # noqa: F401  (re-exported for symmetry with parse_worker)

logger = logging.getLogger("semigraph.uploads.compare_worker")


def _unit_from_dict(d: dict):
    from .units import Unit

    return Unit(unit_id=d["unit_id"], kind=d["kind"], headline=d.get("headline") or "",
               char_start=d["char_start"], char_end=d["char_end"])


def _version_view_from_dict(d: dict):
    from .changes import VersionView

    units = tuple(_unit_from_dict(u) for u in d["units"])
    chunk_spans = tuple((cid, int(start), int(end)) for cid, start, end in d["chunk_spans"])
    return VersionView(text=d["text"], units=units, chunk_spans=chunk_spans, method=d["method"],
                       chars_per_page=float(d.get("chars_per_page") or 0.0))


def _write(obj: dict) -> None:
    sys.stdout.buffer.write(json.dumps(obj).encode("utf-8"))
    sys.stdout.buffer.flush()


def main(argv: list[str]) -> int:
    try:
        # Imported INSIDE the try (never above it): under RLIMIT_AS the most likely real failure is scipy /
        # OpenBLAS / rapidfuzz itself failing to import (ImportError / MemoryError / OSError) — that exception
        # class is exactly the triage signal finding #28 is about, and it must still reach `_write` below rather
        # than escape uncaught (which would leave stdout empty and the parent unable to tell an import failure
        # from any other crash).
        from .changes import compare_versions

        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
        older = _version_view_from_dict(payload["older"])
        newer = _version_view_from_dict(payload["newer"])
        report = compare_versions(older, newer)
    except Exception as exc:  # noqa: BLE001 — the child must always report SOMETHING, never crash silently
        # never logs the uploaded text, only that the comparison failed; the parent discards stderr in production
        # (stderr=DEVNULL) — this is for local debugging only. The exception CLASS NAME (never its message, which
        # can quote document text) is reported to the parent.
        logger.exception("unhandled error comparing versions")
        _write({"error": "comparison_failed", "exc_type": type(exc).__name__})
        return 1
    _write(report)
    return 0


if __name__ == "__main__":
    apply_sandbox_limits()      # FIRST: before stdin is read and before `changes` (scipy/rapidfuzz) is imported
    sys.exit(main(sys.argv))
