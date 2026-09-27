"""An uncited answer that only reports an EMPTY comparison is not a ``no_citation`` failure (closing review M5).

The answer prompt now says: cite the id printed beside each item you list, on that item's own line, and never attach an id to the
sentence that says a list is empty. A truthful "the text check found no risk factor that no longer appears" therefore carries no citation
when the answer lists nothing else, and the check must not punish it. It stays a failure when the context DOES list removed items (the
answer ignores them), when the answer states a money figure, and for every other check.
"""

import pytest
from test_verify_removal_negation import CONTEXTS

from semigraph.retrieval.answerer import CITE_RE, sources_from_context
from semigraph.retrieval.verify import answer_checks, failed_check_names, verify_answer


def checks_of(text, context_name):
    _, ctx, valid = CONTEXTS[context_name]()
    return answer_checks(text, set(CITE_RE.findall(text)), set(valid), ctx, sources=sources_from_context(ctx), question="q"), valid, ctx


NONE_ANSWERS = [
    "Comparing the two 10-Ks, the text check found no risk factor that no longer appears as a separate risk factor.",
    "No. The \"No longer appears as a separate risk factor\" list shows none found for this pair of filings.",
    "The comparison found no older risk factors that were removed between the two filings.",
]


@pytest.mark.parametrize("text", NONE_ANSWERS)
def test_an_uncited_empty_comparison_report_is_not_a_no_citation_failure_when_the_removed_lists_are_empty(text):
    checks, valid, ctx = checks_of(text, "none")
    assert checks.has_citation is False and checks.is_none_report is True
    assert failed_check_names(checks.as_dict()) == []
    assert "no_citation" not in verify_answer(text, set(), valid, "stop", ctx, question="q")


@pytest.mark.parametrize("text", NONE_ANSWERS)
def test_the_same_answer_still_fails_when_the_context_lists_a_removed_item_it_ignored(text):
    checks, valid, ctx = checks_of(text, "removal")
    assert checks.is_none_report is False
    assert "no_citation" in failed_check_names(checks.as_dict())


def test_an_uncited_answer_that_states_a_money_figure_is_not_a_none_report():
    text = "The text check found no risk factor that no longer appears; revenue was $215.9 billion."
    checks, _, _ = checks_of(text, "none")
    assert checks.is_none_report is False


def test_an_uncited_answer_with_no_empty_finding_is_still_a_no_citation_failure():
    checks, _, _ = checks_of("Nvidia lists several risk factors about export controls and supply constraints.", "none")
    assert checks.is_none_report is False and "no_citation" in failed_check_names(checks.as_dict())


def test_a_cited_answer_never_carries_the_flag_and_an_old_payload_without_the_key_is_unchanged():
    _, valid, _ = checks_of("x", "removal")
    cited = sorted(i for i in valid if CITE_RE.findall(f"[{i}]"))[0]
    checks, _, _ = checks_of(f"The text check found none [{cited}].", "removal")
    assert checks.has_citation and "is_none_report" not in checks.as_dict()           # emitted only when true
    assert failed_check_names({"has_citation": False, "is_refusal": False}) == ["no_citation"]
    assert failed_check_names({"has_citation": False, "is_refusal": False, "is_none_report": True}) == []


def test_the_page_treats_a_none_report_like_a_refusal_for_the_checks_it_reads():
    from pathlib import Path

    page = (Path(__file__).resolve().parents[1] / "src" / "semigraph" / "serve" / "static" / "index.html").read_text(encoding="utf-8")
    assert page.count("c.is_none_report !== true") >= 2 and "reports an empty comparison: nothing to cite" in page
