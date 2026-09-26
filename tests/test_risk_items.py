"""Tests for risk-item detection (semigraph.parsing.risk_items).

Zero network, zero LLM, zero Neo4j. The pure core is exercised with synthetic
HTML + section-text pairs (the section text is built the way the chunker builds
it: elements joined with a blank line); the lake-reading orchestration runs on
a tiny tmp lake. One opt-in test reads the real NVDA filing and is skipped when
the (git-ignored) data lake is absent.
"""

import json
import re
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from semigraph.cli import app
from semigraph.config import Settings
from semigraph.hashing import content_hash
from semigraph.parsing import risk_items as ri
from semigraph.parsing.chunker import chunks_path_for, section_texts_path_for
from semigraph.parsing.risk_items import (
    COVERAGE_MIN,
    ITEM_COLUMNS,
    MAX_UNIT_CHARS,
    DetectedItem,
    build_risk_items,
    coverage,
    detect_items,
    is_category_text,
    overlapping_chunk_ids,
    risk_items_path_for,
)

runner = CliRunner()


# ------------------------------------------------------------- fixture builders

BOLD = "font-weight:700"
PLAIN = "font-weight:400"


def _div(text: str, style: str = PLAIN, tag_style: str = "margin-bottom:6pt") -> str:
    return f'<div style="{tag_style}"><span style="{style}">{text}</span></div>'


def _body(seed: str, n: int = 3) -> str:
    """Deterministic ~300-char body paragraph unique to ``seed``."""
    return " ".join(
        f"{seed} sentence number {i} explains how this exposure could reduce revenue, raise costs, "
        f"and harm our operating results in a material way."
        for i in range(n)
    )[:330].rstrip(". ") + "."


class Filing:
    """Accumulates (html blocks, section-text paragraphs) in lockstep."""

    def __init__(self) -> None:
        self.html: list[str] = []
        self.paras: list[str] = []

    def add(self, text: str, style: str = PLAIN) -> "Filing":
        self.html.append(_div(text, style))
        self.paras.append(text)
        return self

    def bold(self, text: str) -> "Filing":
        return self.add(text, BOLD)

    def bold_italic(self, text: str) -> "Filing":
        self.html.append(_div(text, BOLD + ";font-style:italic"))
        self.paras.append(text)
        return self

    def run_in(self, lead: str, rest: str) -> "Filing":
        self.html.append(f'<div><span style="{BOLD}">{lead}</span><span style="{PLAIN}"> {rest}</span></div>')
        self.paras.append(f"{lead} {rest}")
        return self

    def merged(self, blocks: list[str], style: str = PLAIN) -> "Filing":
        """Several HTML blocks that sec-parser fuses into ONE section-text paragraph."""
        self.html.extend(_div(b, style) for b in blocks)
        self.paras.append("".join(blocks))
        return self

    def html_doc(self, before: str = "") -> str:
        return f"<html><body>{before}{''.join(self.html)}</body></html>"

    def text(self) -> str:
        return "\n\n".join(self.paras)


INTRO = (
    "The following risk factors should be considered in addition to the other information in this "
    "annual report because each of them could harm our business, financial condition and reputation."
)


