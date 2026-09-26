"""M1b context blocks: fiscal-year metrics with computed year-over-year, external events, the text-verified temporal
block, the id -> text map for grounding, and the prompt rules. Pure: no database, no network, no model."""

import random

import pytest

from semigraph.retrieval.answerer import (
    ANSWER_PROMPT,
    build_blocks,
    sources_from_context,
    yoy_note,
)

NVDA = 1045810
OLD, NEW = "0001045810-25-000023", "0001045810-26-000021"


def metric(company, metric_name, value, start, end, cik=NVDA, unit=None):
    row = {"company": company, "cik": cik, "metric": metric_name, "value": value, "period_start": start,
           "period_end": end}
    return {**row, "unit": unit} if unit else row


def retrieval(**layers):
    base = {"anchors": {}, "edges": [], "metrics": [], "risks": [], "temporal": [], "temporal_pairs": [],
            "chunks": []}
    return {**base, **layers}


def metrics_block(rows):
    return build_blocks(retrieval(metrics=rows))[0].metrics_block


# ------------------------------------------------------------------------------------------------ year over year

def row(value, end, unit="USD"):
    return {"value": value, "period_end": end, "period_start": "x", "unit": unit}


def test_yoy_is_computed_in_code_to_one_decimal_with_the_absolute_change():
    note = yoy_note(row(215938000000.0, "2026-01-25"), row(130497000000.0, "2025-01-26"))
    assert note == "computed: +65.5% vs fiscal year ended 2025-01-26 (change +85,441,000,000 USD)"


def test_a_decline_is_negative_with_a_signed_change():
    note = yoy_note(row(80.0, "2025-12-31"), row(100.0, "2024-12-31"))
    assert note == "computed: -20.0% vs fiscal year ended 2024-12-31 (change -20 USD)"


@pytest.mark.parametrize("prior", [0.0, -1.0, -18756000000.0])
def test_a_zero_or_negative_prior_gives_no_percentage_only_the_change(prior):
    note = yoy_note(row(5.0, "2025-12-27"), row(prior, "2024-12-28"))
    assert note.startswith("computed: n/m vs fiscal year ended 2024-12-28 (change ")
    assert "%" not in note and "percentage not meaningful" in note


@pytest.mark.parametrize("gap_days,expected", [(349, False), (350, True), (364, True), (371, True), (380, True),
                                              (381, False), (730, False)])
def test_year_over_year_needs_the_immediately_preceding_fiscal_year(gap_days, expected):
    from datetime import date, timedelta

    end = date(2026, 1, 25)
    prior_end = (end - timedelta(days=gap_days)).isoformat()
    assert (yoy_note(row(2.0, end.isoformat()), row(1.0, prior_end)) is not None) is expected


def test_no_prior_row_or_a_different_unit_means_no_computed_line():
    assert yoy_note(row(2.0, "2025-12-31"), None) is None
    assert yoy_note(row(2.0, "2025-12-31", "USD"), row(1.0, "2024-12-31", "TWD")) is None


def test_a_missing_unit_counts_as_usd_on_both_sides():
    assert yoy_note({"value": 2.0, "period_end": "2025-12-31"}, {"value": 1.0, "period_end": "2024-12-31"}) is not None


def test_a_malformed_date_yields_no_computed_line_rather_than_an_error():
    assert yoy_note(row(2.0, "not-a-date"), row(1.0, "2024-12-31")) is None


# ------------------------------------------------------------------------------------------------ metrics block

def test_the_live_audit_error_class_cannot_recur_revenue_of_one_year_never_shares_a_group_with_income_of_another():
    """FY2020 revenue paired with FY2021 net income: rows arrive unevenly (net income only for FY2021), yet every
    fiscal year is its own group and a value only ever appears under its own period end."""
    rows = [metric("Nvidia", "net_income", 4332000000.0, "2020-01-28", "2021-01-31"),
            metric("Nvidia", "revenue", 16675000000.0, "2020-01-28", "2021-01-31"),
            metric("Nvidia", "revenue", 10918000000.0, "2019-01-28", "2020-01-26")]
    block = metrics_block(rows)
    fy2021, fy2020 = block.split("Nvidia: fiscal year ended 2020-01-26\n")
    assert "Nvidia: fiscal year ended 2021-01-31" in fy2021
    assert "net_income" in fy2021 and "4,332,000,000" in fy2021 and "16,675,000,000" in fy2021
    assert "net_income" not in fy2020 and "4,332,000,000" not in fy2020 and "10,918,000,000" in fy2020


