"""The removal-claim check must not flag what the answer prompt itself tells the model to write: NEGATED and NONE-FOUND
statements ("the text check found no risk factor that no longer appears ...", "not evidence that any risk factor was
dropped") and the LABELS copied from the context ("### No longer appears as a separate risk factor"), while a positive claim
that no removed list supports is still flagged. Pure: no database, no network, no model.

The sentences are the ones the 60-question deployed-path eval flagged wrongly (data/processed/eval_runs.v2-deployed.jsonl,
rows T1 T3 T4 T6 T7 T8 T10 T11 T13 T14 T15). The contexts are built the way tests/test_answerer_review.py builds them.
The last section holds the regression TABLES of two adversarial passes (honest text that must pass, positive claims that must
be flagged, and the documented limits of each direction)."""

import pytest

from semigraph.retrieval.answerer import CITE_RE, build_blocks, sources_from_context
from semigraph.retrieval.context_layout import (
    PASSAGES_PREFIX,
    PASSAGES_REMOVED_PHRASE,
    REMOVED_ITEMS_PREFIX,
    UNSETTLED_ITEMS_PREFIX,
)
from semigraph.retrieval.verify import answer_checks, verify_answer

NVDA = 1045810
OLD, NEW = "0001045810-25-000023", "0001045810-26-000021"
REMOVED_ITEM, KEPT_ITEM, NEW_ITEM = f"{OLD}:I.1A:0300", f"{OLD}:I.1A:0400", f"{NEW}:I.1A:0500"
OLD_IDS = [f"{OLD}:I.1A:0210", f"{OLD}:I.1A:0211"]
ADDED_PASSAGE = f"{NEW}:I.1A:0350"
NAC = "The Notified Advanced Computing, or NAC, process has not resulted in approvals for exports of products to customers in China."


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


def removal_context():
    """One removed item, one reworded item, one new item, one removed passage, one added passage, one reworded passage."""
    items = [item("removed", "Hong Kong transition risk", [REMOVED_ITEM]),
             item("reworded", "New wording", older=[KEPT_ITEM], newer=[f"{NEW}:I.1A:0401"]),
             item("new", "Sovereign AI", newer=[NEW_ITEM])]
    p = pair(totals={"removed": 1, "new": 1, "reworded": 1}, passage_totals={"removed": 1, "added": 1, "reworded": 1})
    passages = [passage("removed", NAC, chunk_ids=OLD_IDS),
                passage("added", "New sentence about licences.", chunk_ids=[ADDED_PASSAGE]),
                passage("reworded", "Old.", chunk_ids=[f"{OLD}:I.1A:0140"], counterpart="New.")]
    return build_blocks(retrieval(temporal=items, temporal_pairs=[p], temporal_passages=passages))


def checks_of(text):
    _, context, valid = removal_context()
    return answer_checks(text, set(CITE_RE.findall(text)), valid, context, sources=sources_from_context(context))


def claims_of(text):
    return checks_of(text).removal_claims


def cited(sentence):
    """The sentence with an id the removed lists do NOT support (a reworded item) cited at its end."""
    return sentence.rstrip(".") + f" [{KEPT_ITEM}]."


# ---------------------------------------------------------------------------------- the eval's wrongly flagged sentences

NEGATED_OR_NONE_FOUND = [
    # T1, T7, T8, T10, T11: "the text check found none", with the list's label quoted
    '- The text check found no risk factor that no longer appears as a separate risk factor: the "No longer appears as a '
    'separate risk factor" list shows none found for this comparison.',
    'The "No longer appears as a separate risk factor" list for this comparison shows none found, meaning the text check '
    'found no risk factor from the fiscal year ended December 28, 2024 filing that no longer appears as a separate risk '
    'factor in the fiscal year ended December 27, 2025 filing.',
    '- The comparison\'s "No longer appears as a separate risk factor" list shows none found, meaning the text check found '
    'no risk factor that no longer appears as a separate risk factor between these two filings.',
    'The text check found no risk factor that no longer appears as a separate risk factor between these two filings.',
    # T1: a passage's wording is not a dropped risk factor
    'A differently worded version of these statements may exist in the newer filing; this is evidence about wording only, '
    'not evidence that any risk factor was dropped.',
    'In summary, the comparison data does not support saying Nvidia stopped disclosing any risk factor between these two 10-Ks.',
    # T4, T6, T8: "the comparison did not verify / identify / show ..."
    'In summary, the text comparison did not verify any risk factor as no longer appearing as a separate risk factor '
    'between these two filings.',
    'In summary, the text comparison did not identify any risk factor as removed or as no longer appearing as a separate '
    'risk factor between these two annual reports; the changes identified were rewordings and passage-level wording '
    'differences within risk factors that remain disclosed.',
    'In short: the evidence does not show any risk factor being removed between these two annual reports; instead, the '
    'comparison found rewording of existing risk factors.',
    '- **No risk factors were found to no longer appear as a separate risk factor.** The text check found none in this category.',
    # T7: a refusal that names the claim it cannot make
    'The context does not contain a risk-factor comparison specifically between the FY2024 and FY2025 annual reports, so I '
    'cannot say whether Nvidia removed any risk factors between those two filings.',
    # T11, T13, T15: "this does not mean ... was removed"
    'This means a differently worded version of that statement may exist in the newer filing; it does not mean the risk '
    'factor or that statement was removed.',
    'This means a differently worded version of the same statement may exist in the newer filing; it does not mean the '
    'company removed, deleted, or stopped disclosing the underlying risk.',
    'This should not be read as the company having "removed" or "dropped" the underlying risk factor, only that this '
    'particular sentence\'s exact wording could not be matched.',
    # T14: a label quoted with the sentence's own comma inside the quotation marks, then "not removed"
    '- This sentence appears in the list of "Passages of surviving risk factors whose wording was not found in the newer '
    'filing," meaning the risk factor itself ("We are subject to complex laws, rules, regulations, and political and other '
    'actions, including restrictions on the export of our products, which may adversely impact our business") is still '
    'disclosed and was only reworded, not removed.',
]


@pytest.mark.parametrize("text", NEGATED_OR_NONE_FOUND)
def test_the_negated_and_none_found_statements_the_prompt_asks_for_are_not_removal_claims(text):
    assert claims_of(text) == (), text
    assert claims_of(cited(text)) == (), cited(text)      # ... whatever id follows: a negation claims no removal to support


def test_an_answer_built_from_those_statements_passes_every_check():
    text = ("Based on the automated text comparison, the answer is no.\n\n"
            "### New risk factor\n"
            f"Sovereign AI is new, or a restructured older risk factor [{NEW_ITEM}].\n\n"
            "### No longer appears as a separate risk factor\n"
            "The text check found none: no risk factor was identified as no longer appearing as a separate risk factor.\n\n"
            "### Passages whose wording was not found in the newer filing\n"
            "The text check found none for this comparison; this is evidence about wording only, not evidence that any "
            "risk factor was dropped.\n\n"
            "In summary, the comparison data does not support saying Nvidia stopped disclosing any risk factor.")
    _, context, valid = removal_context()
    assert verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context,
                         sources=sources_from_context(context)) == []


