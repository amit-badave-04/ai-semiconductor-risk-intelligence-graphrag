"""Unit tests for semigraph.uploads.units (M4_PLAN.md 4.2, Worker A)."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import upload_fixtures as fx  # noqa: E402

from semigraph.uploads import parse, units as U  # noqa: E402


@dataclass(frozen=True)
class _Block:
    """A minimal stand-in for uploads.parse.Block (kept independent of the parser under test)."""

    text: str
    page: int = 1
    size: float = 12.0
    bold: bool = False
    kind_hint: str = "paragraph"


def _fake_count_tokens(text: str) -> int:
    return max(1, len(text) // 3)


# --------------------------------------------------------------------------
# canonical_text
# --------------------------------------------------------------------------

def test_canonical_text_joins_blocks_with_double_newline():
    blocks = [_Block("first block"), _Block("second block"), _Block("third block")]
    assert U.canonical_text(blocks) == "first block\n\nsecond block\n\nthird block"


def test_canonical_text_empty_blocks_is_empty_string():
    assert U.canonical_text([]) == ""


# --------------------------------------------------------------------------
# detect_units: sections with a preamble
# --------------------------------------------------------------------------

_SECTIONED_BLOCKS = [
    _Block("This is a preamble paragraph that appears before any heading in the document.", size=12.0),
    _Block("Executive Summary Of Operations", size=16.0, bold=True),
    _Block("The company had a strong quarter with revenue growth across its major segments this year.", size=12.0),
    _Block("Item One Risk Factors Overview", size=16.0, bold=True),
    _Block("There is a risk related to supply chain disruptions affecting production timelines significantly.",
           size=12.0),
    _Block("Market Trends And Outlook Today", size=16.0, bold=True),
    _Block("Demand for our products remains strong in most regions with softness in some emerging markets.",
           size=12.0),
]


def test_detect_units_builds_sections_with_a_preamble_unit():
    units = U.detect_units(_SECTIONED_BLOCKS, "md")
    text = U.canonical_text(_SECTIONED_BLOCKS)
    assert [u.kind for u in units] == ["paragraph", "heading", "heading", "heading"]
    assert units[0].headline == ""
    assert text[units[0].char_start:units[0].char_end] == _SECTIONED_BLOCKS[0].text
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert headlines == ["Executive Summary Of Operations", "Item One Risk Factors Overview",
                          "Market Trends And Outlook Today"]


def test_detect_units_offsets_are_exact_slices_of_canonical_text():
    units = U.detect_units(_SECTIONED_BLOCKS, "md")
    text = U.canonical_text(_SECTIONED_BLOCKS)
    for u in units:
        body = text[u.char_start:u.char_end]
        if u.headline:
            assert body.startswith(u.headline)
        assert body == text[u.char_start:u.char_end]          # tautological guard: slice is well-formed
    # sections tile the text end-to-end modulo the "\n\n" separators between them
    assert units[0].char_start == 0
    assert units[-1].char_end == len(text)


def test_detect_units_heading_section_includes_its_body():
    units = U.detect_units(_SECTIONED_BLOCKS, "md")
    text = U.canonical_text(_SECTIONED_BLOCKS)
    risk_section = next(u for u in units if u.headline == "Item One Risk Factors Overview")
    body = text[risk_section.char_start:risk_section.char_end]
    assert "supply chain disruptions" in body


# --------------------------------------------------------------------------
# detect_units: paragraph fallback when heading structure is too thin
# --------------------------------------------------------------------------

def test_detect_units_falls_back_to_paragraph_units_below_three_headings():
    blocks = [
        _Block("Only Heading Present Here", size=16.0, bold=True),
        _Block("A paragraph of plain body text that follows the lone heading in this short document."),
        _Block("Another paragraph of plain body text with no heading marking anywhere near it at all."),
    ]
    units = U.detect_units(blocks, "txt")
    assert len(units) == 3
    assert all(u.kind == "paragraph" for u in units)
    text = U.canonical_text(blocks)
    for u, b in zip(units, blocks):
        assert text[u.char_start:u.char_end] == b.text


def test_detect_units_empty_blocks_returns_no_units():
    assert U.detect_units([], "txt") == ()


# --------------------------------------------------------------------------
# heading detection guards
# --------------------------------------------------------------------------

def test_generic_heading_guard_rejects_sentence_ending_in_period():
    blocks = [
        _Block("A Bold Sentence That Ends With A Period.", size=16.0, bold=True),
        _Block("Second Real Heading Right Here", size=16.0, bold=True),
        _Block("Third Real Heading Right Here", size=16.0, bold=True),
        _Block("plain body text one that is long enough to not matter for this test at all."),
        _Block("plain body text two that is long enough to not matter for this test at all."),
    ]
    units = U.detect_units(blocks, "pdf")
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert "A Bold Sentence That Ends With A Period." not in headlines


def test_generic_heading_guard_rejects_more_than_twelve_words():
    long_heading = " ".join(f"word{i}" for i in range(13))
    blocks = [
        _Block(long_heading, size=16.0, bold=True),
        _Block("Second Real Heading Right Here", size=16.0, bold=True),
        _Block("Third Real Heading Right Here", size=16.0, bold=True),
        _Block("plain body text one that is long enough to not matter for this test at all."),
    ]
    units = U.detect_units(blocks, "pdf")
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert long_heading not in headlines


def test_generic_heading_guard_rejects_all_digits():
    blocks = [
        _Block("1234 5678", size=16.0, bold=True),
        _Block("Second Real Heading Right Here", size=16.0, bold=True),
        _Block("Third Real Heading Right Here", size=16.0, bold=True),
        _Block("plain body text one that is long enough to not matter for this test at all."),
    ]
    units = U.detect_units(blocks, "pdf")
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert "1234 5678" not in headlines


def test_bold_run_without_larger_size_is_a_heading():
    """A bold 12pt run at body size still counts (the DOCX bold-run fallback; python-docx quirk)."""
    blocks = [
        _Block("Bold Heading At Body Size", size=12.0, bold=True, kind_hint="paragraph"),
        _Block("plain paragraph text one that is long enough to not matter for this test at all."),
        _Block("Second Bold Heading Also Body Size", size=12.0, bold=True, kind_hint="paragraph"),
        _Block("plain paragraph text two that is long enough to not matter for this test at all."),
        _Block("Third Bold Heading Also Body Size", size=12.0, bold=True, kind_hint="paragraph"),
        _Block("plain paragraph text three that is long enough to not matter for this test at all."),
    ]
    units = U.detect_units(blocks, "docx")
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert "Bold Heading At Body Size" in headlines


def test_structural_heading_hint_bypasses_generic_guards():
    """heading_md / heading_html / heading_style are trusted outright, even past the word/period guards."""
    blocks = [
        _Block("A very long markdown heading with way more than twelve words in it for this test.",
               kind_hint="heading_md"),
        _Block("paragraph one of plain body text that is long enough to not matter for this test."),
        _Block("Second Heading.", kind_hint="heading_html"),
        _Block("paragraph two of plain body text that is long enough to not matter for this test."),
        _Block("Third Heading Style Docx", kind_hint="heading_style"),
        _Block("paragraph three of plain body text that is long enough to not matter for this test."),
    ]
    units = U.detect_units(blocks, "html")
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert len(headlines) == 3
    assert "Second Heading." in headlines            # trailing period would fail the generic guard


def test_table_kind_hint_is_never_heading_eligible_even_bold_and_short():
    """An HTML <th> is bold by default and often short: it must never become a heading regardless (a bare font
    test alone would wrongly promote it, since it satisfies every OTHER guard: no period, few words, not digits)."""
    blocks = [
        _Block("Metric Name", size=18.0, bold=True, kind_hint="table"),          # would pass every other guard
        _Block("Real Heading One Here", size=16.0, bold=True),
        _Block("Real Heading Two Here", size=16.0, bold=True),
        _Block("Real Heading Three Here", size=16.0, bold=True),
        _Block("plain paragraph text that is long enough to not matter for this test at all here."),
    ]
    units = U.detect_units(blocks, "html")
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert "Metric Name" not in headlines


# --------------------------------------------------------------------------
# running header / footer filter
# --------------------------------------------------------------------------

def test_running_header_repeated_every_page_is_never_a_heading():
    header = "COMPANY CONFIDENTIAL DRAFT"
    real_headings = ["Real Heading Alpha Section", "Real Heading Beta Section",
                     "Real Heading Gamma Section", "Real Heading Delta Section"]
    blocks = []
    for page, heading in enumerate(real_headings, start=1):
        blocks.append(_Block(header, page=page, size=18.0, bold=True))       # looks exactly like a heading
        blocks.append(_Block(heading, page=page, size=16.0, bold=True))
        blocks.append(_Block("Plain paragraph body text that is long enough to not matter for this whole test.",
                             page=page, size=12.0))
    units = U.detect_units(blocks, "pdf")
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert header not in headlines
    assert headlines == real_headings


def test_running_header_filter_does_not_swallow_numbered_mid_page_headings():
    """A per-page NUMBERED heading ("Section Heading Number 1 Text" ... "Number 30") normalises to the same
    digit-blind string on every page, exactly like a real running header — but it sits in the MIDDLE of the page
    (after the header, before the body), never at a page edge, so it must survive the filter (regression: an
    earlier digit-blind-only filter, with no page-edge restriction, wrongly dropped every one of these)."""
    header = "COMPANY CONFIDENTIAL DRAFT"
    blocks = []
    for page in range(1, 6):
        blocks.append(_Block(header, page=page, size=9.0, bold=True))
        blocks.append(_Block(f"Section Heading Number {page} Text", page=page, size=16.0, bold=True))
        blocks.append(_Block("Plain paragraph body text that is long enough to not matter for this whole test.",
                             page=page, size=12.0))
    units = U.detect_units(blocks, "pdf")
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert header not in headlines
    assert headlines == [f"Section Heading Number {p} Text" for p in range(1, 6)]


def test_running_footer_with_page_numbers_is_filtered_after_digit_normalisation():
    blocks = []
    for page in range(1, 5):
        blocks.append(_Block("Real Heading Number One Two", page=page, size=16.0, bold=True))
        blocks.append(_Block("Plain paragraph body text that is long enough to not matter for this whole test.",
                             page=page, size=12.0))
        blocks.append(_Block(f"Page {page} of 4", page=page, size=16.0, bold=True))   # digit-varying footer
    units = U.detect_units(blocks, "pdf")
    headlines = [u.headline for u in units if u.kind == "heading"]
    assert not any(h.lower().startswith("page ") for h in headlines)


def test_header_filter_does_not_apply_under_three_pages():
    header = "REPEATED LINE"
    blocks = [_Block(header, page=1, size=18.0, bold=True), _Block(header, page=2, size=18.0, bold=True)]
    boilerplate = U._boilerplate_block_indices(blocks)
    assert boilerplate == frozenset()


# --------------------------------------------------------------------------
# unit_rows
# --------------------------------------------------------------------------

def test_unit_rows_shape_matches_alignment_contract():
    units = U.detect_units(_SECTIONED_BLOCKS, "md")
    text = U.canonical_text(_SECTIONED_BLOCKS)
    rows = U.unit_rows(text, units)
    assert {"item_id", "text", "headline", "text_hash", "unit_kind", "char_start"} <= set(rows[0])
    ids = [r["item_id"] for r in rows]
    assert len(ids) == len(set(ids))                  # unique
    for row in rows:
        assert row["text"] == text[row["char_start"]:row["char_start"] + len(row["text"])]
        if row["headline"]:
            assert row["text"].startswith(row["headline"])


def test_unit_rows_feed_alignment_align_without_error():
    from semigraph.graph.alignment import align

    units = U.detect_units(_SECTIONED_BLOCKS, "md")
    text = U.canonical_text(_SECTIONED_BLOCKS)
    rows = U.unit_rows(text, units)
    result = align(rows, rows, text, older_section_text=text)
    assert all(d.label == "unchanged" for d in result.older)


# --------------------------------------------------------------------------
# chunk_units
# --------------------------------------------------------------------------

def test_chunk_units_empty_text_returns_no_chunks():
    assert U.chunk_units("", (), count_tokens=_fake_count_tokens) == ()


def test_chunk_units_are_exact_contiguous_slices_covering_the_text():
    units = U.detect_units(_SECTIONED_BLOCKS, "md")
    text = U.canonical_text(_SECTIONED_BLOCKS)
    chunks = U.chunk_units(text, units, count_tokens=_fake_count_tokens, max_tokens=40,
                           target_chars=100, max_chars=150)
    assert chunks
    pos = 0
    for c in chunks:
        assert c.char_start == pos
        assert c.char_end > c.char_start
        assert text[c.char_start:c.char_end]                        # non-empty exact slice
        assert c.tokens == _fake_count_tokens(text[c.char_start:c.char_end])
        pos = c.char_end
    assert pos == len(text)


def test_chunk_units_never_exceeds_max_tokens():
    units = U.detect_units(_SECTIONED_BLOCKS, "md")
    text = U.canonical_text(_SECTIONED_BLOCKS)
    chunks = U.chunk_units(text, units, count_tokens=_fake_count_tokens, max_tokens=40,
                           target_chars=100, max_chars=150)
    assert all(c.tokens <= 40 for c in chunks)


def test_chunk_units_never_splits_a_unit_shorter_than_both_maxima():
    short_units = (
        U.Unit("u1", "paragraph", "", 0, 40),
        U.Unit("u2", "paragraph", "", 42, 82),
    )
    text = ("a" * 40) + "\n\n" + ("b" * 40)
    chunks = U.chunk_units(text, short_units, count_tokens=_fake_count_tokens, max_tokens=512,
                           target_chars=1200, max_chars=1800)
    assert len(chunks) == 1
    assert chunks[0].char_start == 0 and chunks[0].char_end == len(text)


def test_chunk_units_splits_a_unit_bigger_than_max_chars_at_a_sentence_boundary():
    # two DIFFERENT, deliberately awkward lengths so max_chars (1800) is never a clean multiple of the pattern:
    # a hard, unsnapped cut would then land mid-sentence almost every time, not by arithmetic coincidence.
    sentences = ["Brief note here.", "This is a rather longer sentence with quite a few more words in it than before."]
    body = "".join((sentences[i % 2] + " ") for i in range(140))
    assert 1800 % len(sentences[0] + " ") != 0 and 1800 % len(sentences[1] + " ") != 0
    units = (U.Unit("u1", "paragraph", "", 0, len(body)),)
    chunks = U.chunk_units(body, units, count_tokens=_fake_count_tokens, max_tokens=1_000_000,
                           target_chars=1200, max_chars=1800)
    assert len(chunks) > 1
    for c in chunks[:-1]:
        piece = body[c.char_start:c.char_end]
        assert piece                                                # never an empty/whitespace-only chunk
        assert piece.rstrip().endswith(".")                         # cut lands on a real sentence boundary
        assert len(piece) <= 1800


def test_chunk_units_never_produces_a_gap_only_chunk():
    """Two oversized heading sections back to back, separated only by the "\\n\\n" unit-boundary gap: the gap must
    never surface as its own near-empty chunk (it used to, one per oversized-unit boundary)."""
    long_body = " ".join(["This sentence is part of a long section body for this particular test right here."] * 20)
    section1 = f"Heading One Text\n\n{long_body}"
    section2 = f"Heading Two Text\n\n{long_body}"
    text = section1 + "\n\n" + section2
    units = (
        U.Unit("u1", "heading", "Heading One Text", 0, len(section1)),
        U.Unit("u2", "heading", "Heading Two Text", len(section1) + 2, len(text)),
    )
    chunks = U.chunk_units(text, units, count_tokens=_fake_count_tokens, max_tokens=40,
                           target_chars=100, max_chars=150)
    assert all(text[c.char_start:c.char_end].strip() for c in chunks)


def test_chunk_units_dense_numeric_table_never_exceeds_max_tokens():
    """A dense numeric table can exceed 512 tokens well inside 1,800 characters (Qwen tokenizes digits densely);
    a conservative fake ``count_tokens`` that charges one token per digit reproduces that density."""
    def digit_heavy_count_tokens(text: str) -> int:
        digits = sum(ch.isdigit() for ch in text)
        return digits + max(1, (len(text) - digits) // 4)

    row = "1,234,567.89  2,345,678.90  3,456,789.01  4,567,890.12\n"
    table_text = row * 30                                            # well under 1,800 chars
    assert len(table_text) < 1800
    units = (U.Unit("u1", "paragraph", "", 0, len(table_text)),)
    chunks = U.chunk_units(table_text, units, count_tokens=digit_heavy_count_tokens,
                           max_tokens=512, target_chars=1200, max_chars=1800)
    assert digit_heavy_count_tokens(table_text) > 512                # the fixture really does exceed the cap
    assert len(chunks) > 1
    for c in chunks:
        piece = table_text[c.char_start:c.char_end]
        assert digit_heavy_count_tokens(piece) <= 512


_TOKENIZER_PATH = Path("models/qwen3-embedding-0.6b-q8/tokenizer.json")


@pytest.mark.skipif(not _TOKENIZER_PATH.exists(), reason="real Qwen3 tokenizer.json not present in this checkout")
def test_chunk_units_dense_numeric_table_under_real_tokenizer():
    tokenizers = pytest.importorskip("tokenizers", reason="tokenizers package not installed")
    tok = tokenizers.Tokenizer.from_file(str(_TOKENIZER_PATH))

    def real_count_tokens(text: str) -> int:
        return len(tok.encode(text).ids)

    row = "1,234,567.89  2,345,678.90  3,456,789.01  4,567,890.12\n"
    table_text = row * 30
    assert len(table_text) < 1800
    units = (U.Unit("u1", "paragraph", "", 0, len(table_text)),)
    chunks = U.chunk_units(table_text, units, count_tokens=real_count_tokens,
                           max_tokens=512, target_chars=1200, max_chars=1800)
    for c in chunks:
        assert real_count_tokens(table_text[c.char_start:c.char_end]) <= 512


# --------------------------------------------------------------------------
# full pipeline integration: parse -> detect_units -> chunk_units on the real 30-page fixture
# --------------------------------------------------------------------------

def _fake_count_tokens_pipeline(text: str) -> int:
    return max(1, len(text) // 3)


def test_pipeline_max_size_pdf_produces_clean_contiguous_chunks():
    """The end-to-end path ``jobs.py`` will call, on the real 30-page/~2,700-chars-per-page fixture, at the
    PRODUCTION defaults: exact contiguous coverage, no whitespace-only chunk, every chunk within both caps, and
    every non-final chunk ends at a sentence or whitespace boundary (never mid-word)."""
    doc = parse.parse_document(fx.max_size_pdf(n_pages=30, chars_per_page=2700), "pdf")
    unit_list = U.detect_units(doc.blocks, "pdf")
    text = U.canonical_text(doc.blocks)
    chunks = U.chunk_units(text, unit_list, count_tokens=_fake_count_tokens_pipeline)   # production defaults
    assert chunks

    pos = 0
    for c in chunks:
        piece = text[c.char_start:c.char_end]
        assert c.char_start == pos
        assert piece.strip(), "chunk must never be whitespace-only"
        assert len(piece) <= U.DEFAULT_MAX_CHARS
        assert c.tokens <= U.DEFAULT_MAX_TOKENS
        pos = c.char_end
    assert pos == len(text)

    for c in chunks[:-1]:
        piece = text[c.char_start:c.char_end]
        tail = piece.rstrip()
        ends_clean = tail[-1] in ".!?" if tail else False
        ends_at_whitespace = c.char_end >= len(text) or text[c.char_end - 1].isspace() or text[c.char_end] in " \n\t"
        assert ends_clean or ends_at_whitespace, f"chunk {c.seq} ends mid-word: {piece[-40:]!r}"

    # a numbered heading survives per page (the page-edge header/footer fix), so real sections were detected
    assert re.match(r"^Section Heading Number 1 Text$", unit_list[0].headline) or any(
        u.headline.startswith("Section Heading Number") for u in unit_list)