def test_only_the_last_three_fiscal_periods_are_shown_and_the_fourth_is_only_the_base_of_the_third():
    ends = ["2026-01-25", "2025-01-26", "2024-01-28", "2023-01-29"]
    rows = [metric("Nvidia", "revenue", v, "s", e) for v, e in zip([400.0, 300.0, 200.0, 100.0], ends, strict=True)]
    lines = metrics_block(rows).splitlines()
    assert [ln for ln in lines if not ln.startswith("- ")] == [
        "Nvidia: fiscal year ended 2026-01-25", "Nvidia: fiscal year ended 2025-01-26",
        "Nvidia: fiscal year ended 2024-01-28"]                           # the 4th period is not a group
    assert ("- revenue for period s..2024-01-28: 200 USD [xbrl:1045810:revenue:2024-01-28] | "
            "computed: +100.0% vs fiscal year ended 2023-01-29 (change +100 USD)") in lines


def test_the_order_rows_arrive_in_does_not_change_the_block():
    rows = [metric("Nvidia", "revenue", 3.0, "s", "2026-01-25"), metric("Nvidia", "revenue", 2.0, "s", "2025-01-26"),
            metric("Nvidia", "rnd", 1.0, "s", "2026-01-25"), metric("Nvidia", "rnd", 0.5, "s", "2025-01-26")]
    shuffled = rows[:]
    random.Random(7).shuffle(shuffled)
    assert shuffled != rows
    assert metrics_block(shuffled) == metrics_block(rows)


def test_companies_appear_in_the_order_they_were_retrieved():
    rows = [metric("Nvidia", "revenue", 3.0, "s", "2026-01-25"), metric("AMD", "revenue", 9.0, "s", "2025-12-27", cik=2488)]
    heads = [ln for ln in metrics_block(rows).splitlines() if not ln.startswith("- ")]
    assert heads == ["Nvidia: fiscal year ended 2026-01-25", "AMD: fiscal year ended 2025-12-27"]


def test_every_metric_line_is_citable_and_the_xbrl_ids_join_the_valid_ids():
    rows = [metric("Nvidia", "revenue", 3.0, "s", "2026-01-25")]
    _, context, valid_ids = build_blocks(retrieval(metrics=rows))
    assert "[xbrl:1045810:revenue:2026-01-25]" in context
    assert valid_ids == {"xbrl:1045810:revenue:2026-01-25"}


def test_a_row_without_a_cik_or_a_well_formed_metric_name_is_shown_but_not_citable():
    rows = [{"company": "Legacy", "metric": "revenue", "value": 5.0, "period_start": "s", "period_end": "2025-12-31"},
            metric("Nvidia", "R&D", 6.0, "s", "2025-12-31")]
    blocks, _, valid_ids = build_blocks(retrieval(metrics=rows))
    block = blocks.metrics_block
    assert "- revenue for period s..2025-12-31: 5 USD" in block and "R&D for period" in block
    assert "xbrl:" not in block and valid_ids == set()


# ------------------------------------------------------------------------------------------------ external events

def rule(company, rule_id, title, date, chunk_ids=None):
    return {"source": company, "relation": "AFFECTED_BY", "target": title, "status": "Active", "quote": None,
            "chunk_ids": chunk_ids or ["0001045810-26-000021:I.1A:0345"], "rule_id": rule_id, "date": date,
            "url": None, "kind": "x", "link_source": "federal_register", "link_method": "keyword", "external": True}


def test_rules_get_their_own_dated_block_with_an_fr_id_and_leave_the_relationships_block():
    company_edge = {"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC", "status": "Active",
                    "quote": None, "chunk_ids": None}
    blocks, context, valid_ids = build_blocks(retrieval(edges=[company_edge, rule("Nvidia", "2026-19537", "Rule A", "2026-03-12")]))
    assert "AFFECTED_BY" not in blocks.edges_block and "Rule A" not in blocks.edges_block
    assert blocks.external_block == "- 2026-03-12 [fr:2026-19537] Rule A (linked to Nvidia by keyword match)"
    assert "fr:2026-19537" in valid_ids