# ---------------------------------------------------------------------------------- (c) negation scope

# ten neutral words, none of them a negator, a scope breaker, "and" or a removal wording
_FILLER = "say whether the older wording plainly shows that the newer".split()


@pytest.mark.parametrize("words, flagged", [(0, False), (2, False), (8, False), (9, True)])
def test_a_negator_cancels_a_removal_verb_up_to_eight_words_ahead_of_it(words, flagged):
    text = f"Nvidia did not {' '.join(_FILLER[:words])} removed the risk factor [{KEPT_ITEM}]"
    assert bool(claims_of(text)) is flagged, text


@pytest.mark.parametrize("negation", [
    "The risk factor wasn't removed", "The risk factor wasn’t removed", "Nvidia didn't drop the risk factor",
    "Nvidia never announced that the risk factor had been removed",
    "Nor was any risk factor ever formally dropped from the 10-K",
    "Nvidia reworded the risk factor without ever fully removing it",
    "The comparison cannot confirm that any risk factor was removed",
    "The risk factor was reworded rather than actually deleted",
    "Neither the older nor the newer wording shows the risk factor as removed",
])
def test_every_negator_form_cancels_the_removal_verb(negation):
    assert claims_of(f"{negation} [{KEPT_ITEM}].") == (), negation


@pytest.mark.parametrize("breaker", ["but", "yet", "however", "although", "whereas", "while"])
def test_a_contrast_word_between_the_negator_and_the_verb_breaks_the_negation(breaker):
    text = f"Nvidia did not add new risks {breaker} the indebtedness risk factor was removed [{KEPT_ITEM}]"
    assert len(claims_of(text)) == 1, text


@pytest.mark.parametrize("text", [
    f"Nvidia did not add new risk factors and removed the indebtedness risk factor [{KEPT_ITEM}]",           # "and" is last
    f"Nvidia did not add new risk factors and also removed the indebtedness risk factor [{KEPT_ITEM}]",     # ... second to last
    f"Nvidia did not add new risk factors, and removed the indebtedness risk factor [{KEPT_ITEM}]",
    "Nvidia did not add new risk factors and removed the indebtedness risk factor.",
])
def test_and_right_before_the_verb_starts_a_new_claim_that_the_negation_does_not_cover(text):
    assert len(claims_of(text)) == 1, text


def test_and_earlier_in_the_scope_does_not_break_it():
    assert claims_of(f"The text check did not show the older and newer filings as removed [{KEPT_ITEM}].") == ()


@pytest.mark.parametrize("text", [
    f"Nvidia did not add new risks: it removed the export risk factor [{KEPT_ITEM}]",
    f"Nvidia did not change the wording - the export risk factor was removed [{KEPT_ITEM}]",
    f"Nvidia did not add risks—the export risk factor was removed [{KEPT_ITEM}]",
])
def test_a_colon_or_a_dash_starts_a_new_statement_the_negation_does_not_reach(text):
    assert len(claims_of(text)) == 1, text


@pytest.mark.parametrize("text", [
    # the hedge's subject noun ("risks", "risk factors") is where the match opens: the negation before it ended at the colon / "but"
    "Passages of surviving risk factors show wording differences without indicating removal of the underlying risks: 7 of 7 "
    "passages had wording not found in the newer filing.",
    f"Nvidia did not change risk factors but the wording was not found in the newer filing [{KEPT_ITEM}]",
])
def test_a_hedge_that_opens_with_its_subject_is_not_shielded_by_a_negation_that_ended_before_it(text):
    assert len(claims_of(text)) == 1, text


def test_a_negated_hedge_is_still_not_a_claim():
    assert claims_of(f"Nvidia did not say that the wording was not found in the newer filing [{KEPT_ITEM}].") == ()


# ---------------------------------------------------------------------------------- (d) "no / none ... that / which / whose"

@pytest.mark.parametrize("text", [
    "The text check found no risk factor from the fiscal year ended December 28, 2024 filing that was dropped in the newer filing.",
    "The list shows none of the older risk factors from the fiscal year ended December 28, 2024 filing that were removed.",
    "The text check found no older risk factor from the fiscal year ended December 28, 2024 filing which was eliminated.",
    "The text check found no risk factor from the fiscal year ended December 28, 2024 filing whose disclosure was deleted.",
])
def test_a_none_quantifier_followed_by_a_relative_pronoun_cancels_the_verb_at_any_distance(text):
    assert claims_of(text) == (), text
    assert claims_of(cited(text)) == (), text


def test_a_contrast_word_after_the_quantifier_breaks_that_reach_too():
    text = (f"Nvidia found no new risk factors in the older filing but the export risk factor that Nvidia listed was "
            f"removed [{KEPT_ITEM}]")
    assert len(claims_of(text)) == 1


def test_the_hedge_of_a_removed_item_is_not_a_none_quantifier():
    """"no matching text" is the check's finding for the item, not "no item ... that ..." (existing behaviour, kept)."""
    text = (f"The export licensing risk, for which the text check found no matching text, no longer appears as a separate "
            f"risk factor [{KEPT_ITEM}].")
    assert len(claims_of(text)) == 1


# ---------------------------------------------------------------------------------- positive claims are still flagged

@pytest.mark.parametrize("text", [
    f"Nvidia removed the indebtedness risk factor [{KEPT_ITEM}]",
    "Nvidia removed the indebtedness risk factor.",
    f"Nvidia did not add new risk factors and removed the indebtedness risk factor [{KEPT_ITEM}]",
    "Nvidia did not add new risk factors and removed the indebtedness risk factor.",
    f"Nvidia did not reword the export risk factor but dropped the indebtedness risk factor [{KEPT_ITEM}]",
    f"It is not certain, yet the risk factor was removed [{KEPT_ITEM}]",
    f"It is not certain yet the risk factor was removed [{KEPT_ITEM}]",
    "The NAC sentence was not found in the newer filing.",
    "At least 25 passages of surviving risk factors had wording not found in the newer filing (8 shown as examples).",
    # a negator too far ahead of the verb
    f"Nvidia did not mention anything about the older filing during the call after the indebtedness risk factor was dropped [{KEPT_ITEM}]",
])
def test_a_positive_claim_that_no_removed_list_supports_is_still_flagged(text):
    assert len(claims_of(text)) == 1, text
    _, context, valid = removal_context()
    assert "unsupported_removal_claim" in verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=context,
                                                        sources=sources_from_context(context))


