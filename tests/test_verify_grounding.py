"""M1b numeric grounding and pseudo-citation checks (verify.py) - every answer, cited or not.

The live audit found: a correct answer stating "+$85.441B" that no context line contained, a net income taken
from the wrong fiscal year, and ``[Reported Metrics]`` style citations nothing can resolve. These tests pin the
mechanical net for those classes. Pure and free: no model, no database.
"""

import pytest

from semigraph.retrieval.verify import AnswerChecks, answer_checks, verify_answer

CHUNK = "0001045810-26-000021:I.1A:0361"
CHUNK2 = "0001045810-26-000021:I.1A:0362"
REV26 = "xbrl:1045810:revenue:2026-01-25"
REV25 = "xbrl:1045810:revenue:2025-01-26"
INTC_NI = "xbrl:50863:net_income:2024-12-28"
VALID = {CHUNK, CHUNK2, REV26, REV25, INTC_NI}

CTX = (
    "RELATIONSHIPS:\n(none)\n\nMETRICS:\n"
    "Nvidia: fiscal year ended 2026-01-25\n"
    f"- revenue for period 2025-01-27..2026-01-25: 215,938,000,000 USD [{REV26}] | computed: +65.5% vs fiscal year "
    "ended 2025-01-26 (change +85,441,000,000 USD)\n"
    "Nvidia: fiscal year ended 2025-01-26\n"
    f"- revenue for period 2024-01-29..2025-01-26: 130,497,000,000 USD [{REV25}]\n"
    "Intel: fiscal year ended 2024-12-28\n"
    f"- net_income for period 2023-12-31..2024-12-28: -18,756,000,000 USD [{INTC_NI}]\n\n"
    "ACTIVE RISKS:\n(none)\n\nEXCERPTS:\n"
    f"[{CHUNK}]\nOne customer accounted for 19% of revenue and our gross margin was 71.1%.\n"
)
SOURCES = {CHUNK: "One customer accounted for 19% of revenue and our gross margin was 71.1%.",
           CHUNK2: "Sales to two customers were 12 percent and 8.5% of revenue."}


def reasons(text, *, cited=(), context=CTX, sources=SOURCES, question=None, valid=VALID, finish="stop"):
    return verify_answer(text, set(cited), valid, finish, context=context, sources=sources, question=question)


# --- the live Test 3 answer (correct) must pass: the dollar change is a computed line, not the model's arithmetic ---

def test_the_correct_live_test_3_answer_is_grounded():
    text = (f"Revenue for the fiscal year ended January 25, 2026 was $215.938 billion [{REV26}], up $85.441 billion "
            f"(65.5%) from $130.497 billion in the fiscal year ended January 26, 2025 [{REV25}].")
    assert reasons(text, cited=[REV26, REV25]) == []
    checks = answer_checks(text, {REV26, REV25}, VALID, CTX, sources=SOURCES)
    assert checks == AnswerChecks(citations_retrieved=True, numbers_grounded=True, unmatched_numbers=(),
                                  pseudo_citations=(), numbers_checked=4, has_citation=True)


# --- dollar values ---

def test_a_cited_answer_with_an_invented_dollar_value_is_flagged():
    text = f"Revenue was $190 billion [{REV26}]."
    assert reasons(text, cited=[REV26]) == ["ungrounded_number"]
    assert answer_checks(text, {REV26}, VALID, CTX).unmatched_numbers == ("$190 billion",)


@pytest.mark.parametrize("stated", ["$216.5 billion", "$215.0 billion", "$215,938 million", "$215,938,000,000"])
def test_a_dollar_value_within_half_a_percent_of_a_context_value_is_grounded(stated):
    assert reasons(f"Revenue was {stated} [{REV26}].", cited=[REV26]) == []


def test_a_dollar_value_just_outside_the_half_percent_tolerance_is_flagged():
    assert reasons(f"Revenue was $217.1 billion [{REV26}].", cited=[REV26]) == ["ungrounded_number"]   # +0.54%


def test_every_dollar_value_must_match_not_just_one():
    text = f"Revenue was $215.9 billion [{REV26}] against $61.0 billion a year earlier."
    assert reasons(text, cited=[REV26]) == ["ungrounded_number"]
    assert answer_checks(text, {REV26}, VALID, CTX).unmatched_numbers == ("$61.0 billion",)


