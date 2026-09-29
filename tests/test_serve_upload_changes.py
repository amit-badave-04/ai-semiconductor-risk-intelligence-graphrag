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


def test_compare_versions_tense_only_edit_lands_in_minor_rewordings_not_unchanged(md_v1, md_v2):
    """Finding #16 (M4_PLAN.md 15.6): a unit the aligner marks ``reworded`` with no surviving passage stays
    VISIBLE in its own ``minor_rewordings`` list — it must never be silently folded into ``unchanged_count``."""
    report = C.compare_versions(md_v1, md_v2)
    minor_headlines = {m["headline"] for m in report["minor_rewordings"]}
    assert minor_headlines == {"Company History And Background"}
    for entry in report["minor_rewordings"]:
        assert entry["older_unit_id"]
        assert entry["newer_unit_id"]


def test_compare_versions_invariant_accounts_for_every_older_unit(md_v1, md_v2):
    report = C.compare_versions(md_v1, md_v2)
    total = (len(report["removed"]) + len(report["changed"]) + len(report["minor_rewordings"])
            + report["unchanged_count"])
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
# finding #16 (M4_PLAN.md 15.6): the upload-specific PassageParams must tell a MEANING REVERSAL from a tense-only
# edit — the reviewer's exact "Market Outlook" scenario, reproduced with the real pipeline end to end
# --------------------------------------------------------------------------

def test_compare_versions_uses_its_own_upload_passage_params_by_default():
    """``compare_versions`` must not silently fall back to the SEC-tuned defaults (``graph.passages.PassageParams()``,
    ``present_min_ratio=75``): uploads need their OWN calibration (M4_PLAN.md 15.6), defined in this module, not in
    ``graph/passages.py`` (frozen, G3)."""
    from semigraph.graph.passages import PassageParams as SecDefaultPassageParams

    assert C.UPLOAD_PASSAGE_PARAMS.present_min_ratio != SecDefaultPassageParams().present_min_ratio


def test_compare_versions_meaning_reversal_yields_a_changed_passage_by_default():
    """The reviewer's exact repro: 'The market is expected to grow next year.' -> '...to shrink next year,
    reversing the prior forecast.' must produce a real passage and land in ``changed`` — not be folded away as a
    near-verbatim rewording (``fuzz.partial_ratio`` between the two is ~87.5, which the SEC-tuned default
    ``present_min_ratio=75`` would treat as still PRESENT and thus report nothing for)."""
    v1 = _build_view(fx.MARKET_REVERSAL_V1)
    v2 = _build_view(fx.MARKET_REVERSAL_V2)
    report = C.compare_versions(v1, v2)                                  # default (upload) passage_params
    changed_headlines = {c["headline"] for c in report["changed"]}
    assert "Market Outlook" in changed_headlines
    outlook = next(c for c in report["changed"] if c["headline"] == "Market Outlook")
    assert outlook["passages"]
    assert {c["headline"] for c in report["minor_rewordings"]} == set()


def test_compare_versions_tense_only_edit_yields_no_passage_by_default():
    """A tense-only edit ('will impact' -> 'impacted') at ~95.5 ``partial_ratio`` must stay a minor rewording, not
    a reported change, under the SAME upload-tuned params that catch the meaning reversal above."""
    v1 = _build_view(fx.TENSE_ONLY_V1)
    v2 = _build_view(fx.TENSE_ONLY_V2)
    report = C.compare_versions(v1, v2)
    changed_headlines = {c["headline"] for c in report["changed"]}
    assert "Regulatory Impact" not in changed_headlines
    minor_headlines = {m["headline"] for m in report["minor_rewordings"]}
    assert "Regulatory Impact" in minor_headlines


# --------------------------------------------------------------------------
# negation polarity (Worker A3, changes.py module docstring "(b)"; fixes the LIMITATION the docstring used to
# describe): a pure-negation edit barely moves fuzz.partial_ratio, so it needs its own deterministic check rather
# than a present_min_ratio / absence_min_ratio threshold.
# --------------------------------------------------------------------------

def _quotes_by_kind(entry: dict) -> dict[str, str]:
    return {p["kind"]: p["quote"] for p in entry["passages"]}


def test_compare_versions_negation_added_is_reported_as_changed():
    """'is expected to grow' -> 'is not expected to grow': the removed (older) and added (newer) sentences must be
    quoted verbatim, and nothing else in the fixture is disturbed."""
    v1, v2 = _build_view(fx.NEGATION_GROWTH_V1), _build_view(fx.NEGATION_GROWTH_V2)
    report = C.compare_versions(v1, v2)
    changed_headlines = {c["headline"] for c in report["changed"]}
    assert "Market Trends And Outlook Today" in changed_headlines
    outlook = next(c for c in report["changed"] if c["headline"] == "Market Trends And Outlook Today")
    quotes = _quotes_by_kind(outlook)
    assert quotes["removed"] == ("The market is expected to grow next year across all of our served end markets "
                                 "and major product categories worldwide.")
    assert quotes["added"] == ("The market is not expected to grow next year across all of our served end markets "
                               "and major product categories worldwide.")
    assert {m["headline"] for m in report["minor_rewordings"]} == set()


def test_compare_versions_negation_removed_is_reported_as_changed():
    """The other direction: 'we will not renew' -> 'we will renew' must be caught exactly like the addition of a
    negator above (the check is symmetric in both texts' sentences)."""
    v1, v2 = _build_view(fx.NEGATION_RENEWAL_V1), _build_view(fx.NEGATION_RENEWAL_V2)
    report = C.compare_versions(v1, v2)
    changed_headlines = {c["headline"] for c in report["changed"]}
    assert "Lease Commitments And Renewals" in changed_headlines
    lease = next(c for c in report["changed"] if c["headline"] == "Lease Commitments And Renewals")
    quotes = _quotes_by_kind(lease)
    assert quotes["removed"].startswith("We will not renew the facility lease")
    assert quotes["added"].startswith("We will renew the facility lease")