def test_a_positive_claim_that_cites_the_removed_list_is_still_supported():
    assert claims_of(f"Nvidia removed the Hong Kong transition risk factor [{REMOVED_ITEM}].") == ()
    assert claims_of(f"Nvidia did not add risks and removed the Hong Kong transition risk [{REMOVED_ITEM}].") == ()


# ---------------------------------------------------------------------------------- (e) label echoes

_LABELS = [
    "No longer appears as a separate risk factor",
    "No longer appears as a separate paragraph",
    "No longer appears as a separate risk factor or paragraph",
    "Passages of surviving risk factors whose wording was not found in the newer filing",
    "Passages of surviving paragraphs whose wording was not found in the newer filing",
    "Passages of surviving risk factors and paragraphs whose wording was not found in the newer filing",
    "Passages whose wording was not found in the newer filing",
    "Not matched",
]
_PROSE = "The list shows none found for this comparison."


def test_the_labels_written_out_here_are_the_ones_the_context_builds():
    assert REMOVED_ITEMS_PREFIX + "risk factor" == _LABELS[0] and REMOVED_ITEMS_PREFIX + "paragraph" == _LABELS[1]
    assert f"{PASSAGES_PREFIX}risk factors {PASSAGES_REMOVED_PHRASE}" == _LABELS[3]
    assert f"{PASSAGES_PREFIX}paragraphs {PASSAGES_REMOVED_PHRASE}" == _LABELS[4]
    assert UNSETTLED_ITEMS_PREFIX.startswith(_LABELS[7])


@pytest.mark.parametrize("label", _LABELS)
@pytest.mark.parametrize("form", [
    "### {}", "## {}", "###### {}", "### {}:", "### {} - none found.", "**{}**", "**{}:**", "**{}**:", "**{}:** none found.",
    "- **{}:** none found.", "  ### {}  ",
])
def test_a_label_copied_as_a_heading_and_followed_only_by_prose_is_not_a_removal_claim(label, form):
    assert claims_of(f"{form.format(label)}\n{_PROSE}") == ()
    assert claims_of(form.format(label)) == ()


@pytest.mark.parametrize("label", _LABELS)
@pytest.mark.parametrize("quotes", ['""', "“”", "''", "‘’", '“"'])
def test_a_label_quoted_inside_a_sentence_is_not_a_removal_claim(label, quotes):
    open_, close = quotes
    assert claims_of(f"The {open_}{label}{close} list shows none found for this comparison.") == ()
    assert claims_of(f"See the {open_}{label}{close} list.") == ()
    # naming an item under a REMOVED list is a claim that it was removed: it needs the id of a removed item or passage (the
    # "Not matched" list is no removal list). Until 2026-09-27 an uncited one passed: the tie-break of the task is to flag it.
    sentence = f"This sentence appears in the list of {open_}{label},{close} which the check printed"
    if label == _LABELS[7]:
        assert claims_of(f"{sentence}.") == ()
    else:
        assert len(claims_of(f"{sentence}.")) == 1 and len(claims_of(f"{sentence} [{KEPT_ITEM}].")) == 1
        assert claims_of(f"{sentence} [{OLD_IDS[0]}].") == ()


def test_a_label_echo_never_hides_a_claim_made_next_to_it():
    label = _LABELS[0]
    text = f'The Hong Kong risk was dropped [{KEPT_ITEM}]; see the "{label}" list.'
    assert len(claims_of(text)) == 1
    assert len(claims_of(f'See the "{label}" list: Nvidia dropped the export risk factor [{KEPT_ITEM}].')) == 1


def test_a_quoted_claim_in_lower_case_is_a_claim_not_a_label():
    """The prompt teaches the model to SAY a risk factor "no longer appears as a separate risk factor": that wording, cited
    for one risk, is the claim itself and needs the removed list's ids (only the capitalised heading is a label)."""
    assert len(claims_of(f'The Hong Kong risk "no longer appears as a separate risk factor" [{KEPT_ITEM}].')) == 1
    assert claims_of(f'The Hong Kong risk "no longer appears as a separate risk factor" [{REMOVED_ITEM}].') == ()


def test_text_after_the_label_inside_the_quotation_marks_is_not_swallowed():
    text = f'"No longer appears as a separate risk factor: the Hong Kong risk was dropped [{KEPT_ITEM}]"'
    assert len(claims_of(text)) == 1


def test_a_label_heading_with_a_paragraph_of_its_own_claim_below_it_still_flags_the_claim():
    text = f"### {_LABELS[0]}\nNvidia dropped the export risk factor [{KEPT_ITEM}]."
    assert len(claims_of(text)) == 1


# ---------------------------------------------------------------------------------- headings WITH bullets stay judged with their bullets

def test_a_removed_heading_echoed_with_bullets_is_judged_by_the_ids_of_its_bullets():
    heading = f"### {REMOVED_ITEMS_PREFIX}risk factor"
    assert len(claims_of(f"{heading}\n- Hong Kong risk [{KEPT_ITEM}]")) == 1                  # a reworded item: flagged
    assert claims_of(f"{heading}\n- Hong Kong risk [{REMOVED_ITEM}]") == ()                    # the removed item: supported
    assert claims_of(f"{heading}\n- None found.") == ()
    assert len(claims_of(f"{heading}\n- Hong Kong risk [{REMOVED_ITEM}]\n- Export risk [{KEPT_ITEM}]")) == 1


def test_a_bold_removed_heading_echoed_with_bullets_is_judged_the_same_way():
    heading = f"**{REMOVED_ITEMS_PREFIX}risk factor:**"
    assert len(claims_of(f"{heading}\n- Hong Kong risk [{KEPT_ITEM}]")) == 1
    assert claims_of(f"{heading}\n- Hong Kong risk [{REMOVED_ITEM}]") == ()


def test_a_removed_passages_heading_echoed_with_bullets_needs_the_removed_passage_ids():
    heading = f"### {PASSAGES_PREFIX}risk factors {PASSAGES_REMOVED_PHRASE}"
    assert claims_of(f"{heading}\n- the NAC sentence [{OLD_IDS[0]}]") == ()
    assert len(claims_of(f"{heading}\n- a sentence [{ADDED_PASSAGE}]")) == 1
    assert len(claims_of(f"{heading}\n- a sentence [{KEPT_ITEM}]")) == 1


def test_an_added_passages_heading_is_no_removal_heading_at_all():
    heading = "### Passages whose wording was not found in the older filing"
    assert claims_of(f"{heading}\n- a new sentence [{ADDED_PASSAGE}]") == ()


# ================================================================================================= adversarial tables
# Regression tables from two independent adversarial passes over ``verify.py`` (the eval's sentences above are the seed).
#
# ``PASS_CASES``  honest text the check must NOT flag (the answer prompt itself asks the model to write most of it).
# ``FLAG_CASES``  positive removal claims the check MUST flag: each cites an id no removed list supports, or cites nothing.
# ``LIMIT_FP``    honest text the check still flags: a documented, pinned limitation (the reason is in the family name).
# ``LIMIT_FN``    positive claims the check still lets through: a documented, pinned limitation.
#
# Every case is ``(family, context, text)``. ``context`` is one of ``removal`` (the context above), ``none`` (a comparison whose
# removed lists are empty), ``unsettled`` (one 'Not matched' item) and ``plain`` (no temporal block at all). The tables are data
# only, so a scratch harness can import them to compare verifier versions. This file is over the 800-line soft ceiling because of
# them: test data.