def test_a_net_loss_is_grounded_although_the_context_prints_it_with_a_minus_sign():
    """Intel FY2024 net income is -18,756,000,000: the grouped integer is harvested by absolute value."""
    assert reasons(f"Intel reported a net loss of $18.8 billion [{INTC_NI}].", cited=[INTC_NI]) == []
    assert reasons(f"Intel reported a net loss of $18.756 billion [{INTC_NI}].", cited=[INTC_NI]) == []


def test_a_scaled_amount_written_without_a_dollar_sign_in_a_chunk_grounds_the_dollar_value_in_the_answer():
    ctx = CTX + f"[{CHUNK2}]\nResearch and development expense increased to 4,304 million, and a 5.2 billion charge.\n"
    assert reasons(f"R&D was $4.3 billion [{CHUNK2}].", cited=[CHUNK2], context=ctx) == []
    assert reasons(f"There was a $5.2 billion charge [{CHUNK2}].", cited=[CHUNK2], context=ctx) == []
    assert reasons(f"R&D was $9.9 billion [{CHUNK2}].", cited=[CHUNK2], context=ctx) == ["ungrounded_number"]


def test_a_dollar_value_stated_only_in_the_question_is_echoed_and_never_grounded():
    """Rewritten by the review of the M1b checks (H1): this test used to assert that a figure the QUESTION states passes
    (``== []``). That released "Yes, revenue reached $500 billion [xbrl:...]" to a question about $500 billion as
    verified. Now the figure is reported in ``echoed_numbers`` and the answer is ``ungrounded_number`` (a cheap draft
    escalates; a strong answer is reported and not cached). tests/test_verify_review.py holds the probes."""
    text = f"Yes - revenue of $215.9 billion [{REV26}] is well above $100 billion."
    assert reasons(text, cited=[REV26]) == ["ungrounded_number"]
    question = "Did Nvidia's revenue exceed $100 billion?"
    assert reasons(text, cited=[REV26], question=question) == ["ungrounded_number"]
    checks = answer_checks(text, {REV26}, VALID, CTX, question=question)
    assert checks.echoed_numbers == ("$100 billion",) and checks.unmatched_numbers == ()
    assert checks.numbers_grounded is False


def test_without_a_context_no_numeric_check_is_possible_and_none_is_made():
    assert reasons("Revenue was $1B [a:I.1:0001].", cited=["a:I.1:0001"], context=None, valid={"a:I.1:0001"}) == []


def test_a_refusal_naming_a_grounded_figure_passes_but_an_ungrounded_one_is_flagged():
    ok = "The context does not contain fiscal 2028 revenue; the latest reported is $215.9 billion."
    bad = "The context does not contain fiscal 2028 revenue; analysts expect $300 billion."
    assert "ungrounded_number" not in reasons(ok)
    assert "ungrounded_number" in reasons(bad)


# --- percentages: computed lines, or a chunk the SAME answer cites ---

def test_a_percentage_from_a_computed_line_is_grounded():
    assert reasons(f"Revenue grew 65.5% [{REV26}].", cited=[REV26]) == []


def test_an_invented_percentage_is_flagged_even_when_the_answer_is_cited():
    text = f"Revenue grew 70.1% [{REV26}]."
    assert reasons(text, cited=[REV26]) == ["ungrounded_number"]
    assert answer_checks(text, {REV26}, VALID, CTX).unmatched_numbers == ("70.1%",)


def test_a_percentage_in_a_cited_chunk_is_grounded():
    assert reasons(f"One customer was 19% of revenue [{CHUNK}].", cited=[CHUNK]) == []


def test_a_percentage_in_a_chunk_the_answer_does_not_cite_is_not_grounding():
    """19% is in CHUNK, but this answer cites CHUNK2: the figure is not backed by anything it cites."""
    assert reasons(f"One customer was 19% of revenue [{CHUNK2}].", cited=[CHUNK2]) == ["ungrounded_number"]


def test_percent_written_as_a_word_in_a_chunk_is_recognised():
    assert reasons(f"Two customers were 12% and 8.5% of revenue [{CHUNK2}].", cited=[CHUNK2]) == []


def test_percentages_compare_by_absolute_value_a_decline_is_written_without_the_sign():
    ctx = CTX.replace("computed: +65.5%", "computed: -12.3%")
    assert reasons(f"Revenue fell 12.3% [{REV26}].", cited=[REV26], context=ctx) == []


