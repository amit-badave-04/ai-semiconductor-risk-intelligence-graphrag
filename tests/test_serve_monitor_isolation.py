"""The freshness monitor runs in the SERVE image, which has no pandas (docs/v2/M4_PLAN.md D3 and 4.1).

``semigraph.ingestion`` used to import ``xbrl`` (pandas) eagerly in its ``__init__``, so NOTHING under it imported in the serve
image. The package now resolves the xbrl names lazily (PEP 562, like ``semigraph.graph``); the monitor needs only ``edgar``,
``freshness`` and ``federal_register``, which are stdlib + config + universe. There are two modules named ``freshness``:
``semigraph.graph.freshness`` imports pandas and must never be pulled in by the monitor. Checks run in a SUBPROCESS: other tests
in this session have already imported pandas.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "semigraph"
ENV = {**os.environ, "PYTHONPATH": str(SRC.parent)}
CHECK = "import sys; {imports}; bad = [m for m in ({names}) if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)"
HEAVY = ("pandas", "pyarrow", "semigraph.ingestion.xbrl", "semigraph.graph.freshness")


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=SRC.parents[1], env=ENV)


def _imports_leave_out(imports: str, *names: str) -> subprocess.CompletedProcess:
    return _run(CHECK.format(imports=imports, names=", ".join(repr(n) for n in names)))


def test_the_modules_the_monitor_needs_import_without_pandas():
    done = _imports_leave_out("import semigraph.ingestion, semigraph.ingestion.edgar, semigraph.ingestion.freshness, "
                              "semigraph.ingestion.federal_register", *HEAVY)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_package_still_exports_every_name_it_did():
    code = ("import semigraph.ingestion as I; "
            "names = ['ANNUAL_SINCE', 'FILERS', 'KEY_CONCEPTS', 'RELEVANT_KINDS', 'RULE_KINDS', 'TOPIC_KEYWORDS', 'FilingRecord', "
            "'classify_rule', 'curate_metrics', 'download_bis_rules', 'download_companyfacts', 'download_filings', "
            "'extract_metrics', 'federal_register_pending', 'fetch_submissions', 'load_manifest', 'load_ticker_to_cik', "
            "'parse_as_of', 'pending_filings', 'resolve_local_path', 'select_targets', 'supplement_metrics_from_filing_xbrl']; "
            "assert sorted(I.__all__) == sorted(names), I.__all__")
    done = _run(code)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_xbrl_names_resolve_lazily_where_pandas_is_installed():
    pytest.importorskip("pandas", reason="the serve image has no pandas; the pipeline environment does")
    code = ("import sys; import semigraph.ingestion as I; assert 'semigraph.ingestion.xbrl' not in sys.modules; "
            "from semigraph.ingestion import curate_metrics, KEY_CONCEPTS; "
            "from semigraph.ingestion.xbrl import curate_metrics as direct; "
            "assert curate_metrics is direct and 'semigraph.ingestion.xbrl' in sys.modules and KEY_CONCEPTS")
    done = _run(code)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_api_process_never_imports_a_document_parser():
    """Uploaded bytes are parsed ONLY in the sandboxed parse subprocess (docs/v2/M4_PLAN.md 5): the API process must not load
    pypdfium2 / pdfplumber / pdfminer / python-docx at all, so a parser bug cannot take the serving process with it."""
    # pandas / pyarrow are not asserted here: the neo4j driver imports them optionally when they are installed
    # (neo4j/_optional_deps.py), which the dev environment does; the serve image has neither (the serve-shipped CI job).
    done = _imports_leave_out("import semigraph.serve.main, semigraph.serve.routes, semigraph.uploads",
                              "pypdfium2", "pdfplumber", "pdfminer", "docx", "semigraph.uploads.parse_worker",
                              "semigraph.ingestion.xbrl", "semigraph.graph.freshness")
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_monitor_module_itself_leaves_out_xbrl_and_graph_freshness():
    """``semigraph.serve.monitor`` (M4 Worker B) must never pull in the pandas-only half of the codebase: not
    ``semigraph.ingestion.xbrl`` and not ``semigraph.graph.freshness`` (a different module with the same name that
    also imports pandas). Bare ``pandas`` is deliberately NOT asserted here — the neo4j driver imports it optionally
    when it is installed (``neo4j/_optional_deps.py``), which the dev ``.venv`` does and the serve-shipped venv does
    not; the two modules above are the actual, unconditional proof."""
    done = _imports_leave_out("import semigraph.serve.monitor", "semigraph.ingestion.xbrl", "semigraph.graph.freshness")
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_api_process_never_imports_the_aligner_or_its_scipy_rapidfuzz_dependencies():
    """The upload job's "comparing" stage now runs in a SANDBOXED SUBPROCESS (docs/v2/M4_PLAN.md 4.2 extension,
    ``uploads/sandbox.py`` + ``uploads/compare_worker.py``), never in-process: ``uploads.jobs`` must never import
    ``semigraph.graph.alignment`` / ``semigraph.graph.passages`` (or their own ``scipy`` / ``rapidfuzz``
    dependencies) at module level or lazily during import — only ``uploads.compare`` (stdlib-only), which launches
    the child that actually imports them."""
    done = _imports_leave_out("import semigraph.serve.main, semigraph.uploads.jobs",
                              "semigraph.graph.alignment", "semigraph.graph.passages", "scipy", "rapidfuzz")
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_api_process_never_loads_the_aligner_even_after_every_lazy_import_a_job_can_trigger():
    """Stronger than the import-time-only pin above: exercises every module ``uploads.jobs`` lazily imports across a
    job's stages (``repo``, ``parse``, ``units``, ``compare``, ``versions``, ``hashing``, ``retrieval.ids``,
    ``retrieval.workspace`` — never ``changes``, which this module does not import at all) and asserts
    ``semigraph.graph.alignment`` / ``semigraph.graph.passages`` / ``scipy`` are STILL absent afterwards — the
    literal goal ("the API process never loads the aligner"), not just what importing the bare modules shows.

    NOTE (seam, reported — not this worker's to fix): ``semigraph.uploads.units`` itself imports
    ``semigraph.graph.align_text`` for sentence-splitting (``chunk_units``'s boundary snapping), and
    ``graph.align_text`` imports ``rapidfuzz`` at module level. So ``rapidfuzz`` — unlike ``scipy`` and the two
    aligner modules — DOES land in ``sys.modules`` the moment any job reaches its chunking stage, independently of
    whether a comparison ever runs. This is pre-existing (``units.py`` already needed sentence boundaries before
    M4's comparison-sandboxing extension) and out of this file's ownership (``units.py`` / ``graph/align_text.py``
    are not in the M4 "comparing"-stage sandboxing scope) — rapidfuzz is therefore deliberately NOT asserted absent
    here, only the two aligner modules and scipy, which is the actual GIL/RLIMIT concern this sandboxing fixes."""
    done = _imports_leave_out(
        "import semigraph.uploads.repo, semigraph.uploads.parse, semigraph.uploads.units, "
        "semigraph.uploads.compare, semigraph.uploads.versions, semigraph.hashing, semigraph.retrieval.ids, "
        "semigraph.retrieval.workspace",
        "semigraph.graph.alignment", "semigraph.graph.passages", "scipy")
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_unknown_name_is_still_an_attribute_error():
    done = _run("import semigraph.ingestion as I\ntry:\n    I.no_such_name\nexcept AttributeError:\n    pass\n"
                "else:\n    raise SystemExit('no AttributeError')")
    assert done.returncode == 0, done.stdout + done.stderr