IDS = {"K": KEPT_ITEM, "R": REMOVED_ITEM, "P": OLD_IDS[0], "U": f"{OLD}:I.1A:0600", "N": NEW_ITEM, "NEWREW": f"{NEW}:I.1A:0401",
       "ADDED": ADDED_PASSAGE, "REW": f"{OLD}:I.1A:0140", "REW2": f"{NEW}:I.1A:0141", "RISK": f"{NEW}:I.1A:0354"}


def t(text):
    """The text with ``{K}`` (a reworded item id no removed list supports), ``{R}`` (the removed item), ``{P}`` (a removed passage)
    ... replaced by the ids of the test contexts."""
    return text.format(**IDS)


def none_context():
    return build_blocks(retrieval(temporal_pairs=[pair()]))


def unsettled_context():
    items = [item("removed", "Hong Kong transition risk", [REMOVED_ITEM]), item("unsettled", "Export licensing risk", [IDS["U"]])]
    totals = {"removed": 1, "unsettled": 1, "new": 0, "reworded": 0}
    return build_blocks(retrieval(temporal=items, temporal_pairs=[pair(totals=totals)]))


def plain_context():
    return None, f"ACTIVE RISKS:\n- Export controls risk [{IDS['RISK']}]", {IDS["RISK"]}


CONTEXTS = {"removal": removal_context, "none": none_context, "unsettled": unsettled_context, "plain": plain_context}


def claims(context, text):
    _, ctx, valid = CONTEXTS[context]()
    sources = sources_from_context(ctx) if context != "plain" else {}
    return answer_checks(text, set(CITE_RE.findall(text)), valid, ctx, sources=sources).removal_claims


