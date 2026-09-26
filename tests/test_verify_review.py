"""Findings of the independent review of the M1b answer checks (verify.py): echoed figures, currency, sign-aware
percentages, uncited answers, refusal and metric-answer shape. Pure and free: no model, no database.

Every probe of the review is a test here; the tests that locked the old behaviour were rewritten in place
(tests/test_verify_grounding.py, tests/test_answer_checks_events.py) and say so in their names.
"""

import pytest

from semigraph.retrieval.verify import (
    AnswerChecks,
    answer_checks,
    checks_failed,
    failed_check_names,
    refusal_shaped,
    verify_answer,
)

CHUNK = "0001045810-26-000021:I.1A:0361"
REV26 = "xbrl:1045810:revenue:2026-01-25"
TSMC_REV = "xbrl:1046179:revenue:2025-12-31"
ASML_RND = "xbrl:937966:rnd:2025-12-31"
VALID = {CHUNK, REV26, TSMC_REV, ASML_RND}

CTX = (
    "RELATIONSHIPS:\n(none)\n\nMETRICS:\n"
    "Nvidia: fiscal year ended 2026-01-25\n"
    f"- revenue for period 2025-01-27..2026-01-25: 215,938,000,000 USD [{REV26}] | computed: +65.5% vs fiscal year "
    "ended 2025-01-26 (change +85,441,000,000 USD)\n"
    "TSMC: fiscal year ended 2025-12-31\n"
    f"- revenue for period 2025-01-01..2025-12-31: 3,809,054,000,000 TWD [{TSMC_REV}] | computed: +31.6% vs fiscal "
    "year ended 2024-12-31 (change +914,746,000,000 TWD)\n"
    "ASML: fiscal year ended 2025-12-31\n"
    f"- rnd for period 2025-01-01..2025-12-31: 4,304,000,000 EUR [{ASML_RND}] | computed: -5.0% vs fiscal year "
    "ended 2024-12-31 (change -227,000,000 EUR)\n\n"
    "ACTIVE RISKS:\n(none)\n\nEXCERPTS:\n"
    f"[{CHUNK}]\nOne customer accounted for 19% of revenue.\n"
)
SOURCES = {
    CHUNK: "One customer accounted for 19% of revenue.",
    REV26: f"- revenue for period 2025-01-27..2026-01-25: 215,938,000,000 USD [{REV26}] | computed: +65.5% vs fiscal "
           "year ended 2025-01-26 (change +85,441,000,000 USD)",
    ASML_RND: f"- rnd for period 2025-01-01..2025-12-31: 4,304,000,000 EUR [{ASML_RND}] | computed: -5.0% vs fiscal "
              "year ended 2024-12-31 (change -227,000,000 EUR)",
    TSMC_REV: f"- revenue for period 2025-01-01..2025-12-31: 3,809,054,000,000 TWD [{TSMC_REV}] | computed: +31.6% "
              "vs fiscal year ended 2024-12-31 (change +914,746,000,000 TWD)",
}


def reasons(text, *, cited=None, context=CTX, question=None, finish="stop"):
    from semigraph.retrieval.answerer import CITE_RE

    cited = set(CITE_RE.findall(text)) if cited is None else set(cited)
    return verify_answer(text, cited, VALID, finish, context=context, sources=SOURCES, question=question)


def checks(text, *, question=None, context=CTX):
    from semigraph.retrieval.answerer import CITE_RE

    return answer_checks(text, set(CITE_RE.findall(text)), VALID, context, sources=SOURCES, question=question)


# =========================================================================================================== H1

def test_the_review_probe_a_figure_only_the_question_states_is_not_grounded():
    """Q "Did NVIDIA revenue reach $500 billion?" A "Yes, NVIDIA revenue reached $500 billion [xbrl...]" was released
    as grounded although nothing in the context says $500 billion."""
    question = "Did NVIDIA revenue reach $500 billion?"
    text = f"Yes, NVIDIA revenue reached $500 billion [{REV26}]."
    c = checks(text, question=question)
    assert c.numbers_grounded is False
    assert c.echoed_numbers == ("$500 billion",) and c.unmatched_numbers == ()
    assert reasons(text, question=question) == ["ungrounded_number"]


