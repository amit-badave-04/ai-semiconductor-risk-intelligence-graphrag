"""Review of the M1b context: the temporal block with passages (graph contract L.7), not-compared pairs, paragraph units,
period-aware METRICS, the removal-claim check on the real block, the prompt rules and the template fingerprint.
Pure: no database, no network, no model."""

import re

import pytest

import semigraph.retrieval.answerer as answerer_mod
from semigraph.retrieval.answerer import (
    ANSWER_PROMPT,
    CITE_RE,
    CONTEXT_HEADERS,
    build_blocks,
    metrics_lines,
    sources_from_context,
    template_fingerprint,
)
from semigraph.retrieval.context_layout import (
    NOT_COMPARED_PREFIX,
    PASSAGE_QUOTE_CHARS,
    PASSAGES_ADDED_PHRASE,
    PASSAGES_REMOVED_PHRASE,
    REMOVED_ITEMS_PREFIX,
    UNSETTLED_ITEMS_PREFIX,
    removal_supported_ids,
)
from semigraph.retrieval.verify import answer_checks, verify_answer

NVDA = 1045810
OLD, NEW = "0001045810-25-000023", "0001045810-26-000021"
NEW_LABEL = "No matching risk factor found in the earlier filing"
_HEDGE = "a differently worded version of the same statement may exist"
# The two passage headings, written out by hand (the writer builds them from the same constants; this pins the words).
REMOVED_PASSAGES_HEADING = ("Passages of surviving risk factors whose wording was not found in the newer filing "
                            "(showing {shown} of {total}; " + _HEDGE + "):")
ADDED_PASSAGES_HEADING = ("Passages of surviving risk factors whose wording was not found in the older filing "
                          "(showing {shown} of {total}; " + _HEDGE + "):")


def retrieval(**layers):
    base = {"anchors": {}, "edges": [], "metrics": [], "risks": [], "temporal": [], "temporal_pairs": [],
            "temporal_passages": [], "chunks": []}
    return {**base, **layers}


def pair(**over):
    return {"company": "Nvidia", "cik": NVDA, "older_accession": OLD, "older_form": "10-K", "older_date": "2025-02-26",
            "newer_accession": NEW, "newer_form": "10-K", "newer_date": "2026-02-25", "compared": True,
            "not_compared_reason": None, "totals": {"removed": 0, "new": 0, "reworded": 0},
            "passage_totals": {"removed": 0, "added": 0, "reworded": 0}, **over}


def item(change, headline, older=(), newer=(), **over):
    return {"company": "Nvidia", "cik": NVDA, "change": change, "headline": headline, "older_headline": None,
            "unit_kind": "headline", "section_id": "I.1A", "older_chunk_ids": list(older),
            "newer_chunk_ids": list(newer), "decided_by": None, "lead_text": None, **over}


def passage(kind, text, *, chunk_ids=(), counterpart=None, counterpart_ids=(), headline="Export controls", **over):
    return {"cik": NVDA, "passage_id": f"p-{kind}", "kind": kind, "item_id": "i1", "item_headline": headline,
            "item_unit_kind": "headline", "section_id": "I.1A", "lead_text": None, "text": text,
            "counterpart_text": counterpart, "similarity": 0.7, "chunk_ids": list(chunk_ids),
            "counterpart_chunk_ids": list(counterpart_ids), **over}


def temporal(items=(), pairs=None, passages=()):
    r = retrieval(temporal=list(items), temporal_pairs=[pair()] if pairs is None else pairs,
                  temporal_passages=list(passages))
    return build_blocks(r)


# ---------------------------------------------------------------------------------- H2: a pair that was not compared

def test_a_not_compared_pair_says_so_with_the_reason_and_nothing_else():
    blocks, _, valid_ids = temporal(pairs=[pair(compared=False, not_compared_reason="older section suspect (coverage 0.62)",
                                                totals={"removed": 0, "new": 0, "reworded": 0})])
    lines = blocks.temporal_block.splitlines()
    assert lines[0].startswith("Nvidia: 10-K filed 2025-02-26")
    assert lines[1:] == [f"{NOT_COMPARED_PREFIX} (older section suspect (coverage 0.62))"]
    assert "none found" not in blocks.temporal_block and "Removed" not in blocks.temporal_block
    assert valid_ids == set()


def test_a_not_compared_pair_is_never_read_as_nothing_changed():
    text = temporal(pairs=[pair(compared=False, not_compared_reason=None)])[0].temporal_block
    assert "comparison not available (reason not recorded)" in text and "nothing changed" not in text.lower()


def test_a_not_compared_company_and_a_compared_one_sit_side_by_side():
    intel = pair(company="Intel", cik=50863, compared=False, not_compared_reason="section suspect",
                 older_accession="I1", newer_accession="I2")
    block = temporal([item("new", "N", newer=[f"{NEW}:I.1A:0001"])],
                     [pair(totals={"removed": 0, "new": 1, "reworded": 0}), intel])[0].temporal_block
    first, second = block.split("\n\n")
    assert first.startswith("Nvidia:") and f"{NEW_LABEL} - showing 1 of 1" in first
    assert second.startswith("Intel:") and "comparison not available (section suspect)" in second


# ---------------------------------------------------------------------------------- M3: paragraphs and their labels

def test_section_lines_say_risk_factors_for_headline_units_and_paragraphs_for_paragraph_units():
    heads = temporal([item("removed", "A", [f"{OLD}:I.1A:0001"])], [pair(totals={"removed": 1, "new": 0, "reworded": 0})])[0].temporal_block
    paras = temporal([item("removed", "", [f"{OLD}:I.1A:0001"], unit_kind="paragraph")],
                     [pair(totals={"removed": 1, "new": 0, "reworded": 0})])[0].temporal_block
    assert ("No longer appears as a separate risk factor - showing 1 of 1 risk factors (the text check found no matching "
            "text in the newer filing; parts of their content may be covered inside other risk factors):") in heads
    # a paragraph is not a risk factor: the label, the noun and the note all say paragraph
    assert ("No longer appears as a separate paragraph - showing 1 of 1 paragraphs (the text check found no matching text "
            "in the newer filing; parts of their content may be covered inside other paragraphs):") in paras
    assert "separate risk factor" not in paras