def headline_filing(n: int = 10, categories: bool = True) -> tuple[Filing, list[str]]:
    f = Filing().add(INTRO)
    heads: list[str] = []
    for i in range(n):
        if categories and i in (0, n // 2):
            f.bold(f"Risks Related to Category Number {i}")
        head = f"Failure to manage exposure number {i} could adversely affect our business and results."
        heads.append(head)
        f.bold(head).add(_body(f"alpha{i}")).add(_body(f"beta{i}"))
    return f, heads


# ------------------------------------------------------------------ headline mode

class TestHeadlineItems:
    def test_headlines_recovered_from_bold_html(self):
        f, heads = headline_filing(10)
        toc = _div("Risk Factors", BOLD) + _div("Risks Related to Category Number 0", BOLD)
        res = detect_items(f.html_doc(before=toc), f.text())

        assert res.unit_kind == "headline" and res.method == "bold"
        assert [it.headline for it in res.items] == heads
        assert res.coverage >= COVERAGE_MIN and not res.low_coverage
        assert all(it.unit_kind == "headline" for it in res.items)

    def test_items_are_exact_substrings_sorted_and_disjoint(self):
        f, heads = headline_filing(10)
        text = f.text()
        res = detect_items(f.html_doc(), text)

        prev_end = -1
        for it in res.items:
            assert text[it.char_start : it.char_end] == it.text
            assert it.text.startswith(it.headline)
            assert it.char_start >= prev_end
            prev_end = it.char_end
        assert [it.seq for it in res.items] == list(range(len(res.items)))

    def test_item_body_contains_only_its_own_paragraphs(self):
        f, _ = headline_filing(10)
        res = detect_items(f.html_doc(), f.text())

        assert "alpha3" in res.items[3].text and "beta3" in res.items[3].text
        assert "alpha4" not in res.items[3].text and "alpha2" not in res.items[3].text

    def test_category_headings_are_boundaries_not_items_and_not_item_tails(self):
        f, heads = headline_filing(10)
        res = detect_items(f.html_doc(), f.text())

        cat = "Risks Related to Category Number 5"
        assert cat in f.text()
        assert all(cat not in it.text for it in res.items)       # never at the tail of item 4
        assert all(it.headline != cat for it in res.items)

    def test_title_case_headline_without_period_is_a_headline(self):
        f = Filing().add(INTRO)
        heads = []
        for i in range(9):
            head = f"We Face Intense Competition Number {i}" if i % 2 else f"We Have Foreign Exchange Risk {i}"
            heads.append(head)
            f.bold(head).add(_body(f"gamma{i}"))
        res = detect_items(f.html_doc(), f.text())

        assert [it.headline for it in res.items] == heads

    def test_payments_related_risks_is_a_headline_not_a_category(self):
        f = Filing().add(INTRO)
        f.bold("Business and Industry Risks")
        for i in range(9):
            f.bold(f"Our business faces exposure number {i} that may harm us.").add(_body(f"delta{i}"))
        f.bold("We Are Subject to Payments-Related Risks").add(_body("payments"))
        res = detect_items(f.html_doc(), f.text())

        heads = [it.headline for it in res.items]
        assert "We Are Subject to Payments-Related Risks" in heads
        assert "Business and Industry Risks" not in heads
        assert "Business and Industry Risks" not in res.items[0].text

    @pytest.mark.parametrize("text,expected", [
        ("Risks Related to Our Industry and Markets", True),
        ("Risks Related to Regulatory, Legal, Our Stock, and Other Matters", True),
        ("Business and Industry Risks", True),
        ("GENERAL RISK FACTORS", True),
        ("General Risk Factors", True),                          # the catch-all caption is not a risk item
        ("Risk Factors Summary", True),
        ("We Are Subject to Payments-Related Risks", False),
        ("We Have Foreign Exchange Risk", False),
        ("Adverse economic conditions may harm our business.", False),
        ("Risks related to our supply chain could harm our margins.", False),
    ])
    def test_is_category_text(self, text, expected):
        assert is_category_text(text) is expected

    def test_run_in_bold_lead_is_the_headline(self):
        f = Filing().add(INTRO)
        heads = []
        for i in range(9):
            head = f"We may not be able to control exposure number {i} in our operations."
            heads.append(head)
            f.run_in(head, _body(f"eps{i}")).add(_body(f"zeta{i}"))
        res = detect_items(f.html_doc(), f.text())

        assert [it.headline for it in res.items] == heads
        assert res.items[2].text.startswith(heads[2])
        assert "zeta2" in res.items[2].text and "zeta3" not in res.items[2].text

    def test_bold_italic_subheading_stays_inside_the_item_body(self):
        f = Filing().add(INTRO)
        for i in range(9):
            f.bold(f"Competition affects exposure number {i} across all of our markets.")
            f.bold_italic("Competition in the technology sector").add(_body(f"eta{i}"))
        res = detect_items(f.html_doc(), f.text())

        assert len(res.items) == 9
        assert all("Competition in the technology sector" in it.text for it in res.items)

    def test_headline_split_across_consecutive_bold_blocks_is_merged(self):
        f = Filing().add(INTRO)
        f.bold("Risks Relating to Our Business")
        parts = []
        for i in range(9):
            a = f"If we are unable to manage exposure number {i} in a timely and"
            b = f"effective manner, our revenue and profitability may be materially"
            c = "and adversely affected."
            f.bold(a).bold(b).bold(c).add(_body(f"theta{i}"))
            parts.append(f"{a} {b} {c}")
        res = detect_items(f.html_doc(), f.text())

        assert [it.headline for it in res.items] == parts
        assert "Risks Relating to Our Business" not in res.items[0].text      # category not fused

    def test_a_wrapped_headline_continues_past_an_abbreviation_full_stop(self):
        """TSMC FY25: '... the R.O.C.' ends a printed line with a full stop, the headline goes on lower-case."""
        f = Filing().add(INTRO)
        parts = []
        for i in range(9):
            a = f"The market value of our shares {i} may fluctuate due to the volatility of, and government intervention in, the R.O.C."
            f.bold(a).bold("securities market.").add(_body(f"sigma{i}"))
            parts.append(f"{a} securities market.")
        res = detect_items(f.html_doc(), f.text())

        assert [it.headline for it in res.items] == parts

    def test_the_last_item_stops_at_the_next_chapter_heading_and_later_headlines_are_ignored(self):
        """Intel FY25: the risk section runs on into 'Other Key Information' (executive officers), then a 13(r) caption."""
        f = Filing().add(INTRO)
        for i in range(9):
            f.bold(f"Failure to control exposure number {i} may harm us.").add(_body(f"tau{i}")).add(_body(f"upsilon{i}"))
        f.add("Other Key Information").add("Information About Our Executive Officers")
        f.add("Name Current Title Age Experience Jane Doe 55 has been our Chief Executive Officer since 2020. " * 3)
        f.bold("Disclosure Pursuant to Section 13(r) of the Securities Exchange Act of 1934").add(_body("phi"))
        res = detect_items(f.html_doc(), f.text())

        assert len(res.items) == 9
        assert "Executive Officer" not in res.items[-1].text and "Other Key Information" not in res.items[-1].text
        assert all("13(r)" not in it.headline for it in res.items)
        assert any("risk text ends" in n for n in res.notes)
        assert res.low_coverage is True                                  # the foreign tail is flagged, not hidden

    def test_end_of_risk_headings_in_the_first_half_are_not_believed(self):
        f = Filing().add(INTRO).add("Properties")
        for i in range(9):
            f.bold(f"Failure to control exposure number {i} may harm us.").add(_body(f"chi{i}")).add(_body(f"psi{i}"))
        res = detect_items(f.html_doc(), f.text())

        assert len(res.items) == 9 and not any("risk text ends" in n for n in res.notes)

    def test_short_first_block_still_anchors(self):
        """ASML-style: the first section paragraph is split over several short HTML blocks."""
        f = Filing()
        f.html += [_div("The risk factors outlined in this section"), _div("are categorized into the following types.")]
        f.paras.append("The risk factors outlined in this section" + "are categorized into the following types.")
        for i in range(9):
            f.bold(f"Our operations expose us to exposure number {i} that may harm us.").add(_body(f"iota{i}"))
        res = detect_items(f.html_doc(), f.text())

        assert len(res.items) == 9 and res.unit_kind == "headline"
        assert res.aligned_ratio > 0.9

    def test_html_and_text_differing_only_in_typography_still_align(self):
        f = Filing().add(INTRO)
        for i in range(9):
            f.bold(f"The Company's exposure number {i} could harm results.")
            f.add(_body(f"kappa{i}"))
        text = f.text()
        html = f.html_doc().replace("Company's", "Company’s").replace("exposure number", "exposure number")
        res = detect_items(html, text)

        assert len(res.items) == 9
        assert res.items[0].headline == "The Company's exposure number 0 could harm results."

    def test_bodyless_bold_lines_are_not_items(self):
        f = Filing().add(INTRO)
        for i in range(9):
            f.bold(f"Failure to control exposure number {i} may harm us.").add(_body(f"lambda{i}"))
        f.bold("This bold sentence has no body text at all after it.")
        res = detect_items(f.html_doc(), f.text())

        assert len(res.items) == 9

    def test_detection_is_deterministic(self):
        f, _ = headline_filing(10)
        a = detect_items(f.html_doc(), f.text())
        b = detect_items(f.html_doc(), f.text())
        assert a == b

    def test_a_layout_that_lists_headlines_before_their_bodies_is_not_trusted(self):
        """ASML-style page: three bold captions in a row, then the three bodies. Bold is no headline signal here."""
        f = Filing().add(INTRO)
        for g in range(6):
            for k in range(3):
                f.bold(f"Failure to control exposure number {g}{k} may harm our results and reputation.")
            for k in range(3):
                f.add(_body(f"omicron{g}{k}"))
        res = detect_items(f.html_doc(), f.text())

        assert res.unit_kind == "paragraph"
        assert any("without a body" in n for n in res.notes)


# -------------------------------------------------------------------- summary block

class TestSummaryBlock:
    def _with_summary(self, bold_bullets: bool) -> tuple[Filing, list[str], int]:
        heads = [f"Failure to manage summary exposure number {i} could harm our results." for i in range(10)]
        f = Filing().add(INTRO)
        f.bold("Risk Factors Summary")
        f.bold("Risks Related to Category Alpha")
        if bold_bullets:
            for h in heads[:5]:
                f.bold(h)
        else:
            f.merged([f"•{h}" for h in heads[:5]])
        f.bold("Risks Related to Category Beta")
        if bold_bullets:
            for h in heads[5:]:
                f.bold(h)
        else:
            f.merged([f"•{h}" for h in heads[5:]])
        f.bold("Risk Factors")
        f.bold("Risks Related to Category Alpha")
        for i, h in enumerate(heads):
            if i == 5:
                f.bold("Risks Related to Category Beta")
            f.bold(h).add(_body(f"mu{i}"))
        return f, heads, len(heads)

    @pytest.mark.parametrize("bold_bullets", [False, True])
    def test_summary_bullets_never_become_items(self, bold_bullets):
        f, heads, n = self._with_summary(bold_bullets)
        text = f.text()
        res = detect_items(f.html_doc(), text)

        assert [it.headline for it in res.items] == heads          # exactly once each, in order
        real_start = text.index("Risk Factors\n\n")
        assert all(it.char_start > real_start for it in res.items)  # none starts inside the summary
        assert len({it.headline for it in res.items}) == len(res.items)

    def test_summary_without_group_headings_ends_at_the_first_real_item(self):
        heads = [f"Failure to manage exposure number {i} could harm our results." for i in range(9)]
        f = Filing().add(INTRO).bold("Summary of Risk Factors")
        f.merged([f"•{h}" for h in heads])
        for i, h in enumerate(heads):
            f.bold(h).add(_body(f"pi{i}"))
        text = f.text()
        res = detect_items(f.html_doc(), text)

        assert res.summary_found and res.summary_excluded == 9
        assert [it.headline for it in res.items] == heads
        assert all(it.char_start > text.index("Failure to manage exposure number 8 could harm our results.\n\n") for it in res.items[-1:])

    def test_a_filing_without_a_summary_reports_none_found(self):
        f, _ = headline_filing(10)
        res = detect_items(f.html_doc(), f.text())

        assert res.summary_found is False and res.summary_excluded == 0

    def test_summary_bullet_count_is_reported(self):
        f, heads, n = self._with_summary(bold_bullets=False)
        res = detect_items(f.html_doc(), f.text())

        assert res.summary_excluded == n

    def test_summary_is_excluded_from_paragraph_units_too(self):
        f = Filing().add(INTRO)
        f.add("Risk Factors Summary")
        f.add("Risks Related to Category Alpha")
        f.merged([f"•bullet number {i} that only repeats a headline." for i in range(4)])
        f.add("Risk Factors")
        f.add("Risks Related to Category Alpha")
        for i in range(9):
            f.add(_body(f"nu{i}", 4))
        res = detect_items(f.html_doc(), f.text())

        assert res.unit_kind == "paragraph"
        assert all("bullet number" not in it.text for it in res.items)
        assert res.summary_excluded == 4


# ---------------------------------------------------------------- paragraph fallback

class TestParagraphFallback:
    def test_no_headlines_falls_back_to_paragraph_units(self):
        f = Filing().add(INTRO)
        for i in range(12):
            f.add(_body(f"xi{i}", 4))
        text = f.text()
        res = detect_items(f.html_doc(), text)

        assert res.unit_kind == "paragraph" and res.method == "paragraph"
        assert all(it.headline == "" and it.unit_kind == "paragraph" for it in res.items)
        assert len(res.items) == 13
        assert all(text[it.char_start : it.char_end] == it.text for it in res.items)
        assert res.coverage >= COVERAGE_MIN

    def test_fewer_than_the_minimum_headlines_is_not_trusted(self):
        f = Filing().add(INTRO)
        for i in range(3):
            f.bold(f"Failure to control exposure number {i} may harm us.").add(_body(f"pi{i}"))
        for i in range(8):
            f.add(_body(f"rho{i}", 3))
        res = detect_items(f.html_doc(), f.text())

        assert res.unit_kind == "paragraph"

    def test_bullets_merge_into_their_lead_paragraph_using_html_blocks(self):
        """INTC-style: the section text is one blob, the HTML still has real paragraphs and bullets."""
        blocks = [_body(f"sigma{i}", 2) for i in range(6)]
        lead = "Our non-U.S. business may be impacted by factors including the following, among others:"
        bullets = [f"▪ local factor number {i} that differs from our standards and practices;" for i in range(4)]
        html = "<html><body>" + "".join(_div(b) for b in [INTRO, lead, *bullets, *blocks]) + "</body></html>"
        text = "".join([INTRO, lead, *bullets, *blocks])                   # sec-parser fused everything
        res = detect_items(html, text)

        assert res.unit_kind == "paragraph"
        assert len(res.items) >= 6
        assert not any(it.text.startswith("▪") for it in res.items)   # bullets ride with their lead
        assert any(it.text.startswith(lead) and "local factor number 3" in it.text for it in res.items)

    def test_oversized_paragraph_is_split_at_sentence_boundaries(self):
        big = " ".join(f"Sentence {i} describes yet another way this exposure could harm us." for i in range(120))
        f = Filing().add(INTRO).add(big)
        res = detect_items(None, f.text())

        assert res.unit_kind == "paragraph"
        assert all(len(it.text) <= MAX_UNIT_CHARS for it in res.items)
        assert len(res.items) > 3
        assert all(it.text.rstrip().endswith(".") for it in res.items)

    def test_fused_element_seams_split_a_blob_into_the_original_paragraphs(self):
        """ASML-style: sec-parser glued page-sized text together; the seams show as ``.We`` with no space."""
        paras = [
            f"ASML Holding N.V. and its U.S. customers face exposure number {i}. " + _body(f"psi{i}", 2) for i in range(6)
        ]
        res = detect_items(None, "".join(paras))

        assert res.unit_kind == "paragraph"
        assert [it.text for it in res.items] == paras            # split at every seam, never inside "N.V." / "U.S."

    def test_trailing_page_furniture_is_not_part_of_an_item(self):
        f = Filing().add(INTRO)
        for i in range(9):
            f.bold(f"Failure to control exposure number {i} may harm us.").add(_body(f"chi{i}")).add(str(30 + i))
        f.add("Item 1B, 1C")
        res = detect_items(f.html_doc(), f.text())

        assert len(res.items) == 9
        assert all(not re.search(r"\n\n\d+$", it.text) for it in res.items)
        assert not res.items[-1].text.endswith("Item 1B, 1C")

    def test_html_that_cannot_be_aligned_falls_back_to_the_text_paragraphs(self):
        f = Filing().add(INTRO)
        for i in range(6):
            f.add(_body(f"rho{i}", 3))
        unrelated = "<html><body><div>Nothing in this document resembles the risk section at all.</div></body></html>"
        res = detect_items(unrelated, f.text())

        assert res.unit_kind == "paragraph" and res.aligned_ratio == 0.0
        assert any("could not be aligned" in n for n in res.notes)
        assert len(res.items) == 7

    def test_paragraph_units_stop_at_the_end_of_the_risk_factors(self):
        f = Filing().add(INTRO)
        for i in range(8):
            f.add(_body(f"omega{i}", 4))
        f.add("Quantitative and Qualitative Disclosures About Market Risk")
        for i in range(3):
            f.add(_body(f"alpha{i}", 4))
        res = detect_items(f.html_doc(), f.text())

        assert res.unit_kind == "paragraph" and len(res.items) == 9
        assert all("alpha" not in it.text and "Quantitative" not in it.text for it in res.items)

    def test_a_page_number_glued_after_the_last_sentence_is_trimmed(self):
        """TSMC FY25: the body's last paragraph is 'results of operations. 5' (page number after a space)."""
        f = Filing().add(INTRO)
        for i in range(9):
            f.bold(f"Failure to control exposure number {i} may harm us.").add(_body(f"pi{i}") + f" {5 + i}")
        res = detect_items(f.html_doc(), f.text())

        assert len(res.items) == 9
        assert all(re.search(r"\d$", it.text) is None for it in res.items)

    def test_strip_page_furniture_removes_lone_number_lines_only(self):
        text = "Body of the risk.\n\n34\n\nMore body about 2024 results.\n\nItem 1B, 1C\n\nEnd of it."
        assert ri.strip_page_furniture(text) == "Body of the risk.\n\nMore body about 2024 results.\n\nEnd of it."

    def test_no_html_uses_text_paragraphs(self):
        f = Filing().add(INTRO)
        for i in range(5):
            f.add(_body(f"tau{i}", 3))
        res = detect_items(None, f.text())

        assert res.unit_kind == "paragraph" and len(res.items) == 6

    def test_larger_than_body_font_headlines_are_recovered_when_bold_is_absent(self):
        """Intel-style: headlines are not bold; they are set in a larger font than the body."""
        def big(t):
            return f'<div><span style="font-size:12pt;font-weight:400">{t}</span></div>'

        def small(t):
            return f'<div><span style="font-size:9pt;font-weight:400">{t}</span></div>'

        parts, paras = [small(INTRO)], [INTRO]
        for i in range(9):
            head = f"Cyber attack attempts are increasing in exposure number {i}."
            parts += [big(head), small(_body(f"upsilon{i}"))]
            paras += [head, _body(f"upsilon{i}")]
        res = detect_items("<html><body>" + "".join(parts) + "</body></html>", "\n\n".join(paras))

        assert res.unit_kind == "headline" and res.method == "size"
        assert len(res.items) == 9

    def test_empty_and_missing_sections(self):
        for html, text in [("", ""), (None, ""), ("<html></html>", "   \n\n  ")]:
            res = detect_items(html, text)
            assert res.items == () and res.unit_kind == "none"
            assert res.coverage == 0.0 and res.low_coverage is True

    def test_low_coverage_is_flagged_never_silent(self):
        f, _ = headline_filing(10)
        res = detect_items(f.html_doc(), f.text(), min_coverage=0.9999)

        assert res.low_coverage is True
        assert any("coverage" in n for n in res.notes)


# ---------------------------------------------------------------------- html blocks

class TestHtmlBlocks:
    def test_bold_from_style_tag_and_ancestor_and_the_leading_run(self):
        html = (
            "<html><body>"
            '<div><b>Tag bold headline</b></div>'
            '<div style="font-weight:bold"><span>Inherited bold headline</span></div>'
            '<p><strong>Run-in lead.</strong> and then plain text follows here.</p>'
            '<div><span style="font-weight:400">Plain</span> <b>late bold</b></div>'
            "</body></html>"
        )
        blocks = ri.extract_html_blocks(html)

        assert [b.lead for b in blocks] == ["Tag bold headline", "Inherited bold headline", "Run-in lead.", ""]
        assert [b.heading_style for b in blocks] == [True, True, False, False]

    def test_hidden_blocks_and_wrapper_divs_are_skipped_and_text_is_normalised(self):
        html = (
            '<html><body><div style="display:none"><div>ix header junk</div></div>'
            "<div><div>Outer wrapper is not a leaf</div></div>"
            "<div>Non breaking   spaces\n and ’ quotes</div></body></html>"
        )
        assert [b.text for b in ri.extract_html_blocks(html)] == [
            "Outer wrapper is not a leaf", "Non breaking spaces and ’ quotes"]

    def test_font_size_is_read_in_points_and_only_when_uniform(self):
        html = (
            '<html><body><div><span style="font-size:12pt">uniform</span></div>'
            '<div><span style="font-size:16px">px block</span></div>'
            '<div><span style="font-size:12pt">mixed </span><span style="font-size:9pt">sizes</span></div></body></html>'
        )
        assert [b.size for b in ri.extract_html_blocks(html)] == [12.0, 12.0, None]

    def test_underlined_text_is_a_heading_style_and_unparseable_html_is_empty(self):
        html = '<html><body><p><span style="text-decoration:underline solid">STRATEGIC RISKS</span></p></body></html>'
        assert ri.extract_html_blocks(html)[0].heading_style is True
        assert ri.extract_html_blocks("") == [] and ri.extract_html_blocks("   ") == []


# ---------------------------------------------------------------- coverage + overlap

class TestCoverageAndOverlap:
    def _item(self, s: int, e: int) -> DetectedItem:
        return DetectedItem(seq=0, headline="", text="x" * (e - s), char_start=s, char_end=e, unit_kind="paragraph")

    def test_coverage_is_the_share_of_section_characters_inside_items(self):
        items = [self._item(0, 50), self._item(60, 90)]
        assert coverage(items, 100) == pytest.approx(0.8)

    def test_coverage_of_nothing_or_an_empty_section_is_zero(self):
        assert coverage([], 100) == 0.0
        assert coverage([self._item(0, 10)], 0) == 0.0

    def test_coverage_never_exceeds_one(self):
        assert coverage([self._item(0, 100), self._item(0, 100)], 100) == 1.0

    CHUNKS = [("c0", 0, 100), ("c1", 102, 300), ("c2", 302, 500), ("c3", 502, 900)]

    def test_overlap_is_strict_half_open(self):
        assert overlapping_chunk_ids(0, 100, self.CHUNKS) == ["c0"]          # touching neighbours excluded
        assert overlapping_chunk_ids(100, 102, self.CHUNKS) == []             # the separator gap
        assert overlapping_chunk_ids(99, 103, self.CHUNKS) == ["c0", "c1"]   # one char into each
        assert overlapping_chunk_ids(150, 800, self.CHUNKS) == ["c1", "c2", "c3"]
        assert overlapping_chunk_ids(1000, 1100, self.CHUNKS) == []
        assert overlapping_chunk_ids(120, 130, self.CHUNKS) == ["c1"]         # item inside one chunk

    def test_overlap_keeps_chunk_order_and_handles_unsorted_input(self):
        shuffled = [self.CHUNKS[2], self.CHUNKS[0], self.CHUNKS[3], self.CHUNKS[1]]
        assert overlapping_chunk_ids(50, 400, shuffled) == ["c0", "c1", "c2"]


# ------------------------------------------------------------------ lake orchestration

ACC_10K = "0001045810-26-000021"
ACC_10KA = "0001045810-26-000022"
ACC_20F = "0001628280-26-025362"


def _write_lake(root: Path) -> Settings:
    settings = Settings(data_dir=root / "data", _env_file=None)
    raw = settings.raw_dir / "edgar"
    (raw / "NVDA").mkdir(parents=True)
    (raw / "TSM").mkdir(parents=True)

    f, heads = headline_filing(10)
    text = f.text()
    (raw / "NVDA" / f"10-K_2026-02-25_{ACC_10K}.html").write_text(f.html_doc(), encoding="utf-8")
    (raw / "NVDA" / f"10-K_A_2026-03-01_{ACC_10KA}.html").write_text("<html><body>Item 7 only</body></html>", encoding="utf-8")
    g = Filing().add(INTRO)
    for i in range(12):
        g.add(_body(f"omega{i}", 4))
    (raw / "TSM" / f"20-F_2026-04-16_{ACC_20F}.html").write_text(g.html_doc(), encoding="utf-8")

    def row(ticker, cik, form, date, acc, name):
        return {"ticker": ticker, "cik": cik, "form": form, "filing_date": date, "accession_no": acc,
                "source_url": "https://www.sec.gov/x.htm", "size_bytes": 1,
                "local_path": f"data/raw/edgar/{ticker}/{name}"}

    manifest = {
        "NVDA": [row("NVDA", 1045810, "10-K", "2026-02-25", ACC_10K, f"10-K_2026-02-25_{ACC_10K}.html"),
                 row("NVDA", 1045810, "10-K/A", "2026-03-01", ACC_10KA, f"10-K_A_2026-03-01_{ACC_10KA}.html"),
                 row("NVDA", 1045810, "10-Q", "2026-05-20", "0001045810-26-000052", "10-Q_x.html")],
        "TSM": [row("TSM", 1046179, "20-F", "2026-04-16", ACC_20F, f"20-F_2026-04-16_{ACC_20F}.html")],
    }
    (raw / "manifest_universe.json").write_text(json.dumps(manifest), encoding="utf-8")

    def st_row(acc, form, date, sid, txt):
        return {"accession_no": acc, "section_id": sid, "form": form, "filing_date": date,
                "section_title": sid, "text": txt, "n_chars": len(txt)}

    section_texts = {
        "NVDA": [st_row(ACC_10K, "10-K", "2026-02-25", "I.1A", text),
                 st_row(ACC_10K, "10-K", "2026-02-25", "I.1", "Business text " * 20),
                 st_row(ACC_10KA, "10-K/A", "2026-03-01", "II.7", "Item 7 restated " * 20)],
        "TSM": [st_row(ACC_20F, "20-F", "2026-04-16", "I.3", g.text())],
    }
    chunks = {"NVDA": _chunk_rows("NVDA", ACC_10K, "I.1A", text), "TSM": _chunk_rows("TSM", ACC_20F, "I.3", g.text())}
    for ticker in ("NVDA", "TSM"):
        st_path = section_texts_path_for(settings, ticker)
        st_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(section_texts[ticker]).to_parquet(st_path, index=False)
        ch_path = chunks_path_for(settings, ticker)
        ch_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(chunks[ticker]).to_parquet(ch_path, index=False)
    return settings


def _chunk_rows(ticker: str, acc: str, sid: str, text: str) -> list[dict]:
    """Chunks that tile the section text at paragraph boundaries, two paragraphs per chunk."""
    paras = text.split("\n\n")
    rows, cursor, seq = [], 0, 0
    for i in range(0, len(paras), 2):
        group = paras[i : i + 2]
        start = cursor
        end = start + len("\n\n".join(group))
        rows.append({"chunk_id": f"{acc}:{sid}:{seq:04d}", "ticker": ticker, "accession_no": acc,
                     "section_id": sid, "char_start": start, "char_end": end, "text": text[start:end]})
        cursor = end + 2
        seq += 1
    return rows


@pytest.fixture
def lake(tmp_path):
    return _write_lake(tmp_path)


class TestBuildRiskItems:
    def test_writes_one_parquet_per_ticker_with_the_contract_columns(self, lake):
        out = build_risk_items(lake, ["NVDA"])

        path = risk_items_path_for(lake, "NVDA")
        assert path == lake.interim_dir / "risk_items" / "NVDA_risk_items.parquet" and path.exists()
        df = pd.read_parquet(path)
        assert list(df.columns) == ITEM_COLUMNS
        assert len(df) == 10 == out["NVDA"]["n_items"]
        assert (df["ticker"] == "NVDA").all() and (df["filer_cik"] == 1045810).all()
        assert df["filer_cik"].dtype.kind == "i"
        assert (df["form"] == "10-K").all() and (df["section_id"] == "I.1A").all()

    def test_item_ids_and_hashes_follow_the_contract(self, lake):
        build_risk_items(lake, ["NVDA"])
        df = pd.read_parquet(risk_items_path_for(lake, "NVDA"))

        assert df["item_id"].tolist() == [f"{ACC_10K}:I.1A:i{n:03d}" for n in range(10)]
        assert all(re.fullmatch(r"[\d-]+:I\.1A:i\d{3}", i) for i in df["item_id"])
        assert (df["text_hash"] == df["text"].map(content_hash)).all()
        assert (df["seq"] == range(10)).all()
        assert (df["unit_kind"] == "headline").all() and df["headline"].str.len().gt(0).all()

    def test_offsets_index_the_same_section_text_the_chunker_used(self, lake):
        build_risk_items(lake, ["NVDA"])
        df = pd.read_parquet(risk_items_path_for(lake, "NVDA"))
        st = pd.read_parquet(section_texts_path_for(lake, "NVDA"))
        section = st[(st.accession_no == ACC_10K) & (st.section_id == "I.1A")].iloc[0].text

        for r in df.itertuples():
            assert section[r.char_start : r.char_end] == r.text

    def test_chunk_ids_are_the_chunks_overlapping_each_item(self, lake):
        build_risk_items(lake, ["NVDA"])
        df = pd.read_parquet(risk_items_path_for(lake, "NVDA"))
        ch = pd.read_parquet(chunks_path_for(lake, "NVDA"))

        for r in df.itertuples():
            expect = ch[(ch.char_start < r.char_end) & (ch.char_end > r.char_start)]["chunk_id"].tolist()
            assert list(r.chunk_ids) == expect and expect

    def test_a_filing_without_a_risk_section_is_reported_not_silently_dropped(self, lake):
        out = build_risk_items(lake, ["NVDA"])

        skipped = out["NVDA"]["skipped"]
        assert [s["accession_no"] for s in skipped] == [ACC_10KA]
        assert "risk section" in skipped[0]["reason"]
        assert [f["accession_no"] for f in out["NVDA"]["filings"]] == [ACC_10K]   # 10-Q is not annual

    def test_twenty_f_uses_item_3_and_falls_back_to_paragraphs(self, lake):
        out = build_risk_items(lake, ["TSM"])
        df = pd.read_parquet(risk_items_path_for(lake, "TSM"))

        assert out["TSM"]["filings"][0]["section_id"] == "I.3"
        assert (df["unit_kind"] == "paragraph").all() and (df["headline"] == "").all()
        assert out["TSM"]["filings"][0]["method"] == "paragraph"

    def test_report_carries_coverage_and_low_coverage_flag(self, lake):
        out = build_risk_items(lake, ["NVDA"])

        f = out["NVDA"]["filings"][0]
        assert {"accession_no", "form", "filing_date", "section_id", "n_items", "n_headline", "n_paragraph",
                "coverage", "method", "summary_excluded", "low_coverage"} <= set(f)
        assert f["n_headline"] == 10 and f["n_paragraph"] == 0
        assert f["coverage"] >= COVERAGE_MIN and f["low_coverage"] is False

    def test_rebuild_is_idempotent_byte_for_byte(self, lake):
        build_risk_items(lake, ["NVDA", "TSM"])
        path = risk_items_path_for(lake, "NVDA")
        first = path.read_bytes()
        build_risk_items(lake, ["NVDA", "TSM"])

        assert path.read_bytes() == first

    def test_write_false_touches_nothing(self, lake):
        out = build_risk_items(lake, ["NVDA"], write=False)

        assert out["NVDA"]["n_items"] == 10
        assert not risk_items_path_for(lake, "NVDA").exists()

    def test_missing_html_degrades_to_text_paragraphs_with_a_warning(self, lake):
        for p in (lake.raw_dir / "edgar" / "NVDA").glob("10-K_2026*.html"):
            p.unlink()
        out = build_risk_items(lake, ["NVDA"])

        assert out["NVDA"]["filings"][0]["method"] == "paragraph"
        assert any("html" in w.lower() for w in out["NVDA"]["warnings"])

    def test_missing_chunks_file_gives_empty_chunk_ids_and_a_warning(self, lake):
        chunks_path_for(lake, "NVDA").unlink()
        out = build_risk_items(lake, ["NVDA"])
        df = pd.read_parquet(risk_items_path_for(lake, "NVDA"))

        assert all(len(c) == 0 for c in df["chunk_ids"])
        assert any("chunk" in w.lower() for w in out["NVDA"]["warnings"])

    def test_a_section_far_from_the_prior_filings_length_is_flagged(self, lake):
        """ASML: FY24's section text is twice FY25's because it runs into other chapters."""
        older = "Older filing risk text. " * 1700
        st_path = section_texts_path_for(lake, "NVDA")
        st = pd.read_parquet(st_path)
        row = {"accession_no": "0001045810-25-000023", "section_id": "I.1A", "form": "10-K",
               "filing_date": "2025-02-26", "section_title": "I.1A", "text": older, "n_chars": len(older)}
        pd.concat([st, pd.DataFrame([row])], ignore_index=True).to_parquet(st_path, index=False)
        manifest_path = lake.raw_dir / "edgar" / "manifest_universe.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["NVDA"].append({"ticker": "NVDA", "cik": 1045810, "form": "10-K", "filing_date": "2025-02-26",
                                 "accession_no": "0001045810-25-000023", "source_url": "u", "size_bytes": 1,
                                 "local_path": "data/raw/edgar/NVDA/missing.html"})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        out = build_risk_items(lake, ["NVDA"], write=False)

        newest = out["NVDA"]["filings"][-1]
        assert any("prior filing" in n for n in out["NVDA"]["filings"][0]["notes"])   # the older filing is flagged too
        assert newest["accession_no"] == ACC_10K
        assert any("prior filing's length" in n for n in newest["notes"])
        assert any("prior filing's length" in w for w in out["NVDA"]["warnings"])
        assert newest["section_suspect"] is True and out["NVDA"]["filings"][0]["section_suspect"] is True
        assert [s["accession_no"] for s in out["NVDA"]["suspect"]] == ["0001045810-25-000023", ACC_10K]

    def test_paragraph_units_without_a_chunk_are_merged_into_the_next_unit(self, lake):
        """Heading-only units have no chunk (the chunker keeps headings out of chunk text): no invented spans."""
        ch_path = chunks_path_for(lake, "TSM")
        ch = pd.read_parquet(ch_path)
        ch[ch.chunk_id != ch.iloc[2].chunk_id].to_parquet(ch_path, index=False)      # paragraphs 4 and 5 lose their chunk
        out = build_risk_items(lake, ["TSM"])
        df = pd.read_parquet(risk_items_path_for(lake, "TSM"))

        assert out["TSM"]["filings"][0]["n_unchunked_merged"] == 2 and len(df) == 13 - 2
        assert all(len(c) >= 1 for c in df["chunk_ids"])
        assert (df["seq"] == range(len(df))).all()
        section = pd.read_parquet(section_texts_path_for(lake, "TSM")).iloc[0].text
        assert all(section[r.char_start : r.char_end] == r.text for r in df.itertuples())

    def test_ticker_without_section_texts_is_reported(self, lake):
        section_texts_path_for(lake, "TSM").unlink()
        out = build_risk_items(lake, ["TSM"])

        assert out["TSM"]["n_items"] == 0 and out["TSM"]["skipped"]


