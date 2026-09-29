"""Upload gate: identify a file's kind and reject unsafe or oversized bytes before parsing (M4_PLAN.md 4.2, 5, 14.2).

Two checks, in order, run by the caller (``serve/workspace_routes.py``, not this module) before a single byte is
parsed: ``sniff`` identifies the kind from magic bytes (the filename extension is NEVER trusted for a binary kind;
it only chooses which text kind a UTF-8-decodable file is), then ``check_bytes`` runs the kind-specific security
scan. Both raise :class:`GateError` with zero side effects; nothing here writes, parses or touches the network.
"""

from __future__ import annotations

import logging
import re
import zipfile
from io import BytesIO

import puremagic

logger = logging.getLogger("semigraph.uploads.gate")

SUPPORTED_KINDS = ("pdf", "docx", "html", "md", "txt")

GATE_CODES = ("unsupported_type", "too_large", "active_content", "encrypted", "zip_bomb", "empty")

# Any puremagic guess in this family means "zip container, try it as a docx" (an ordinary .zip, .xlsx, .pptx, ...
# all share the PK signature; the real answer comes from check_bytes opening it and requiring word/document.xml).
_ZIP_FAMILY_EXTS = frozenset({
    "docx", "docm", "dotx", "dotm", "zip", "xlsx", "xlsm", "xlsb", "xltm", "xltx", "xlam",
    "pptx", "pptm", "potx", "potm", "jar", "odt", "odp", "ott", "apk", "cbz", "xpi",
})
_TEXT_EXT_KIND = {".html": "html", ".htm": "html", ".md": "md", ".markdown": "md", ".txt": "txt"}


class GateError(Exception):
    """Raised by ``sniff`` / ``check_bytes``; ``code`` is one of :data:`GATE_CODES`."""

    def __init__(self, code: str, message: str = "") -> None:
        """``message`` MUST be a fixed, non-upload-controlled string (it can reach the caller and the client): a
        filename, zip member name or library exception text is never embedded here — log those separately, at
        most as counts/lengths, never the text itself."""
        if code not in GATE_CODES:
            raise ValueError(f"unknown gate error code {code!r}; expected one of {GATE_CODES}")
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _decode_utf8_prefix(head: bytes) -> str | None:
    """``head`` decoded as UTF-8, tolerating a multibyte character cut at the read boundary.

    A NUL byte anywhere fails the check outright (binary content masquerading as text).
    """
    if b"\x00" in head:
        return None
    for drop in range(4):                      # a UTF-8 codepoint is at most 4 bytes
        chunk = head[:len(head) - drop] if drop else head
        if not chunk and drop:
            return None
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _magic_binary_kind(head: bytes) -> str | None:
    """``pdf`` / ``docx`` from magic bytes via puremagic, or ``None`` (not a recognised binary kind)."""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "docx"
    try:
        guesses = {m.extension.lstrip(".").lower() for m in puremagic.magic_string(head)}
    except Exception:
        return None
    if "pdf" in guesses:
        return "pdf"
    if guesses & _ZIP_FAMILY_EXTS:
        return "docx"
    return None


def sniff(head: bytes, filename: str) -> str:
    """The document kind of ``head`` (the first bytes of the upload), one of :data:`SUPPORTED_KINDS`.

    ``pdf`` and ``docx`` are decided from magic bytes alone. Everything else must decode as UTF-8 text, and only
    then does the DECLARED extension of ``filename`` pick which text kind it is (``html`` / ``md`` / ``txt``); a
    binary file with a ``.txt`` name is rejected, not silently read as text.
    """
    if not head:
        raise GateError("empty", "the file is empty")
    binary_kind = _magic_binary_kind(head)
    if binary_kind is not None:
        return binary_kind
    if _decode_utf8_prefix(head) is None:
        raise GateError("unsupported_type", "not a recognised document type")
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    kind = _TEXT_EXT_KIND.get(f".{suffix}")
    if kind is None:
        logger.info("rejected upload: unsupported extension, suffix_len=%d", len(suffix))
        raise GateError("unsupported_type", "unsupported file extension")
    return kind


# --------------------------------------------------------------------------
# PDF: active content and encryption
# --------------------------------------------------------------------------

# A PDF name token boundary: whitespace, a delimiter, or end of buffer — never a following name character, so
# "/JS" does not match inside "/JSON" and "/AA" does not match inside "/AAPL".
_PDF_DELIM = rb"(?=[\s()<>\[\]{}/%]|$)"
_PDF_ACTIVE_NAMES = ("JavaScript", "JS", "Launch", "OpenAction", "AA", "EmbeddedFile", "RichMedia")
_PDF_ACTIVE_RE = re.compile(
    b"/(" + b"|".join(name.encode("ascii") for name in _PDF_ACTIVE_NAMES) + b")" + _PDF_DELIM
)
_PDF_ENCRYPT_RE = re.compile(rb"/Encrypt" + _PDF_DELIM)
_PDF_HEX_ESCAPE_RE = re.compile(rb"#([0-9a-fA-F]{2})")