def test_the_company_chunk_ids_on_a_rule_edge_are_not_printed_beside_the_rule():
    """They are the company's own 10-K chunks that matched keywords: printed beside a rule they read as its source."""
    blocks, _, valid_ids = build_blocks(retrieval(edges=[rule("Nvidia", "2026-19537", "Rule A", "2026-03-12")]))
    assert "0001045810-26-000021:I.1A:0345" not in blocks.external_block
    assert "0001045810-26-000021:I.1A:0345" not in valid_ids


def test_a_rule_linked_to_two_companies_appears_once_per_company_and_a_correction_id_is_supported():
    edges = [rule("Nvidia", "C1-2026-16628", "Correction", "2026-04-01"), rule("AMD", "C1-2026-16628", "Correction", "2026-04-01")]
    blocks, _, valid_ids = build_blocks(retrieval(edges=edges))
    assert blocks.external_block.count("[fr:C1-2026-16628]") == 2 and valid_ids == {"fr:C1-2026-16628"}


def test_a_legacy_rule_row_without_a_document_number_is_shown_undated_and_uncitable():
    legacy = {"source": "Nvidia", "relation": "AFFECTED_BY", "target": "Old rule", "status": "Active",
              "quote": None, "chunk_ids": None}
    blocks, _, valid_ids = build_blocks(retrieval(edges=[legacy]))
    assert blocks.external_block == "- undated Old rule (linked to Nvidia by keyword match)" and valid_ids == set()


def test_only_rules_means_the_relationships_block_is_none_and_no_rules_means_the_external_block_is_none():
    assert build_blocks(retrieval(edges=[rule("Nvidia", "2026-19537", "Rule A", "2026-03-12")]))[0].edges_block == "(none)"
    assert build_blocks(retrieval())[0].external_block == "(none)"


# ------------------------------------------------------------------------------------------------ temporal block

def pair(**over):
    return {"company": "Nvidia", "cik": NVDA, "older_accession": OLD, "older_form": "10-K", "older_date": "2025-02-26",
            "newer_accession": NEW, "newer_form": "10-K", "newer_date": "2026-02-25",
            "totals": {"removed": 0, "new": 0, "reworded": 0}, **over}


def item(change, headline, older=(), newer=(), **over):
    return {"company": "Nvidia", "cik": NVDA, "change": change, "headline": headline, "older_headline": None,
            "unit_kind": "headline", "section_id": "I.1A", "older_chunk_ids": list(older),
            "newer_chunk_ids": list(newer), "decided_by": None, **over}


def temporal_block(items, pairs):
    return build_blocks(retrieval(temporal=items, temporal_pairs=pairs))[0].temporal_block


def test_no_riskitem_data_means_the_block_is_none():
    assert temporal_block([], []) == "(none)"


REMOVED_LABEL = "No longer appears as a separate risk factor"
NEW_LABEL = "No matching risk factor found in the earlier filing"
REMOVED_NOTE = ("the text check found no matching text in the newer filing; parts of their content may be covered inside "
                "other risk factors")


def test_a_comparison_with_no_changes_says_so_instead_of_none():
    block = temporal_block([], [pair()])
    assert block.splitlines()[0].startswith("Nvidia: 10-K filed 2025-02-26 (accession 0001045810-25-000023) compared with")
    assert f"{REMOVED_LABEL} - none found." in block and f"{NEW_LABEL} - none found." in block
    assert "Reworded - none found" in block
    assert block != "(none)"


def test_the_true_totals_are_stated_beside_the_capped_lists():
    block = temporal_block([item("removed", "A", [f"{OLD}:I.1A:0001"])], [pair(totals={"removed": 21, "new": 0, "reworded": 0})])
    assert f"{REMOVED_LABEL} - showing 1 of 21" in block