# ------------------------------------------------------------------------------ CLI

class TestCli:
    @pytest.fixture
    def fake(self, monkeypatch):
        rec = {}
        report = {"NVDA": {
            "n_items": 24, "path": "data/interim/risk_items/NVDA_risk_items.parquet", "warnings": [],
            "skipped": [{"accession_no": "acc-a", "form": "10-K/A", "filing_date": "2026-03-01", "reason": "no risk section text"}],
            "filings": [
                {"accession_no": "0001045810-26-000021", "form": "10-K", "filing_date": "2026-02-25", "section_id": "I.1A",
                 "n_items": 24, "n_headline": 24, "n_paragraph": 0, "coverage": 0.957, "method": "bold",
                 "summary_excluded": 24, "low_coverage": False},
                {"accession_no": "0001045810-25-000023", "form": "10-K", "filing_date": "2025-02-26", "section_id": "I.1A",
                 "n_items": 5, "n_headline": 0, "n_paragraph": 5, "coverage": 0.61, "method": "paragraph",
                 "summary_excluded": 0, "low_coverage": True},
                {"accession_no": "0000937966-25-000009", "form": "20-F", "filing_date": "2025-03-05", "section_id": "I.3",
                 "n_items": 149, "n_headline": 0, "n_paragraph": 149, "coverage": 0.999, "method": "paragraph",
                 "summary_excluded": 0, "low_coverage": False, "section_suspect": True, "section_chars": 108928},
            ]}}

        def fake_build(settings=None, tickers=None, *, write=True):
            rec.update(tickers=tickers, write=write)
            return report

        monkeypatch.setattr(ri, "build_risk_items", fake_build)
        monkeypatch.setattr("semigraph.cli._settings", lambda: Settings(_env_file=None))
        return rec

    def test_prints_a_per_filing_table_and_writes(self, fake):
        result = runner.invoke(app, ["risk-items", "-t", "NVDA"])

        assert result.exit_code == 0, result.output
        assert fake == {"tickers": ["NVDA"], "write": True}
        assert "0001045810-26-000021" in result.output or "2026-02-25" in result.output
        assert "95.7" in result.output and "24" in result.output
        assert "LOW" in result.output.upper()                       # the 61% filing is flagged
        assert "SUSPECT" in result.output                           # 99.9% coverage does not vouch for a suspect section
        assert "1 filing(s) with a SUSPECT" in result.output
        assert "no risk section text" in result.output              # skipped filings are surfaced

    def test_the_real_build_runs_through_the_cli_on_a_tmp_lake(self, lake, monkeypatch):
        monkeypatch.setattr("semigraph.cli._settings", lambda: lake)

        dry = runner.invoke(app, ["risk-items", "--coverage", "-t", "NVDA"])
        assert dry.exit_code == 0, dry.output
        assert "I.1A" in dry.output and "bold" in dry.output and not risk_items_path_for(lake, "NVDA").exists()
        assert "SKIPPED" in dry.output and "no risk section text" in dry.output      # the 10-K/A

        wrote = runner.invoke(app, ["risk-items", "-t", "NVDA", "-t", "TSM"])
        assert wrote.exit_code == 0, wrote.output
        assert risk_items_path_for(lake, "NVDA").exists() and risk_items_path_for(lake, "TSM").exists()
        assert "0 filing(s) below" in wrote.output

    def test_coverage_flag_prints_only(self, fake):
        result = runner.invoke(app, ["risk-items", "--coverage"])

        assert result.exit_code == 0, result.output
        assert fake["write"] is False and fake["tickers"] is None