# ============================================================================================ honest text: must not be flagged
_LABEL = "No longer appears as a separate risk factor"
_NONE_FOUND = "The list shows none found."
PASS_CASES = [(family, context, t(text)) for family, context, texts in [
    # --- the context's own labels copied as plain lines, bullets, bold, italics, tables, numbered headings, setext headings ---
    ("label-plain-none", "none", [
        f"{_LABEL} - none found.", f"- {_LABEL} - none found.", f"{_LABEL}: none found.",
        f"**{_LABEL}:** The text check found none.", f"**{_LABEL}:** None.",
        f"- **{_LABEL}:** none found for this comparison.", f"**{_LABEL}:** none listed for this comparison.",
        f"**{_LABEL}:** 0 risk factors.", f"- **{_LABEL}:** The text check found none in this comparison.",
        f"*{_LABEL}:* none found.", f"1. **{_LABEL}:** none found.",
        f"Under the heading {_LABEL}, the comparison lists none.",
        f"The `{_LABEL}` list shows none found for this comparison.",
        f"The {_LABEL} list shows none found for this comparison.",
        f'The "{_LABEL} - showing 0 of 0 risk factors" line lists nothing.',
        f"| {_LABEL} | None found |",
        f"| Category | Result |\n|---|---|\n| {_LABEL} | None found |\n| Passages whose wording was not found in the newer "
        f"filing | None found |"]),
    ("label-heading-none", "none", [
        f"### {_LABEL} (none found)\nThe text check found no risk factor that no longer appears as a separate risk factor.",
        f"### {_LABEL} (none found)", f"### {_LABEL}: none", f"### {_LABEL}: None", f"### {_LABEL} — none",
        "### Passages whose wording was not found in the newer filing (none)",
        f"### {_LABEL}\n- The text check found none for this comparison.",
        f"### **{_LABEL}**\n{_NONE_FOUND}", f"### 2. {_LABEL}\n{_NONE_FOUND}", f"*{_LABEL}*\n{_NONE_FOUND}",
        f"__{_LABEL}__\n{_NONE_FOUND}", f"### {_LABEL.lower()}\n{_NONE_FOUND}", f"**{_LABEL.lower()}:**\n{_NONE_FOUND}",
        f"{_LABEL}\n---\n{_NONE_FOUND}",
        "## Risk factors removed / added / reworded (text-verified comparison)\nThe text check found no risk factor that no "
        "longer appears as a separate risk factor."]),
    ("removed-list-named-empty", "none", [
        "The REMOVED / ADDED / REWORDED block for these two filings shows no risk factor that no longer appears as a separate "
        "risk factor.", "The REMOVED list of risk factors for this comparison shows none found.",
        "The removed risk factors list is empty for this comparison.",
        "The comparison lists 0 risk factors that no longer appear as a separate risk factor.",
        "Risk factors that no longer appear as a separate risk factor: 0 of 0.",
        "The list of risk factors that no longer appear as a separate risk factor is empty for this comparison.",
        "Risk factors no longer appearing as a separate risk factor: none found."]),
    ("label-heading-supported-items", "removal", [
        f"### {_LABEL} - showing 1 of 1 risk factors\nThe Hong Kong transition risk no longer appears as a separate risk factor "
        f"[{{R}}].",
        f"### {_LABEL} (the text check found no matching text in the newer filing)\nOne risk factor is listed: the Hong Kong "
        f"transition risk [{{R}}].",
        f"### {_LABEL}\nThe Hong Kong transition risk [{{R}}].",
        f"### {_LABEL}\nThe following:\n- The Hong Kong transition risk [{{R}}]",
        "### Passages whose wording was not found in the newer filing\nThe NAC sentence [{P}]."]),
    ("section-title-echo", "removal", [
        "### Risk factors removed / added / reworded\n- Sovereign AI: no matching risk factor was found in the earlier filing "
        "(it is new, or a restructured older risk factor) [{N}]\n- New wording: reworded, still disclosed [{K}] [{NEWREW}]"]),
    ("not-matched-heading-copy", "unsettled", [
        "### Not matched (the text check could not verify whether these older risk factors still appear; they may have been "
        "removed or absorbed into another risk factor) - showing 1 of 1:\n- Hong Kong transition risk: it could not be verified "
        "whether it still appears [{U}]"]),
    # --- "the check cannot tell which": inability verb + whether / that, however long the clause (answer.txt: "say that the check
    #     cannot tell which and stop there") ---
    ("cannot-tell-whether", "removal", [
        "The check cannot tell whether the statement is still disclosed in other words or was removed.",
        "The automated comparison cannot tell whether NVIDIA still makes this statement in different words or whether it was "
        "dropped.", "The text check cannot tell whether this sentence was reworded somewhere else or removed.",
        "Nor can one conclude from the text match alone that NVIDIA deleted the statement from its 10-K."]),
    ("could-not-verify-whether", "unsettled", [
        "It could not be verified whether the Hong Kong transition risk factor from the older filing was removed [{U}].",
        "The text check was unable to confirm whether the Hong Kong risk factor was removed [{U}].",
        "It is impossible to say whether the Hong Kong risk factor was removed [{U}].",
        "The text check failed to settle whether the Hong Kong risk factor was removed or absorbed into another risk factor [{U}].",
        "It is unclear whether the Hong Kong transition risk factor was removed; it could not be verified whether it still "
        "appears [{U}].", "The comparison has not yet shown that the Hong Kong risk factor was removed [{U}]."]),
    # --- "did not verify / identify any risk factor ... as removed" with the fiscal-year dates the prompt requires ---
    ("negated-as-removed-with-dates", "none", [
        "The text comparison did not identify any of the risk factors in the fiscal year ended January 26, 2025 10-K as removed.",
        "The text comparison did not verify any risk factor from the fiscal year ended January 26, 2025 10-K as no longer "
        "appearing as a separate risk factor."]),
    ("none-subject-with-dates", "none", [
        "No risk factor from the fiscal year ended January 26, 2025 10-K was found to no longer appear as a separate risk factor "
        "in the fiscal year ended January 25, 2026 10-K.",
        "None of the risk factors in Nvidia's 10-K for the fiscal year ended January 26, 2025 was identified by the automated "
        "check as no longer appearing as a separate risk factor.",
        "No risk factor in Nvidia's older 10-K was found by the automated text comparison to have been dropped."]),
    ("parenthetical-however", "unsettled", [
        "The text check could not, however, verify whether these older risk factors were removed [{U}].",
        "The comparison does not, however, show that the Hong Kong risk factor was removed [{U}]."]),
    ("negated-evidence-verb-with-a-long-that-clause", "removal", [
        "The comparison does not indicate that any of Nvidia's older export-control risk factors were removed [{K}].",
        "The comparison does not show that any risk factor from the fiscal year ended January 26, 2025 10-K was dropped [{K}]."]),
    ("parenthetical-dashes", "none", [
        "This is not evidence — only a wording difference — that any risk factor was dropped.",
        "The comparison does not show — for any risk factor — that it no longer appears as a separate risk factor.",
        "Nvidia did not, according to the comparison, drop any risk factor.",
        "The text check does not say whether, between these two 10-Ks, any risk factor was removed."]),
    ("instead-of-is-a-negator", "removal", [
        "Instead of being removed, the export risk factor was reworded [{K}].",
        "The export risk factor was reworded as opposed to removed [{K}]."]),
    ("year-range-en-dash", "removal", [
        "It is not true that in the 2024–2025 comparison Nvidia dropped the export risk factor [{K}].",
        "The comparison does not show that the 2024–2025 risk factor was removed [{K}].",
        "The check did not, for FY2025–FY2026, show a risk factor as removed [{K}]."]),
    ("apostrophe-variants", "removal", ["The risk factor wasnʼt removed [{K}].", "The risk factor wasn‘t removed [{K}]."]),
    # sentences of real answers of the deployed eval that a first version of this change flagged (HEAD passed them)
    ("real-answers-none-quantifier", "removal", [
        "instead it shows rewording of existing risk factors and changes in supporting sentence-level wording, with no risk "
        "factors verified as no longer appearing separately [{K}].",
        "- No data on which risk factors were added, reworded, or dropped between FY2023, FY2025 [{K}]",
        "- I cannot answer the question as posed because the excerpts lack risk factor content: no KNOWN RELATIONSHIPS, REPORTED "
        "METRICS, DISCLOSED RISKS, or DROPPED RISK LINEAGES.",
        "The dropped risk factors are all empty for this comparison."]),
    ("continuing-clause-about-something-else", "removal", [
        "The export risk factor was reworded, and shares were removed from the index [{K}].",
        "The risk factor is disclosed, but this is a dropped or historical lineage rather than a current relationship [{K}]."]),
    ("negated-indefinite-relative-clause", "removal", [
        "The check did not identify any risk factor that was removed [{K}].",
        "The check cannot confirm that any risk factor was removed [{K}].",
        "The comparison found no risk factor that was removed [{K}]."]),
    ("coordinated-removal-verbs-under-one-negation", "removal", [
        "This does not mean the statement was removed and is no longer disclosed.",
        "It does not mean the company removed, deleted, or stopped disclosing the underlying risk [{K}]."]),
    # --- the hedge and its id in ONE sentence (answer.txt: "keep the statement and its ids in one sentence (no semicolon
    #     between them)"): a comma-connective must not strand the id ---
    ("hedge-and-id-in-one-sentence", "removal", [
        "The Hong Kong transition risk factor no longer appears as a separate risk factor, and parts of its content may be "
        "covered inside other risk factors [{R}].",
        "The Hong Kong transition risk factor no longer appears as a separate risk factor, but parts of its content may be "
        "covered inside other risk factors [{R}].",
        "The Hong Kong transition risk factor no longer appears as a separate risk factor, which means parts of its content may "
        "be covered inside other risk factors [{R}].",
        "The NAC sentence's wording was not found in the newer filing, but a differently worded version of the same statement may "
        "exist [{P}].",
        "The NAC sentence's wording \"was not found\" in the newer filing, and \"a differently worded version of the same "
        "statement may exist\" [{P}].",
        "The NAC sentence's wording was not found in the newer filing, so the check cannot tell whether it was reworded or "
        "removed [{P}].",
        "In short: the risk factor itself was not removed and still appears (reworded) in the fiscal year ended January 25, 2026 "
        "10-K [{NEWREW}], but the specific NAC sentence's exact wording was not found in that newer filing, so a differently "
        "worded version of the same statement may exist [{P}].",
        "Per the automated text comparison, this means that wording \"was not found\" in the fiscal year ended January 25, 2026 "
        "10-K, and a differently worded version of the same statement may exist [{P}]."]),
    ("quoted-filing-sentence-with-commas", "removal", [
        "- One sentence from within this risk factor in the earlier filing — the capital-expenditure estimate for 2025 — had its "
        "wording not found in the newer filing: \"We estimate capital expenditures in 2025 for property, plant, and equipment, "
        "net of proceeds from government incentives, to be around mid-30% range of revenue for the year\" [{P}]. This means a "
        "differently worded version of that statement may exist in the newer filing; it does not mean the risk factor or that "
        "statement was removed."]),
    ("quoted-label-none-or-uncited", "none", [
        f'The "{_LABEL}" list shows none found for this comparison.',
        f'The "{_LABEL}" list shows none found for this comparison [{{K}}].',
        f'See the "{_LABEL}" list.']),
] for text in texts]

