"""Tests for semigraph.uploads.parse / parse_worker (M4_PLAN.md 4.2, 5, 14.2, Worker A)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import upload_fixtures as fx  # noqa: E402

from semigraph.uploads import parse  # noqa: E402

_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


# --------------------------------------------------------------------------
# happy path, real subprocess, every kind
# --------------------------------------------------------------------------

def test_parse_document_pdf_end_to_end():
    doc = parse.parse_document(fx.simple_pdf(), "pdf")
    assert doc.method == "pypdfium2"
    assert doc.pages == 4
    assert doc.chars_per_page > 200
    assert any(b.bold and b.size >= 16.0 for b in doc.blocks)
    assert any("COMPANY CONFIDENTIAL DRAFT" in b.text for b in doc.blocks)


def test_parse_document_docx_end_to_end_table_text_present():
    doc = parse.parse_document(fx.docx_with_table_and_bold_heading(), "docx")
    assert doc.method == "python-docx"
    table_blocks = [b for b in doc.blocks if b.kind_hint == "table"]
    assert any("Metric Name" in b.text for b in table_blocks)
    assert any("Revenue growth rate" in b.text for b in table_blocks)


def test_parse_document_docx_bold_run_is_flagged_bold():
    doc = parse.parse_document(fx.docx_with_table_and_bold_heading(), "docx")
    bold_heading = next(b for b in doc.blocks if b.text == "Item One Risk Factors Overview")
    assert bold_heading.bold is True


def test_parse_document_docx_partially_bold_paragraph_is_not_flagged_bold():
    """A single bold word inside an otherwise plain paragraph must not make the whole block a bold-run heading
    candidate (only a paragraph whose runs are ALL bold should be)."""
    doc = parse.parse_document(fx.docx_with_partially_bold_paragraph(), "docx")
    mixed = next(b for b in doc.blocks if b.text.startswith("This sentence has only one"))
    assert mixed.bold is False


def test_parse_document_md_end_to_end():
    doc = parse.parse_document(fx.MD_V1.encode("utf-8"), "md")
    assert doc.method == "text"
    headings = [b for b in doc.blocks if b.kind_hint == "heading_md"]
    assert {h.text for h in headings} >= {"Executive Summary Of Operations", "Item One Risk Factors Overview"}


def test_parse_document_html_end_to_end_scripts_dropped():
    doc = parse.parse_document(fx.HTML_SAMPLE.encode("utf-8"), "html")
    all_text = " ".join(b.text for b in doc.blocks)
    assert "dropped" not in all_text
    assert "color: red" not in all_text
    assert any(b.kind_hint == "heading_html" for b in doc.blocks)


def test_parse_document_md_hash_inside_backtick_fence_is_not_a_heading():
    """A ``#`` line inside a fenced code block (a shell comment, a Python comment, ...) must never be read as a
    Markdown heading (M4_PLAN.md 15.7)."""
    md = ("# Real Heading One\n\n"
         "```\n"
         "# not a heading: a shell comment inside a fenced code block\n"
         "echo hello\n"
         "```\n\n"
         "# Real Heading Two\n\n"
         "Body paragraph after the fence.\n")
    doc = parse.parse_document(md.encode("utf-8"), "md")
    headings = {b.text for b in doc.blocks if b.kind_hint == "heading_md"}
    assert headings == {"Real Heading One", "Real Heading Two"}
    all_text = " ".join(b.text for b in doc.blocks)
    assert "not a heading" in all_text                        # fence content preserved as body text, not dropped


def test_parse_document_md_hash_inside_tilde_fence_is_not_a_heading():
    md = "# Real Heading\n\n~~~\n# not a heading either\n~~~\n\nBody paragraph.\n"
    doc = parse.parse_document(md.encode("utf-8"), "md")
    headings = {b.text for b in doc.blocks if b.kind_hint == "heading_md"}
    assert headings == {"Real Heading"}


def test_parse_document_md_unterminated_fence_treats_rest_of_document_as_code():
    """An unterminated fence still suppresses heading detection for every line after it (never crashes, never
    silently resumes heading detection)."""
    md = "# Real Heading\n\n```\n# still not a heading\n\n# also not a heading\n"
    doc = parse.parse_document(md.encode("utf-8"), "md")
    headings = {b.text for b in doc.blocks if b.kind_hint == "heading_md"}
    assert headings == {"Real Heading"}


def test_parse_document_txt_end_to_end():
    doc = parse.parse_document(b"Paragraph one here.\n\nParagraph two here.", "txt")
    assert [b.text for b in doc.blocks] == ["Paragraph one here.", "Paragraph two here."]


# --------------------------------------------------------------------------
# worker-reported error codes, real subprocess
# --------------------------------------------------------------------------

def test_parse_document_blank_pdf_is_empty():
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(fx.blank_pdf(2), "pdf")
    assert exc.value.code == "empty"


def test_parse_document_sparse_pdf_is_scanned():
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(fx.sparse_pdf(3), "pdf")
    assert exc.value.code == "scanned"


def test_parse_document_over_page_cap_is_too_many_pages():
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(fx.simple_pdf(), "pdf", max_pages=2)
    assert exc.value.code == "too_many_pages"


def test_parse_document_empty_bytes_is_empty():
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(b"", "txt")
    assert exc.value.code == "empty"


# --------------------------------------------------------------------------
# section 15.7: a structural active-content scan INSIDE the sandbox catches what the raw byte scan
# (gate.check_pdf_bytes, run before the sandbox) cannot: names hidden in a compressed object stream
# --------------------------------------------------------------------------

def test_gate_raw_scan_is_blind_to_javascript_hidden_in_an_object_stream():
    """Precondition for the next two tests: the raw byte scan really cannot see this (it is a regex over raw
    bytes; a compressed FlateDecode object stream's content is opaque to it)."""
    from semigraph.uploads import gate

    gate.check_pdf_bytes(fx.pdf_with_javascript_hidden_in_object_stream())          # must NOT raise
    gate.check_pdf_bytes(fx.pdf_with_hash_escaped_name_hidden_in_object_stream())   # must NOT raise


def test_parse_document_rejects_javascript_hidden_in_an_object_stream():
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(fx.pdf_with_javascript_hidden_in_object_stream(), "pdf")
    assert exc.value.code == "active_content"


def test_parse_document_rejects_hash_escaped_name_hidden_in_an_object_stream():
    """``#4A#61vaScript`` decodes to ``/JavaScript``; pdfminer decodes ``#xx`` escapes as part of normal
    tokenization, so the structural scan sees the escaped name too."""
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(fx.pdf_with_hash_escaped_name_hidden_in_object_stream(), "pdf")
    assert exc.value.code == "active_content"


def test_parse_document_accepts_a_clean_pdf_with_no_structural_active_content():
    doc = parse.parse_document(fx.simple_pdf(), "pdf")           # must not raise active_content
    assert doc.pages == 4


def test_scan_pdf_structural_active_content_does_not_fail_open_on_a_pathological_object():
    """A deeply nested array in one object must never let a LATER, genuinely malicious object escape the scan: a
    recursive walk would hit ``RecursionError`` on the nested array, which an ``except Exception`` around the
    whole scan would then swallow, silently skipping every object not yet visited (including the real
    ``/JavaScript`` one). The walk must be bounded/iterative, and the whole document must still be rejected."""
    from semigraph.uploads import parse_worker

    data = fx.pdf_with_objstm_objects([b"[" * 5000 + b"]" * 5000, b"<< /S /JavaScript >>"])
    with pytest.raises(parse_worker._ParseWorkerError) as exc:
        parse_worker._scan_pdf_structural_active_content(data)
    assert exc.value.code in ("active_content", "parse_failed")


def test_scan_pdf_structural_active_content_getobj_failure_fails_closed():
    """Once the document has been opened and its object ids listed successfully, a failure reading any ONE of them
    must reject the whole document (``parse_failed``), never silently skip that object and keep going: a
    structural active-content scan that can be defeated by corrupting the one object it would have flagged is not
    a scan. (Corrupting the compressed ``/ObjStm`` bytes makes pdfminer's own ``getobj()`` raise for every object
    stored in it, while the cross-reference table still lists them — a realistic single-object failure, not a
    contrived mock.)"""
    from semigraph.uploads import parse_worker

    with pytest.raises(parse_worker._ParseWorkerError) as exc:
        parse_worker._scan_pdf_structural_active_content(fx.pdf_with_a_corrupted_object_stream())
    assert exc.value.code == "parse_failed"


def test_scan_pdf_structural_active_content_rejects_over_the_object_count_bound(monkeypatch):
    """Direct unit test of the bound itself (section 15.7): a document listing more objects than the cap is
    rejected outright as ``parse_failed``, never walked — proven by lowering the cap rather than building a real
    50,000-object PDF."""
    from semigraph.uploads import parse_worker

    monkeypatch.setattr(parse_worker, "_MAX_STRUCTURAL_PDF_OBJECTS", 2)
    with pytest.raises(parse_worker._ParseWorkerError) as exc:
        parse_worker._scan_pdf_structural_active_content(fx.simple_pdf())          # a clean PDF, > 2 objects
    assert exc.value.code == "parse_failed"


def test_scan_pdf_structural_active_content_is_a_noop_for_a_non_pdf_or_unparsable_input():
    """Defense in depth only: something pdfminer cannot structurally parse at all must not itself become a
    failure (that verdict is left to the byte scan and the real content extractors)."""
    from semigraph.uploads import parse_worker

    parse_worker._scan_pdf_structural_active_content(b"not a pdf at all")          # must not raise


# --------------------------------------------------------------------------
# subprocess plumbing: timeout, output cap, malformed output (fixture workers)
# --------------------------------------------------------------------------

def test_parse_document_is_killed_at_timeout(monkeypatch):
    monkeypatch.setattr(parse, "_worker_command",
                        lambda kind, max_pages: [sys.executable, "-c", "import time; time.sleep(60)"])
    start = time.monotonic()
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(b"irrelevant", "pdf", timeout_s=1)
    elapsed = time.monotonic() - start
    assert exc.value.code == "timeout"
    assert elapsed < 20, "the subprocess was not actually killed at the timeout"


def test_parse_document_rejects_oversized_stdout(monkeypatch):
    oversized = parse.MAX_OUTPUT_BYTES + 1024
    script = f"import sys; sys.stdout.buffer.write(b'x' * {oversized})"
    monkeypatch.setattr(parse, "_worker_command", lambda kind, max_pages: [sys.executable, "-c", script])
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(b"irrelevant", "pdf", timeout_s=30)
    assert exc.value.code == "too_large"


def test_parse_document_malformed_json_is_parse_failed(monkeypatch):
    monkeypatch.setattr(parse, "_worker_command",
                        lambda kind, max_pages: [sys.executable, "-c", "print('not json at all')"])
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(b"irrelevant", "pdf", timeout_s=30)
    assert exc.value.code == "parse_failed"


def test_parse_document_unknown_error_code_from_worker_maps_to_parse_failed(monkeypatch):
    script = "import json, sys; sys.stdout.write(json.dumps({'error': 'not_a_real_code'}))"
    monkeypatch.setattr(parse, "_worker_command", lambda kind, max_pages: [sys.executable, "-c", script])
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(b"irrelevant", "pdf", timeout_s=30)
    assert exc.value.code == "parse_failed"


def test_parse_document_rejects_unknown_block_kind_hint(monkeypatch):
    payload = json.dumps({"method": "x", "pages": 1, "chars_per_page": 10.0, "warnings": [],
                          "blocks": [{"text": "hi", "page": 1, "size": 12.0, "bold": False,
                                     "kind_hint": "not_a_real_hint"}]})
    script = f"import sys; sys.stdout.write({payload!r})"
    monkeypatch.setattr(parse, "_worker_command", lambda kind, max_pages: [sys.executable, "-c", script])
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(b"irrelevant", "pdf", timeout_s=30)
    assert exc.value.code == "parse_failed"


# --------------------------------------------------------------------------
# finding #6: the child gets an ALLOWLISTED environment, never a copy of the parent's (no API keys, tokens,
# Neo4j credentials, ...)
# --------------------------------------------------------------------------

_SECRET_LEAK_RE = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|^NEO4J_", re.IGNORECASE)


def test_child_env_is_an_allowlist_not_a_copy_of_the_parent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak-either")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-should-never-leak")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "ts-should-never-leak")
    monkeypatch.setenv("NEO4J_PASSWORD", "neo4j-should-never-leak")
    monkeypatch.setenv("NEO4J_URI", "bolt://should-never-leak")
    monkeypatch.setenv("SOME_UNRELATED_PARENT_VAR", "must not leak either: not on the allowlist")

    env = parse._child_env()

    assert not any(_SECRET_LEAK_RE.search(name) for name in env), env.keys()
    assert "SOME_UNRELATED_PARENT_VAR" not in env
    assert env["MALLOC_ARENA_MAX"] == "2"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["LANG"] == "C.UTF-8"
    assert "PYTHONPATH" in env
    assert "PATH" in env


