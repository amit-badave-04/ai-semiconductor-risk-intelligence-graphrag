"""The deterministic answer verifier: what must hold before a cheap model's draft may be shown."""

import pytest

from semigraph.retrieval.verify import REFUSAL_RE, verify_answer

VALID = {"a:I.1:0001", "a:I.1:0002"}


def test_a_cited_answer_with_valid_ids_passes():
    assert verify_answer("Revenue was $1B [a:I.1:0001].", {"a:I.1:0001"}, VALID, "stop") == []


def test_an_empty_answer_fails():
    assert verify_answer("   ", set(), VALID, "stop") == ["empty"]


def test_a_truncated_answer_fails():
    assert "truncated" in verify_answer("Revenue was $1B [a:I.1:0001]", {"a:I.1:0001"}, VALID, "length")


def test_a_citation_outside_the_retrieved_context_fails():
    assert verify_answer("x [a:I.1:0009]", {"a:I.1:0009"}, VALID, "stop") == ["invalid_citation"]


def test_no_citation_fails_unless_the_answer_is_a_refusal():
    assert verify_answer("Revenue was $1B.", set(), VALID, "stop") == ["no_citation"]
    assert verify_answer("The context does not contain that information.", set(), VALID, "stop") == []


def test_reasons_accumulate():
    assert verify_answer("x [a:I.1:0009]", {"a:I.1:0009"}, VALID, "length") == ["truncated", "invalid_citation"]


@pytest.mark.parametrize("text", ["Samsung is not an SEC filer.", "This cannot be determined from the filings.", "no data available"])
def test_refusal_pattern_matches_the_wordings_the_benchmark_expects(text):
    assert REFUSAL_RE.search(text)


# --- refusal wordings a model may legitimately use (found in the bake-off: these were false negatives) ---

@pytest.mark.parametrize("text", [
    "The provided excerpts do not state Samsung’s total annual revenue.",          # curly apostrophe, "do not state"
    "It contains no transcript, so I can’t determine what he said.",
    "The context doesn't include that figure, so I can't answer.",
    "The filings do not mention this; it cannot be determined.",
    "I am unable to determine this from the context.",
])
def test_refusal_pattern_accepts_the_common_ways_of_declining(text):
    assert REFUSAL_RE.search(text)


@pytest.mark.parametrize("text", ["Nvidia depends on TSMC [a:I.1:0001].", "Revenue was $215.9 billion."])
def test_refusal_pattern_does_not_match_substantive_answers(text):
    assert not REFUSAL_RE.search(text)


# --- an uncited number is fine when every dollar figure in it is IN the retrieved context (XBRL metrics have no chunk id) ---

METRICS_CTX = ("RELATIONSHIPS:\n(none)\n\nMETRICS:\n- Nvidia revenue for period 2025-01-27..2026-01-25: 215,938,000,000 USD\n"
               "- Nvidia revenue for period 2023-01-30..2024-01-28: 60,922,000,000 USD\n\nACTIVE RISKS:\n(none)\n\n"
               "DROPPED RISK LINEAGES:\n(none)\n\nEXCERPTS:\n[a:I.1:0001]\nRevenue was $60,922 million.\n")


def test_an_uncited_figure_found_in_the_metrics_block_still_needs_a_citation():
    """XBRL figures carry citable ids now: grounded is not enough, the answer must cite the metric line."""
    for text in ("Nvidia's revenue was about $215.9 billion.", "Revenue was $60,922,000,000.", "It was $60.9 billion."):
        assert verify_answer(text, set(), VALID, "stop", context=METRICS_CTX) == ["no_citation"]


def test_an_uncited_figure_not_in_the_context_still_fails():
    # M1b: an uncited figure that is not in the context is now ALSO reported as an ungrounded number
    assert verify_answer("Revenue was $190 billion.", set(), VALID, "stop", context=METRICS_CTX) == ["no_citation", "ungrounded_number"]


def test_one_ungrounded_figure_among_grounded_ones_fails():
    assert verify_answer("Revenue was $215.9 billion, up from $50 billion.", set(), VALID, "stop",
                         context=METRICS_CTX) == ["no_citation", "ungrounded_number"]


def test_an_uncited_answer_with_no_figure_still_fails_even_with_a_context():
    assert verify_answer("Nvidia depends on TSMC.", set(), VALID, "stop", context=METRICS_CTX) == ["no_citation"]


def test_without_a_context_the_old_rule_applies():
    assert verify_answer("Nvidia's revenue was about $215.9 billion.", set(), VALID, "stop") == ["no_citation"]


# --- review finding C1: a stray negation must not turn an uncited claim into a "refusal" ---

