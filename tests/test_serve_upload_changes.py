"""Tests for semigraph.uploads.changes (M4_PLAN.md 4.2, Worker A, spike S4).

Builds ``VersionView``s through the REAL pipeline (``parse.parse_document`` -> ``units.detect_units`` ->
``units.chunk_units``) so these tests exercise exactly what ``uploads/jobs.py`` will call, not a shortcut.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import upload_fixtures as fx  # noqa: E402

from semigraph.uploads import changes as C  # noqa: E402
from semigraph.uploads import parse, units as U  # noqa: E402


def _fake_count_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def _build_view(text: str, kind: str = "md") -> C.VersionView:
    doc = parse.parse_document(text.encode("utf-8"), kind)
    unit_list = U.detect_units(doc.blocks, kind)
    canonical = U.canonical_text(doc.blocks)
    chunks = U.chunk_units(canonical, unit_list, count_tokens=_fake_count_tokens,
                           max_tokens=512, target_chars=1200, max_chars=1800)
    chunk_spans = tuple((f"c{c.seq}", c.char_start, c.char_end) for c in chunks)
    return C.VersionView(text=canonical, units=unit_list, chunk_spans=chunk_spans,
                         method=doc.method, chars_per_page=doc.chars_per_page)


@pytest.fixture(scope="module")
def md_v1() -> C.VersionView:
    return _build_view(fx.MD_V1)


@pytest.fixture(scope="module")
def md_v2() -> C.VersionView:
    return _build_view(fx.MD_V2)


# --------------------------------------------------------------------------
# the known edit set (spike S4): 1 added, 1 removed, 2 changed, 1 tense-only edit absent
# --------------------------------------------------------------------------

def test_compare_versions_known_edit_set(md_v1, md_v2):
    report = C.compare_versions(md_v1, md_v2)
    assert report["items_compared"] is True
    assert report["not_compared_reason"] is None

    added_headlines = {a["headline"] for a in report["added"]}
    removed_headlines = {r["headline"] for r in report["removed"]}
    changed_headlines = {c["headline"] for c in report["changed"]}

    assert added_headlines == {"Cybersecurity Risk Management Practices"}
    assert removed_headlines == {"Legal Proceedings Overview Statement"}
    assert changed_headlines == {"Item One Risk Factors Overview", "Market Trends And Outlook Today"}
    assert "Company History And Background" not in changed_headlines          # the tense-only edit
    assert "Executive Summary Of Operations" not in changed_headlines         # byte-identical


def test_compare_versions_invariant_accounts_for_every_older_unit(md_v1, md_v2):
    report = C.compare_versions(md_v1, md_v2)
    total = len(report["removed"]) + len(report["changed"]) + report["unchanged_count"]
    assert total == len(md_v1.units)


def test_compare_versions_every_quote_is_a_substring_of_its_cited_chunk(md_v1, md_v2):
    report = C.compare_versions(md_v1, md_v2)
    older_chunks = {cid: md_v1.text[start:end] for cid, start, end in md_v1.chunk_spans}
    newer_chunks = {cid: md_v2.text[start:end] for cid, start, end in md_v2.chunk_spans}
    for entry in report["changed"]:
        for passage in entry["passages"]:
            assert passage["kind"] in ("removed", "reworded", "added")
            # removed/reworded passages cite an OLDER-side chunk; added passages cite a NEWER-side chunk
            side = older_chunks if passage["kind"] in ("removed", "reworded") else newer_chunks
            assert passage["quote"] in side[passage["chunk_id"]]


def test_compare_versions_changed_entries_carry_both_unit_ids(md_v1, md_v2):
    report = C.compare_versions(md_v1, md_v2)
    for entry in report["changed"]:
        assert entry["older_unit_id"]
        assert entry["newer_unit_id"]                    # both sides survive as the same headline in this fixture
        assert entry["passages"]                          # a "changed" entry always has at least one passage


# --------------------------------------------------------------------------
# not_compared_reason guards
# --------------------------------------------------------------------------

def test_compare_versions_identical_content(md_v1):
    same = _build_view(fx.MD_V1)
    report = C.compare_versions(md_v1, same)
    assert report == {
        "items_compared": False, "not_compared_reason": "identical_content",
        "added": [], "removed": [], "changed": [], "unchanged_count": len(md_v1.units),
    }


def test_compare_versions_parse_method_mismatch(md_v1, md_v2):
    mismatched = replace(md_v2, method="pdfplumber")
    report = C.compare_versions(replace(md_v1, method="pypdfium2"), mismatched)
    assert report["items_compared"] is False
    assert report["not_compared_reason"] == "parse_method_mismatch"


def test_compare_versions_low_text_yield(md_v1, md_v2):
    thin = replace(md_v2, chars_per_page=50.0)
    report = C.compare_versions(md_v1, thin)
    assert report["not_compared_reason"] == "low_text_yield"


def test_compare_versions_heading_coverage_mismatch(md_v1):
    # plain TXT never produces heading units, but needs enough density to clear the low_text_yield guard first
    dense_paragraph = " ".join(["This is a plain sentence with no heading structure in the document at all."] * 6)
    flat = _build_view(dense_paragraph, kind="txt")
    assert not C._has_headings(flat.units)
    assert C._has_headings(md_v1.units)
    assert flat.chars_per_page >= C.LOW_TEXT_YIELD_CHARS_PER_PAGE
    report = C.compare_versions(md_v1, flat)
    assert report["not_compared_reason"] == "heading_coverage_mismatch"


def test_compare_versions_too_many_units(md_v1, md_v2):
    # all-heading units so this guard is reached rather than heading_coverage_mismatch (md_v2 also has headings)
    huge_units = tuple(U.Unit(f"u{i}", "heading", f"Heading Number {i} Text", i * 10, i * 10 + 5)
                       for i in range(C.MAX_UNITS_FOR_COMPARISON + 1))
    huge = replace(md_v1, units=huge_units)
    report = C.compare_versions(huge, md_v2)
    assert report["not_compared_reason"] == "too_many_units"


# --------------------------------------------------------------------------
# _clip_to_chunk
# --------------------------------------------------------------------------

def _make_passage(item_id, kind, char_start, char_end, chunk_ids):
    from semigraph.graph.passages import Passage
    return Passage(passage_id=f"{item_id}:x", kind=kind, item_id=item_id, seq=0, text="x",
                  char_start=char_start, char_end=char_end, counterpart_text=None, counterpart_span=None,
                  similarity=None, chunk_ids=tuple(chunk_ids), decided_by="sentence_absent")


def test_clip_to_chunk_clips_to_the_first_overlapping_chunk():
    text = "0123456789ABCDEFGHIJ"
    view = C.VersionView(text=text, units=(), chunk_spans=(("c0", 0, 10), ("c1", 10, 20)),
                         method="text", chars_per_page=1000.0)
    passage = _make_passage("u1", "removed", 5, 15, ["c0", "c1"])
    quote, chunk_id = C._clip_to_chunk(passage, view)
    assert chunk_id == "c0"
    assert quote == text[5:10]
    assert quote in text[0:10]


def test_clip_to_chunk_drops_a_passage_with_no_overlapping_chunk():
    text = "0123456789"
    view = C.VersionView(text=text, units=(), chunk_spans=(("c0", 0, 5),),
                         method="text", chars_per_page=1000.0)
    passage = _make_passage("u1", "removed", 6, 9, [])
    assert C._clip_to_chunk(passage, view) is None