def test_a_list_of_mixed_units_says_risk_factor_or_paragraph():
    items = [item("new", "A", newer=[f"{NEW}:I.1A:0001"]), item("new", "", newer=[f"{NEW}:I.1A:0002"], unit_kind="paragraph")]
    block = temporal(items, [pair(totals={"removed": 0, "new": 2, "reworded": 0})])[0].temporal_block
    assert ("No matching risk factor or paragraph found in the earlier filing - showing 2 of 2 risk factors and paragraphs "
            "(new, or a restructured older risk factor or paragraph):") in block


def test_a_headline_less_paragraph_unit_is_labelled_by_its_first_sentence_cut_at_160_characters():
    lead = "We depend on a single foundry for our most advanced products. It is located in Taiwan."
    block = temporal([item("new", "", newer=[f"{NEW}:I.1A:0001"], unit_kind="paragraph", lead_text=lead)],
                     [pair(totals={"removed": 0, "new": 1, "reworded": 0})])[0].temporal_block
    assert '- "We depend on a single foundry for our most advanced products." [' in block
    assert "untitled" not in block and "It is located in Taiwan" not in block
    long = "word " * 100
    block = temporal([item("new", "", newer=[f"{NEW}:I.1A:0001"], unit_kind="paragraph", lead_text=long)],
                     [pair(totals={"removed": 0, "new": 1, "reworded": 0})])[0].temporal_block
    line = next(ln for ln in block.splitlines() if ln.startswith("- "))
    assert line.count("word") < 40 and '..."' in line and len(line) < 220


def test_a_paragraph_unit_without_any_text_keeps_an_honest_placeholder():
    block = temporal([item("new", None, newer=[f"{NEW}:I.1A:0001"], unit_kind="paragraph")],
                     [pair(totals={"removed": 0, "new": 1, "reworded": 0})])[0].temporal_block
    assert '- "(untitled paragraph, section I.1A)" [' in block


# ---------------------------------------------------------------------------------- passages (contract L.7)

OLD_IDS = [f"{OLD}:I.1A:0210", f"{OLD}:I.1A:0211"]
NAC = "The Notified Advanced Computing, or NAC, process has not resulted in approvals for exports of products to customers in China."


def passages_context():
    passages = [passage("removed", NAC, chunk_ids=OLD_IDS),
                passage("added", "In April 2025 the U.S. government required licenses for H20.", chunk_ids=[f"{NEW}:I.1A:0350"]),
                passage("reworded", "We impact revenue.", chunk_ids=[f"{OLD}:I.1A:0140"], counterpart="We impacted revenue.",
                        counterpart_ids=[f"{NEW}:I.1A:0347"])]
    p = pair(totals={"removed": 0, "new": 0, "reworded": 0}, passage_totals={"removed": 12, "added": 6, "reworded": 5})
    return temporal([], [p], passages)


def test_the_passages_render_inside_the_temporal_block_after_the_item_lists_with_their_totals():
    block = passages_context()[0].temporal_block
    lines = block.splitlines()
    assert lines[-6] == REMOVED_PASSAGES_HEADING.format(shown=1, total=12)
    assert ADDED_PASSAGES_HEADING.format(shown=1, total=6) in lines
    assert "Passages of surviving risk factors that were reworded (showing 1 of 5):" in lines
    assert block.index("Reworded - none found.") < block.index(REMOVED_PASSAGES_HEADING.format(shown=1, total=12))


def test_the_passage_headings_say_wording_not_found_with_the_hedge_and_never_gone_or_new():
    """Held-out gold, sentence level: removed passages are right about 0.84 of the time (about 1 in 6 is still stated in
    different words), added passages about 0.92. The headings carry that as a hedge; the old 'that no longer appear' and
    'that are new' are gone."""
    block = passages_context()[0].temporal_block
    assert "that no longer appear" not in block and "that are new" not in block
    assert "a differently worded version of the same statement may exist" in REMOVED_PASSAGES_HEADING
    assert "a differently worded version of the same statement may exist" in ADDED_PASSAGES_HEADING
    assert "newer filing" in REMOVED_PASSAGES_HEADING and "older filing" in ADDED_PASSAGES_HEADING


def test_a_removed_passage_names_its_item_quotes_its_text_and_cites_its_own_chunk_ids():
    block = passages_context()[0].temporal_block
    assert f'- in "Export controls": "{NAC}" [{OLD_IDS[0]}] [{OLD_IDS[1]}]' in block


def test_an_added_passage_cites_the_newer_filing_and_a_reworded_one_both_wordings():
    block = passages_context()[0].temporal_block
    assert f'- in "Export controls": "In April 2025 the U.S. government required licenses for H20." [{NEW}:I.1A:0350]' in block
    assert (f'- in "Export controls": earlier wording: "We impact revenue." [{OLD}:I.1A:0140] | later wording: '
            f'"We impacted revenue." [{NEW}:I.1A:0347]') in block


def test_a_reworded_counterpart_with_no_chunk_ids_is_quoted_uncited_never_under_a_guessed_id():
    """The graph contract L.7 gives a passage only the chunk ids of the filing it is quoted from."""
    p = passage("reworded", "We impact revenue.", chunk_ids=[f"{OLD}:I.1A:0140"], counterpart="We impacted revenue.")
    blocks, _, valid_ids = temporal([], [pair(passage_totals={"removed": 0, "added": 0, "reworded": 1})], [p])
    line = next(ln for ln in blocks.temporal_block.splitlines() if "earlier wording" in ln)
    assert line.endswith('| later wording (no citable id): "We impacted revenue."')
    assert valid_ids == {f"{OLD}:I.1A:0140"}


def test_passage_chunk_ids_become_citable_and_only_three_are_printed():
    ids = [f"{OLD}:I.1A:{n:04d}" for n in range(5)]
    _, _, valid_ids = temporal([], [pair(passage_totals={"removed": 1, "added": 0, "reworded": 0})],
                               [passage("removed", NAC, chunk_ids=ids)])
    assert valid_ids == set(ids[:3])


