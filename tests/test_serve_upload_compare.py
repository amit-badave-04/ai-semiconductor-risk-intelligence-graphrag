"""Tests for semigraph.uploads.compare / compare_worker (M4_PLAN.md 4.2, section 1/16 extension, Worker A).

The upload job's "comparing" stage used to run ``changes.compare_versions`` IN the API process, on the job thread —
this module proves the replacement: the SAME comparison, computed in a sandboxed subprocess
(``uploads/sandbox.py`` + ``uploads/compare_worker.py``), with a wall-clock bound and a crash/timeout that never
leaks uploaded text.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import upload_fixtures as fx  # noqa: E402

from semigraph.uploads import compare, parse, sandbox  # noqa: E402
from semigraph.uploads import units as U  # noqa: E402
from semigraph.uploads.changes import VersionView, compare_versions  # noqa: E402
from semigraph.uploads.parse import Block  # noqa: E402
from semigraph.uploads.units import Unit  # noqa: E402

_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


def _fake_count_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def _view_dict(text: str, kind: str = "md") -> dict:
    """The JSON-serializable ``VersionView`` shape ``uploads.jobs`` builds, via the REAL pipeline
    (``parse.parse_document`` -> ``units.detect_units`` -> ``units.chunk_units``)."""
    doc = parse.parse_document(text.encode("utf-8"), kind)
    unit_list = U.detect_units(doc.blocks, kind)
    canonical = U.canonical_text(doc.blocks)
    chunks = U.chunk_units(canonical, unit_list, count_tokens=_fake_count_tokens,
                           max_tokens=512, target_chars=1200, max_chars=1800)
    return {
        "text": canonical,
        "units": [{"unit_id": u.unit_id, "kind": u.kind, "headline": u.headline,
                  "char_start": u.char_start, "char_end": u.char_end} for u in unit_list],
        "chunk_spans": [[f"c{c.seq}", c.char_start, c.char_end] for c in chunks],
        "method": doc.method,
        "chars_per_page": doc.chars_per_page,
    }


def _view_from_dict(d: dict) -> VersionView:
    units = tuple(Unit(unit_id=u["unit_id"], kind=u["kind"], headline=u["headline"],
                       char_start=u["char_start"], char_end=u["char_end"]) for u in d["units"])
    chunk_spans = tuple(tuple(s) for s in d["chunk_spans"])
    return VersionView(text=d["text"], units=units, chunk_spans=chunk_spans, method=d["method"],
                       chars_per_page=d["chars_per_page"])


# --------------------------------------------------------------------------
# real subprocess comparison equals the in-process compare_versions result
# --------------------------------------------------------------------------

def test_compare_in_subprocess_matches_the_in_process_result_for_the_md_fixture():
    older, newer = _view_dict(fx.MD_V1), _view_dict(fx.MD_V2)

    start = time.monotonic()
    subprocess_report = compare.compare_in_subprocess(older, newer, timeout_s=30)
    elapsed = time.monotonic() - start
    print("compare_in_subprocess wall time (MD v1/v2 fixture):", elapsed)

    in_process_report = compare_versions(_view_from_dict(older), _view_from_dict(newer))
    assert subprocess_report == in_process_report
    assert subprocess_report["items_compared"] is True
    assert subprocess_report["added"] and subprocess_report["removed"] and subprocess_report["changed"]


def test_compare_in_subprocess_identical_content_matches_the_in_process_shortcut():
    view = _view_dict(fx.MD_V1)
    subprocess_report = compare.compare_in_subprocess(view, view, timeout_s=30)
    in_process_report = compare_versions(_view_from_dict(view), _view_from_dict(view))
    assert subprocess_report == in_process_report
    assert subprocess_report["not_compared_reason"] == "identical_content"


# --------------------------------------------------------------------------
# timeout: comparison_timeout, same key set as a real report, never hangs
# --------------------------------------------------------------------------

def test_compare_in_subprocess_a_real_forced_timeout_returns_comparison_timeout():
    """End to end, NO monkeypatch: a real child, forced to a ``timeout_s=0`` budget so the reader thread's
    ``join(0)`` returns before the child can possibly have produced output yet — deterministic, unlike a small but
    nonzero timeout racing the MD fixture's own ~1.2s compare wall time (measured above)."""
    older = _view_dict(fx.MD_V1)
    newer = _view_dict(fx.MD_V2)
    start = time.monotonic()
    report = compare.compare_in_subprocess(older, newer, timeout_s=0)
    elapsed = time.monotonic() - start
    assert report["items_compared"] is False
    assert report["not_compared_reason"] == "comparison_timeout"
    assert set(report.keys()) == set(compare._REQUIRED_REPORT_KEYS)
    assert report["minor_rewordings"] == []
    assert report["negation_check_skipped"] == 0
    assert report["unchanged_count"] == len(older["units"])
    assert elapsed < 20, "the child was not actually killed at the timeout"


