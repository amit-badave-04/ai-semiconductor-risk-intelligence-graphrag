"""The answer prompt's rules added after the 2026-09-27 deployed eval (T11, T14 and the yes/no temporal questions).

The prompt is read as text (no model): these pin that the rules exist and stay consistent with the hedged headings the
context layout writes. Changing the prompt changes ``template_fingerprint()``, which un-seeds every example answer: the
examples are regenerated in the same change (scripts/build_examples.py).
"""

import re

from semigraph.retrieval.answerer import ANSWER_PROMPT
from semigraph.retrieval.context_layout import PASSAGES_REMOVED_PHRASE, REMOVED_ITEMS_PREFIX

FLAT = re.sub(r"\s+", " ", ANSWER_PROMPT)


def test_a_wording_not_found_finding_is_never_extended_to_still_disclosed_or_removed():
    assert "do not conclude from it that the statement is still disclosed in other words" in FLAT
    assert "do not conclude that it was removed" in FLAT
    assert "the check cannot tell which and stop there" in FLAT


def test_a_reworded_risk_factor_is_never_called_identical_or_unchanged():
    assert 'A risk factor listed as "Reworded" is still disclosed with changed wording' in FLAT
    assert "never call its wording identical or unchanged" in FLAT


def test_the_answer_says_which_pair_it_describes_and_states_a_note_line_first():
    assert "Answer about the pair the question asks for and say which two filings you are describing" in FLAT
    assert 'carries a "Note for <company>:" line' in FLAT and "state that note plainly before the findings" in FLAT


def test_a_yes_no_change_question_leads_with_the_hedged_finding_and_lists_only_what_bears_on_it():
    assert "For a yes/no question about a change" in FLAT
    assert "begin with one sentence that gives the finding in the hedged wording above" in FLAT
    assert "Do not list reworded risk factors or unrelated counts unless the question asks about them" in FLAT


def test_the_rules_use_the_labels_the_layout_writes_and_keep_the_hedge_that_forbids_removed():
    """A rule that names a heading must name the one the context prints (the writer builds them from these constants)."""
    assert REMOVED_ITEMS_PREFIX.strip().lower() in FLAT.lower()
    assert PASSAGES_REMOVED_PHRASE in FLAT
    assert "Never write that the company removed, dropped, deleted, eliminated or withdrew the risk" in FLAT


def test_the_period_end_wording_rule_still_forbids_fy_labels_because_the_covers_line_uses_period_ends():
    assert 'never label a fiscal year "FY2026" or by a bare year' in FLAT