# ==================================================================================== positive claims: must be flagged
FLAG_CASES = [(family, context, t(text)) for family, context, texts in [
    # --- rule (c): "and" anywhere between the negator and the verb starts a claim of its own ---
    ("and-starts-a-new-claim", "removal", [
        "Nvidia did not add risks and marked it as removed [{K}].",
        "The check cannot tell whether it was reworded and the indebtedness risk factor was removed [{K}].",
        "It did not disclose revenue and, in the 10-K, removed the indebtedness risk factor [{K}].",
        "Nvidia did not add new risk factors and the newer filing removed the indebtedness risk factor [{K}].",
        "Nvidia did not add new risk factors and the newer filing removed the indebtedness risk factor.",
        "Nvidia did not add new risk factors and also quietly removed the indebtedness risk factor [{K}].",
        "Nvidia did not add new risk factors and, most notably, removed the indebtedness risk factor [{K}].",
        "Nvidia did not add new risk factors and it also removed the indebtedness risk factor [{K}].",
        "Nvidia never discussed the debt and so it removed the indebtedness risk factor [{K}].",
        "Nvidia did not add new risk factors and then quietly removed the indebtedness risk factor [{K}].",
        "Nvidia didn't change the export risk and the company deleted the indebtedness risk factor [{K}].",
        "Nvidia did not add risks and the export risk was removed [{K}].",
        "Nvidia did not add risks and the export risk was removed.",
        "There are no new risk factors in the newer 10-K that Nvidia added this year and in the same filing Nvidia removed the "
        "indebtedness risk factor [{K}].",
        "Nvidia added no new risk factors and removed the indebtedness risk factor [{K}].",
        "Nvidia added no new risk factors and removed the indebtedness risk factor.",
        "The text check found no new risk factors in the older filing and the comparison shows that Nvidia removed the "
        "indebtedness risk factor [{K}]."]),
    # --- a comma, a parenthesis or a fronted phrase ends a negation: "Not surprisingly, X removed Y" is a positive claim ---
    ("fronted-or-comma-ended-negation", "removal", [
        "Not surprisingly, Nvidia removed the indebtedness risk factor [{K}].",
        "Not surprisingly, Nvidia removed the indebtedness risk factor.",
        "Not least, Nvidia removed the indebtedness risk factor from the 10-K [{K}].",
        "Not only was it reworded, the risk factor was removed [{K}].",
        "Not only was the export risk reworded, Nvidia also removed the indebtedness risk factor [{K}].",
        "Without explanation, Nvidia removed the indebtedness risk factor [{K}].",
        "Without explanation, Nvidia removed the indebtedness risk factor.",
        "Without much fanfare, the newer 10-K dropped the indebtedness risk factor.",
        "Without further explanation, Nvidia removed the indebtedness risk factor [{K}].",
        "Nvidia, without saying why, removed the indebtedness risk factor [{K}].",
        "Rather than rewording it, Nvidia removed the indebtedness risk factor [{K}].",
        "Rather than rewording it, Nvidia removed the indebtedness risk factor.",
        "Nvidia, rather than keeping the indebtedness risk factor, dropped it [{K}].",
        "Not mentioned in the 10-K, the risk factor was removed [{K}].",
        "Not waiting for the 2026 filing, Nvidia deleted the indebtedness risk factor [{K}].",
        "Whether or not investors noticed, Nvidia removed the export risk factor [{K}].",
        "Nvidia, which did not comment, removed the export risk factor [{K}].",
        "Nvidia, which has not explained the change, eliminated the export risk factor [{K}].",
        "Nvidia (which did not comment) removed the export risk factor [{K}].",
        "Unlike the Hong Kong risk, which was not removed, the indebtedness risk factor was dropped [{K}].",
        "The Hong Kong risk was not removed, the indebtedness risk factor was dropped [{K}].",
        "The indebtedness risk factor was not kept, it was removed [{K}].",
        "The indebtedness risk factor wasn't reworded, it was deleted [{K}].",
        "Nvidia did not add risks, it only removed the indebtedness risk factor [{K}].",
        "Nvidia did not add risks, only removed the indebtedness risk factor [{K}].",
        "Nvidia did not disclose why, it removed the indebtedness risk factor [{K}].",
        "Nvidia did not just reword the risk factor, it removed the indebtedness risk entirely [{K}].",
        "Nvidia did not add risks (it removed the indebtedness risk factor) [{K}].",
        "**Not reworded, removed:** the indebtedness risk factor [{K}]",
        "- Indebtedness risk factor — not reworded, removed [{K}]",
        "In the Not matched list the export risk may have been removed [{K}]."]),
    # a fronted "without / rather than / instead of" phrase reaches two words: with no comma after it the next words are
    # the subject of a new claim
    ("however-after-a-complete-object-is-a-contrast", "removal", [
        "Nvidia did not add risks, however, removed the indebtedness risk factor [{K}].",
        "Nvidia did not add risks, however, the indebtedness risk factor was removed [{K}]."]),
    ("fronted-phrase-without-a-comma", "removal", [
        "Without much fanfare the indebtedness risk factor was removed [{K}].",
        "Without much fanfare Nvidia removed the indebtedness risk factor [{K}].",
        "Rather than rewording it the indebtedness risk factor was removed [{K}].",
        "Instead of rewording it the indebtedness risk factor was removed [{K}]."]),
    ("dash-ends-a-negation", "removal", [
        "Nvidia did not add risks--it removed the indebtedness risk factor [{K}].",
        "Nvidia did not add risks -- it removed the indebtedness risk factor [{K}].",
        "Nvidia did not add new risks: it removed the export risk factor [{K}]."]),
    # --- a full stop ends a negation whatever follows it (a bracket, a quotation mark, a lower-case word) ---
    ("negation-does-not-cross-a-sentence", "removal", [
        "(Nvidia did not add new risks.) The indebtedness risk factor was removed [{K}].",
        "Nvidia did not add new risks. ‘Indebtedness’ was removed as a risk factor [{K}].",
        "Nvidia did not add risks. the indebtedness risk factor was removed [{K}]."]),
    # --- the removal is presupposed or asserted after a connective, or the negation is about something else ---
    ("negated-verb-of-another-clause", "removal", [
        "Although Nvidia did not say so, the indebtedness risk factor was removed [{K}].",
        "Although Nvidia did not say so, the indebtedness risk factor was removed.",
        "Nvidia never explained why it removed the indebtedness risk factor [{K}].",
        "Nvidia did not explain why it removed the indebtedness risk factor [{K}].",
        "Nvidia did not explain why it dropped the indebtedness risk factor [{K}].",
        "Nvidia did not explain why it dropped the indebtedness risk factor.",
        "The filing does not say why the indebtedness risk factor was removed [{K}].",
        "It is not clear why the indebtedness risk factor was removed [{K}].",
        "Nvidia did not replace the indebtedness risk factor after removing it [{K}].",
        "The newer 10-K doesn't have the indebtedness risk factor anymore because Nvidia removed it [{K}].",
        "The newer 10-K does not contain the indebtedness risk factor because Nvidia removed it [{K}].",
        "The 10-K does not mention Russia because Nvidia dropped the Russia risk factor [{K}].",
        "The 10-K does not mention Russia because Nvidia dropped the Russia risk factor.",
        "The newer 10-K doesn't cover Hong Kong because Nvidia eliminated that risk factor [{K}].",
        "Investors cannot find the indebtedness risk factor because Nvidia removed it [{K}].",
        "Nvidia did not add risks though the indebtedness risk factor was removed [{K}].",
        "Nvidia did not add risks even though it removed the export risk factor [{K}].",
        "Nvidia did not reword the indebtedness risk factor instead it removed it [{K}].",
        "Nvidia did not change anything except that it removed the indebtedness risk factor [{K}].",
        "The risk factor that Nvidia had not updated since 2022 was removed [{K}].",
        "The risk factor that Nvidia had not updated since 2022 was removed.",
        "The paragraph Nvidia never updated after 2022 was deleted [{K}].",
        "The 10-K does not say so unless Nvidia removed the export risk factor [{K}]."]),
    ("assertive-negation", "removal", [
        "Nvidia did not hesitate to remove the indebtedness risk factor [{K}].",
        "It is not a coincidence that the indebtedness risk factor was removed [{K}].",
        "It cannot be denied that the indebtedness risk factor was removed [{K}].",
        "Investors cannot doubt that Nvidia removed the indebtedness risk factor [{K}].",
        "No one noticed when Nvidia removed the indebtedness risk factor [{K}].",
        "There is no doubt that Nvidia removed the indebtedness risk factor [{K}].",
        "No doubt the indebtedness risk factor was removed [{K}].",
        "There can be no doubt the indebtedness risk factor was removed [{K}].",
        "There is no question that Nvidia removed the indebtedness risk factor [{K}].",
        "It is no secret that Nvidia removed the indebtedness risk factor [{K}].",
        "Since Nvidia has no operations in Russia anymore it is unsurprising that the Russia risk factor was dropped [{K}]."]),
    # --- a none-quantifier does not reach across a colon, a dash, a comma, a contrast or a carve-out ---
    ("none-quantifier-cut", "removal", [
        "Nvidia added no new risk factors: it removed the indebtedness risk factor [{K}].",
        "Nvidia added no new risks but removed the indebtedness risk factor [{K}].",
        "Nvidia gave no reason: the indebtedness risk factor was simply removed [{K}].",
        "Nvidia offered no explanation — the indebtedness risk factor was removed [{K}].",
        "Nvidia removed the indebtedness risk factor: nothing else changed [{K}].",
        "With no explanation, Nvidia removed the indebtedness risk factor [{K}].",
        "Nvidia made no changes other than removing the indebtedness risk factor [{K}].",
        "Nothing changed except that the indebtedness risk factor was removed [{K}].",
        "Nothing changed except that Nvidia removed the export risk factor [{K}].",
        "None of the other risk factors changed, the indebtedness risk factor was removed [{K}].",
        "Neither survived: both risk factors were dropped [{K}].",
        "Apart from the indebtedness risk factor, no risk factor was removed [{K}].",
        "The text check found no new risk factors in this comparison of the two annual reports: it shows that Nvidia removed "
        "the indebtedness risk factor [{K}].",
        "The text check found no new risk factors in this comparison of the two annual reports: it shows that Nvidia removed "
        "the indebtedness risk factor.",
        "The text check found no new risk factors in this comparison of the two annual reports — it shows that Nvidia removed "
        "the indebtedness risk factor [{K}].",
        "No fewer than three risk factors were removed [{K}].",
        "The newer 10-K shows that no fewer than three risk factors were dropped [{K}]."]),
    # --- a quoted removed-list label used as the predicate of a cited id claims that the item is on the list ---
    ("quoted-label-as-predicate", "removal", [
        f'Under "{_LABEL}" we see the indebtedness risk [{{K}}].',
        f'The indebtedness risk factor is listed under "{_LABEL}".', f'Under "{_LABEL}" we see the indebtedness risk.',
        f'This sentence appears in the list of "{_LABEL}," which the check printed.',
        f'The indebtedness risk factor is listed under "{_LABEL}" [{{K}}].',
        f'The "{_LABEL}" list includes the indebtedness risk [{{K}}].',
        f"Nvidia's '{_LABEL}' list names the indebtedness risk [{{K}}].",
        'The NAC sentence is one of the "Passages whose wording was not found in the newer filing" [{K}].',
        f"The indebtedness risk factor falls under “{_LABEL}” in this comparison [{{K}}].",
        f'The Hong Kong risk is listed under "{_LABEL}" [{{K}}].',
        f'The export risk factor appears in the "{_LABEL}" list [{{K}}].',
        f'The export risk sits under "{_LABEL}" [{{K}}].',
        f"The export risk factor is in the “{_LABEL}” category [{{K}}].",
        f'The export risk "{_LABEL}" in the newer filing [{{K}}].',
        'Under "Passages of surviving risk factors whose wording was not found in the newer filing", the NAC sentence is listed '
        '[{K}].']),
    # --- a removal heading's items are judged by their ids however they are written under it ---
    ("removal-heading-items", "removal", [
        f"### {_LABEL}\n- Hong Kong risk [{{K}}]", f"### {_LABEL}\n+ Indebtedness risk [{{K}}]",
        f"### {_LABEL}\n- None found.\n- Export risk [{{K}}]",
        f"### {_LABEL}\nThe Hong Kong transition risk [{{R}}].\nNvidia also dropped the export risk factor.",
        f"### {_LABEL}\nThe following:\n- Indebtedness risk [{{K}}]",
        f"### {_LABEL}\n| Risk | Source |\n|---|---|\n| Indebtedness risk | [{{K}}] |",
        f"### {_LABEL}\nThe indebtedness risk factor [{{K}}].", f"**{_LABEL}:**\nIndebtedness risk [{{K}}]",
        "### Passages whose wording was not found in the newer filing\nThe indebtedness sentence [{K}].",
        f"### {_LABEL}\nNvidia's indebtedness risk factor [{{K}}] is in this list.",
        f"### {_LABEL}\nOne risk factor from the older filing is in this list:\n- Export controls risk [{{K}}]",
        f"### {_LABEL}\n- No explanation given; the indebtedness risk factor [{{K}}]",
        "**Removed risk factors:**\n- No longer disclosed: the indebtedness risk factor [{K}]",
        "Nvidia added no new risk factors but removed these:\n- Indebtedness risk [{K}]",
        "No risk factors were added; the following were removed:\n- Indebtedness risk [{K}]",
        "**Not carried forward (deleted):**\n- Export risk [{K}]",
        "**Not in the newer filing (removed):**\n- Export risk [{K}]",
        "**Older risk factors with no counterpart anywhere in the fiscal year ended January 25, 2026 annual report on Form 10-K "
        "that were removed:**\n- Export risk [{K}]"]),
    # --- a clause with a removal verb and no disclosure noun of its own, continuing a sentence that names one ---
    ("continuing-clause-without-a-disclosure-noun", "removal", [
        "The indebtedness risk factor was not reworded; it was deleted [{K}].",
        "Nvidia was unable to keep the export risk factor; it removed it [{K}].",
        "The export risk factor has never been reworded, but has been removed [{K}].",
        "The export risk factor was kept by Nvidia, which dropped it later [{K}]."]),
    ("continuing-clause-cites-an-unsettled-item", "unsettled", [
        "The Not matched list has the export risk [{U}], which was removed."]),
    # --- the removal is a fact the sentence takes for granted (a relative clause on a definite noun) ---
    ("presupposed-by-a-definite-relative-clause", "removal", [
        "We cannot find the export risk factor that was removed [{K}].",
        "The check did not identify the export risk factor that was removed [{K}].",
        "Nvidia does not deny it removed the export risk factor [{K}].",
        "Nvidia cannot hide that it removed the export risk factor [{K}]."]),
    # --- the negation belongs to a relative clause on the subject; the removal is the main predicate ---
    ("negation-inside-a-relative-clause", "removal", [
        "The risk factor that Nvidia did not update was removed [{K}].",
        "The risk factor Nvidia never updated was deleted [{K}].",
        "The risk factor Nvidia did not reword no longer appears in the newer 10-K [{K}]."]),
    # --- forms the removal lexicon covers only in some spellings ---
    ("lexicon-twins", "removal", [
        "The newer 10-K saw the elimination of the indebtedness risk factor [{K}].",
        "The indebtedness risk factor doesn't appear in the newer filing [{K}].",
        "The indebtedness risk factor didn't appear in the newer filing [{K}]."]),
] for text in texts]

