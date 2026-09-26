"""The temporal block with SEVERAL pairs of one company and with notices (plan step 2 of the multi-pair retrieval).

One section per pair, oldest first, each headed by the two filings it compares; a pair that was chosen because the question
names fiscal years (or spans several annual reports) says which fiscal years it covers; a notice (a named year the graph has no
comparison for) is stated, never silently replaced. The lists inside a section, the labels and ``removal_supported_ids`` are
the ones of the single-pair block: those tests (tests/test_answerer_blocks.py, tests/test_answerer_review.py) stay untouched.
"""

from semigraph.retrieval.answerer import build_blocks
from semigraph.retrieval.context_layout import (
    CONTEXT_HEADERS,
    NONE_BLOCK,
    NOT_COMPARED_PREFIX,
    REMOVED_ITEMS_PREFIX,
    removal_supported_ids,
    temporal_block,
)

NVDA = 1045810
A, B, C = "0001045810-24-000029", "0001045810-25-000023", "0001045810-26-000021"


def pair(older, newer, *, selection=None, older_pe="2024-01-28", newer_pe="2025-01-26", **over):
    base = {"company": "Nvidia", "cik": NVDA, "older_accession": older, "older_form": "10-K", "older_date": "2024-02-21",
            "newer_accession": newer, "newer_form": "10-K", "newer_date": "2025-02-26", "compared": True,
            "not_compared_reason": None, "totals": {"removed": 1, "unsettled": 0, "new": 0, "reworded": 0},
            "passage_totals": {"removed": 0, "added": 0, "reworded": 0}}
    if selection:
        base.update(selection=selection, older_period_end=older_pe, newer_period_end=newer_pe, older_fy=2024, newer_fy=2025)
    return {**base, **over}


def item(change, headline, older_acc, newer_acc, **ids):
    return {"company": "Nvidia", "cik": NVDA, "change": change, "headline": headline, "older_headline": None,
            "unit_kind": "headline", "section_id": "I.1A", "older_chunk_ids": ids.get("older", []),
            "newer_chunk_ids": ids.get("newer", []), "decided_by": None, "lead_text": None,
            "older_accession": older_acc, "newer_accession": newer_acc}


def passage(kind, text, older, newer, chunk):
    return {"cik": NVDA, "kind": kind, "item_headline": "Export controls", "lead_text": None, "section_id": "I.1A", "text": text,
            "counterpart_text": None, "chunk_ids": [chunk], "counterpart_chunk_ids": [], "older_accession": older,
            "newer_accession": newer}


def two_pairs():
    return [pair(A, B, selection="multi"), pair(B, C, selection="multi", older_pe="2025-01-26", newer_pe="2026-01-25",
                                              totals={"removed": 0, "unsettled": 0, "new": 1, "reworded": 0})]


def two_pair_items():
    return [item("removed", "Indebtedness could hurt us", A, B, older=[f"{A}:I.1A:0162"]),
            item("new", "Commercial arrangements expose us to counterparty risks", B, C, newer=[f"{C}:I.1A:0349"])]


def test_two_pairs_of_one_company_are_two_sections_each_with_its_own_items_and_the_fiscal_years_it_covers():
    block, valid = temporal_block(two_pair_items(), two_pairs())
    first, second = block.split("\n\n")
    assert first.splitlines()[0].startswith("Nvidia: 10-K filed 2024-02-21 (accession " + A)
    assert f"compared with 10-K filed 2025-02-26 (accession {B})" in first.splitlines()[0]
    assert "covers the fiscal year ended 2024-01-28 -> the fiscal year ended 2025-01-26" in first
    assert "spans several annual reports" in first
    assert "Indebtedness could hurt us" in first and "Commercial arrangements" not in first
    assert "Commercial arrangements" in second and "Indebtedness" not in second
    assert "covers the fiscal year ended 2025-01-26 -> the fiscal year ended 2026-01-25" in second
    assert valid == {f"{A}:I.1A:0162", f"{C}:I.1A:0349"}


def test_a_pair_named_by_the_question_says_so():
    block, _ = temporal_block([], [pair(A, B, selection="named")])
    assert "shown because the question names these fiscal years" in block and "covers the fiscal year ended 2024-01-28" in block