def test_compare_versions_negation_short_unit_is_reported_as_changed_by_default():
    """'no material impact' -> 'a material impact': a SHORT one-line item, only reachable via
    :data:`C.UPLOAD_ALIGN_PARAMS` (module docstring "(a)") — see the ablation test right below for why."""
    v1, v2 = _build_view(fx.NEGATION_SHORT_IMPACT_V1), _build_view(fx.NEGATION_SHORT_IMPACT_V2)
    report = C.compare_versions(v1, v2)
    changed_headlines = {c["headline"] for c in report["changed"]}
    assert "Risk Note" in changed_headlines
    note = next(c for c in report["changed"] if c["headline"] == "Risk Note")
    assert _quotes_by_kind(note) == {"removed": "There is no material impact.", "added": "There is a material impact."}


def test_compare_versions_negation_short_unit_needs_upload_align_params_to_reach_a_matched_partner():
    """Ablates part (a) of the fix: under the plain SEC-tuned ``graph.alignment.AlignParams()`` (``min_body_tokens``
    10), the short 'Risk Note' item never enters the body assignment at all and is left ``uncertain`` with NO
    matched partner — the negation-polarity check (b) has nothing to pair it against, so it falls to
    ``minor_rewordings`` with no ``newer_unit_id`` instead of being caught. This is exactly the gap
    :data:`C.UPLOAD_ALIGN_PARAMS` closes (module docstring "(a)")."""
    from semigraph.graph.alignment import AlignParams as SecDefaultAlignParams

    v1, v2 = _build_view(fx.NEGATION_SHORT_IMPACT_V1), _build_view(fx.NEGATION_SHORT_IMPACT_V2)
    report = C.compare_versions(v1, v2, align_params=SecDefaultAlignParams())
    assert {c["headline"] for c in report["changed"]} == set()
    minor = next(m for m in report["minor_rewordings"] if m["headline"] == "Risk Note")
    assert minor["newer_unit_id"] is None


def test_compare_versions_negation_multiple_sentences_only_flags_the_flipped_one():
    """A unit with two sentences: only the first flips negation polarity, the second is an unrelated word swap
    (regional -> worldwide) that must NOT itself produce a spurious quote pair."""
    v1, v2 = _build_view(fx.NEGATION_MULTI_SENTENCE_V1), _build_view(fx.NEGATION_MULTI_SENTENCE_V2)
    report = C.compare_versions(v1, v2)
    matter = next(c for c in report["changed"] if c["headline"] == "Regulatory And Compliance Matters")
    assert len(matter["passages"]) == 2                       # exactly one removed/added pair, not two
    quotes = _quotes_by_kind(matter)
    assert quotes["removed"] == "The company is not subject to any material pending regulatory investigations at this time."
    assert quotes["added"] == "The company is subject to any material pending regulatory investigations at this time."
    for passage in matter["passages"]:
        assert "regional" not in passage["quote"] and "worldwide" not in passage["quote"]


def test_compare_versions_double_negation_of_the_same_parity_is_not_a_flip():
    """A double negation that keeps the SAME parity on both sides (two negators become two different negators: a
    'restriction' that is 'never waived' becomes a 'clause' that is 'never waived') must NOT be reported as a
    polarity flip — it still lands in ``minor_rewordings`` like any other sub-threshold rewording, never
    ``changed``, and never silently folded into ``unchanged_count`` either."""
    v1, v2 = _build_view(fx.NEGATION_DOUBLE_V1), _build_view(fx.NEGATION_DOUBLE_V2)
    report = C.compare_versions(v1, v2)
    assert C._negation_count("There is no restriction that is never waived under the terms of our standard "
                             "distribution agreements today.") == 2
    assert C._negation_count("There is no clause that is never waived under the terms of our standard "
                             "distribution agreements today.") == 2
    assert {c["headline"] for c in report["changed"]} == set()
    assert {m["headline"] for m in report["minor_rewordings"]} == {"Contractual Restrictions Overview"}
    assert report["unchanged_count"] == 2                     # Executive Summary + Company History only


def test_compare_versions_byte_identical_units_are_never_flagged_as_a_negation_flip():
    """A negation check that ran on every ``unchanged`` pair regardless of content would be pure waste (identical
    text can never flip polarity); this pins that byte-identical units among a negation fixture still count as
    plain ``unchanged``, never promoted."""
    v1 = _build_view(fx.NEGATION_GROWTH_V1)
    v2 = _build_view(fx.NEGATION_GROWTH_V1)
    report = C.compare_versions(v1, v2)
    assert report["not_compared_reason"] == "identical_content"


def test_compare_versions_tense_only_edit_still_lands_in_minor_rewordings_with_negation_fix_active():
    """The tense-only fixture above (no negator on either side) must be completely unaffected by the
    negation-polarity check: 0 negators on both sides is the same parity, so it is never even a candidate flip."""
    assert C._negation_count("New regulations will impact our supply chain operations significantly this year.") == 0
    assert C._negation_count("New regulations impacted our supply chain operations significantly this year.") == 0


# --------------------------------------------------------------------------
# not_compared_reason guards
# --------------------------------------------------------------------------

def test_compare_versions_identical_content(md_v1):
    same = _build_view(fx.MD_V1)
    report = C.compare_versions(md_v1, same)
    assert report == {
        "items_compared": False, "not_compared_reason": "identical_content",
        "added": [], "removed": [], "changed": [], "minor_rewordings": [], "unchanged_count": len(md_v1.units),
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
