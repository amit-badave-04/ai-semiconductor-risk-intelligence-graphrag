"""Fixture builders for the upload text pipeline tests (M4_PLAN.md 4.2, Worker A).

PDFs are hand-built minimal PDF syntax (standard-14 Helvetica / Helvetica-Bold fonts, an explicit xref table) so no
PDF-writing library is needed; the security fixtures reuse that byte stream and splice in the flagged tokens
directly, since ``gate.check_pdf_bytes`` is a raw byte scan and never requires a fully cross-referenced object
graph. DOCX fixtures use ``python-docx`` (already a project dependency) and raw ``zipfile`` for the zip-bomb cases.
"""

from __future__ import annotations

import io
import zipfile

# --------------------------------------------------------------------------
# PDF: minimal hand-built syntax
# --------------------------------------------------------------------------

PAGE_SIZE = (612, 792)


def _pdf_escape(text: str) -> bytes:
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    return escaped.encode("latin-1", "replace")


def build_pdf(pages: list[list[tuple[str, float, bool]]], *, header: str | None = None) -> bytes:
    """A minimal, valid, hand-built PDF. ``pages``: per page, a top-to-bottom list of ``(text, size, bold)``
    lines (each becomes one ``Tj`` at a fresh ``Td``; long "paragraph" text is drawn as one line — pypdfium2 does
    not need real word-wrap to extract the text). ``header``: an optional line rendered first on every page, at a
    distinct large bold size, to simulate a running header/footer for the header-filter tests.
    """
    n_pages = len(pages)
    page_obj_nums = list(range(3, 3 + n_pages))
    content_obj_nums = list(range(3 + n_pages, 3 + 2 * n_pages))
    font1_num = 3 + 2 * n_pages
    font2_num = font1_num + 1

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_obj_nums)}] /Count {n_pages} >>".encode(),
        font1_num: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        font2_num: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    }
    for i, pnum in enumerate(page_obj_nums):
        objects[pnum] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_SIZE[0]} {PAGE_SIZE[1]}] "
            f"/Resources << /Font << /F1 {font1_num} 0 R /F2 {font2_num} 0 R >> >> "
            f"/Contents {content_obj_nums[i]} 0 R >>"
        ).encode()
        objects[content_obj_nums[i]] = _content_stream(pages[i], header)
    return _assemble_pdf(objects)


def _content_stream(lines: list[tuple[str, float, bool]], header: str | None) -> bytes:
    all_lines = list(lines)
    if header is not None:
        all_lines = [(header, 9.0, True)] + all_lines
    parts, y = [], PAGE_SIZE[1] - 60
    for text, size, bold in all_lines:
        font = "F2" if bold else "F1"
        parts.append(b"BT /%b %g Tf 60 %g Td (%b) Tj ET\n" % (font.encode(), size, y, _pdf_escape(text)))
        y -= max(size * 1.4, 14)
    stream = b"".join(parts)
    return f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"


