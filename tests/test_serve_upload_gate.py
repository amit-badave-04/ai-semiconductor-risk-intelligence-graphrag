"""Tests for semigraph.uploads.gate (M4_PLAN.md 4.2, 5, 14.2, Worker A)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import upload_fixtures as fx  # noqa: E402

from semigraph.uploads import gate  # noqa: E402


# --------------------------------------------------------------------------
# sniff: binary kinds via magic bytes
# --------------------------------------------------------------------------

def test_sniff_pdf_from_magic_bytes():
    data = fx.simple_pdf()
    assert gate.sniff(data[:4096], "whatever.txt") == "pdf"     # filename never trusted for a binary kind


def test_sniff_docx_from_magic_bytes():
    data = fx.docx_with_table_and_bold_heading()
    assert gate.sniff(data[:4096], "whatever.bin") == "docx"


def test_sniff_exe_bytes_renamed_pdf_is_unsupported():
    data = fx.exe_bytes_renamed_pdf()
    with pytest.raises(gate.GateError) as exc:
        gate.sniff(data[:4096], "sample.pdf")
    assert exc.value.code == "unsupported_type"


def test_sniff_empty_bytes_raises_empty():
    with pytest.raises(gate.GateError) as exc:
        gate.sniff(b"", "empty.txt")
    assert exc.value.code == "empty"


def test_sniff_random_binary_junk_is_unsupported():
    junk = bytes([0xDE, 0xAD, 0xBE, 0xEF]) * 20
    with pytest.raises(gate.GateError) as exc:
        gate.sniff(junk, "junk.pdf")
    assert exc.value.code == "unsupported_type"


# --------------------------------------------------------------------------
# sniff: text kinds via UTF-8 decode + declared extension
# --------------------------------------------------------------------------

@pytest.mark.parametrize("filename, expected", [
    ("notes.md", "md"), ("notes.markdown", "md"), ("page.html", "html"), ("page.htm", "html"),
    ("plain.txt", "txt"),
], ids=["md", "markdown-ext", "html", "htm", "txt"])
def test_sniff_text_kind_from_declared_extension(filename, expected):
    assert gate.sniff("hello world, this is plain text.".encode("utf-8"), filename) == expected


def test_sniff_text_bytes_with_unsupported_extension_is_unsupported_type():
    with pytest.raises(gate.GateError) as exc:
        gate.sniff(b"hello world", "notes.docx.bak")
    assert exc.value.code == "unsupported_type"


def test_sniff_binary_data_with_txt_extension_is_rejected_not_read_as_text():
    binary = bytes([0x00, 0x01, 0x02, 0xFF, 0xFE]) * 4
    with pytest.raises(gate.GateError) as exc:
        gate.sniff(binary, "notes.txt")
    assert exc.value.code == "unsupported_type"


def test_sniff_utf8_multibyte_character_cut_at_head_boundary_still_decodes():
    text = "café resume with a trailing accented character café"
    encoded = text.encode("utf-8")
    truncated = encoded[:-1]                    # cuts the last multibyte character in half
    assert gate.sniff(truncated, "notes.txt") == "txt"


def test_sniff_nul_byte_in_text_head_is_rejected():
    with pytest.raises(gate.GateError) as exc:
        gate.sniff(b"hello\x00world", "notes.txt")
    assert exc.value.code == "unsupported_type"


# --------------------------------------------------------------------------
# PDF active content / encryption
# --------------------------------------------------------------------------

def test_check_pdf_bytes_accepts_a_clean_pdf():
    gate.check_pdf_bytes(fx.simple_pdf())               # no exception


def test_check_pdf_bytes_rejects_javascript():
    with pytest.raises(gate.GateError) as exc:
        gate.check_pdf_bytes(fx.pdf_with_javascript())
    assert exc.value.code == "active_content"


def test_check_pdf_bytes_rejects_encryption():
    with pytest.raises(gate.GateError) as exc:
        gate.check_pdf_bytes(fx.pdf_with_encryption())
    assert exc.value.code == "encrypted"


@pytest.mark.parametrize("token", ["/JavaScript", "/JS", "/Launch", "/OpenAction", "/AA", "/EmbeddedFile", "/RichMedia"],
                        ids=["javascript", "js", "launch", "openaction", "aa", "embeddedfile", "richmedia"])
def test_check_pdf_bytes_rejects_each_active_content_name(token):
    base = fx.build_pdf([[("Heading Placeholder Text", 16.0, True), ("Body placeholder text right here.", 12.0, False)]])
    injected = base.replace(b"%%EOF", f"99 0 obj\n<< {token} (x) >>\nendobj\n%%EOF".encode())
    with pytest.raises(gate.GateError) as exc:
        gate.check_pdf_bytes(injected)
    assert exc.value.code == "active_content"


def test_check_pdf_bytes_name_boundary_js_does_not_match_json():
    base = fx.build_pdf([[("Heading Placeholder Text", 16.0, True), ("Body placeholder text right here.", 12.0, False)]])
    injected = base.replace(b"%%EOF", b"99 0 obj\n<< /Type /JSON /Value (x) >>\nendobj\n%%EOF")
    gate.check_pdf_bytes(injected)                       # must NOT raise: /JSON is not /JS


def test_check_pdf_bytes_name_boundary_aa_does_not_match_aapl():
    base = fx.build_pdf([[("Heading Placeholder Text", 16.0, True), ("Body placeholder text right here.", 12.0, False)]])
    injected = base.replace(b"%%EOF", b"99 0 obj\n<< /Ticker /AAPL >>\nendobj\n%%EOF")
    gate.check_pdf_bytes(injected)                       # must NOT raise: /AAPL is not /AA


def test_check_pdf_bytes_catches_hex_escaped_name():
    """``/J#61vaScript`` decodes to ``/JavaScript`` per the PDF name-escape syntax; a scanner that only matches the
    literal ASCII name would miss it."""
    base = fx.build_pdf([[("Heading Placeholder Text", 16.0, True), ("Body placeholder text right here.", 12.0, False)]])
    injected = base.replace(b"%%EOF", b"99 0 obj\n<< /S /J#61vaScript /JS (x) >>\nendobj\n%%EOF")
    with pytest.raises(gate.GateError) as exc:
        gate.check_pdf_bytes(injected)
    assert exc.value.code == "active_content"


# --------------------------------------------------------------------------
# DOCX zip checks
# --------------------------------------------------------------------------

def test_check_docx_zip_accepts_a_clean_docx():
    gate.check_docx_zip(fx.docx_with_table_and_bold_heading())     # no exception


def test_check_docx_zip_rejects_member_count_bomb():
    with pytest.raises(gate.GateError) as exc:
        gate.check_docx_zip(fx.zip_bomb_docx_member_count())
    assert exc.value.code == "zip_bomb"


def test_check_docx_zip_rejects_ratio_bomb():
    with pytest.raises(gate.GateError) as exc:
        gate.check_docx_zip(fx.zip_bomb_docx_ratio())
    assert exc.value.code == "zip_bomb"


def test_check_docx_zip_rejects_disallowed_member():
    with pytest.raises(gate.GateError) as exc:
        gate.check_docx_zip(fx.docx_with_disallowed_member())
    assert exc.value.code == "active_content"


def test_check_docx_zip_rejects_plain_zip_without_document_xml():
    with pytest.raises(gate.GateError) as exc:
        gate.check_docx_zip(fx.plain_zip_not_docx())
    assert exc.value.code in ("active_content", "unsupported_type")


def test_check_docx_zip_rejects_absolute_member_path():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
        info = zipfile.ZipInfo("/etc/passwd")
        zf.writestr(info, "malicious")
    with pytest.raises(gate.GateError) as exc:
        gate.check_docx_zip(buf.getvalue())
    assert exc.value.code == "active_content"


def test_check_docx_zip_rejects_parent_traversal_member_path():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
        zf.writestr("word/../../../etc/passwd", "malicious")
    with pytest.raises(gate.GateError) as exc:
        gate.check_docx_zip(buf.getvalue())
    assert exc.value.code == "active_content"


def test_check_docx_zip_rejects_not_a_zip_at_all():
    with pytest.raises(gate.GateError) as exc:
        gate.check_docx_zip(b"not actually a zip file at all")
    assert exc.value.code == "unsupported_type"


# --------------------------------------------------------------------------
# check_bytes dispatch (used by the route layer once a kind is known)
# --------------------------------------------------------------------------

def test_check_bytes_dispatches_pdf_and_docx_and_is_a_noop_for_text_kinds():
    gate.check_bytes(fx.simple_pdf(), "pdf")
    gate.check_bytes(fx.docx_with_table_and_bold_heading(), "docx")
    gate.check_bytes(b"plain text", "txt")
    gate.check_bytes(b"# heading", "md")
    gate.check_bytes(b"<h1>hi</h1>", "html")


def test_check_bytes_rejects_javascript_pdf():
    with pytest.raises(gate.GateError) as exc:
        gate.check_bytes(fx.pdf_with_javascript(), "pdf")
    assert exc.value.code == "active_content"


def test_gate_error_rejects_unknown_code():
    with pytest.raises(ValueError):
        gate.GateError("not_a_real_code", "x")


# --------------------------------------------------------------------------
# error messages never echo upload-controlled content (never surfaced or logged as raw text)
# --------------------------------------------------------------------------

def test_sniff_error_message_never_echoes_the_filename():
    secret_marker = "xXSECRETMARKERXx"
    with pytest.raises(gate.GateError) as exc:
        gate.sniff(b"hello world", f"notes.{secret_marker}")
    assert secret_marker not in exc.value.message


def test_check_docx_zip_error_message_never_echoes_member_names():
    import io
    import zipfile as zf_module

    secret_marker = "xXSECRETMARKERXx"
    buf = io.BytesIO()
    with zf_module.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
        zf.writestr(f"word/{secret_marker}/vbaProject.bin", b"x")
    with pytest.raises(gate.GateError) as exc:
        gate.check_docx_zip(buf.getvalue())
    assert secret_marker not in exc.value.message


def test_check_docx_zip_bad_zip_message_never_echoes_library_exception_text():
    with pytest.raises(gate.GateError) as exc:
        gate.check_docx_zip(b"not actually a zip file at all, with unique text ZZMARKERZZ")
    assert "ZZMARKERZZ" not in exc.value.message
