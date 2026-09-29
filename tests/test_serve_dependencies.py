"""The API image's pinned dependency set (deploy/requirements-serve.txt) for M4 (docs/v2/M4_PLAN.md sections 3 and 5).

PyMuPDF is AGPL: it must never be resolved into the image, directly or transitively (owner rule). The upload workspace's parsers
are pinned exactly, so the serve-shipped CI job tests what production runs.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PINS = {m.group(1).lower(): m.group(2)
        for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)", (ROOT / "deploy" / "requirements-serve.txt").read_text(encoding="utf-8"),
                             re.MULTILINE)}


@pytest.mark.parametrize("name", ["pymupdf", "pymupdfb", "pymupdf-pro", "fitz", "frontend"])
def test_no_agpl_pdf_library_is_resolved_into_the_image(name):
    assert name not in PINS


@pytest.mark.parametrize("name", ["python-multipart", "pypdfium2", "pdfplumber", "pdfminer-six", "python-docx", "puremagic",
                                  "rapidfuzz", "scipy"])
def test_the_upload_workspace_dependencies_are_pinned_exactly(name):
    assert name in PINS, f"{name} missing from deploy/requirements-serve.txt"


def test_python_multipart_carries_every_published_fix():
    """All eight python-multipart CVEs were fixed by 0.0.31 (research 2026-09-29); the plan requires >= 0.0.32."""
    assert tuple(int(p) for p in PINS["python-multipart"].split(".")) >= (0, 0, 32)