def _assemble_pdf(objects: dict[int, bytes]) -> bytes:
    max_num = max(objects)
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in range(1, max_num + 1):
        offsets[num] = buf.tell()
        buf.write(f"{num} 0 obj\n".encode())
        buf.write(objects[num])
        buf.write(b"\nendobj\n")
    xref_offset = buf.tell()
    buf.write(f"xref\n0 {max_num + 1}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for num in range(1, max_num + 1):
        buf.write(f"{offsets[num]:010d} 00000 n \n".encode())
    buf.write(f"trailer\n<< /Size {max_num + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode())
    return buf.getvalue()


def simple_pdf() -> bytes:
    """A 4-page PDF: a running header, real headings and body paragraphs on every page."""
    pages = []
    sections = [
        ("Executive Summary Of Operations",
         "The company reported steady operational performance across its principal business segments this year.",
         "Revenue grew modestly while operating margins held roughly flat compared with the prior fiscal year period."),
        ("Item One Risk Factors Overview",
         "There is a risk related to supply chain disruptions that could affect production timelines significantly.",
         "Component shortages at third-party suppliers could further delay shipments to several key customer accounts."),
        ("Market Trends And Outlook Today",
         "Demand for our products remains resilient in most regions with some softness in select emerging markets.",
         "Management expects gradual improvement in demand conditions over the remaining quarters of the fiscal year."),
        ("Legal Proceedings Overview Statement",
         "The company is party to routine litigation arising in the ordinary course of business operations worldwide.",
         "None of the currently pending matters is expected to have a material adverse effect on financial condition."),
    ]
    for heading, body1, body2 in sections:
        pages.append([(heading, 16.0, True), (body1, 12.0, False), (body2, 12.0, False)])
    return build_pdf(pages, header="COMPANY CONFIDENTIAL DRAFT")


def pdf_with_javascript() -> bytes:
    """A PDF whose byte stream carries a ``/JavaScript`` action (never a real, cross-referenced one: the gate is
    a raw byte scan run BEFORE any object graph is parsed, so this is enough to prove detection)."""
    base = build_pdf([[("Heading Placeholder Text", 16.0, True), ("Body text placeholder for this fixture.", 12.0, False)]])
    injection = b"99 0 obj\n<< /Type /Action /S /JavaScript /JS (app.alert\\(1\\);) >>\nendobj\n%%EOF"
    return base.replace(b"%%EOF", injection)


def pdf_with_encryption() -> bytes:
    """A PDF whose trailer dictionary declares ``/Encrypt`` (real RC4/AES setup is not needed: the gate rejects on
    the raw ``/Encrypt`` key alone, exactly as it must for a document it will never be able to decrypt)."""
    base = build_pdf([[("Heading Placeholder Text", 16.0, True), ("Body text placeholder for this fixture.", 12.0, False)]])
    return base.replace(b"/Root 1 0 R >>", b"/Root 1 0 R /Encrypt 9 0 R >>")


def exe_bytes_renamed_pdf() -> bytes:
    """A Windows PE-style byte blob (real ``MZ`` DOS-stub magic) given a ``.pdf`` filename."""
    return b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + bytes(range(256)) * 4


def blank_pdf(n_pages: int = 2) -> bytes:
    """Pages with no text content at all (the "no text -> empty" parser fixture)."""
    return build_pdf([[] for _ in range(n_pages)])


def sparse_pdf(n_pages: int = 3) -> bytes:
    """A few words per page, well under the 200-chars/page scanned threshold on both extractors."""
    return build_pdf([[("Confidential", 10.0, False)] for _ in range(n_pages)])


def max_size_pdf(n_pages: int = 30, chars_per_page: int = 2700) -> bytes:
    """A dense ``n_pages``-page PDF at roughly ``chars_per_page`` characters/page (the RLIMIT_AS / page-cap
    fixture): one heading plus enough distinct filler sentences to reach the target density."""
    filler = ("Segment {n} of the quarterly disclosure discusses operational results, cost trends and "
             "currency effects across the reporting period in some additional detail for this test. ")
    pages = []
    for p in range(1, n_pages + 1):
        body, n = "", 0
        while len(body) < chars_per_page:
            body += filler.format(n=n)
            n += 1
        pages.append([(f"Section Heading Number {p} Text", 16.0, True), (body, 12.0, False)])
    return build_pdf(pages, header="COMPANY CONFIDENTIAL DRAFT")


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------

def docx_with_table_and_bold_heading() -> bytes:
    """A DOCX with a real ``Heading 1`` style paragraph, a BOLD-RUN heading that keeps the "Normal" style (the
    verified python-docx quirk: a bold 12pt run still reports style "Normal"), a table, and plain paragraphs."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.add_heading("Executive Summary Of Operations", level=1)
    doc.add_paragraph("The company reported steady operational performance across its principal segments this year.")

    bold_heading = doc.add_paragraph()
    run = bold_heading.add_run("Item One Risk Factors Overview")
    run.bold = True
    run.font.size = Pt(12)
    doc.add_paragraph("There is a risk related to supply chain disruptions affecting production timelines significantly.")

    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Metric Name"
    table.cell(0, 1).text = "Metric Value"
    table.cell(1, 0).text = "Revenue growth rate for the segment during the most recent reporting period disclosed."
    table.cell(1, 1).text = "Approximately twelve percent year over year across the comparable reporting periods."

    doc.add_heading("Market Trends And Outlook Today", level=1)
    doc.add_paragraph("Demand for our products remains resilient in most regions with softness in some emerging markets.")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def docx_with_partially_bold_paragraph() -> bytes:
    """A paragraph where only ONE word of several is bold: must NOT be flagged as a bold-run heading (only a
    paragraph whose non-empty runs are ALL bold should be)."""
    from docx import Document

    doc = Document()
    doc.add_heading("Executive Summary Of Operations", level=1)
    p = doc.add_paragraph()
    p.add_run("This sentence has only one ")
    bold_run = p.add_run("bold")
    bold_run.bold = True
    p.add_run(" word among several plain ones in the same paragraph here.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def zip_bomb_docx_member_count(n_members: int = 201) -> bytes:
    """A docx-shaped zip with more members than ``gate.DOCX_MAX_MEMBERS`` allows."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
        for i in range(n_members):
            zf.writestr(f"word/media/image{i}.bin", b"x")
    return buf.getvalue()


def zip_bomb_docx_ratio(uncompressed_bytes: int = 10 * 1024 * 1024) -> bytes:
    """A docx-shaped zip whose ``word/document.xml`` member alone expands far past the compression-ratio bound."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", b"\x00" * uncompressed_bytes)
    return buf.getvalue()


def docx_with_disallowed_member() -> bytes:
    """A docx-shaped zip carrying a member outside the allowed package layout (``active_content``)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
        zf.writestr("word/vbaProject.bin", b"macro-bytes")
    return buf.getvalue()


def plain_zip_not_docx() -> bytes:
    """An ordinary zip (no ``word/document.xml``): shares the PK magic with a docx but is not one."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("hello.txt", "just a plain zip file, not a document")
    return buf.getvalue()


# --------------------------------------------------------------------------
# Markdown v1 / v2: a known edit set (1 added, 1 removed, 2 changed, 1 tense-only edit that must NOT be reported)
# --------------------------------------------------------------------------

MD_V1 = """\
# Executive Summary Of Operations

The company reported strong results across its principal business segments during the reporting period this year.

# Item One Risk Factors Overview

Supply chain disruptions could delay shipments to key customers in several regions during the current fiscal year.
Competitive pressure in the memory market has intensified over the past twelve months across multiple product lines.

# Market Trends And Outlook Today

Demand for data center products grew rapidly in the current fiscal year across nearly every served customer segment.
Currency fluctuations had a modest favorable effect on reported revenue during the most recent quarterly period.

# Legal Proceedings Overview Statement

The company is party to routine litigation arising in the ordinary course of business across several jurisdictions.
None of the pending matters is expected to have a material adverse effect on the company's consolidated financial position.

# Company History And Background

The company was founded decades ago and has expanded its manufacturing footprint significantly since that time.
"""

MD_V2 = """\
# Executive Summary Of Operations

The company reported strong results across its principal business segments during the reporting period this year.

# Item One Risk Factors Overview

Competitive pressure in the memory market has intensified over the past twelve months across multiple product lines.
A new export licensing regime now restricts shipments of certain advanced components to a subset of overseas markets.

# Market Trends And Outlook Today

Demand for data center products grew rapidly in the current fiscal year across nearly every served customer segment.
Component shortages at several third-party foundries constrained output during the back half of the fiscal year.

# Company History And Background

The company was founded decades ago and has expanded its manufacturing footprint significantly since that time period.

# Cybersecurity Risk Management Practices

The company maintains a dedicated program to identify, assess and remediate cybersecurity risks across its global network.
"""


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

HTML_SAMPLE = """\
<html><head><title>Sample</title><style>body { color: red; }</style>
<script>console.log("dropped");</script></head>
<body>
<h1>Executive Summary Of Operations</h1>
<p>The company reported steady operational performance across its principal business segments this year.</p>
<h2>Item One Risk Factors Overview</h2>
<p>There is a risk related to supply chain disruptions that could affect production timelines significantly.</p>
</body></html>
"""