def test_a_whole_number_percentage_may_round_a_computed_one_decimal_value_but_a_decimal_may_not():
    assert reasons(f"Revenue grew about 66% [{REV26}].", cited=[REV26]) == []          # 65.5 rounds to 66
    assert reasons(f"Revenue grew about 65% [{REV26}].", cited=[REV26]) == []          # 0.5 away is the limit
    assert reasons(f"Revenue grew about 64% [{REV26}].", cited=[REV26]) == ["ungrounded_number"]
    assert reasons(f"Revenue grew 65.4% [{REV26}].", cited=[REV26]) == ["ungrounded_number"]   # decimals must match


def test_a_percentage_only_the_question_states_is_echoed_not_grounded():
    """Rewritten by the review (H1): it used to assert ``== []``; a percentage the asker supplied is not evidence."""
    text, question = f"Yes, above 50% [{REV26}].", "Did revenue grow more than 50%?"
    assert reasons(text, cited=[REV26]) == ["ungrounded_number"]
    assert reasons(text, cited=[REV26], question=question) == ["ungrounded_number"]
    assert answer_checks(text, {REV26}, VALID, CTX, question=question).echoed_numbers == ("50%",)


# --- pseudo-citations ---

@pytest.mark.parametrize("bracket", ["Reported Metrics", "Dropped Risk Lineages", "Excerpts",
                                     "0001045810-26-000021 lineage data"])
def test_bracketed_prose_is_a_pseudo_citation(bracket):
    text = f"Revenue was $215.938 billion [{REV26}] [{bracket}]."
    assert reasons(text, cited=[REV26]) == ["pseudo_citation"]
    assert answer_checks(text, {REV26}, VALID, CTX).pseudo_citations == (bracket,)


def test_a_composite_citation_with_a_semicolon_or_comma_is_a_pseudo_citation():
    for joined in (f"{CHUNK}; {CHUNK2}", f"{CHUNK}, {CHUNK2}"):
        text = f"One customer was 19% of revenue [{joined}]."
        assert "pseudo_citation" in reasons(text, cited=[])
        assert answer_checks(text, set(), VALID, CTX).pseudo_citations == (joined,)


def test_a_well_formed_citation_is_never_a_pseudo_citation_even_if_it_was_not_retrieved():
    """A fabricated but well-formed id is ``invalid_citation`` (a different reason), not a pseudo-citation."""
    bogus = "0009999999-99-999999:I.1A:0001"
    assert reasons(f"x [{bogus}]", cited=[bogus]) == ["invalid_citation"]


def test_markdown_links_and_bracket_noise_are_not_pseudo_citations():
    text = (f"See [the SEC filing](https://www.sec.gov/x) for details [{CHUNK}]; the filing says it will [...] "
            f"[sic] and lists [1] and [T]he rest.")
    checks = answer_checks(text, {CHUNK}, VALID, CTX)
    assert checks.pseudo_citations == ()


def test_reasons_are_appended_after_the_existing_ones_in_a_fixed_order():
    text = f"Revenue was $190 billion [{REV26}] [Reported Metrics] [0009999999-99-999999:I.1A:0001]."
    got = verify_answer(text, {REV26, "0009999999-99-999999:I.1A:0001"}, VALID, "length", context=CTX)
    assert got == ["truncated", "invalid_citation", "ungrounded_number", "pseudo_citation"]


# --- the checks object (reported for every answer, including the ones that cannot escalate) ---

def test_checks_report_an_uncited_unretrieved_and_ungrounded_answer_without_hiding_anything():
    bogus = "0009999999-99-999999:I.1A:0001"
    text = f"Revenue was $190 billion [{bogus}] [Reported Metrics]."
    checks = answer_checks(text, {bogus}, VALID, CTX)
    assert checks.citations_retrieved is False and checks.numbers_grounded is False
    assert checks.unmatched_numbers == ("$190 billion",) and checks.pseudo_citations == ("Reported Metrics",)
    assert checks.as_dict() == {
        "citations_retrieved": False, "numbers_grounded": False, "numbers_checked": 1,
        "unmatched_numbers": ["$190 billion"], "echoed_numbers": [], "pseudo_citations": ["Reported Metrics"],
        "has_citation": True, "is_refusal": False, "unsupported_removal_claim": False,
        "unsupported_removal_sentences": []}


def test_checks_are_json_serialisable():
    import json

    checks = answer_checks(f"x [{CHUNK}]", {CHUNK}, VALID, CTX)
    assert json.loads(json.dumps(checks.as_dict())) == {
        "citations_retrieved": True, "numbers_grounded": True, "numbers_checked": 0, "unmatched_numbers": [],
        "echoed_numbers": [], "pseudo_citations": [], "has_citation": True, "is_refusal": False,
        "unsupported_removal_claim": False, "unsupported_removal_sentences": []}