def test_child_env_allowlist_is_exactly_the_documented_set(monkeypatch):
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    env = parse._child_env()
    allowed = {"PATH", "PYTHONPATH", "MALLOC_ARENA_MAX", "PYTHONDONTWRITEBYTECODE", "LANG"}
    if sys.platform.startswith("win"):
        allowed |= {"SYSTEMROOT", "TEMP", "TMP"} & set(env)          # only the ones actually needed to start
    assert set(env) <= allowed


def test_child_subprocess_never_actually_receives_secret_env_vars(monkeypatch, tmp_path):
    """End-to-end: a REAL spawned child (through ``parse_document`` / ``_child_env`` / ``Popen``) never sees the
    parent's secrets, not just the dict ``_child_env()`` builds in isolation."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("NEO4J_PASSWORD", "neo4j-should-never-leak")
    dump_path = tmp_path / "child_env_dump.json"
    script = textwrap.dedent(f"""
        import json, os
        with open({str(dump_path)!r}, "w") as f:
            json.dump(dict(os.environ), f)
        print(json.dumps({{"error": "parse_failed"}}))
    """)
    monkeypatch.setattr(parse, "_worker_command", lambda kind, max_pages: [sys.executable, "-c", script])
    with pytest.raises(parse.ParseError):
        parse.parse_document(b"irrelevant", "pdf", timeout_s=30)
    child_env = json.loads(dump_path.read_text())
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "NEO4J_PASSWORD" not in child_env
    assert not any(_SECRET_LEAK_RE.search(name) for name in child_env), child_env.keys()


# --------------------------------------------------------------------------
# finding #28: the child reports its exception CLASS NAME (never the message), and the parent logs code +
# exc_type only
# --------------------------------------------------------------------------

def test_worker_main_reports_exc_type_never_the_exception_message(monkeypatch, tmp_path):
    """A direct unit test of ``parse_worker.main``'s catch-all handler: an unexpected exception (a broken native
    dependency, not one of the worker's own recognised codes) must surface its CLASS NAME to the parent, and its
    message — which could quote document text — must never reach stdout."""
    from semigraph.uploads import parse_worker

    secret_marker = "SECRET_DOCUMENT_TEXT_MUST_NEVER_LEAK"
    monkeypatch.setattr(parse_worker, "_parse",
                        lambda kind, data, max_pages: (_ for _ in ()).throw(RuntimeError(secret_marker)))
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": __import__("io").BytesIO(b"irrelevant bytes")})())
    out = __import__("io").BytesIO()
    monkeypatch.setattr(sys, "stdout", type("S", (), {"buffer": out})())

    rc = parse_worker.main(["parse_worker", "pdf", "30"])

    assert rc == 1
    payload = json.loads(out.getvalue())
    assert payload == {"error": "parse_failed", "exc_type": "RuntimeError"}
    assert secret_marker not in out.getvalue().decode("utf-8")


def test_parse_document_surfaces_exc_type_from_a_child_reported_unexpected_exception(monkeypatch):
    script = ("import json, sys; "
             "sys.stdout.write(json.dumps({'error': 'parse_failed', 'exc_type': 'ImportError'}))")
    monkeypatch.setattr(parse, "_worker_command", lambda kind, max_pages: [sys.executable, "-c", script])
    with pytest.raises(parse.ParseError) as exc:
        parse.parse_document(b"irrelevant", "pdf", timeout_s=30)
    assert exc.value.code == "parse_failed"
    assert exc.value.exc_type == "ImportError"


def test_parse_document_logs_code_and_exc_type_never_the_message(monkeypatch, caplog):
    secret_marker = "SECRET_DOCUMENT_TEXT_MUST_NEVER_APPEAR_IN_LOGS"
    script = ("import json, sys; "
             f"sys.stdout.write(json.dumps({{'error': 'parse_failed', 'exc_type': 'ImportError', "
             f"'message': {secret_marker!r}}}))")
    monkeypatch.setattr(parse, "_worker_command", lambda kind, max_pages: [sys.executable, "-c", script])
    with caplog.at_level("WARNING", logger="semigraph.uploads.parse"):
        with pytest.raises(parse.ParseError):
            parse.parse_document(b"irrelevant", "pdf", timeout_s=30)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "parse_failed" in logged
    assert "ImportError" in logged
    assert secret_marker not in logged                       # the child's own "message" field is never logged


def test_parse_document_logs_severe_exc_types_at_error_level(monkeypatch, caplog):
    script = "import json, sys; sys.stdout.write(json.dumps({'error': 'parse_failed', 'exc_type': 'ImportError'}))"
    monkeypatch.setattr(parse, "_worker_command", lambda kind, max_pages: [sys.executable, "-c", script])
    with caplog.at_level("WARNING", logger="semigraph.uploads.parse"):
        with pytest.raises(parse.ParseError):
            parse.parse_document(b"irrelevant", "pdf", timeout_s=30)
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_parse_error_carries_no_exc_type_by_default():
    assert parse.ParseError("empty").exc_type is None


def test_worker_command_invokes_the_real_module():
    cmd = parse._worker_command("pdf", 30)
    assert cmd[0] == sys.executable
    assert cmd[1:] == ["-m", "semigraph.uploads.parse_worker", "pdf", "30"]


def test_parse_error_rejects_unknown_code():
    with pytest.raises(ValueError):
        parse.ParseError("not_a_real_code")


# --------------------------------------------------------------------------
# RLIMIT_AS under the real 30-page / max-size fixture (Linux only; Windows proves nothing here)
# --------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "linux",
                    reason="RLIMIT_AS/preexec-free rlimits are Linux-only; proven by the serve-shipped CI job")
def test_max_size_pdf_parses_under_rlimit_as_and_reports_child_rss(tmp_path):
    """Runs ``parse_document`` inside a FRESH wrapper subprocess so ``RUSAGE_CHILDREN`` reflects only this
    fixture's own parse-worker child, not whatever else this pytest session has reaped (advisor guidance)."""
    fixture_path = tmp_path / "max.pdf"
    fixture_path.write_bytes(fx.max_size_pdf(n_pages=30, chars_per_page=2700))
    script = textwrap.dedent(f"""
        import json, resource, sys
        sys.path.insert(0, {_SRC_DIR!r})
        from semigraph.uploads.parse import parse_document
        data = open({str(fixture_path)!r}, "rb").read()
        doc = parse_document(data, "pdf", timeout_s=90, max_pages=30)
        rss_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        print(json.dumps({{"pages": doc.pages, "ru_maxrss_kb": rss_kb}}))
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["pages"] == 30
    print("parse-worker child ru_maxrss (KB):", payload["ru_maxrss_kb"])
    assert payload["ru_maxrss_kb"] < 300 * 1024, "the child exceeded the 300 MB acceptance bound under RLIMIT_AS 1 GiB"