def test_a_passage_up_to_the_stored_cap_is_quoted_whole_and_the_quote_limit_matches_it():
    """graph/passages.py cuts a passage at max_passage_chars (450), so the block must quote 450 characters: with 300 the NAC and
    Hong Kong sentences in the middle of a stored passage were clipped out of the answer context."""
    from semigraph.graph.passages import PassageParams

    assert PASSAGE_QUOTE_CHARS >= PassageParams().max_passage_chars == 450
    text = "The Notified Advanced Computing, or NAC, process " + "has not resulted in approvals. " * 12   # ~420 characters
    assert 400 < len(text) <= 450
    block = temporal([], [pair(passage_totals={"removed": 1, "added": 0, "reworded": 0})],
                     [passage("removed", text, chunk_ids=OLD_IDS)])[0].temporal_block
    assert text.strip() in block and "..." not in block.split("Passages", 1)[1].split("[", 1)[0]


def test_a_long_passage_is_quoted_up_to_the_limit_on_one_line():
    text = "First line of the passage.\nSecond line " + "word " * 200
    block = temporal([], [pair(passage_totals={"removed": 1, "added": 0, "reworded": 0})],
                     [passage("removed", text, chunk_ids=OLD_IDS)])[0].temporal_block
    line = next(ln for ln in block.splitlines() if ln.startswith("- in "))
    quote = line.split(': "', 1)[1].rsplit('" [', 1)[0]
    assert "First line of the passage. Second line word" in quote and "\n" not in line
    assert len(quote) <= PASSAGE_QUOTE_CHARS and quote.endswith("...")


def test_a_pair_with_no_passage_layer_prints_no_passage_section_rather_than_none_found():
    block = temporal([], [pair()], [])[0].temporal_block
    assert "Passages" not in block


def test_passages_of_paragraph_units_say_paragraphs_and_are_labelled_by_the_units_first_sentence():
    p = passage("removed", NAC, chunk_ids=OLD_IDS, headline="", item_unit_kind="paragraph",
                lead_text="Our business is subject to complex export laws. More.")
    block = temporal([], [pair(passage_totals={"removed": 1, "added": 0, "reworded": 0})], [p])[0].temporal_block
    assert REMOVED_PASSAGES_HEADING.format(shown=1, total=1).replace("risk factors", "paragraphs") in block
    assert '- in "Our business is subject to complex export laws.":' in block


def test_each_company_gets_its_own_passages():
    amd = pair(company="AMD", cik=2488, older_accession="A1", newer_accession="A2",
               passage_totals={"removed": 1, "added": 0, "reworded": 0})
    mine = passage("removed", "Nvidia passage one.", chunk_ids=OLD_IDS)
    theirs = passage("removed", "AMD passage one.", chunk_ids=["A1:I.1A:0001"], cik=2488)
    block = temporal([], [pair(passage_totals={"removed": 1, "added": 0, "reworded": 0}), amd], [mine, theirs])[0].temporal_block
    first, second = block.split("\n\n")
    assert "Nvidia passage one." in first and "AMD passage" not in first
    assert "AMD passage one." in second and "Nvidia passage" not in second


def test_passage_text_is_the_text_behind_its_ids_for_the_grounding_map():
    _, context, _ = passages_context()
    sources = sources_from_context(context)
    assert NAC in sources[OLD_IDS[0]] and NAC in sources[OLD_IDS[1]]


# ---------------------------------------------------------------------------------- M2: the removal-claim check

REMOVED_ITEM = f"{OLD}:I.1A:0300"
KEPT_ITEM = f"{OLD}:I.1A:0400"


def removal_context():
    items = [item("removed", "Hong Kong transition risk", [REMOVED_ITEM]), item("reworded", "New wording",
             older=[KEPT_ITEM], newer=[f"{NEW}:I.1A:0401"]), item("new", "Sovereign AI", newer=[f"{NEW}:I.1A:0500"])]
    p = pair(totals={"removed": 1, "new": 1, "reworded": 1}, passage_totals={"removed": 1, "added": 1, "reworded": 1})
    passages = [passage("removed", NAC, chunk_ids=OLD_IDS),
                passage("added", "New sentence about licences.", chunk_ids=[f"{NEW}:I.1A:0350"]),
                passage("reworded", "Old.", chunk_ids=[f"{OLD}:I.1A:0140"], counterpart="New.")]
    return build_blocks(retrieval(temporal=items, temporal_pairs=[p], temporal_passages=passages))


def checks_of(text, context, valid_ids):
    return answer_checks(text, set(CITE_RE.findall(text)), valid_ids, context, sources=sources_from_context(context))


def test_only_ids_under_the_removed_lists_support_a_removal_claim():
    _, context, _ = removal_context()
    assert removal_supported_ids(context) == {REMOVED_ITEM, *OLD_IDS}


def test_removed_ids_are_read_back_from_the_context_string_alone():
    """The verifier never sees the retrieval dict: it works from the context, exactly as the model saw it."""
    _, context, _ = removal_context()
    assert removal_supported_ids("") == set() and removal_supported_ids("METRICS:\n(none)") == set()
    assert CONTEXT_HEADERS[4] in context


@pytest.mark.parametrize("text", [
    f"The Hong Kong transition risk was removed from the risk factors [{REMOVED_ITEM}].",
    f"This sentence of the older filing no longer appears in the newer Item 1A [{OLD_IDS[0]}] [{OLD_IDS[1]}].",
    f"- Removed: the NAC sentence was dropped from the risk factors [{OLD_IDS[0]}]",
])
def test_a_removal_claim_that_cites_only_removed_ids_is_supported(text):
    _, context, valid = removal_context()
    c = checks_of(text, context, valid)
    assert c.removal_claims == () and verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context,
                                                    sources=sources_from_context(context)) == []


@pytest.mark.parametrize("text", [
    f"Nvidia dropped its export-control risk factor [{KEPT_ITEM}].",                                  # a surviving (reworded) item
    f"Nvidia dropped its export-control risk factor [{NEW}:I.1A:0401].",                              # the newer wording
    f"Nvidia removed the risk about Hong Kong [{REMOVED_ITEM}] [{NEW}:I.1A:0401].",                   # one supported, one not
    "The risk factor about crypto was removed from the 10-K.",                                       # no citation at all
    f"The NAC sentence no longer appears in the filing [{NEW}:I.1A:0350].",                          # an ADDED passage's id
    f"Nvidia stopped disclosing the export-control risk [{KEPT_ITEM}].",
    f"The disclosure was eliminated [{KEPT_ITEM}].", f"The language was deleted from the 10-K [{KEPT_ITEM}].",
])
def test_a_removal_claim_citing_anything_else_is_reported_and_escalates(text):
    _, context, valid = removal_context()
    c = checks_of(text, context, valid)
    assert len(c.removal_claims) == 1 and c.as_dict()["unsupported_removal_claim"] is True
    assert "unsupported_removal_claim" in verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context,
                                                        sources=sources_from_context(context))