# --------------------------------------------------------------- opt-in real lake

REAL_HTML = Path("data/raw/edgar/NVDA/10-K_2026-02-25_0001045810-26-000021.html")
REAL_TEXTS = Path("data/interim/section_texts/nvda_section_texts.parquet")


@pytest.mark.skipif(not (REAL_HTML.exists() and REAL_TEXTS.exists()), reason="data lake absent")
def test_real_nvda_fy26_yields_one_item_per_summary_bullet():
    st = pd.read_parquet(REAL_TEXTS)
    row = st[(st.accession_no == "0001045810-26-000021") & (st.section_id == "I.1A")].iloc[0]
    res = detect_items(REAL_HTML.read_text(encoding="utf-8"), row.text)

    assert res.unit_kind == "headline" and res.coverage >= COVERAGE_MIN
    assert len(res.items) == 24                                      # the summary lists 24 bullets
    norm = [" ".join(it.headline.split()).lower() for it in res.items]
    assert len(set(norm)) == len(norm)                               # no summary duplicates
    assert all(not h.startswith("risks related to") for h in norm)   # no category headings
    ends = [it.char_end for it in res.items]
    starts = [it.char_start for it in res.items]
    assert all(s2 >= e1 for e1, s2 in zip(ends, starts[1:]))          # sorted, disjoint


