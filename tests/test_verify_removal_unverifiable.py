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