def test_the_claim_is_judged_per_clause_so_a_supported_removal_next_to_an_addition_passes():
    _, context, valid = removal_context()
    text = (f"The Hong Kong transition risk was removed [{REMOVED_ITEM}]; a new Sovereign AI risk was added "
            f"[{NEW}:I.1A:0500]. The Hong Kong risk was removed [{REMOVED_ITEM}], and revenue grew [xbrl:1045810:revenue:2026-01-25].")
    assert checks_of(text, context, valid | {"xbrl:1045810:revenue:2026-01-25"}).removal_claims == ()


def test_a_removal_verb_that_is_not_about_a_disclosure_is_not_a_removal_claim():
    _, context, valid = removal_context()
    for text in (f"Revenue dropped 5% [{KEPT_ITEM}].", f"Nvidia no longer sells the H20 in China [{KEPT_ITEM}].",
                 f"Shares were removed from the index [{KEPT_ITEM}]."):
        assert checks_of(text, context, valid).removal_claims == (), text


def test_a_bracketed_section_label_is_not_a_removal_claim():
    _, context, valid = removal_context()
    assert checks_of(f"Revenue rose [{KEPT_ITEM}] [Dropped Risk Lineages].", context, valid).removal_claims == ()


@pytest.mark.parametrize("text", [
    f"The acquisition risk was reworded, not removed [{KEPT_ITEM}].",
    f"The risk factor was never dropped [{KEPT_ITEM}].", f"The risk factor has not been removed from the 10-K [{KEPT_ITEM}].",
    f"The risk was reworded rather than deleted [{KEPT_ITEM}].",
])
def test_a_negated_removal_is_a_claim_that_the_disclosure_survives_not_a_removal_claim(text):
    _, context, valid = removal_context()
    assert checks_of(text, context, valid).removal_claims == ()


def test_without_a_context_no_removal_claim_is_made():
    text = f"Nvidia dropped its risk factor [{KEPT_ITEM}]."
    assert answer_checks(text, {KEPT_ITEM}, {KEPT_ITEM}, None).removal_claims == ()


def test_the_removed_lists_are_found_whatever_the_unit_noun():
    paras = build_blocks(retrieval(temporal=[item("removed", "", [REMOVED_ITEM], unit_kind="paragraph")],
                                   temporal_pairs=[pair(totals={"removed": 1, "new": 0, "reworded": 0})]))
    assert removal_supported_ids(paras[1]) == {REMOVED_ITEM}


# ---------------------------------------------------------------------------------- unsettled items and the removal claim

UNSETTLED_ITEM = f"{OLD}:I.1A:0600"


def unsettled_context():
    """One removed item and one 'Not matched' (unsettled) item: the unsettled ids are citable but never support a removal."""
    items = [item("removed", "Hong Kong transition risk", [REMOVED_ITEM]),
             item("unsettled", "Export licensing risk", [UNSETTLED_ITEM])]
    p = pair(totals={"removed": 1, "unsettled": 1, "new": 0, "reworded": 0})
    return build_blocks(retrieval(temporal=items, temporal_pairs=[p]))


def test_unsettled_ids_are_citable_but_never_support_a_removal_claim():
    _, context, valid = unsettled_context()
    assert UNSETTLED_ITEM in valid and REMOVED_ITEM in valid
    assert removal_supported_ids(context) == {REMOVED_ITEM}


def test_the_unsettled_heading_is_not_a_removed_heading_so_the_reader_never_counts_its_lines():
    blocks, _, _ = unsettled_context()
    heading = next(ln for ln in blocks.temporal_block.splitlines() if ln.startswith(UNSETTLED_ITEMS_PREFIX))
    assert not heading.startswith(REMOVED_ITEMS_PREFIX) and not UNSETTLED_ITEMS_PREFIX.startswith(REMOVED_ITEMS_PREFIX)
    assert not REMOVED_ITEMS_PREFIX.startswith(UNSETTLED_ITEMS_PREFIX)


def test_the_removed_list_stays_supported_when_an_unsettled_list_follows_it():
    blocks, context, _ = unsettled_context()
    assert blocks.temporal_block.index(REMOVED_ITEMS_PREFIX) < blocks.temporal_block.index("Not matched (")
    assert REMOVED_ITEM in removal_supported_ids(context) and UNSETTLED_ITEM not in removal_supported_ids(context)


@pytest.mark.parametrize("text", [
    f"The export licensing risk factor was removed from the 10-K [{UNSETTLED_ITEM}].",
    f"Nvidia dropped the export licensing risk [{UNSETTLED_ITEM}].",
    f"The export licensing risk no longer appears in the filing [{UNSETTLED_ITEM}].",
    f"The Hong Kong risk was removed [{REMOVED_ITEM}] [{UNSETTLED_ITEM}].",              # one supported id, one unsettled id
    f"- Removed: the Hong Kong risk [{REMOVED_ITEM}] and the export licensing risk [{UNSETTLED_ITEM}]",
    f"The export licensing risk may have been removed [{UNSETTLED_ITEM}].",              # even hedged: the wording is 'could not be verified'
])
def test_a_removal_claim_that_cites_an_unsettled_id_stays_flagged(text):
    _, context, valid = unsettled_context()
    c = checks_of(text, context, valid)
    assert len(c.removal_claims) == 1 and c.as_dict()["unsupported_removal_claim"] is True
    assert "unsupported_removal_claim" in verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context,
                                                        sources=sources_from_context(context))


def test_a_heading_with_bullets_that_lists_an_unsettled_item_under_removed_is_flagged():
    _, context, valid = unsettled_context()
    text = f"**Removed risk factors:**\n- Hong Kong risk [{REMOVED_ITEM}]\n- Export licensing risk [{UNSETTLED_ITEM}]"
    assert len(checks_of(text, context, valid).removal_claims) == 1