# --- review follow-ups (Opus review of 59de9fc): paragraph-mode guard, zero-item filings, stale files, CLI notes ---

def _detection(method="bold", coverage=0.96, low=False):
    return ri.DetectionResult(items=(), unit_kind=method, coverage=coverage, method=method, summary_found=False,
                              summary_excluded=0, low_coverage=low, n_candidates=0, aligned_ratio=1.0,
                              notes=("aligned 100% onto 3 HTML blocks",))


def test_a_paragraph_mode_pair_uses_the_tighter_length_guard():
    # 70% of the prior length: fine for headline units (>= 60%), suspect when paragraph units tile the section
    assert ri._length_note(700, 1000, ri.SECTION_LENGTH_WARN) is None
    assert ri._length_note(700, 1000, ri.SECTION_LENGTH_WARN_PARAGRAPH) is not None
    assert ri._length_note(1400, 1000, ri.SECTION_LENGTH_WARN_PARAGRAPH) is not None      # over-extended: 1/0.8 = 1.25x
    assert ri._length_note(1000, 0, ri.SECTION_LENGTH_WARN_PARAGRAPH) is None


def test_a_filing_whose_units_all_vanished_is_low_coverage_never_a_clean_zero(tmp_path):
    report = ri._filing_report({"accession_no": "a", "form": "20-F", "filing_date": "2025-01-01"}, "I.3",
                               _detection(method="paragraph", coverage=1.0), [], 5000, n_merged=4)
    assert report["low_coverage"] is True and report["coverage"] == 0.0 and report["n_items"] == 0
    assert any("no items" in n for n in report["notes"])


