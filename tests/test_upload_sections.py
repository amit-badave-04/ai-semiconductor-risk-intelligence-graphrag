"""I4a / R14: section detection for uploaded SEC filings (src/semigraph/uploads/sections.py)."""

from __future__ import annotations

import time

from semigraph.uploads import sections as sec

FILLER = "The company discusses matters relevant to this part of the filing in some detail. " * 15


def _doc(*parts: str) -> str:
    return "\n\n".join(parts)


def _tenk() -> str:
    return _doc(
        "UNITED STATES SECURITIES AND EXCHANGE COMMISSION", "FORM 10-K", "Annual report for the fiscal year ended 2025",
        "TABLE OF CONTENTS", "Item 1. Business 3", "Item 1A. Risk Factors 12", "Item 7. Management's Discussion 40",
        "PART I", "Item 1. Business", FILLER, "Item 1A. Risk Factors", FILLER * 2, "Item 1B. Unresolved Staff Comments", "None.",
        "Item 2. Properties", FILLER, "PART II", "Item 7. Management's Discussion and Analysis", FILLER * 3,
        "Item 7A. Quantitative and Qualitative Disclosures", FILLER)


def _tenq() -> str:
    return _doc(
        "FORM 10-Q", "Quarterly report for the quarter ended 2025", "PART I - FINANCIAL INFORMATION",
        "Item 1. Financial Statements", FILLER, "Item 2. Management's Discussion and Analysis", FILLER * 2,
        "Item 3. Quantitative and Qualitative Disclosures About Market Risk", FILLER, "PART II - OTHER INFORMATION",
        "Item 1. Legal Proceedings", FILLER, "Item 1A. Risk Factors", FILLER * 2, "Item 2. Unregistered Sales", FILLER)


def _twentyf() -> str:
    return _doc(
        "FORM 20-F", "Annual report of a foreign private issuer", "Item 3. Key Information", "A. [Reserved]", FILLER,
        "D. Risk Factors", FILLER * 2, "Item 4. Information on the Company", FILLER * 2, "Item 5. Operating and Financial Review",
        FILLER * 2, "Item 6. Directors", FILLER)


def _keys(result: sec.SectionResult) -> list[str]:
    return [s.key for s in result.sections]


def test_detect_form_from_the_cover_page():
    assert sec.detect_form(_tenk()) == "10-K"
    assert sec.detect_form(_tenq()) == "10-Q"
    assert sec.detect_form(_twentyf()) == "20-F"
    assert sec.detect_form("a memo about nothing in particular") is None


def test_section_order_per_form():
    """Priority order: 10-K 1A, 7, 1; 10-Q Part II Item 1A, Part I Item 2; 20-F 3.D, 5, 4 (OD11)."""
    ordered = lambda text: [s.key for s in sorted(sec.detect_sections(text).sections, key=lambda s: s.priority)]  # noqa: E731
    assert ordered(_tenk()) == ["10-K:1A", "10-K:7", "10-K:1"]
    assert ordered(_tenq()) == ["10-Q:II.1A", "10-Q:I.2"]
    assert ordered(_twentyf()) == ["20-F:3.D", "20-F:5", "20-F:4"]


def test_the_toc_entry_does_not_win_over_the_real_section():
    text = _tenk()
    result = sec.detect_sections(text)
    one_a = next(s for s in result.sections if s.key == "10-K:1A")
    assert one_a.char_end - one_a.char_start > len(FILLER)
    assert text[one_a.char_start:one_a.char_end].count(FILLER[:40]) >= 2


def test_sections_do_not_overlap_and_lie_inside_the_text():
    text = _tenk()
    spans = sorted((s.char_start, s.char_end) for s in sec.detect_sections(text).sections)
    assert all(0 <= a < b <= len(text) for a, b in spans)
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))


def test_no_sections_found_falls_back_to_document_order_with_a_message():
    result = sec.detect_sections("FORM 10-K\n\n" + FILLER * 5)
    assert result.sections == () and result.message
    assert sec.priority_for_span(result, 0, 100) == sec.NON_PRIORITY
    plain = sec.detect_sections("an ordinary document\n\n" + FILLER)
    assert plain.form is None and plain.sections == () and plain.message


def test_priority_for_a_chunk_span_uses_its_section():
    text = _tenk()
    result = sec.detect_sections(text)
    one_a = next(s for s in result.sections if s.key == "10-K:1A")
    seven = next(s for s in result.sections if s.key == "10-K:7")
    assert sec.priority_for_span(result, one_a.char_start + 5, one_a.char_start + 50) < \
        sec.priority_for_span(result, seven.char_start + 5, seven.char_start + 50) < sec.NON_PRIORITY
    assert sec.priority_for_span(result, len(text) - 10, len(text)) in (sec.NON_PRIORITY, sec.detect_sections(text).sections[-1].priority)


def test_sections_pathological_input_is_fast():
    nasty = ("item " * 60_000) + "\n" + (" " * 200_000) + "\n" + ("part part item 1a. " * 30_000) + "\n" + "A" * 400_000
    started = time.perf_counter()
    sec.detect_sections("FORM 10-K\n" + nasty)
    assert time.perf_counter() - started < 2.0
    started = time.perf_counter()
    sec.detect_sections("FORM 20-F\n" + "D. " * 200_000 + "\n" + "item 3." * 100_000)
    assert time.perf_counter() - started < 2.0


def test_a_toc_or_cross_reference_hit_with_a_tiny_span_is_not_a_section():
    """Real case (an annual-report-style 20-F): the cross-reference table names Items 4 and 5 in a few hundred characters, and
    the real text has no ``Item`` headings. Those hits must not become priority sections."""
    text = _doc("FORM 20-F", "Cross-reference to Form 20-F", "Item 4 Information on the Company page 12",
                "Item 5 Operating and Financial Review page 40", "Item 6 Directors page 80", FILLER * 5)
    result = sec.detect_sections(text)
    assert result.sections == () and result.message == sec.NO_SECTIONS_MESSAGE
    assert sec.MIN_SECTION_CHARS >= 500