def test_a_figure_present_in_both_the_question_and_the_context_is_grounded_not_echoed():
    question = "Did revenue reach $215.9 billion?"
    c = checks(f"Yes, $215.9 billion [{REV26}].", question=question)
    assert c.numbers_grounded is True and c.echoed_numbers == () and c.unmatched_numbers == ()


def test_a_percentage_only_the_question_states_is_echoed_and_not_grounded():
    question = "Did revenue grow more than 90%?"
    c = checks(f"Yes, it grew 90% [{REV26}].", question=question)
    assert c.numbers_grounded is False and c.echoed_numbers == ("90%",) and c.unmatched_numbers == ()


def test_echoed_and_invented_figures_are_reported_in_separate_lists():
    question = "Did revenue reach $500 billion?"
    c = checks(f"Revenue reached $500 billion [{REV26}], not $190 billion.", question=question)
    assert c.echoed_numbers == ("$500 billion",) and c.unmatched_numbers == ("$190 billion",)
    assert c.numbers_grounded is False


def test_numbers_grounded_is_false_whenever_anything_was_echoed():
    c = checks(f"Above $100 billion, at $215.9 billion [{REV26}].", question="Is revenue above $100 billion?")
    assert c.echoed_numbers == ("$100 billion",) and c.numbers_grounded is False


# =========================================================================================================== H3

@pytest.mark.parametrize("stated", ["NT$3.81 trillion", "3.81 trillion TWD", "TWD 3.81 trillion",
                                    "3.81 trillion New Taiwan dollars", "3.81 trillion NT dollars"])
def test_a_figure_in_the_metric_s_own_currency_is_grounded(stated):
    assert reasons(f"TSMC revenue was {stated} [{TSMC_REV}].") == []


@pytest.mark.parametrize("stated", ["$3.81 trillion", "US$3.81 trillion", "3.81 trillion USD", "3.81 trillion dollars",
                                    "3.81 trillion US dollars", "EUR 3.81 trillion", "€3.81 trillion"])
def test_a_figure_whose_only_match_is_another_currency_is_ungrounded(stated):
    """The review probe: TSMC reports in TWD, so "$3.81 trillion" states a value nothing in the context supports."""
    assert reasons(f"TSMC revenue was {stated} [{TSMC_REV}].") == ["ungrounded_number"]
    assert checks(f"TSMC revenue was {stated} [{TSMC_REV}].").unmatched_numbers == (stated,)


@pytest.mark.parametrize("stated", ["€4.3 billion", "EUR 4.3 billion", "4.3 billion EUR", "4.3 billion euros",
                                    "4.304 billion euros"])
def test_euro_amounts_ground_against_eur_metric_lines(stated):
    assert reasons(f"ASML R&D was {stated} [{ASML_RND}].") == []


@pytest.mark.parametrize("stated", ["$4.3 billion", "USD 4.3 billion", "4.3 billion dollars", "TWD 4.3 billion"])
def test_a_euro_metric_does_not_ground_a_dollar_amount(stated):
    assert reasons(f"ASML R&D was {stated} [{ASML_RND}].") == ["ungrounded_number"]


def test_usd_written_after_the_number_grounds_against_the_usd_metric_line():
    assert reasons(f"Revenue was 215.9 billion USD [{REV26}], or US$215.9 billion.") == []
    assert reasons(f"Revenue was 215.9 billion dollars [{REV26}].") == []


def test_a_currency_prefix_other_than_the_known_ones_is_never_grounded():
    assert reasons(f"Revenue was HK$215.9 billion [{REV26}].") == ["ungrounded_number"]


def test_the_change_part_of_a_computed_line_carries_its_currency_too():
    """"(change +914,746,000,000 TWD)": a dollar amount of that size is not supported, a TWD one is."""
    assert reasons(f"Revenue rose by NT$914.7 billion [{TSMC_REV}].") == []
    assert reasons(f"Revenue rose by $914.7 billion [{TSMC_REV}].") == ["ungrounded_number"]