def test_the_removed_and_new_headings_carry_the_hedge_and_never_the_bare_verdict_words():
    """Held-out gold (M1b): an older item the pipeline calls removed is gone as a STANDALONE risk factor in 4 of 4 cases but 2
    of the 4 were absorbed into another risk factor; a newer item called new is new in only 6 of 12 (the rest are restructured
    older text). The headings are written by hand from that evidence (see the module docstring of context_layout)."""
    block = temporal_block([item("removed", "R", [f"{OLD}:I.1A:0001"]), item("new", "N", newer=[f"{NEW}:I.1A:0003"])],
                           [pair(totals={"removed": 1, "new": 1, "reworded": 0})])
    assert (f"{REMOVED_LABEL} - showing 1 of 1 risk factors ({REMOVED_NOTE}):") in block.splitlines()
    assert (f"{NEW_LABEL} - showing 1 of 1 risk factors (new, or a restructured older risk factor):") in block.splitlines()
    for stale in ("Removed -", "Added -", "text verified absent", "new in the later filing"):
        assert stale not in block, stale


def test_chunk_ids_of_shown_items_join_the_valid_ids_and_only_three_are_printed_per_side():
    older = [f"{OLD}:I.1A:{n:04d}" for n in range(5)]
    items = [item("removed", "A", older), item("new", "B", newer=[f"{NEW}:I.1A:0009"])]
    blocks, context, valid_ids = build_blocks(retrieval(temporal=items, temporal_pairs=[pair(totals={"removed": 1, "new": 1, "reworded": 0})]))
    assert valid_ids == {*older[:3], f"{NEW}:I.1A:0009"}
    assert f"[{older[3]}]" not in blocks.temporal_block


def test_a_headline_less_paragraph_unit_is_labelled_not_left_blank():
    """Changed by the review (M3): with no text at all the placeholder now says "paragraph" (a paragraph unit is not a
    "passage": that word names the changed sentences inside a surviving item). With text it is the first sentence
    (tests/test_answerer_review.py)."""
    block = temporal_block([item("new", None, newer=[f"{NEW}:I.1A:0001"], unit_kind="paragraph")],
                           [pair(totals={"removed": 0, "new": 1, "reworded": 0})])
    assert '- "(untitled paragraph, section I.1A)" [' in block


def test_headlines_are_flattened_and_truncated():
    long = "word " * 200
    block = temporal_block([item("new", "line one\nline two " + long, newer=[f"{NEW}:I.1A:0001"])],
                           [pair(totals={"removed": 0, "new": 1, "reworded": 0})])
    line = next(ln for ln in block.splitlines() if ln.startswith("- "))
    assert "line one line two" in line and "\n" not in line and len(line) < 400 and "..." in line


def test_each_company_gets_its_own_section_separated_by_a_blank_line():
    amd = pair(company="AMD", cik=2488, older_accession="A1", newer_accession="A2")
    block = temporal_block([item("new", "N", newer=[f"{NEW}:I.1A:0001"]),
                            item("new", "M", newer=["0000002488-26-000018:I.1A:0002"], company="AMD", cik=2488)],
                           [pair(totals={"removed": 0, "new": 1, "reworded": 0}), {**amd, "totals": {"removed": 0, "new": 1, "reworded": 0}}])
    first, second = block.split("\n\n")
    assert first.startswith("Nvidia:") and second.startswith("AMD:") and '"N"' in first and '"M"' in second


# ------------------------------------------------------------------------------------------------ not matched (unsettled)

UNSETTLED_HEADING = ("Not matched (the text check could not verify whether these older risk factors still appear; they may have "
                     "been removed or absorbed into another risk factor) - showing {shown} of {total}:")


def unsettled_pair(unsettled, **totals):
    return pair(totals={"removed": 0, "unsettled": unsettled, "new": 0, "reworded": 0, **totals})


def test_unsettled_items_are_a_separate_section_with_a_heading_that_claims_nothing():
    items = [item("unsettled", "Export controls risk", [f"{OLD}:I.1A:0001", f"{OLD}:I.1A:0002"]),
             item("unsettled", "Tax risk", [f"{OLD}:I.1A:0009"])]
    lines = temporal_block(items, [unsettled_pair(9)]).splitlines()
    at = lines.index(UNSETTLED_HEADING.format(shown=2, total=9))
    assert lines[at + 1:at + 3] == [f'- "Export controls risk" [{OLD}:I.1A:0001] [{OLD}:I.1A:0002]', f'- "Tax risk" [{OLD}:I.1A:0009]']