@pytest.mark.parametrize("text", [
    f"The text check could not verify whether the export licensing risk still appears [{UNSETTLED_ITEM}].",
    f"One older risk factor could not be verified as still present:\n- Export licensing risk [{UNSETTLED_ITEM}]",
    f"The Hong Kong risk was removed [{REMOVED_ITEM}]; the export licensing risk could not be matched [{UNSETTLED_ITEM}].",
    f"The export licensing risk was not removed; it could not be verified [{UNSETTLED_ITEM}].",
])
def test_wording_that_says_could_not_be_verified_is_not_a_removal_claim(text):
    _, context, valid = unsettled_context()
    assert checks_of(text, context, valid).removal_claims == ()
    assert verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context, sources=sources_from_context(context)) == []


def test_an_answer_with_no_temporal_block_in_its_context_supports_no_removal_at_all():
    text = f"The export licensing risk factor was removed [{UNSETTLED_ITEM}]."
    assert len(answer_checks(text, {UNSETTLED_ITEM}, {UNSETTLED_ITEM}, "EXCERPTS:\n(none)").removal_claims) == 1


# ---------------------------------------------------------------------------------- period-aware METRICS

def metric(value, end, name="revenue", company="Nvidia", cik=NVDA, unit=None):
    start = f"{int(end[:4]) - 1}-{end[5:]}"
    row = {"company": company, "cik": cik, "metric": name, "value": value, "period_start": start, "period_end": end}
    return {**row, "unit": unit} if unit else row


SIX_YEARS = [metric(10.0 * (y - 2020), f"{y}-01-26") for y in range(2021, 2027)]      # 2021 .. 2026


def test_a_question_that_names_a_year_gets_that_year_and_the_one_before_it_besides_the_latest_three():
    fetched = [m for m in SIX_YEARS if m["period_end"][:4] in ("2026", "2025", "2024", "2023", "2021")] \
        + [metric(0.0, "2020-01-26")]
    lines, ids = metrics_lines(fetched, years=[2021])
    ends = [ln for ln in lines if ln.startswith("Nvidia:")]
    assert ends == ["Nvidia: fiscal year ended 2026-01-26", "Nvidia: fiscal year ended 2025-01-26",
                    "Nvidia: fiscal year ended 2024-01-26", "Nvidia: fiscal year ended 2021-01-26",
                    "Nvidia: fiscal year ended 2020-01-26"]
    assert "xbrl:1045810:revenue:2021-01-26" in ids and "xbrl:1045810:revenue:2020-01-26" in ids
    named = next(ln for ln in lines if "2021-01-26]" in ln)
    assert "computed: n/m vs fiscal year ended 2020-01-26" in named      # its prior year is the base (a zero prior: n/m)


def test_the_named_year_carries_its_computed_change_against_the_year_before():
    rows = [metric(50.0, "2021-01-26"), metric(40.0, "2020-01-26"), metric(300.0, "2026-01-26"), metric(200.0, "2025-01-26"),
            metric(100.0, "2024-01-26"), metric(90.0, "2023-01-26")]
    lines, _ = metrics_lines(rows, years=[2021])
    line = next(ln for ln in lines if "2021-01-26]" in ln)
    assert "computed: +25.0% vs fiscal year ended 2020-01-26 (change +10 USD)" in line


def test_a_named_date_selects_that_period_end_only():
    rows = [metric(50.0, "2021-01-26"), metric(40.0, "2020-01-26"), metric(300.0, "2026-01-26"), metric(200.0, "2025-01-26"),
            metric(100.0, "2024-01-26"), metric(90.0, "2023-01-26"), metric(80.0, "2022-01-26")]
    lines, _ = metrics_lines(rows, dates=["2022-01-26"])
    heads = [ln for ln in lines if ln.startswith("Nvidia:")]
    assert "Nvidia: fiscal year ended 2022-01-26" in heads and "Nvidia: fiscal year ended 2021-01-26" in heads   # + its prior year
    assert "Nvidia: fiscal year ended 2020-01-26" not in heads and "Nvidia: fiscal year ended 2023-01-26" not in heads


def test_a_question_with_no_year_is_unchanged():
    rows = SIX_YEARS[-4:]
    assert metrics_lines(rows) == metrics_lines(rows, years=[], dates=[])
    assert len([ln for ln in metrics_lines(rows)[0] if ln.startswith("Nvidia:")]) == 3        # the 4th is only a base


def test_a_year_among_the_latest_three_adds_nothing_and_a_year_without_data_adds_nothing():
    rows = SIX_YEARS[-4:]
    assert metrics_lines(rows, years=[2026]) == metrics_lines(rows)
    assert metrics_lines(rows, years=[1999]) == metrics_lines(rows)


def test_build_blocks_reads_the_named_periods_from_the_retrieval_result():
    fetched = SIX_YEARS[-4:] + [metric(10.0, "2021-01-26"), metric(0.0, "2020-01-26")]
    plain = build_blocks(retrieval(metrics=fetched))[0].metrics_block
    named = build_blocks(retrieval(metrics=fetched, metric_periods={"years": [2021], "dates": []}))[0].metrics_block
    assert "fiscal year ended 2021-01-26" not in plain and "fiscal year ended 2021-01-26" in named
    assert build_blocks(retrieval(metrics=fetched, metric_periods={"years": [], "dates": []}))[0].metrics_block == plain


# ---------------------------------------------------------------------------------- the prompt and its fingerprint

