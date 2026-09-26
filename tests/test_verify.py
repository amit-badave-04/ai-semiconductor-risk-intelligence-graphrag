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
