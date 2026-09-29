"""Fixture builders for the upload text pipeline tests (M4_PLAN.md 4.2, Worker A).

PDFs are hand-built minimal PDF syntax (standard-14 Helvetica / Helvetica-Bold fonts, an explicit xref table) so no
PDF-writing library is needed; the security fixtures reuse that byte stream and splice in the flagged tokens
directly, since ``gate.check_pdf_bytes`` is a raw byte scan and never requires a fully cross-referenced object
graph. DOCX fixtures use ``python-docx`` (already a project dependency) and raw ``zipfile`` for the zip-bomb cases.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib

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


def _compress_objstm(hidden_obj_nums: list[int], hidden_bodies: list[bytes]) -> tuple[bytes, int]:
    header_parts, body_parts, offset = [], [], 0
    for num, body in zip(hidden_obj_nums, hidden_bodies):
        header_parts.append(f"{num} {offset}")
        body_parts.append(body)
        offset += len(body) + 1
    header = (" ".join(header_parts) + "\n").encode("ascii")
    content = header + b"\n".join(body_parts) + b"\n"
    return zlib.compress(content), len(header)


def _xref_stream_rows(offsets: dict[int, int], xref_offset: int, content_obj_num: int,
                      hidden_obj_nums: list[int]) -> bytes:
    def row(entry_type: int, f2: int, f3: int) -> bytes:
        return bytes([entry_type]) + f2.to_bytes(4, "big") + f3.to_bytes(2, "big")

    rows = bytearray()
    rows += row(0, 0, 65535)                                            # obj 0: free (mandatory first entry)
    for num in range(1, 6):
        rows += row(1, xref_offset if num == 5 else offsets[num], 0)    # obj 5 is the xref stream itself
    for i in range(len(hidden_obj_nums)):
        rows += row(2, 1, i)                                            # compressed in ObjStm 1, index i
    rows += row(1, offsets[content_obj_num], 0)
    return zlib.compress(bytes(rows))


def pdf_with_objstm_objects(hidden_bodies: list[bytes]) -> bytes:
    """A minimal, valid, one-page PDF (PDF 1.5 cross-reference STREAM, no classic xref table) whose ``hidden_bodies``
    (raw ``<< ... >>`` object dictionaries) are stored ONLY inside a compressed ``/ObjStm`` (FlateDecode) — never as
    plain top-level bytes — and are reachable ONLY by walking the xref table's own object list (they are not linked
    from the Catalog/Pages tree at all). A raw byte/regex scan of the file (``gate.check_pdf_bytes``) never sees
    their content; only a structural walk of every xref object, including compressed ones, does (section 15.7)."""
    n_hidden = len(hidden_bodies)
    hidden_obj_nums = list(range(6, 6 + n_hidden))
    content_obj_num = 6 + n_hidden                                       # right after the hidden objects
    size = content_obj_num + 1                                          # object numbers 0..content_obj_num
    compressed_objstm, first = _compress_objstm(hidden_obj_nums, hidden_bodies)

    buf = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}

    def emit(num: int, body_bytes: bytes) -> None:
        offsets[num] = len(buf)
        buf.extend(f"{num} 0 obj\n".encode())
        buf.extend(body_bytes)
        buf.extend(b"\nendobj\n")

    emit(1, (f"<< /Type /ObjStm /N {n_hidden} /First {first} /Length {len(compressed_objstm)} "
             f"/Filter /FlateDecode >>\nstream\n").encode() + compressed_objstm + b"\nendstream")
    emit(2, b"<< /Type /Catalog /Pages 3 0 R >>")                       # no reference to the hidden objects
    emit(3, b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>")
    emit(4, (f"<< /Type /Page /Parent 3 0 R /MediaBox [0 0 612 792] /Resources << >> "
            f"/Contents {content_obj_num} 0 R >>").encode())
    emit(content_obj_num, b"<< /Length 0 >>\nstream\n\nendstream")

    xref_offset = len(buf)
    compressed_rows = _xref_stream_rows(offsets, xref_offset, content_obj_num, hidden_obj_nums)
    buf.extend(b"5 0 obj\n")
    buf.extend((f"<< /Type /XRef /Size {size} /W [1 4 2] /Root 2 0 R /Index [0 {size}] "
               f"/Length {len(compressed_rows)} /Filter /FlateDecode >>\nstream\n").encode())
    buf.extend(compressed_rows)
    buf.extend(b"\nendstream\nendobj\n")
    buf.extend(f"startxref\n{xref_offset}\n%%EOF".encode())
    return bytes(buf)


def pdf_with_javascript_hidden_in_object_stream() -> bytes:
    """``/JavaScript`` + ``/JS`` stored only inside a compressed object stream (section 15.7 fixture)."""
    return pdf_with_objstm_objects([b"<< /S /JavaScript /JS (app.alert\\(1\\);) >>"])


def pdf_with_a_corrupted_object_stream() -> bytes:
    """A structurally valid PDF (same shape as :func:`pdf_with_javascript_hidden_in_object_stream`) whose ``/ObjStm``
    compressed bytes are corrupted: pdfminer's cross-reference table still lists every object, but ``getobj()``
    raises for the ones stored inside that stream. A structural scan that skips an object it cannot read, instead of
    rejecting the whole document, can be defeated by corrupting exactly the object that would have been flagged."""
    data = bytearray(pdf_with_javascript_hidden_in_object_stream())
    filter_idx = data.find(b"/Filter /FlateDecode")
    stream_idx = data.find(b"stream\n", filter_idx) + len(b"stream\n")
    data[stream_idx + 5] ^= 0xFF
    return bytes(data)


def pdf_with_hash_escaped_name_hidden_in_object_stream() -> bytes:
    """``#4A#61vaScript`` (decodes to ``/JavaScript`` per the PDF name-escape syntax) as a dict VALUE, and
    ``#4A#53`` (decodes to ``/JS``) as a dict KEY — both stored only inside a compressed object stream, and with NO
    plaintext active-content name anywhere in the object (unlike the plain ``/JS`` key form), so this fixture
    actually exercises escape DECODING and not just the plain-name match (section 15.7)."""
    return pdf_with_objstm_objects([b"<< /S /#4A#61vaScript /#4A#53 (app.alert\\(2\\);) >>"])


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


def _minimal_docx_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<document/>")
    return buf.getvalue()


def zip_bad_extract_version() -> bytes:
    """A docx-shaped zip whose central-directory ``version needed to extract`` field is set past
    ``zipfile.MAX_EXTRACT_VERSION``: ``zipfile.ZipFile()`` raises ``NotImplementedError``, not ``BadZipFile``
    (finding #12: a malformed zip container must never escape the gate as anything but ``GateError``)."""
    data = bytearray(_minimal_docx_zip())
    idx = data.find(b"PK\x01\x02")
    while idx != -1:
        version_needed_offset = idx + 6
        data[version_needed_offset:version_needed_offset + 2] = struct.pack("<H", 0xFFFF)
        idx = data.find(b"PK\x01\x02", idx + 4)
    return bytes(data)


def zip_bad_utf8_member_name() -> bytes:
    """A docx-shaped zip whose UTF-8 filename flag is set but the member name is not valid UTF-8:
    ``zipfile.ZipFile()`` raises ``UnicodeDecodeError`` while decoding the central directory
    (finding #12)."""
    data = bytearray(_minimal_docx_zip())
    for sig, flag_delta, namelen_delta, name_delta in ((b"PK\x03\x04", 6, 26, 30), (b"PK\x01\x02", 8, 28, 46)):
        idx = data.find(sig)
        while idx != -1:
            flag_off, namelen_off, name_off = idx + flag_delta, idx + namelen_delta, idx + name_delta
            flag = struct.unpack("<H", data[flag_off:flag_off + 2])[0] | 0x0800
            data[flag_off:flag_off + 2] = struct.pack("<H", flag)
            namelen = struct.unpack("<H", data[namelen_off:namelen_off + 2])[0]
            if namelen:
                data[name_off] = 0xFF                # invalid UTF-8 leading byte
            idx = data.find(sig, idx + 4)
    return bytes(data)


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
# Markdown: the reviewer's exact "Market Outlook" meaning-reversal fixture (finding #16 / M4_PLAN.md 15.6), plus a
# tense-only counterpart that must NOT be reported as changed
# --------------------------------------------------------------------------

MARKET_REVERSAL_V1 = ("# Executive Summary\nThe company performed well this quarter.\n\n"
                     "# Legal Proceedings\nThere are no material legal proceedings.\n\n"
                     "# Market Outlook\nThe market is expected to grow next year.\n\n"
                     "# Company History\nThe company was founded and has grown steadily.\n")
MARKET_REVERSAL_V2 = ("# Executive Summary\nThe company performed well this quarter.\n\n"
                     "# Market Outlook\nThe market is expected to shrink next year, reversing the prior forecast.\n\n"
                     "# Company History\nThe company was founded and has grown steadily.\n\n"
                     "# Cybersecurity Practices\nThe company has adopted new cybersecurity controls.\n")

TENSE_ONLY_V1 = ("# Executive Summary\nThe company performed well this quarter with strong results overall.\n\n"
                "# Regulatory Impact\nNew regulations will impact our supply chain operations significantly this year.\n\n"
                "# Company History\nThe company was founded and has grown steadily over the past decade.\n")
TENSE_ONLY_V2 = ("# Executive Summary\nThe company performed well this quarter with strong results overall.\n\n"
                "# Regulatory Impact\nNew regulations impacted our supply chain operations significantly this year.\n\n"
                "# Company History\nThe company was founded and has grown steadily over the past decade.\n")


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