# ==================================================================================================== documented limits
LIMIT_FP = [(family, context, t(text)) for family, context, texts in [
    # (the fixed eight-word window, whose limit this family used to pin, no longer holds for a negated EVIDENCE verb: "does not
    # indicate / show that ..." reaches its whole that-clause, see PASS_CASES "negated-evidence-verb-with-a-long-that-clause")
    # an "and" after the negator starts a claim of its own: "did not add X and removed Y" (must flag) has the same shape
    ("negated-complement-with-a-coordinated-verb", "removal", [
        "This is evidence about wording only, not evidence that Nvidia reassessed the risk and dropped it.",
        "It does not mean the company reconsidered the risk and dropped the disclosure."]),
    # (", however," right after the negator is an aside now: PASS_CASES "parenthetical-however"; ", however," after a complete object,
    #  "did not add risks, however, removed X", still makes a claim)
    # a restated yes/no question is judged like an assertion
    ("restated-question", "none", [
        "Were any risk factors removed? The text check found none that no longer appear as a separate risk factor.",
        "Did Nvidia drop any risk factors between these two 10-Ks? Based on the text comparison, the answer is no."]),
    # removal verbs in risk-content prose: the check cannot tell a disclosure that went away from a world fact
    ("world-fact-prose-with-a-removal-verb", "plain", [
        "Nvidia discloses the risk that customers may discontinue purchases of its products [{RISK}].",
        "Nvidia's risk factor states that it is no longer able to sell H20 products in China [{RISK}].",
        "The risk factor says export controls could eliminate Nvidia's ability to serve customers in China [{RISK}].",
        "The filing's risk discussion notes that the USG removed certain license exceptions for exports to China [{RISK}].",
        "Nvidia's risk factors say its China data center revenue dropped sharply after the export controls took effect [{RISK}]."]),
    # the wording difference INSIDE a reworded passage, described as a removal of words: reworded ids never support a removal
    ("wording-removed-inside-a-reworded-passage", "removal", [
        "- A reworded passage shows the removal of explicit Middle East/Tier 2 market references in inventory risk language "
        "[{REW}] [{REW2}].",
        "- A reworded passage simplified the geopolitical tensions language, dropping the explicit Taiwan/China revenue-share "
        "framing [{REW}] [{REW2}]."]),
    # documented in verify.py: an added-passage hedge that names the older filing by its date has no known direction
    ("added-passage-hedge-naming-the-older-filing-by-date", "removal", [
        "The licences sentence's wording was not found in the 10-K for the fiscal year ended January 26, 2025, the older "
        "filing [{ADDED}]."]),
] for text in texts]