def test_rewriting_a_ticker_that_now_yields_nothing_removes_its_stale_files(tmp_path):
    from types import SimpleNamespace

    settings = SimpleNamespace(interim_dir=tmp_path)
    folder = tmp_path / "risk_items"
    folder.mkdir()
    (folder / "XYZ_risk_items.parquet").write_bytes(b"old")
    (folder / "XYZ_risk_items_quality.json").write_text("{}", encoding="utf-8")
    summary = ri._ticker_summary(settings, "XYZ", [], [], [], [], True)
    assert summary["written"] is False and not list(folder.glob("XYZ_*"))


def test_a_coverage_only_run_leaves_existing_files_alone(tmp_path):
    from types import SimpleNamespace

    folder = tmp_path / "risk_items"
    folder.mkdir()
    (folder / "XYZ_risk_items.parquet").write_bytes(b"old")
    ri._ticker_summary(SimpleNamespace(interim_dir=tmp_path), "XYZ", [], [], [], [], False)
    assert (folder / "XYZ_risk_items.parquet").exists()


def test_the_cli_table_shows_every_note_except_the_routine_alignment_line(capsys):
    from semigraph import cli

    filing = {"form": "10-K", "filing_date": "2025-01-01", "section_id": "I.1A", "n_items": 3, "n_headline": 3,
              "n_paragraph": 0, "coverage": 0.95, "method": "bold", "summary_excluded": 0, "low_coverage": False,
              "section_suspect": False, "notes": ["paragraph units tile the section by construction", "aligned 90% onto 5 HTML blocks"]}
    cli._echo_risk_item_report({"XYZ": {"filings": [filing], "skipped": [], "warnings": []}}, 0.9)
    out = capsys.readouterr().out
    assert "paragraph units tile the section" in out and "aligned 90%" not in out