def test_the_default_pair_renders_no_covers_line_so_a_question_naming_no_pair_is_unchanged():
    for selection in (None, "latest"):
        block, _ = temporal_block([], [pair(A, B, selection=selection)])
        assert "covers the fiscal year" not in block and "shown because" not in block


def test_items_and_passages_without_pair_accessions_belong_to_their_companys_only_pair():
    plain = {k: v for k, v in item("removed", "Old risk", A, B, older=[f"{A}:I.1A:0001"]).items()
             if k not in ("older_accession", "newer_accession")}
    block, _ = temporal_block([plain], [pair(A, B)])
    assert '- "Old risk"' in block


def test_a_notice_for_a_company_with_a_pair_comes_first_in_its_section():
    notice = {"cik": NVDA, "company": "Nvidia", "text": "no annual-filing comparison covering fiscal 2020 is in the graph"}
    block, _ = temporal_block([], [pair(B, C)], notices=[notice])
    assert block.splitlines()[0] == "Note for Nvidia: " + notice["text"] + "."
    assert block.splitlines()[1].startswith("Nvidia: 10-K filed")


def test_a_notice_for_a_company_with_no_pair_is_the_whole_section_not_none():
    notice = {"cik": 2488, "company": "AMD", "text": "no annual-filing comparison covering fiscal 2020 is in the graph"}
    assert temporal_block([], [], notices=[notice])[0] == "Note for AMD: " + notice["text"] + "."
    assert temporal_block([], [], notices=[])[0] == NONE_BLOCK


def test_two_companies_with_notices_and_pairs_keep_each_notice_with_its_own_company():
    other = {**pair(A, B), "company": "Micron", "cik": 723125}
    notices = [{"cik": 723125, "company": "Micron", "text": "latest shown instead"}]
    block, _ = temporal_block([], [pair(A, B), other], notices=notices)
    nvidia, micron = block.split("\n\n")
    assert "Note for" not in nvidia and micron.splitlines()[0] == "Note for Micron: latest shown instead."


def test_a_not_compared_named_pair_still_says_comparison_not_available_with_the_reason():
    block, _ = temporal_block([], [pair(A, B, selection="named", compared=False,
                                        not_compared_reason="no risk items were loaded for the 10-K filed 2024-02-21")])
    assert f"{NOT_COMPARED_PREFIX} (no risk items were loaded" in block and "covers the fiscal year" in block


def context_of(items, pairs, passages=(), notices=()):
    block, _ = temporal_block(items, pairs, passages, notices)
    return CONTEXT_HEADERS[4] + block + CONTEXT_HEADERS[5]


def test_removal_supported_ids_is_the_union_over_both_pairs_and_excludes_the_new_items_of_either():
    items = two_pair_items() + [item("removed", "Second removal", B, C, older=[f"{B}:I.1A:0300"])]
    pairs = [pair(A, B, selection="multi"), pair(B, C, selection="multi", totals={"removed": 1, "unsettled": 0, "new": 1, "reworded": 0})]
    ctx = context_of(items, pairs, [passage("removed", "a sentence", B, C, f"{B}:I.1A:0400")])
    supported = removal_supported_ids(ctx)
    assert supported == {f"{A}:I.1A:0162", f"{B}:I.1A:0300", f"{B}:I.1A:0400"}   # both removed items and the removed passage, not the NEW item [C:...0349]


def test_the_lines_this_change_adds_never_look_like_a_list_line_or_a_removed_heading():
    notice = [{"cik": NVDA, "company": "Nvidia", "text": "showing the 2 most recent of 3 annual-filing comparisons for Nvidia"}]
    block, _ = temporal_block(two_pair_items(), two_pairs(), notices=notice)
    for line in block.splitlines():
        if line.startswith(("Note for", "covers the fiscal year")):
            assert not line.startswith("- ") and not line.startswith(REMOVED_ITEMS_PREFIX)


def test_build_blocks_passes_the_notices_through():
    r = {"anchors": {}, "edges": [], "metrics": [], "risks": [], "chunks": [], "temporal": [], "temporal_pairs": [],
         "temporal_passages": [], "temporal_notices": [{"cik": NVDA, "company": "Nvidia", "text": "shown instead"}]}
    blocks, _, _ = build_blocks(r)
    assert blocks.temporal_block == "Note for Nvidia: shown instead."
