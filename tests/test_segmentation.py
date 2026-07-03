"""Tests for the pure segmentation logic (semigraph.parsing.segmentation).

No sec-parser fixtures needed: segment_by_items / segment_by_pageheaders
only inspect ``type(el).__name__`` and ``el.text``, so dummy element
classes reproduce every battle scar synthetically. Zero network, zero LLM,
zero Neo4j.
"""

import pytest

from semigraph.parsing.segmentation import (
    KEEP_SECTIONS,
    PAGEHEAD_RE,
    needs_fallback,
    norm_marker,
    segment_by_items,
    segment_by_pageheaders,
)

_CLASSES: dict[str, type] = {}


def el(class_name: str, text: str):
    """Fake sec-parser element: only type name + .text are inspected."""
    cls = _CLASSES.setdefault(class_name, type(class_name, (), {}))
    obj = cls()
    obj.text = text
    return obj


PROSE = (
    "Our business depends on third-party foundries and could be harmed by "
    "supply constraints affecting advanced process nodes."
)


# ------------------------------------------------------- item-heading path

class TestSegmentByItems:
    def test_part_qualified_section_ids(self):
        rows = segment_by_items(
            [
                el("TitleElement", "PART I"),
                el("TitleElement", "Item 1. Business"),
                el("TextElement", PROSE),
                el("TitleElement", "Item 1A. Risk Factors"),
                el("TextElement", PROSE),
                el("TitleElement", "PART II"),
                el("TitleElement", "Item 7. Management's Discussion and Analysis"),
                el("TextElement", PROSE),
            ]
        )
        assert [r["section_id"] for r in rows] == ["I.1", "I.1A", "II.7"]
        assert rows[1]["section_title"] == "Item 1A. Risk Factors"

    def test_cover_page_before_first_item_is_dropped(self):
        rows = segment_by_items(
            [el("TextElement", "UNITED STATES SECURITIES AND EXCHANGE COMMISSION"),
             el("TitleElement", "Item 1. Business"),
             el("TextElement", PROSE)]
        )
        assert len(rows) == 1 and rows[0]["section_id"] == "I.1"

    def test_noise_elements_are_dropped(self):
        rows = segment_by_items(
            [el("TitleElement", "Item 1A. Risk Factors"),
             el("PageHeaderElement", "NVIDIA Corporation"),
             el("TextElement", PROSE)]
        )
        assert [r["element_type"] for r in rows] == ["TextElement"]


# ------------------------------------------------------- fallback trigger

class TestFallbackTrigger:
    def test_asml_cover_junk_yields_rows_but_still_triggers_fallback(self):
        """The ASML battle scar: the 20-F cover page's 'Item 17 ☐ 18 ☐'
        checkbox heading creates a (junk) I.17 section, so rows are NOT
        empty — the fallback must trigger on 'no KEEP sections', never on
        'no rows'."""
        rows = segment_by_items(
            [el("TitleElement", "Item 17 ☐ Item 18 ☐"),
             el("TextElement", "Indicate by check mark which financial statement item.")]
        )
        assert rows, "junk cover section should produce rows"
        assert all(r["section_id"] not in KEEP_SECTIONS["20-F"] for r in rows)
        assert needs_fallback(rows, "20-F") is True

    def test_intel_no_item_headings_triggers_fallback(self):
        rows = segment_by_items([el("TextElement", PROSE)])  # zero headings
        assert rows == []
        assert needs_fallback(rows, "10-K") is True

    def test_normal_10k_does_not_trigger_fallback(self):
        rows = segment_by_items(
            [el("TitleElement", "Item 1A. Risk Factors"), el("TextElement", PROSE)]
        )
        assert needs_fallback(rows, "10-K") is False


# ----------------------------------------------------- page-header fallback

class TestSegmentByPageheaders:
    def test_intel_style_numbered_running_headers(self):
        rows = segment_by_pageheaders(
            [
                el("TextElement", "Risk Factors44"),   # numbered marker, any type
                el("TextElement", PROSE),
                el("TextElement", "MD&A58"),
                el("TextElement", PROSE),
                el("TextElement", "Available Information112"),  # unmapped section
                el("TextElement", "This text belongs to no kept section."),
            ],
            form="10-K",
        )
        assert [r["section_id"] for r in rows] == ["I.1A", "II.7"]

    def test_asml_style_continued_pageheaders(self):
        rows = segment_by_pageheaders(
            [
                el("TitleElement", "Risk factors"),
                el("TextElement", PROSE),
                el("PageHeaderElement", "Risk factors (continued)"),
                el("TextElement", PROSE),
                el("PageHeaderElement", "Corporate governance"),  # page boundary
                el("TextElement", "Board composition text must be excluded."),
            ],
            form="20-F",
        )
        assert [r["section_id"] for r in rows] == ["I.3", "I.3"]
        assert all("Board composition" not in r["text"] for r in rows)

    def test_plain_short_text_is_not_a_marker(self):
        """A short non-header TextElement matching a mapped name must not
        start a section unless numbered or a heading/page-header."""
        rows = segment_by_pageheaders(
            [el("TextElement", "risk factors"), el("TextElement", PROSE)],
            form="10-K",
        )
        assert rows == []  # 'risk factors' TextElement is not a valid marker

    def test_unmarkable_filing_yields_no_rows(self):
        """ASML 2023/24: neither path marks anything — warn + skip by design."""
        rows = segment_by_pageheaders(
            [el("TextElement", PROSE), el("TextElement", PROSE)], form="20-F"
        )
        assert rows == []
        assert needs_fallback(rows, "20-F") is True


# --------------------------------------------------------------- helpers

class TestMarkerNormalization:
    def test_strips_trailing_page_number(self):
        assert norm_marker("Risk Factors44") == "risk factors"
        assert norm_marker("MD&A58") == "md&a"

    def test_strips_continued_suffix(self):
        assert norm_marker("Risk factors (continued)") == "risk factors"
        # notebook-12 order: "(continued)$" strips before digits, so a page
        # number AFTER "(continued)" shields it — a case absent from the
        # corpus; documented here as-is rather than "fixed"
        assert norm_marker("Risk factors (continued) 12") == "risk factors (continued)"

    def test_pagehead_re_shape(self):
        assert PAGEHEAD_RE.match("Risk Factors44")
        assert PAGEHEAD_RE.match("Fundamentals of Our Business7")
        assert not PAGEHEAD_RE.match("Risk Factors")       # no page number
        assert not PAGEHEAD_RE.match("2023 was a record")  # doesn't start alpha


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