def test_the_unsettled_section_comes_after_removed_and_before_added_then_reworded():
    items = [item("removed", "R", [f"{OLD}:I.1A:0001"]), item("unsettled", "U", [f"{OLD}:I.1A:0002"]),
             item("new", "N", newer=[f"{NEW}:I.1A:0003"]), item("reworded", "W", [f"{OLD}:I.1A:0004"], [f"{NEW}:I.1A:0004"])]
    block = temporal_block(items, [pair(totals={"removed": 1, "unsettled": 1, "new": 1, "reworded": 1})])
    marks = [f"{REMOVED_LABEL} - showing 1 of 1", "Not matched (", f"{NEW_LABEL} - showing 1 of 1", "Reworded - showing 1 of 1"]
    assert [block.index(m) for m in marks] == sorted(block.index(m) for m in marks)
    assert (block.index('- "R"') < block.index("Not matched (") < block.index('- "U"') < block.index(NEW_LABEL)
            < block.index('- "N"'))


def test_the_unsettled_heading_never_says_removed_dropped_or_absent_as_a_fact():
    """The only place 'removed' appears is the hedge 'may have been removed or absorbed'; nothing says the risk is gone."""
    block = temporal_block([item("unsettled", "U", [f"{OLD}:I.1A:0001"])], [unsettled_pair(1)])
    heading = next(ln for ln in block.splitlines() if ln.startswith("Not matched ("))
    assert heading == UNSETTLED_HEADING.format(shown=1, total=1)
    for fact in ("were removed", "was removed", "no longer appear", "dropped", "absent", "text verified"):
        assert fact not in heading
    assert "could not verify" in heading and "may have been removed" in heading


def test_an_unsettled_item_cites_the_older_filings_chunk_ids_and_they_become_valid_ids():
    older = [f"{OLD}:I.1A:{n:04d}" for n in range(5)]
    blocks, _, valid_ids = build_blocks(retrieval(temporal=[item("unsettled", "U", older)], temporal_pairs=[unsettled_pair(1)]))
    assert valid_ids == set(older[:3]) and f"[{older[3]}]" not in blocks.temporal_block


def test_the_totals_are_stated_beside_the_capped_unsettled_list():
    block = temporal_block([item("unsettled", "U", [f"{OLD}:I.1A:0001"])], [unsettled_pair(14)])
    assert UNSETTLED_HEADING.format(shown=1, total=14) in block


def test_no_unsettled_items_means_no_section_at_all_not_a_none_found_line():
    for totals in ({"removed": 0, "unsettled": 0, "new": 0, "reworded": 0}, {"removed": 0, "new": 0, "reworded": 0}):   # or a payload without the key
        block = temporal_block([], [pair(totals=totals)])
        assert "Not matched" not in block and f"{REMOVED_LABEL} - none found." in block


def test_a_headline_less_unsettled_paragraph_is_labelled_by_its_first_sentence():
    unit = item("unsettled", None, [f"{OLD}:I.1A:0001"], unit_kind="paragraph", lead_text="We depend on TSMC for wafers. More.")
    block = temporal_block([unit], [unsettled_pair(1)])
    assert f'- "We depend on TSMC for wafers." [{OLD}:I.1A:0001]' in block


def test_a_pair_that_was_not_compared_shows_no_unsettled_section_even_when_rows_are_handed_in():
    block = temporal_block([item("unsettled", "U", [f"{OLD}:I.1A:0001"])],
                           [{**unsettled_pair(1), "compared": False, "not_compared_reason": "suspect section"}])
    assert "comparison not available (suspect section)" in block and "Not matched" not in block and '"U"' not in block


def test_each_company_lists_its_own_unsettled_items():
    amd = {**unsettled_pair(1), "company": "AMD", "cik": 2488, "older_accession": "A1", "newer_accession": "A2"}
    block = temporal_block([item("unsettled", "NVDA risk", [f"{OLD}:I.1A:0001"]),
                            item("unsettled", "AMD risk", ["A1:I.1A:0002"], company="AMD", cik=2488)], [unsettled_pair(1), amd])
    first, second = block.split("\n\n")
    assert '"NVDA risk"' in first and '"AMD risk"' not in first and '"AMD risk"' in second and '"NVDA risk"' not in second