def test_compare_in_subprocess_maps_a_sandbox_timeout_to_comparison_timeout(monkeypatch):
    """Unit-level mapping test: ``compare_in_subprocess`` must translate ANY ``sandbox.SandboxTimeout`` (whatever
    its exact cause) into the fixed ``comparison_timeout`` reason — proven here independently of the real
    subprocess timing exercised by the end-to-end test above."""
    def always_times_out(module, args, stdin, *, timeout_s, max_output_bytes, env_extra=None):
        raise sandbox.SandboxTimeout(f"simulated timeout after {timeout_s}s")

    monkeypatch.setattr(compare.sandbox, "run_sandboxed", always_times_out)
    older = _view_dict(fx.MD_V1)
    newer = _view_dict(fx.MD_V2)
    report = compare.compare_in_subprocess(older, newer, timeout_s=1)
    assert report["items_compared"] is False
    assert report["not_compared_reason"] == "comparison_timeout"
    assert report["minor_rewordings"] == []
    assert report["negation_check_skipped"] == 0
    assert report["unchanged_count"] == len(older["units"])


def test_a_genuinely_slow_child_is_actually_killed_at_a_tiny_timeout():
    """End-to-end, no monkeypatch: proves the shared sandbox runner's own kill-on-timeout path (which
    ``compare.compare_in_subprocess`` relies on for its ``comparison_timeout`` mapping) really terminates a wedged
    child rather than leaving it running or hanging the caller."""
    script = "import time; time.sleep(60)"
    start = time.monotonic()
    with pytest.raises(sandbox.SandboxTimeout):
        sandbox.run_sandboxed_command([sys.executable, "-c", script], b"irrelevant", timeout_s=1,
                                      max_output_bytes=4 * 1024 * 1024)
    elapsed = time.monotonic() - start
    assert elapsed < 20, "the subprocess was not actually killed at the timeout"


# --------------------------------------------------------------------------
# a crashing child: comparison_failed, never the exception message
# --------------------------------------------------------------------------

def test_compare_worker_main_reports_exc_type_never_the_exception_message(monkeypatch):
    """Direct unit test of ``compare_worker.main``'s catch-all handler (mirrors
    ``test_serve_upload_parse.py::test_worker_main_reports_exc_type_never_the_exception_message``): an unexpected
    exception raised BY ``compare_versions`` itself (a broken native dependency, not a malformed-payload
    ``KeyError``) must surface its CLASS NAME to the parent, and its message — which could quote uploaded document
    text — must never reach stdout. This is the real proof that the message never leaks; the subprocess-level test
    below only proves the end-to-end shape (a ``KeyError`` whose OWN message happens not to contain secret text)."""
    import io

    from semigraph.uploads import compare_worker

    secret_marker = "SECRET_DOCUMENT_TEXT_MUST_NEVER_LEAK"
    monkeypatch.setattr("semigraph.uploads.changes.compare_versions",
                        lambda older, newer: (_ for _ in ()).throw(RuntimeError(secret_marker)))
    payload = json.dumps({"older": _view_dict(fx.MD_V1), "newer": _view_dict(fx.MD_V2)}).encode("utf-8")
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(payload)})())
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", type("S", (), {"buffer": out})())

    rc = compare_worker.main(["compare_worker"])

    assert rc == 1
    result = json.loads(out.getvalue())
    assert result == {"error": "comparison_failed", "exc_type": "RuntimeError"}
    assert secret_marker not in out.getvalue().decode("utf-8")


