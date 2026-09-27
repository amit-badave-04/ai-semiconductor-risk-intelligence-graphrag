"""``unverifiable for removal`` says NOTHING was verified: it is not a removal claim (third deployed run, T5 and T8).

The answer prompt tells the model to say that a "Not matched" item "could not be verified"; the model words the empty case as
"...so there are no older risk factors flagged as unverifiable for removal either", where the noun "removal" used to look like a
removal claim. A positive claim is unaffected, and the same words with a verified removal stay a claim.
"""

import pytest
from test_verify_removal_negation import claims


def flagged(text: str, context_name: str = "none") -> list[str]:
    return list(claims(context_name, text))


@pytest.mark.parametrize("text", [
    "No \"Not matched\" list is shown for this comparison, so there are no older risk factors flagged as unverifiable for removal either.",
    "No \"Not matched\" list is shown, so no older risk factors are flagged as unverifiable for removal either.",
    "Nothing is unverified for removal in this comparison.",
    "The text check left no older risk factor not verified for removal.",
    "So none of the older risk factors is unverifiable for removal.",
])
def test_a_statement_that_nothing_is_unverified_for_removal_is_not_a_removal_claim(text):
    assert flagged(text) == []


@pytest.mark.parametrize("text", [
    "Nvidia removed the indebtedness risk factor.",
    "The indebtedness risk factor was flagged for removal and then removed.",
    "Nvidia dropped the export risk factor after the review, which found it unverifiable.",
])
def test_the_same_words_do_not_excuse_a_positive_claim(text):
    assert flagged(text), text


# --- run 4 (T5, T8): "beyond the removal question" names the QUESTION, it does not say anything was removed ---

@pytest.mark.parametrize("text", [
    "Beyond the removal question: the comparison did find at least 32 risk factors reworded (still disclosed, wording changed).",
    "For context beyond the removal question: at least one risk factor found no matching text in the earlier filing, so it is new or restructured.",
    "But since the question specifically asks about removal, the answer is: the text check found none.",
    "Regarding the removal part of the question, the text check found no risk factor that no longer appears.",
])
def test_a_phrase_that_names_the_removal_question_is_not_a_removal_claim(text):
    assert flagged(text) == []


@pytest.mark.parametrize("text", [
    "Nvidia answered the question by confirming the removal of the indebtedness risk factor.",
    "The removal question is settled: the indebtedness risk factor was removed.",
])
def test_naming_the_question_does_not_excuse_a_positive_claim_in_the_same_sentence(text):
    assert flagged(text), text