def test_an_untagged_scaled_amount_in_a_chunk_still_grounds_any_currency():
    """A filing sentence "4,304 million" carries no currency word: it stays a currency-agnostic known value."""
    ctx = CTX + f"[{CHUNK}]\nResearch and development expense increased to 4,304 million.\n"
    assert reasons(f"R&D was $4.3 billion [{CHUNK}].", context=ctx) == []


def test_a_no_break_space_between_the_number_and_its_scale_or_currency_is_still_an_amount():
    assert reasons(f"Revenue was $215.9 billion [{REV26}].") == []
    assert reasons(f"Revenue was $190 billion [{REV26}].") == ["ungrounded_number"]
    assert reasons(f"Revenue was 215.9 billion USD [{REV26}].") == []


def test_numbers_checked_counts_every_figure_examined():
    c = checks(f"Revenue was $215.9 billion [{REV26}], up 65.5%.")
    assert c.numbers_checked == 2 and c.numbers_grounded is True
    assert checks(f"Nvidia depends on TSMC [{CHUNK}].").numbers_checked == 0


def test_with_no_figures_numbers_grounded_stays_true_so_figure_free_answers_are_not_escalated():
    c = checks(f"Nvidia depends on TSMC [{CHUNK}].")
    assert c.numbers_grounded is True and c.numbers_checked == 0
    assert reasons(f"Nvidia depends on TSMC [{CHUNK}].") == []


def test_without_a_context_nothing_is_examined():
    c = answer_checks(f"Revenue was $215.9 billion [{REV26}].", {REV26}, VALID, None)
    assert c.numbers_checked == 0 and c.numbers_grounded is True


def test_an_uncited_figure_in_another_currency_does_not_pass_as_a_grounded_metric_answer():
    """The short-uncited-metric-answer rule used its own, currency-blind grounding: it goes through the same check now."""
    assert reasons("TSMC's revenue was $3.81 trillion.") == ["no_citation", "ungrounded_number"]
    assert reasons("TSMC's revenue was NT$3.81 trillion.") == []


# =========================================================================================================== M1

@pytest.mark.parametrize("text", [
    f"Revenue fell 65.5% [{REV26}].", f"Revenue declined 65.5% from the prior year [{REV26}].",
    f"Revenue was down 65.5% [{REV26}].", f"Revenue decreased by 65.5% [{REV26}].",
    f"Revenue dropped 65.5% [{REV26}].", f"Revenue was lower by 65.5% [{REV26}].",
])
def test_a_decline_worded_percentage_against_a_computed_rise_is_ungrounded(text):
    """The computed line says +65.5%; the answer cites it and says it fell. Magnitude alone used to pass."""
    assert reasons(text) == ["ungrounded_number"]
    assert checks(text).unmatched_numbers == ("65.5%",)


@pytest.mark.parametrize("text", [
    f"Revenue rose 65.5% [{REV26}].", f"Revenue grew by 65.5% [{REV26}].", f"Revenue was up 65.5% [{REV26}].",
    f"Revenue increased 65.5% [{REV26}].", f"Revenue was higher by 65.5% [{REV26}].",
    f"Revenue growth of 65.5% [{REV26}].", f"Revenue rose +65.5% [{REV26}].",
])
def test_a_rise_worded_percentage_against_a_computed_rise_is_grounded(text):
    assert reasons(text) == []


def test_a_rise_worded_percentage_against_a_computed_decline_is_ungrounded():
    assert reasons(f"ASML R&D grew 5.0% [{ASML_RND}].") == ["ungrounded_number"]
    assert reasons(f"ASML R&D fell 5.0% [{ASML_RND}].") == []
    assert reasons(f"ASML R&D declined 5% [{ASML_RND}].") == []


def test_an_explicit_sign_is_read_as_a_direction():
    assert reasons(f"Revenue changed by -65.5% [{REV26}].") == ["ungrounded_number"]
    assert reasons(f"Revenue changed by +65.5% [{REV26}].") == []
    assert reasons(f"ASML R&D changed by -5.0% [{ASML_RND}].") == []


def test_a_percentage_with_no_direction_word_is_compared_by_magnitude_as_before():
    assert reasons(f"The year-over-year change was 65.5% [{REV26}].") == []
    assert reasons(f"The year-over-year change was 5.0% [{ASML_RND}].") == []