# ------------------------------------------------------------------------------------------------ id -> text map

C1, C2, C3 = (f"{NEW}:I.1A:{n:04d}" for n in (1, 2, 3))


def test_sources_from_context_maps_graph_lines_and_excerpt_text_to_their_ids():
    r = retrieval(
        chunks=[{"chunk_id": C1, "text": "Customer A was 19% of revenue.\nSecond line."}],
        risks=[{"company": "Nvidia", "category": "x", "summary": "Customer concentration of 19%.", "chunk_id": C1},
               {"company": "Nvidia", "category": "x", "summary": "Other.", "chunk_id": C2}],
        temporal=[item("new", "Sovereign AI demand rose 25%", newer=[C3])],
        temporal_pairs=[pair(totals={"removed": 0, "new": 1, "reworded": 0})],
        edges=[rule("Nvidia", "2026-19537", "Rule about 50% tariffs", "2026-03-12")],
        metrics=[metric("Nvidia", "revenue", 3.0, "s", "2026-01-25"), metric("Nvidia", "revenue", 2.0, "s", "2025-01-26")])
    _, context, _ = build_blocks(r)
    sources = sources_from_context(context)
    assert "Customer A was 19% of revenue." in sources[C1] and "Second line." in sources[C1]
    assert "Customer concentration of 19%." in sources[C1]                 # the risk summary joins its chunk
    assert "Other." in sources[C2]
    assert "Sovereign AI demand rose 25%" in sources[C3]                   # a temporal headline is text behind its chunk id
    assert "Rule about 50% tariffs" in sources["fr:2026-19537"]            # a rule title is text behind its fr id
    assert "computed: +50.0%" in sources["xbrl:1045810:revenue:2026-01-25"]


def test_sources_from_context_reads_a_legacy_context_too():
    legacy = (f"RELATIONSHIPS:\n(none)\n\nMETRICS:\n(none)\n\nACTIVE RISKS:\n- Nvidia (x): summary of 7% [{C1}]\n\n"
              f"DROPPED RISK LINEAGES:\n(none)\n\nEXCERPTS:\n[{C2}]\nBody of 9%.\n\n[{C3}]\nOther body.\n")
    sources = sources_from_context(legacy)
    assert "summary of 7%" in sources[C1] and sources[C2].strip() == "Body of 9%." and "Other body." in sources[C3]


def test_a_context_without_an_excerpts_header_is_read_as_graph_blocks_only():
    assert sources_from_context(f"ACTIVE RISKS:\n- x [{C1}]") == {C1: f"- x [{C1}]"}


def test_the_link_method_of_the_row_is_printed_not_assumed():
    edge = {**rule("Nvidia", "2026-19537", "Rule A", "2026-03-12"), "link_method": "embedding"}
    assert "(linked to Nvidia by embedding match)" in build_blocks(retrieval(edges=[edge]))[0].external_block


# ------------------------------------------------------------------------------------------------ prompt rules

@pytest.mark.parametrize("fragment", [
    # citations: ids only, one per bracket, never section names
    "ONE id per bracket", "[Reported Metrics]", "[Dropped Risk Lineages]", "[xbrl:", "[fr:",
    # fiscal years by period end date, never computed ratios
    "fiscal year ended", "computed:", "never calculate",
    # external events are not disclosures
    "never describe", "as something the company disclosed",
    # capped lists: "at least"
    "at least",
    # existing guidance kept
    "ONLY the context", "say so plainly", "never fill gaps from memory", "Be concise", "bullet",
])
def test_the_answer_prompt_states_the_m1b_rules_and_keeps_the_existing_guidance(fragment):
    assert fragment in ANSWER_PROMPT


def test_the_prompt_template_has_exactly_the_documented_placeholders():
    import string

    names = {f for _, f, _, _ in string.Formatter().parse(ANSWER_PROMPT) if f}
    assert names == {"question", "edges_block", "external_block", "metrics_block", "risks_block", "temporal_block",
                     "chunks_block"}