# (text, reasons): M1b adds ``ungrounded_number`` after ``no_citation`` when the stray claim also states a figure that
# no context line supports; the claims with no figure keep exactly the old single reason.
@pytest.mark.parametrize("text,expected", [
    ("Nvidia's largest customer is Microsoft at 19% of revenue. This isn't a small concentration.",
     ["no_citation", "ungrounded_number"]),
    ("TSMC depends on ASML for EUV tools; the filing does not specify the contract length.", ["no_citation"]),
    ("Nvidia's gross margin is 90%. Data not available for 2019.", ["no_citation", "ungrounded_number"]),
    ("Revenue grew 300% to $215.9 billion, and Nvidia plans to acquire Intel next year.",
     ["no_citation", "ungrounded_number"]),
    ("Nvidia designs GPUs. The context does not contain more detail.", ["no_citation"]),   # claim first, disclaimer second
])
def test_an_uncited_draft_with_a_stray_negation_or_an_ungrounded_claim_is_escalated(text, expected):
    assert verify_answer(text, set(), VALID, "stop", context=METRICS_CTX) == expected


@pytest.mark.parametrize("text", [
    "The provided excerpts do not state Samsung’s total annual revenue.",
    "Based on the provided context, I cannot answer this question.\n\n- The knowledge graph contains no reported metrics for Samsung.",
    "The context does not contain Samsung's revenue.",
    "**I can’t determine that** from the retrieved excerpts.",
    "I cannot answer this from the filings.",
])
def test_a_clear_refusal_up_front_is_released_uncited(text):
    assert verify_answer(text, set(), VALID, "stop", context=METRICS_CTX) == []


def test_a_refusal_that_states_figures_is_not_a_refusal():
    assert verify_answer("The context does not contain Samsung's revenue, but Nvidia earned $999 billion.", set(), VALID, "stop",
                         context=METRICS_CTX) == ["no_citation", "ungrounded_number"]


def test_an_uncited_grounded_figure_is_accepted_only_for_a_short_answer_without_percentages():
    long_answer = "Nvidia's revenue was $215.9 billion. " + "Analysts also expect further growth. " * 15
    assert verify_answer(long_answer, set(), VALID, "stop", context=METRICS_CTX) == ["no_citation"]
    assert verify_answer("Revenue was $215.9 billion, up 5%.", set(), VALID, "stop",
                         context=METRICS_CTX) == ["no_citation", "ungrounded_number"]


# --- found in the 60-question deployed run (2026-09-27): a correct refusal in wording the benchmark pattern missed ---

@pytest.mark.parametrize("text", [
    "The provided context identifies Jensen Huang as NVIDIA’s CEO but contains no earnings-call transcript or remarks, "
    "so it doesn’t establish what he said on the most recent call.",                       # U2, Luna
    "The context contains no such figure.",
    "The filings do not establish who the supplier is.",
    "The excerpts don't identify a Samsung filing.",
])
def test_refusal_pattern_accepts_contains_no_and_does_not_establish(text):
    assert REFUSAL_RE.search(text)


@pytest.mark.parametrize("text", ["Nvidia's filings establish that it depends on TSMC [a:I.1:0001].", "Nvidia's context is large.",
                                  "Revenue identified in the filing was $215.9 billion."])
def test_the_wider_refusal_pattern_still_ignores_positive_statements(text):
    assert not REFUSAL_RE.search(text)


# --- found in the second 60-question deployed run (2026-09-27): U1 "does not give Samsung's total annual revenue" ---

@pytest.mark.parametrize("text", [
    "The provided context does not give Samsung’s total annual revenue.",                        # U1, Luna
    "The context does not directly state Samsung's revenue.",
    "The filings do not have a Samsung annual report.",
    "The excerpts do not list a Samsung revenue figure.",
    "The knowledge graph does not describe that filing.",
])
def test_refusal_pattern_accepts_a_negated_give_have_list_or_describe_with_an_optional_adverb(text):
    assert REFUSAL_RE.search(text)


@pytest.mark.parametrize("text", ["Samsung gives no guidance in Qualcomm's filing [a:I.1:0001].", "Qualcomm lists Samsung as a licensee [a:I.1:0001].",
                                  "The filing has a Samsung section."])
def test_the_wider_refusal_pattern_still_ignores_positive_statements_about_giving_listing_or_having(text):
    assert not REFUSAL_RE.search(text)


# --- closing review M3: a refusal answer states no money figure (a fabricated answer that mentions "does not always give ..." fails) ---

def test_the_benchmark_refusal_predicate_rejects_an_answer_that_states_a_money_figure():
    from semigraph.eval.runner import is_refusal_answer

    assert is_refusal_answer("The provided context does not give Samsung’s total annual revenue.")
    assert is_refusal_answer("The context does not give Samsung's revenue; it reports that Samsung is among Qualcomm's customers with 10% or more of its revenue.")
    assert not is_refusal_answer("Intel's 2012 revenue was $53.3 billion; Intel does not always give quarterly guidance.")
    assert not is_refusal_answer("AMD's revenue in fiscal 2010 was $6.5 billion, and the 10-K provides no segment breakdown.")
    assert not is_refusal_answer("Nvidia's revenue was 60.9 billion USD, but the filing does not state the segment.")