def test_words_that_merely_contain_a_direction_word_are_not_directions():
    """"supply" contains "up", "download" contains "down": word boundaries only."""
    assert reasons(f"The supply change was 5.0% [{ASML_RND}].") == []
    assert reasons(f"The download change was 65.5% [{REV26}].") == []


def test_a_direction_noun_right_after_the_figure_states_its_direction_when_none_precedes_it():
    assert reasons(f"There was a 65.5% decline in revenue [{REV26}].") == ["ungrounded_number"]
    assert reasons(f"There was a 65.5% increase in revenue [{REV26}].") == []
    assert reasons(f"ASML R&D saw a 5.0% reduction [{ASML_RND}].") == []


def test_a_hyphen_inside_a_range_is_not_a_minus_sign():
    assert reasons(f"Growth was between 60-65.5% [{REV26}].") == []


def test_up_to_is_not_a_direction():
    assert reasons(f"ASML R&D changed by up to 5.0% [{ASML_RND}].") == []


def test_the_direction_word_must_be_close_to_the_figure():
    far = f"Revenue fell short of some analyst expectations, but the year-over-year growth was clearly measured at 65.5% [{REV26}]."
    assert reasons(far) == []      # "fell" is more than 40 characters before the figure


def test_a_percentage_in_a_cited_chunk_has_no_sign_so_it_is_compared_by_magnitude():
    assert reasons(f"One customer fell to 19% of revenue [{CHUNK}].") == []


def test_the_sources_path_cannot_launder_a_wrong_direction():
    """The xbrl id's source text IS the metric line, computed value included: citing it must not ground "fell 65.5%"."""
    assert reasons(f"Revenue fell 65.5% [{REV26}].") == ["ungrounded_number"]
    assert reasons(f"Revenue fell 65.5% [{REV26}].", context=CTX) == ["ungrounded_number"]


# =========================================================================================================== M4

def test_an_answer_with_no_citation_that_is_not_a_refusal_is_a_failed_check():
    c = checks("Nvidia depends on TSMC for advanced packaging.")
    assert c.has_citation is False and c.is_refusal is False
    assert checks_failed(c.as_dict()) is True and failed_check_names(c.as_dict()) == ["no_citation"]


def test_a_zero_citation_refusal_is_not_a_failed_check():
    c = checks("The context does not contain Samsung's revenue.")
    assert c.has_citation is False and c.is_refusal is True
    assert checks_failed(c.as_dict()) is False


def test_a_cited_answer_reports_has_citation():
    c = checks(f"Nvidia depends on TSMC [{CHUNK}].")
    assert c.has_citation is True and checks_failed(c.as_dict()) is False


def test_the_checks_object_has_one_settled_json_shape():
    import json

    c = checks(f"Revenue was $215.9 billion [{REV26}], up 65.5%.")
    assert json.loads(json.dumps(c.as_dict())) == {
        "citations_retrieved": True, "numbers_grounded": True, "numbers_checked": 2, "unmatched_numbers": [],
        "echoed_numbers": [], "pseudo_citations": [], "has_citation": True, "is_refusal": False,
        "unsupported_removal_claim": False, "unsupported_removal_sentences": []}


def test_the_dataclass_defaults_describe_a_clean_answer():
    assert AnswerChecks(True, True, (), ()).as_dict()["has_citation"] is True


@pytest.mark.parametrize("checks_dict,names", [
    ({"citations_retrieved": False}, ["citations_not_retrieved"]),
    ({"numbers_grounded": False, "unmatched_numbers": ["$1"]}, ["ungrounded_number"]),
    ({"numbers_grounded": False, "echoed_numbers": ["$1"]}, ["ungrounded_number"]),
    ({"pseudo_citations": ["Excerpts"]}, ["pseudo_citation"]),
    ({"unsupported_removal_claim": True}, ["unsupported_removal_claim"]),
    ({"has_citation": False, "is_refusal": False}, ["no_citation"]),
])
def test_failed_check_names_one_predicate_for_routes_seeding_and_the_page(checks_dict, names):
    assert failed_check_names(checks_dict) == names and checks_failed(checks_dict) is True