def _pdf_unescape_names(data: bytes) -> bytes:
    """Decode PDF ``#xx`` name-escapes so ``/J#61vaScript`` is seen as ``/JavaScript``.

    Only used for the active-content scan: harmless for the surrounding bytes because every replacement is the
    same length or shorter, so no earlier match position is invalidated by a later one (scan runs once, forward).
    """
    return _PDF_HEX_ESCAPE_RE.sub(lambda m: bytes((int(m.group(1), 16),)), data)


def check_pdf_bytes(data: bytes) -> None:
    """Reject a PDF carrying active content or an encryption dictionary (M4_PLAN.md 4.2)."""
    scanned = _pdf_unescape_names(data)
    if _PDF_ENCRYPT_RE.search(scanned):
        raise GateError("encrypted", "the PDF declares an encryption dictionary")
    hit = _PDF_ACTIVE_RE.search(scanned)
    if hit:
        logger.info("rejected pdf: active content name=%s", hit.group(1).decode("ascii"))
        raise GateError("active_content", "the PDF contains active content")


# --------------------------------------------------------------------------
# DOCX: zip member / size / ratio / path checks
# --------------------------------------------------------------------------

DOCX_MAX_MEMBERS = 200
DOCX_MAX_UNCOMPRESSED_BYTES = 60 * 1024 * 1024
DOCX_MAX_COMPRESSION_RATIO = 120.0          # a member expanding more than this is treated as a zip bomb
_DOCX_ALLOWED_PREFIXES = ("word/", "docprops/", "_rels/", "customxml/")
_DOCX_ALLOWED_EXACT = frozenset({"[content_types].xml"})
_DOCX_ACTIVE_INFIXES = ("vbaproject", "activex", "/embeddings/")


def _docx_member_allowed(name: str) -> bool:
    lower = name.lower()
    return lower in _DOCX_ALLOWED_EXACT or lower.startswith(_DOCX_ALLOWED_PREFIXES)


def _docx_member_active(name: str) -> bool:
    lower = name.lower()
    return any(infix in lower for infix in _DOCX_ACTIVE_INFIXES)


def check_docx_zip(data: bytes) -> None:
    """Reject a DOCX zip that is oversized, path-unsafe, carries active content, or is not a real DOCX.

    ``zipfile.ZipFile()`` / ``infolist()`` raise more than :class:`zipfile.BadZipFile` for a malformed container: a
    corrupted "version needed to extract" field raises ``NotImplementedError``, and a UTF-8-flagged member name
    that is not valid UTF-8 raises ``UnicodeDecodeError`` (finding #12; fuzzed on 20,000 random mutations of a
    minimal DOCX). Every one of them means the same thing here — "not a valid document container" — and must
    become a fixed, upload-safe :class:`GateError`, never escape as a bare 500 after the per-IP upload window has
    already been spent.
    """
    try:
        zf = zipfile.ZipFile(BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, NotImplementedError, UnicodeDecodeError, ValueError, OSError, EOFError):
        logger.info("rejected docx: not a valid zip container")
        raise GateError("unsupported_type", "not a valid document container") from None
    if len(infos) > DOCX_MAX_MEMBERS:
        raise GateError("zip_bomb", f"more than {DOCX_MAX_MEMBERS} zip members")
    total_uncompressed = 0
    saw_document_xml = False
    for info in infos:
        name = info.filename
        if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
            logger.info("rejected docx: unsafe member path, name_len=%d", len(name))
            raise GateError("active_content", "unsafe member path in the document container")
        if not _docx_member_allowed(name):
            logger.info("rejected docx: member outside the expected layout, name_len=%d", len(name))
            raise GateError("active_content", "unexpected content in the document container")
        if _docx_member_active(name):
            logger.info("rejected docx: active-content member, name_len=%d", len(name))
            raise GateError("active_content", "embedded active content in the document container")
        if name.lower() == "word/document.xml":
            saw_document_xml = True
        total_uncompressed += info.file_size
        if total_uncompressed > DOCX_MAX_UNCOMPRESSED_BYTES:
            raise GateError("zip_bomb", f"uncompressed size exceeds {DOCX_MAX_UNCOMPRESSED_BYTES} bytes")
        if info.compress_size > 0 and info.file_size / info.compress_size > DOCX_MAX_COMPRESSION_RATIO:
            logger.info("rejected docx: compression ratio exceeded, name_len=%d", len(name))
            raise GateError("zip_bomb", f"a member's compression ratio exceeds {DOCX_MAX_COMPRESSION_RATIO}x")
    if not saw_document_xml:
        raise GateError("unsupported_type", "not a valid document container")


def check_bytes(data: bytes, kind: str) -> None:
    """Run the kind-specific security scan of ``data`` already sniffed as ``kind``; no-op for html/md/txt."""
    if kind == "pdf":
        check_pdf_bytes(data)
    elif kind == "docx":
        check_docx_zip(data)
    elif kind not in SUPPORTED_KINDS:
        raise GateError("unsupported_type", f"unknown kind {kind!r}")