def test_compare_in_subprocess_a_crashing_child_returns_comparison_failed_with_no_message_text(caplog):
    """End-to-end shape check through the REAL subprocess (a ``KeyError`` from a malformed payload, rebuilt inside
    the sandboxed child): proves the plumbing (``compare.compare_in_subprocess`` -> ``sandbox.run_sandboxed`` ->
    the real ``compare_worker`` child) reports ``comparison_failed`` end to end. The message-never-leaks guarantee
    itself is proven directly, at the unit level, by
    ``test_compare_worker_main_reports_exc_type_never_the_exception_message`` above (a ``KeyError``'s own message —
    here just the literal string ``'kind'`` — could never contain the uploaded secret text regardless of whether
    the worker leaked it)."""
    secret_marker = "SECRET_UPLOADED_TEXT_MUST_NEVER_LEAK_IN_A_CRASH_REPORT"
    # Missing "kind"/"char_start"/"char_end" on the unit: the REAL compare_worker crashes with a KeyError while
    # rebuilding the VersionView, before ever running compare_versions.
    older = {"text": secret_marker, "units": [{"unit_id": "u1"}], "chunk_spans": [], "method": "text",
            "chars_per_page": 1000.0}
    newer = {"text": "irrelevant newer text", "units": [], "chunk_spans": [], "method": "text",
            "chars_per_page": 1000.0}

    with caplog.at_level("WARNING", logger="semigraph.uploads.compare"):
        report = compare.compare_in_subprocess(older, newer, timeout_s=30)

    assert report["items_compared"] is False
    assert report["not_compared_reason"] == "comparison_failed"
    assert report["minor_rewordings"] == []
    assert report["negation_check_skipped"] == 0
    assert secret_marker not in json.dumps(report)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert secret_marker not in logged
    assert "KeyError" in logged                     # exc_type IS logged; never the exception's own message


def test_compare_in_subprocess_malformed_json_output_is_comparison_failed(monkeypatch):
    def bad_output(module, args, stdin, *, timeout_s, max_output_bytes, env_extra=None):
        return sandbox.SandboxResult(stdout=b"not json at all", returncode=0)

    monkeypatch.setattr(compare.sandbox, "run_sandboxed", bad_output)
    older = _view_dict(fx.MD_V1)
    report = compare.compare_in_subprocess(older, _view_dict(fx.MD_V2), timeout_s=30)
    assert report["not_compared_reason"] == "comparison_failed"


def test_compare_in_subprocess_oversized_output_is_comparison_failed(monkeypatch):
    def too_big(module, args, stdin, *, timeout_s, max_output_bytes, env_extra=None):
        raise sandbox.SandboxOutputTooLarge("simulated oversized output")

    monkeypatch.setattr(compare.sandbox, "run_sandboxed", too_big)
    older = _view_dict(fx.MD_V1)
    report = compare.compare_in_subprocess(older, _view_dict(fx.MD_V2), timeout_s=30)
    assert report["not_compared_reason"] == "comparison_failed"