@pytest.mark.parametrize("fragment", [
    # the removed ITEMS list: worded as "no longer appears as a separate risk factor", never as a bare removal
    'listed under "No longer appears as a separate risk factor - showing ..."', "REMOVED / ADDED / REWORDED",
    "parts of its content may be covered inside other risk factors",
    'Never write that the company removed, dropped, deleted, eliminated or withdrew the risk',
    "never write that the risk is gone",
    # the newer items: "no matching risk factor found in the earlier filing", never a bare "new risk"
    "no matching risk factor was found in the earlier filing (it is new, or a restructured older risk factor)",
    'never call one a "new risk"',
    # passages: "wording was not found", never a removal or an addition as a fact
    'the "wording was not found" in that filing', "a differently worded version of the same statement may exist",
    "Passages of surviving", "never say the company dropped",
    "paragraphs", "not as risk factors",                                                     # paragraph units
    "comparison not available",                                                              # a pair that was not compared
    # the block's titles are labels of an automated text comparison, not verdicts
    "AUTOMATED TEXT COMPARISON", "labels for what the comparison found, not proof of what the company did",
    # the unsettled ("Not matched") list: never called removed, said to be unverified
    'A "Not matched (...)" list holds OLDER risk factors that the text check could not settle',
    'Never call one removed, dropped, deleted or gone, not even as "may have been removed"',
    'it "could not be verified" whether the risk factor still appears',
    "none was verified as no longer appearing",
    "Keep them out of every removal statement",
])
def test_the_prompt_carries_the_review_rules(fragment):
    assert fragment in " ".join(ANSWER_PROMPT.split())          # the rules are line-wrapped in the template file


def test_every_heading_the_prompt_quotes_is_the_heading_the_writer_prints():
    """The prompt teaches the model the hedge attached to each list by quoting its heading, and the removal check reads the
    same headings back: a heading changed in one place and not the other would teach the model a list that does not exist."""
    flat = " ".join(ANSWER_PROMPT.split())
    block = passages_context()[0].temporal_block + "\n" + temporal(
        [item("removed", "R", [f"{OLD}:I.1A:0001"]), item("unsettled", "U", [f"{OLD}:I.1A:0002"]),
         item("new", "N", newer=[f"{NEW}:I.1A:0003"])],
        [pair(totals={"removed": 1, "unsettled": 1, "new": 1, "reworded": 0})])[0].temporal_block
    quoted = ("No longer appears as a separate risk factor", NEW_LABEL, "Not matched (",
              "whose wording was not found in the newer filing", "whose wording was not found in the older filing")
    for label in quoted:
        assert label in flat and label in block, label
    # ... and they are the very constants the reader (removal_supported_ids) matches on
    assert REMOVED_ITEMS_PREFIX + "risk factor" == quoted[0]
    assert PASSAGES_REMOVED_PHRASE == quoted[3] and PASSAGES_ADDED_PHRASE == quoted[4]
    assert UNSETTLED_ITEMS_PREFIX == quoted[2]


def test_the_prompt_placeholders_are_unchanged():
    import string

    assert {f for _, f, _, _ in string.Formatter().parse(ANSWER_PROMPT) if f} == {
        "question", "edges_block", "external_block", "metrics_block", "risks_block", "temporal_block", "chunks_block"}


def test_the_template_fingerprint_changes_with_the_prompt_or_any_context_header(monkeypatch):
    base = template_fingerprint()
    assert len(base) == 10 and base == template_fingerprint()
    monkeypatch.setattr(answerer_mod, "ANSWER_PROMPT", ANSWER_PROMPT + " ")
    changed_prompt = template_fingerprint()
    monkeypatch.undo()
    monkeypatch.setattr(answerer_mod, "CONTEXT_HEADERS", (*CONTEXT_HEADERS[:-1], "\n\nEXCERPTS (v2):\n"))
    assert len({base, changed_prompt, template_fingerprint()}) == 3


def test_format_metric_line_is_gone_and_the_unit_rendering_is_covered_through_the_metrics_block():
    """It was exported and tested but used by nothing in production (the METRICS block renders through ``metrics_lines``)."""
    import semigraph.retrieval as package

    assert not hasattr(answerer_mod, "format_metric_line") and not hasattr(package, "format_metric_line")
    assert "format_metric_line" not in package.__all__
    for unit, expected in ((None, "60,922,000,000 USD"), ("", "60,922,000,000 USD"), ("USD", "60,922,000,000 USD"),
                           ("TWD", "60,922,000,000 TWD"), ("EUR", "60,922,000,000 EUR")):
        assert answerer_mod._metric_amount({"value": 60922000000.0, "unit": unit}) == expected


# ---------------------------------------------------------------------------------- M2 on how answers are really written
# (the advisor's review of the first version: a correct answer must not be flagged, a false drop must be)

def claims_of(text):
    _, context, valid = removal_context()
    return checks_of(text, context, valid | {f"{NEW}:I.1A:0500"}).removal_claims


@pytest.mark.parametrize("text", [
    "No risk factors were removed between the two filings.",              # the flagship pair: 0 removed items (plan L.1)
    "None of NVIDIA's risk factors were dropped.",
    "Zero of the 24 risk factors in the FY25 filing were removed.",
    "There were no removed risk factors in the newer filing.",
    f"No risk factor was removed [{KEPT_ITEM}].",
    "Nothing was dropped from the 10-K.",
    "Not a single risk factor was deleted from the filing.",
    "Neither risk factor was eliminated.",
])
def test_a_none_quantified_removal_is_a_statement_of_survival_not_a_removal_claim(text):
    assert claims_of(text) == ()


def test_the_flagship_answer_that_opens_with_no_removals_passes_every_check():
    _, context, valid = removal_context()
    text = (f"No risk factors were removed between the two filings. One new risk factor was added: Sovereign AI "
            f"[{NEW}:I.1A:0500].")
    assert verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context,
                         sources=sources_from_context(context)) == []


def test_no_longer_is_a_removal_verb_even_though_it_contains_no():
    assert len(claims_of(f"The export-control risk no longer appears in the filing [{KEPT_ITEM}].")) == 1
    assert claims_of(f"The NAC sentence no longer appears in the newer Item 1A [{OLD_IDS[0]}].") == ()


def test_a_heading_that_announces_removals_is_judged_with_the_bullets_under_it():
    ok = (f"**Removed risk factors** (at least 21; 8 listed):\n- Hong Kong transition risk [{REMOVED_ITEM}]\n"
          f"- The NAC sentence [{OLD_IDS[0]}]")
    assert claims_of(ok) == ()
    assert claims_of(f"At least 21 risks were removed, of which 8 are listed:\n\n- Hong Kong transition risk [{REMOVED_ITEM}]") == ()