@pytest.mark.parametrize("checks_dict", [None, {}, "x", {"has_citation": False, "is_refusal": True},
                                         {"has_citation": True}, {"numbers_checked": 0, "numbers_grounded": True}])
def test_nothing_or_clean_checks_are_not_failed(checks_dict):
    assert checks_failed(checks_dict) is False and failed_check_names(checks_dict) == []


# =========================================================================================================== LOW

REFUSAL_WITH_A_CLAIM = "The filings do not state the exact date; however NVIDIA exited Russia entirely in 2022."


def test_a_refusal_followed_by_an_independent_factual_clause_is_not_a_refusal():
    assert refusal_shaped(REFUSAL_WITH_A_CLAIM) is False
    assert reasons(REFUSAL_WITH_A_CLAIM) == ["no_citation"]
    assert checks(REFUSAL_WITH_A_CLAIM).is_refusal is False


@pytest.mark.parametrize("text", [
    "The context does not contain Samsung's revenue. Nvidia designs GPUs.",
    "The context does not contain Samsung's revenue, but Nvidia depends on TSMC.",
    "The context does not contain that.\n- However, AMD competes with Nvidia.",
])
def test_any_later_clause_or_bullet_that_states_a_fact_defeats_the_refusal(text):
    assert refusal_shaped(text) is False


@pytest.mark.parametrize("text", [
    "The context does not contain Samsung's revenue. It also has no data for AMD.",
    "The provided excerpts do not state Samsung’s total annual revenue.",
    "The context does not contain Samsung's revenue; nor does it mention its filings.",
    "Based on the provided context, I cannot answer this question.\n\n- The knowledge graph contains no reported metrics for Samsung.",
])
def test_further_limitations_do_not_defeat_a_refusal(text):
    assert refusal_shaped(text) is True


def test_a_pure_metric_answer_must_not_continue_past_the_figure_with_another_claim():
    assert reasons("Revenue was $215.9 billion.") == []
    claim = "Nvidia's revenue was $215.938 billion. Nvidia also plans to acquire Intel next year."
    assert reasons(claim) == ["no_citation"]
    same_sentence = "Nvidia's revenue was $215.938 billion; Nvidia also plans to acquire Intel next year."
    assert reasons(same_sentence) == ["no_citation"]
    conj = "Nvidia's revenue was $215.938 billion, and Nvidia plans to acquire Intel next year."
    assert reasons(conj) == ["no_citation"]


def test_a_pure_metric_answer_may_carry_a_limitation_or_a_second_figure():
    assert reasons("Nvidia's revenue was $215.938 billion, up from a base the context does not give.") == []
    assert reasons("Nvidia's revenue was $215.938 billion. The context does not state the change.") == []


def test_the_pure_metric_exemption_survives_and_the_checks_still_flag_the_missing_citation():
    """Decision recorded: a short, fully grounded, uncited metric statement is still RELEASED (no ``no_citation`` reason),
    but its ``checks`` report ``has_citation`` False, so it is not cached and the page shows a warning."""
    text = "Nvidia's revenue was $215.938 billion."
    assert reasons(text) == []
    c = checks(text)
    assert c.has_citation is False and failed_check_names(c.as_dict()) == ["no_citation"]


# ---------------------------------------------------------------------------------- LOW: a negation about the world is not a limitation

@pytest.mark.parametrize("text", [
    "The filings do not state the exact date; however NVIDIA no longer has operations in Russia.",
    "The filings do not state the exact date. Nvidia does not sell to Huawei.",
    "The context does not contain Samsung's revenue; Nvidia has no exposure to Samsung.",
])
def test_a_negated_fact_about_the_world_after_a_refusal_is_still_a_claim(text):
    assert refusal_shaped(text) is False
    assert reasons(text) == ["no_citation"]


def test_a_pure_metric_answer_cannot_continue_with_a_negated_claim_about_the_world():
    text = "Nvidia's revenue was $215.938 billion; Nvidia does not sell to Huawei."
    assert reasons(text) == ["no_citation"]
    assert reasons("Nvidia's revenue was $215.938 billion; the context does not state the change.") == []
