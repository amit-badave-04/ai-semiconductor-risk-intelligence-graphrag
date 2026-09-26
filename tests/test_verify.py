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


def test_an_uncited_figure_found_in_the_metrics_block_passes():
    assert verify_answer("Nvidia's revenue was about $215.9 billion.", set(), VALID, "stop", context=METRICS_CTX) == []
    assert verify_answer("Revenue was $60,922,000,000.", set(), VALID, "stop", context=METRICS_CTX) == []
    assert verify_answer("It was $60.9 billion.", set(), VALID, "stop", context=METRICS_CTX) == []


def test_an_uncited_figure_not_in_the_context_still_fails():
    assert verify_answer("Revenue was $190 billion.", set(), VALID, "stop", context=METRICS_CTX) == ["no_citation"]


def test_one_ungrounded_figure_among_grounded_ones_fails():
    assert verify_answer("Revenue was $215.9 billion, up from $50 billion.", set(), VALID, "stop", context=METRICS_CTX) == ["no_citation"]


def test_an_uncited_answer_with_no_figure_still_fails_even_with_a_context():
    assert verify_answer("Nvidia depends on TSMC.", set(), VALID, "stop", context=METRICS_CTX) == ["no_citation"]


def test_without_a_context_the_old_rule_applies():
    assert verify_answer("Nvidia's revenue was about $215.9 billion.", set(), VALID, "stop") == ["no_citation"]
