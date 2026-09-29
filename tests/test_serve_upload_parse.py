"""Tests for semigraph.uploads.parse / parse_worker (M4_PLAN.md 4.2, 5, 14.2, Worker A)."""

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