def test_the_false_drop_under_a_bare_heading_is_caught():
    """"**Dropped:**" names no disclosure and its bullets carry no removal verb: it was never checked."""
    bad = f"**Dropped:**\n- export controls [{KEPT_ITEM}]"
    assert claims_of(bad) == ("**Dropped:**",)
    mixed = f"**Removed risk factors:**\n- Hong Kong transition risk [{REMOVED_ITEM}]\n- export controls [{KEPT_ITEM}]"
    assert len(claims_of(mixed)) == 1


def test_a_removal_heading_with_no_bullets_and_no_id_is_flagged_and_one_that_says_none_is_not():
    assert len(claims_of("Removed risk factors:")) == 1
    assert claims_of("**Removed risk factors:**\n- None found.") == ()
    assert claims_of("**Removed:**\n- No risk factor was removed.") == ()


def test_a_heading_that_is_not_about_removal_leaves_its_bullets_to_be_judged_one_by_one():
    assert claims_of(f"**Added risk factors:**\n- Sovereign AI [{NEW}:I.1A:0500]") == ()
    assert len(claims_of(f"**Changes:**\n- Removed: the export-control risk [{KEPT_ITEM}]")) == 1


def test_a_standalone_uncited_count_of_removals_is_flagged_so_the_prompt_puts_it_in_a_heading_with_bullets():
    assert len(claims_of("At least 21 risks were removed, of which 8 are listed.")) == 1


def test_the_prompt_tells_the_model_to_cite_removals_where_it_states_them():
    flat = " ".join(ANSWER_PROMPT.split())
    assert "cite the ids listed there in that sentence or in the bullets directly beneath it" in flat
    assert "state it in its own sentence or bullet" in flat
    assert "keep the statement and its ids in one sentence" in flat                     # the check judges a clause with its ids
    assert 'of which 8 are listed")' not in flat          # the old uncited example sentence
    assert "were removed, of which" not in flat           # the lead-in example no longer states a removal as a fact


def test_a_nested_list_under_a_removal_bullet_is_judged_with_its_children():
    assert len(claims_of(f"- **Removed:**\n  - export controls [{KEPT_ITEM}]")) == 1
    assert claims_of(f"- **Removed:**\n  - Hong Kong transition risk [{REMOVED_ITEM}]") == ()
    assert claims_of(f"- **Added risk factors:**\n  - Sovereign AI [{NEW}:I.1A:0500]") == ()


def test_a_removal_heading_reaches_bullets_after_a_blank_line_but_not_a_paragraph_that_follows_them():
    text = (f"**Removed risk factors:**\n\n- Hong Kong transition risk [{REMOVED_ITEM}]\n\n"
            f"Nvidia dropped its export-control risk factor [{KEPT_ITEM}].")
    assert claims_of(text) == ("Nvidia dropped its export-control risk factor [" + KEPT_ITEM + "].",)


# ---------------------------------------------------------------------------------- the hedged wording and the claim check
# (M1b hedging pass: the model is taught to say "no longer appears as a separate risk factor" and "wording was not found in
# the newer filing"; the check must accept that wording for the ids of the matching removed lists and flag it for anything else)

NEW_ITEM = f"{NEW}:I.1A:0500"
ADDED_PASSAGE = f"{NEW}:I.1A:0350"


def hedged_context():
    """One removed item, one 'Not matched' item, one new item, one removed passage and one added passage."""
    items = [item("removed", "Hong Kong transition risk", [REMOVED_ITEM]), item("unsettled", "Export licensing risk", [UNSETTLED_ITEM]),
             item("new", "Sovereign AI", newer=[NEW_ITEM])]
    p = pair(totals={"removed": 1, "unsettled": 1, "new": 1, "reworded": 0}, passage_totals={"removed": 1, "added": 1, "reworded": 0})
    passages = [passage("removed", NAC, chunk_ids=OLD_IDS), passage("added", "New sentence about licences.", chunk_ids=[ADDED_PASSAGE])]
    return build_blocks(retrieval(temporal=items, temporal_pairs=[p], temporal_passages=passages))


def hedged_claims(text):
    _, context, valid = hedged_context()
    return checks_of(text, context, valid).removal_claims


def test_only_the_removed_items_and_the_removed_passages_support_removal_wording_under_the_new_headings():
    _, context, _ = hedged_context()
    supported = removal_supported_ids(context)
    assert supported == {REMOVED_ITEM, *OLD_IDS}
    assert not {UNSETTLED_ITEM, NEW_ITEM, ADDED_PASSAGE} & supported          # not matched, a new item, an added passage


SUPPORTED_HEDGED_REMOVALS = [
    f"The Hong Kong transition risk no longer appears as a separate risk factor (parts of its content may be covered inside "
    f"other risk factors) [{REMOVED_ITEM}].",
    f"The text check found no matching text for the Hong Kong transition risk in the newer filing [{REMOVED_ITEM}].",
    f"The text check found no matching text for the Hong Kong transition risk [{REMOVED_ITEM}].",
    f"This sentence's wording was not found in the newer filing (a differently worded version of the same statement may exist) "
    f"[{OLD_IDS[0]}] [{OLD_IDS[1]}].",
    f"The wording of the NAC sentence was not found in the newer Item 1A [{OLD_IDS[0]}].",
    f"The wording of the NAC sentence could not be found in the newer filing [{OLD_IDS[0]}].",
    f"The wording of the NAC sentence wasn't found in the filing for the fiscal year ended January 25, 2026 [{OLD_IDS[0]}].",
    f"At least 21 risk factors no longer appear as separate risk factors (parts of their content may be covered inside other "
    f"risk factors), of which 8 are listed:\n\n- Hong Kong transition risk [{REMOVED_ITEM}]",
]


def _as_unsettled(text):
    """The same sentence, citing a 'Not matched' item instead of the removed item / removed passage it was written for."""
    for removed_id in (REMOVED_ITEM, *OLD_IDS):
        text = text.replace(f"[{removed_id}]", f"[{UNSETTLED_ITEM}]")
    return text


@pytest.mark.parametrize("text", SUPPORTED_HEDGED_REMOVALS)
def test_the_hedged_removal_wording_is_supported_when_it_cites_the_matching_removed_lists(text):
    assert hedged_claims(text) == ()
    _, context, valid = hedged_context()
    assert verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context,
                         sources=sources_from_context(context)) == []