def test_compare_in_subprocess_a_spawn_failure_is_comparison_failed_not_a_raised_oserror(monkeypatch, caplog):
    """Popen itself can fail (EAGAIN on fork, ENOMEM, a missing interpreter): the module's contract is a well-formed
    "not compared" report on every failure, never an exception escaping into the upload job."""
    def cannot_spawn(module, args, stdin, *, timeout_s, max_output_bytes, env_extra=None):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(compare.sandbox, "run_sandboxed", cannot_spawn)
    older = _view_dict(fx.MD_V1)
    with caplog.at_level("WARNING", logger="semigraph.uploads.compare"):
        report = compare.compare_in_subprocess(older, _view_dict(fx.MD_V2), timeout_s=30)
    assert report["not_compared_reason"] == "comparison_failed"
    assert report["items_compared"] is False
    assert "BlockingIOError" in "\n".join(r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# child env: the allowlist, including the BLAS thread pins (so scipy fits under RLIMIT_AS)
# --------------------------------------------------------------------------

def test_compare_worker_child_env_is_the_allowlist_including_the_blas_thread_pins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("NEO4J_PASSWORD", "neo4j-should-never-leak")
    monkeypatch.setenv("SOME_UNRELATED_PARENT_VAR", "must not leak either")

    env = sandbox.child_env(sandbox.SCIPY_BLAS_THREAD_ENV)

    assert env["OPENBLAS_NUM_THREADS"] == "1"
    assert env["OMP_NUM_THREADS"] == "1"
    assert env["MKL_NUM_THREADS"] == "1"
    assert "ANTHROPIC_API_KEY" not in env
    assert "NEO4J_PASSWORD" not in env
    assert "SOME_UNRELATED_PARENT_VAR" not in env
    allowed = {"PATH", "PYTHONPATH", "MALLOC_ARENA_MAX", "PYTHONDONTWRITEBYTECODE", "LANG",
              "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"}
    if sys.platform.startswith("win"):
        allowed |= {"SYSTEMROOT", "TEMP", "TMP"} & set(env)
    assert set(env) <= allowed


def test_compare_worker_parse_child_env_unaffected_by_the_blas_additions():
    """The parse subprocess must NEVER get the BLAS thread vars (parsing never touches scipy/rapidfuzz) — proves the
    extraction into ``sandbox.py`` did not leak the comparison worker's extras onto the parse worker's env."""
    env = sandbox.child_env()
    assert "OPENBLAS_NUM_THREADS" not in env
    assert "OMP_NUM_THREADS" not in env
    assert "MKL_NUM_THREADS" not in env


def test_compare_worker_real_child_receives_the_blas_pins_and_never_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("NEO4J_PASSWORD", "neo4j-should-never-leak")
    dump_path = tmp_path / "child_env_dump.json"
    script = textwrap.dedent(f"""
        import json, os
        with open({str(dump_path)!r}, "w") as f:
            json.dump(dict(os.environ), f)
        print(json.dumps({{"error": "comparison_failed"}}))
    """)
    sandbox.run_sandboxed_command([sys.executable, "-c", script], b"{}", timeout_s=30,
                                  max_output_bytes=4 * 1024 * 1024, env=sandbox.child_env(sandbox.SCIPY_BLAS_THREAD_ENV))
    child_env = json.loads(dump_path.read_text())
    assert child_env["OPENBLAS_NUM_THREADS"] == "1"
    assert child_env["OMP_NUM_THREADS"] == "1"
    assert child_env["MKL_NUM_THREADS"] == "1"
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "NEO4J_PASSWORD" not in child_env


# --------------------------------------------------------------------------
# RLIMIT_AS under the real 30-page / max-size fixture pair (Linux only; Windows proves nothing here)
# --------------------------------------------------------------------------

def _dense_blocks(seed: str, n_sections: int = 30, section_chars: int = 1500) -> list[Block]:
    """~30 heading-led sections of dense filler text (a synthetic stand-in for a max-size, ~30-page upload's ALREADY
    PARSED blocks — this test targets the comparison subprocess, not the PDF parser, so it never needs a real PDF)."""
    filler = ("The {seed} segment discusses operational results, cost trends and currency effects across the "
             "reporting period in additional detail for synthetic load-test paragraph number {n} here. ")
    blocks: list[Block] = []
    for s in range(n_sections):
        blocks.append(Block(text=f"Section Heading Number {s}", page=1, size=16.0, bold=True, kind_hint="paragraph"))
        body, n = "", 0
        while len(body) < section_chars:
            body += filler.format(seed=seed, n=n)
            n += 1
        blocks.append(Block(text=body, page=1, size=12.0, bold=False, kind_hint="paragraph"))
    return blocks


def _dense_view_dict(seed: str) -> dict:
    blocks = _dense_blocks(seed)
    unit_list = U.detect_units(blocks, "txt")
    canonical = U.canonical_text(blocks)
    chunks = U.chunk_units(canonical, unit_list, count_tokens=_fake_count_tokens, max_tokens=512,
                           target_chars=1200, max_chars=1800)
    return {
        "text": canonical,
        "units": [{"unit_id": u.unit_id, "kind": u.kind, "headline": u.headline,
                  "char_start": u.char_start, "char_end": u.char_end} for u in unit_list],
        "chunk_spans": [[f"c{c.seq}", c.char_start, c.char_end] for c in chunks],
        "method": "text",
        "chars_per_page": 2700.0,
    }


@pytest.mark.skipif(sys.platform != "linux",
                    reason="RLIMIT_AS/preexec-free rlimits are Linux-only; proven by the serve-shipped CI job")
def test_max_size_pair_compares_under_rlimit_as_and_reports_child_rss(tmp_path):
    """Runs ``compare_in_subprocess`` inside a FRESH wrapper subprocess so ``RUSAGE_CHILDREN`` reflects only this
    fixture's own compare-worker child, not whatever else this pytest session has reaped (mirrors
    ``test_serve_upload_parse.py::test_max_size_pdf_parses_under_rlimit_as_and_reports_child_rss``).

    The pair is SYNTHETIC (``_dense_view_dict``, ~49k characters / ~16.5k fake-counted tokens / 30 units / 89
    chunks), built directly from already-parsed ``Block``s rather than through a real ``max_size_pdf`` pair: two
    calls to ``upload_fixtures.max_size_pdf`` with the same arguments produce byte-identical content, which would
    hit the ``identical_content`` guard and never actually run the aligner — exactly the path this test must avoid.
    The task's own requirement is only to print ``ru_maxrss`` under the real RLIMIT_AS; the ``< 300 MB`` check below
    is this test's OWN acceptance bound (matching the parse-sandbox test's identical figure), not a value from
    M4_PLAN.md — a CI failure here means "grew past our own chosen margin", not "violated a hard spec limit"."""
    older, newer = _dense_view_dict("alpha"), _dense_view_dict("beta")
    print("fixture token estimate (older):", _fake_count_tokens(older["text"]))

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({"older": older, "newer": newer}))
    script = textwrap.dedent(f"""
        import json, resource, sys
        sys.path.insert(0, {_SRC_DIR!r})
        from semigraph.uploads.compare import compare_in_subprocess
        payload = json.loads(open({str(payload_path)!r}, "r", encoding="utf-8").read())
        report = compare_in_subprocess(payload["older"], payload["newer"], timeout_s=90)
        rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        print(json.dumps({{"items_compared": report["items_compared"],
                          "not_compared_reason": report["not_compared_reason"], "changed": len(report["changed"]),
                          "ru_maxrss_kb": rss_kb}}))
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    print("compare-worker child ru_maxrss (KB):", out["ru_maxrss_kb"], "| changed units:", out["changed"])
    # This test has never run before this branch's first CI pass (WSL has no scipy/rapidfuzz to run it locally):
    # if compare_in_subprocess caught a SandboxTimeout/crash and fell back to its graceful "not compared" report
    # instead of raising, `result.returncode` above is still 0, so THIS assertion is the one that would actually
    # fail — carry `result.stderr` too, since `compare._log_not_compared`'s exc_type line lands there (Python
    # logging's lastResort handler) and is otherwise invisible in a bare "False is not True".
    assert out["items_compared"] is True, (out["not_compared_reason"], result.stderr)
    # Self-chosen sanity bound (see docstring), not a task requirement: the task asks only to print ru_maxrss.
    assert out["ru_maxrss_kb"] < 300 * 1024, "the child exceeded this test's own 300 MB acceptance margin"