LIMIT_FN = [(family, context, t(text)) for family, context, texts in [
    # lexicon gaps: broad words (absent, missing, cut) would flag risk-content prose in every non-temporal answer
    ("removal-lexicon-gap", "removal", [
        "The indebtedness risk factor is absent from the newer 10-K [{K}].",
        "The indebtedness risk factor is missing from the newer 10-K [{K}].",
        "The indebtedness risk factor was cut from the newer 10-K [{K}].",
        "The indebtedness risk factor is not disclosed any longer [{K}].",
        "Nvidia took the indebtedness risk factor out of the newer 10-K [{K}].",
        "The indebtedness risk factor does not appear in the fiscal year ended January 25, 2026 10-K [{K}]."]),
    # a double negation: no parser (the negation inside a relative clause on the subject, "The risk factor that Nvidia did not update
    # was removed", is flagged now: FLAG_CASES "negation-inside-a-relative-clause")
    ("double-negation", "removal", [
        "It is not true that the indebtedness risk factor was not removed [{K}]."]),
] for text in texts]


def _ids(rows):
    return [f"{family}-{n}" for n, (family, _, _) in enumerate(rows)]


@pytest.mark.parametrize("family, context, text", PASS_CASES, ids=_ids(PASS_CASES))
def test_honest_text_is_not_flagged(family, context, text):
    assert claims(context, text) == (), text


@pytest.mark.parametrize("family, context, text", FLAG_CASES, ids=_ids(FLAG_CASES))
def test_a_positive_removal_claim_no_removed_list_supports_is_flagged(family, context, text):
    found = claims(context, text)
    assert found, text
    _, ctx, valid = CONTEXTS[context]()
    assert "unsupported_removal_claim" in verify_answer(text, set(CITE_RE.findall(text)), valid, "stop", context=ctx,
                                                        sources=sources_from_context(ctx))


@pytest.mark.parametrize("family, context, text", LIMIT_FP, ids=_ids(LIMIT_FP))
def test_known_limit_honest_text_is_still_flagged(family, context, text):
    """Pinned so that a change of this behaviour is a decision, not an accident. The safe direction: it costs one escalation."""
    assert claims(context, text), text


@pytest.mark.parametrize("family, context, text", LIMIT_FN, ids=_ids(LIMIT_FN))
def test_known_limit_positive_claim_is_still_not_flagged(family, context, text):
    assert claims(context, text) == (), text