@pytest.mark.parametrize("text", SUPPORTED_HEDGED_REMOVALS)
def test_every_supported_hedged_removal_is_flagged_when_it_cites_a_not_matched_id_instead(text):
    """The discriminating twin of the test above: a sentence that only passes because the check never recognised it as a
    removal claim would pass here too (a 'Not matched' item is NOT verified removed, whatever the wording)."""
    twin = _as_unsettled(text)
    assert twin != text and UNSETTLED_ITEM in twin
    assert len(hedged_claims(twin)) == 1, twin


@pytest.mark.parametrize("text", [
    f"The export licensing risk no longer appears as a separate risk factor [{UNSETTLED_ITEM}].",                 # Not matched
    f"The text check found no matching text for the export licensing risk in the newer filing [{UNSETTLED_ITEM}].",
    f"The export licensing risk could not be found in the newer filing [{UNSETTLED_ITEM}].",
    f"The wording of the export licensing risk wasn't found in the newer filing [{UNSETTLED_ITEM}].",
    # a filing named by date or as 'latest': the direction is unknown, so the wording counts as a removal claim (the safe side)
    f"This sentence's wording was not found in the 10-K for the fiscal year ended January 25, 2026 [{UNSETTLED_ITEM}].",
    f"This sentence's wording was not found in Nvidia's latest 10-K [{UNSETTLED_ITEM}].",
    # the hedge itself puts "no matching text" 8 words before "no longer": it must not read as a "no ..." survival statement
    f"The export licensing risk, for which the text check found no matching text, no longer appears as a separate risk "
    f"factor [{UNSETTLED_ITEM}].",
    f"Sovereign AI no longer appears as a separate risk factor [{NEW_ITEM}].",                                      # a NEW item
    f"This sentence's wording was not found in the newer filing [{UNSETTLED_ITEM}].",                             # Not matched
    f"This sentence's wording was not found in the newer filing [{ADDED_PASSAGE}].",                              # an ADDED passage
    f"The risk factor's wording does not appear in the newer filing [{UNSETTLED_ITEM}].",
    "This sentence's wording was not found in the newer filing.",                                                  # no citation
    f"The export licensing risk was not found in the more recent filing [{UNSETTLED_ITEM}].",
    f"At least 21 risk factors no longer appear as separate risk factors (parts of their content may be covered inside other "
    f"risk factors), of which 8 are listed:\n\n- Hong Kong transition risk [{REMOVED_ITEM}]\n- Export licensing [{UNSETTLED_ITEM}]",
])
def test_the_hedged_removal_wording_is_still_flagged_for_any_other_id_or_none(text):
    assert len(hedged_claims(text)) == 1, text
    _, context, valid = hedged_context()
    assert "unsupported_removal_claim" in verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context,
                                                        sources=sources_from_context(context))


@pytest.mark.parametrize("text", [
    f"No matching risk factor was found in the earlier filing for Sovereign AI (it is new, or a restructured older risk "
    f"factor) [{NEW_ITEM}].",
    f"This sentence's wording was not found in the older filing (a differently worded version of the same statement may exist) "
    f"[{ADDED_PASSAGE}].",
    f"The Hong Kong risk no longer appears as a separate risk factor [{REMOVED_ITEM}]; Sovereign AI has no matching risk "
    f"factor in the earlier filing [{NEW_ITEM}].",
    f"The text check could not verify whether the export licensing risk still appears [{UNSETTLED_ITEM}].",
    "The retrieved filings do not contain that figure, so it was not found in the filings.",                          # a refusal
    f"This sentence's wording wasn't found in the earlier filing [{ADDED_PASSAGE}].",
    f"This sentence's wording could not be found in the older filing [{ADDED_PASSAGE}].",
    f"The text check found no matching text in the earlier filing for the Sovereign AI paragraph [{NEW_ITEM}].",
])
def test_the_new_and_added_hedges_and_a_plain_not_found_are_not_removal_claims(text):
    """"not found in the OLDER filing" is the added-passage hedge and "no matching risk factor in the earlier filing" the new-item
    hedge: neither says anything went away, so neither may be flagged."""
    assert hedged_claims(text) == ()


def test_the_example_sentences_the_prompt_quotes_pass_the_check_they_are_meant_to_satisfy():
    """The prompt and the check must not drift apart: the sentences the prompt quotes as good wording are pulled out of the
    prompt text itself and judged against the hedged context."""
    flat = " ".join(ANSWER_PROMPT.split())
    lead_in = re.search(r'for example "(At least \d+ risk factors no longer appear[^"]*?:)"', flat)
    none_found = re.search(r'\("(The text check found no risk factor that no longer appears[^"]*)"\)', flat)
    new_hedge = re.search(r'Say that "(no matching risk factor was found in the earlier filing[^"]*)"', flat)
    assert lead_in and none_found and new_hedge, "the prompt no longer quotes its example sentences"
    assert hedged_claims(f"{lead_in.group(1)}\n\n- Hong Kong transition risk [{REMOVED_ITEM}]") == ()
    assert len(hedged_claims(f"{lead_in.group(1)}\n\n- Export licensing risk [{UNSETTLED_ITEM}]")) == 1
    assert hedged_claims(none_found.group(1) + ".") == ()                                # a survival statement, no ids needed
    assert hedged_claims(f"The Sovereign AI risk factor: {new_hedge.group(1)} [{NEW_ITEM}].") == ()
    # ... and the two hedges named in the prompt's rules, written as sentences of their own
    assert hedged_claims(f'This sentence: "wording was not found" in the newer filing [{OLD_IDS[0]}].') == ()
    assert hedged_claims("Its wording was not found in the older filing (a differently worded version may exist).") == ()


def test_the_new_and_removed_hedges_together_pass_every_check_when_each_cites_its_own_list():
    _, context, valid = hedged_context()
    text = (f"The Hong Kong transition risk no longer appears as a separate risk factor [{REMOVED_ITEM}].\n"
            f"No matching risk factor was found in the earlier filing for Sovereign AI (new, or restructured) [{NEW_ITEM}].\n"
            f"The text check could not verify whether the export licensing risk still appears [{UNSETTLED_ITEM}].")
    assert verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context,
                         sources=sources_from_context(context)) == []
