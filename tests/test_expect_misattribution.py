"""The ``not_company_disclosure`` guard: an answer must not present a Federal Register rule as the company's own disclosure.

The live audit found the model narrating keyword-linked BIS rule titles as Nvidia's disclosure framing. The guard is a
deliberately PRECISION-first heuristic (a false alarm would mark a correct answer wrong), so it only looks at clauses
that cite an ``[fr:...]`` id and only when nothing in the sentence cites a filing passage; everything it cannot see (an
attribution with a chunk citation, or with no citation) is the judge's job, which is why the probes are
mechanical-plus-judge questions. Both shapes are pinned below.
"""

import pytest

from semigraph.eval.expect import check_expectation, misattributed_sentences

FR = "[fr:2025-19001]"
CHUNK = "[0000002488-26-000018:I.1A:0020]"
NV = ["NVIDIA", "Nvidia"]


@pytest.mark.parametrize("sentence", [
    "NVIDIA disclosed the affiliates rule in its 10-K %s." % FR,
    "NVIDIA reported that BIS expanded end-user controls to affiliates %s." % FR,
    "According to NVIDIA's annual report, the rule applies to 50%%-owned affiliates %s." % FR,
    "The rule was acknowledged by Nvidia in its filing %s." % FR,
    "Nvidia's 10-K lists the BIS rule %s." % FR,
    "Nvidia stated that it is affected by the expansion of end-user controls %s." % FR,
    "The rule is a BIS action, but NVIDIA disclosed it in its 10-K %s." % FR,
])
def test_a_sentence_that_attributes_a_cited_rule_to_the_company_is_flagged(sentence):
    assert misattributed_sentences(sentence, NV) == [sentence]


@pytest.mark.parametrize("sentence", [
    "The rule is a BIS action published in the Federal Register %s." % FR,
    "NVIDIA's filings do not mention the rule; it is a BIS action %s." % FR,
    "This is a Federal Register rule %s, not something NVIDIA disclosed." % FR,
    "NVIDIA has not disclosed this rule in any filing (the rule itself is %s)." % FR,
    "Neither NVIDIA's 10-K nor its 10-Qs mention the rule %s." % FR,
    "No NVIDIA filing mentions the rule %s." % FR,
    "NVIDIA's 10-K discusses export controls generally, while the BIS rule %s is a separate action." % FR,
    "NVIDIA's filings say nothing about the affiliates rule %s." % FR,
])
def test_an_honest_sentence_is_not_flagged(sentence):
    assert misattributed_sentences(sentence, NV) == []


def test_a_sentence_that_also_cites_a_filing_passage_is_not_flagged():
    """The 'the filing does mention it' answer must pass: the company genuinely disclosed the rule's subject."""
    s = "AMD's 10-K discusses the AI Diffusion Rule %s, which BIS published %s." % (CHUNK, "[fr:2025-00636]")
    assert misattributed_sentences(s, ["AMD"]) == []


def test_an_attribution_without_an_fr_citation_is_out_of_scope_for_the_guard():
    """Documented limit: the judge, not the guard, catches 'NVIDIA disclosed the rule [chunk]' or an uncited claim."""
    assert misattributed_sentences("NVIDIA disclosed the affiliates rule in its 10-K.", NV) == []
    assert misattributed_sentences("NVIDIA disclosed the affiliates rule %s." % CHUNK, NV) == []


def test_another_company_named_in_the_sentence_is_not_the_probed_company():
    assert misattributed_sentences("Samsung disclosed the rule %s." % FR, NV) == []


def test_a_citation_after_the_full_stop_belongs_to_the_sentence_before_it():
    assert misattributed_sentences("NVIDIA disclosed the affiliates rule in its 10-K. %s" % FR, NV) != []


def test_bullets_and_paragraphs_are_checked_separately_and_only_the_offender_is_returned():
    honest = "- The rule is a BIS action %s." % FR
    bad = "- NVIDIA disclosed the rule %s." % FR
    assert misattributed_sentences(f"{honest}\n{bad}\nNothing else.", NV) == [bad]


def test_the_company_match_is_whole_word_and_case_insensitive():
    assert misattributed_sentences("nvidia disclosed the rule %s." % FR, NV) != []
    assert misattributed_sentences("The rule is a BIS action; the NVIDIAN research group disclosed nothing %s." % FR, NV) == []


def test_the_key_is_a_valid_expectation_on_its_own():
    expect = {"not_company_disclosure": NV}
    assert check_expectation(expect, "The rule is a BIS action %s. NVIDIA's filings do not mention it." % FR)
    assert not check_expectation(expect, "NVIDIA disclosed the affiliates rule in its 10-K %s." % FR)


def test_the_key_combines_with_the_other_keys():
    expect = {"not_company_disclosure": NV, "any_of": ["Federal Register"]}
    assert check_expectation(expect, "It is a Federal Register rule %s." % FR)
    assert not check_expectation(expect, "It is a BIS rule %s." % FR)


@pytest.mark.parametrize("bad", [True, [], "NVIDIA", [1], None])
def test_a_malformed_company_list_is_an_error_not_a_silent_pass(bad):
    with pytest.raises(ValueError, match="not_company_disclosure"):
        check_expectation({"not_company_disclosure": bad}, "anything")


# --- X3 (deployed run): a limitation sentence about the SOURCE is not an attribution, however many times it names the company ---

@pytest.mark.parametrize("sentence", [
    "The supplied context does not include an Intel filing passage describing what Intel said about the revocation, so "
    "Intel’s specific disclosure cannot be established from it. %s" % FR,
    "The context does not include any Intel filing that describes the rule %s." % FR,
    "The filings do not contain an Intel statement about the revocation %s." % FR,
])
def test_a_source_limitation_that_names_the_company_is_not_an_attribution(sentence):
    assert misattributed_sentences(sentence, ["Intel"]) == []


@pytest.mark.parametrize("sentence", [
    "The context does not mention the date, but Intel disclosed the revocation in its 10-K %s." % FR,      # contrast splits it
    "The context does not mention the date; Intel disclosed the revocation in its 10-K %s." % FR,
    "The context does not mention the date, and Intel’s 10-K lists the rule %s." % FR,                     # a new statement after the comma
    "Intel disclosed the revocation in its 10-K, which the context does not mention %s." % FR,               # the negation comes AFTER
])
def test_a_limitation_does_not_excuse_a_later_positive_attribution(sentence):
    assert misattributed_sentences(sentence, ["Intel"]) == [sentence]


# --- X1 (second deployed run): "not as part of Nvidia's own filings" is a negation four words before the company ---

@pytest.mark.parametrize("sentence", [
    'The context shows a Federal Register rule titled "Expansion of End-User Controls", linked to Nvidia only by keyword match, '
    "not as part of Nvidia's own filings %s." % FR,
    "This is a Federal Register rule, not from Nvidia's 10-K %s." % FR,
    "The rule is a BIS action and not part of Nvidia's annual report %s." % FR,
])
def test_a_negation_up_to_four_words_before_the_company_cancels_the_attribution(sentence):
    assert misattributed_sentences(sentence, NV) == []


@pytest.mark.parametrize("sentence", [
    "The rule is a BIS action, but in fact as part of Nvidia's own filings it is listed %s." % FR,
    "Not every rule is covered, yet Nvidia's 10-K lists this one %s." % FR,
    "Nvidia's 10-K, which is not short, lists the BIS rule %s." % FR,
])
def test_a_negation_that_is_not_about_the_attribution_does_not_excuse_it(sentence):
    assert misattributed_sentences(sentence, NV) == [sentence]


# --- closing review M3: a wider negation window and the source-limit scope must not excuse a real attribution ---

@pytest.mark.parametrize("sentence", [
    "It is not clear exactly how NVIDIA reported the new policy in its filing %s." % FR,
    "The context does not show the exact date NVIDIA disclosed the rule in its 10-K %s." % FR,
    "The context does not list every rule, but NVIDIA disclosed this one in its 10-K %s." % FR,
])
def test_a_negation_that_is_not_about_the_company_or_a_presupposed_disclosure_does_not_excuse_the_attribution(sentence):
    assert misattributed_sentences(sentence, NV) == [sentence]


@pytest.mark.parametrize("sentence", [
    'The rule, linked to Nvidia only by keyword match, is not part of Nvidia\'s own filings %s.' % FR,
    "This is a Federal Register rule, not from Nvidia's 10-K %s." % FR,
    "The rule is a BIS action, not in Nvidia's annual report %s." % FR,
    "The supplied context does not include an Intel filing passage describing what Intel said about the revocation %s." % FR,
])
def test_a_negation_directly_before_the_filing_phrase_still_cancels_the_attribution(sentence):
    company = ["Intel"] if "Intel" in sentence else NV
    assert misattributed_sentences(sentence, company) == []


# --- second closing review: the source-limit exception needs a NON-presupposing company span (an indefinite "an Intel filing", "what Intel said") ---

@pytest.mark.parametrize("sentence", [
    "The context does not show the rule text that NVIDIA's 10-K discusses at length %s." % FR,
    "The filings do not say more than NVIDIA reports in its 10-K about the rule %s." % FR,
    "The context does not go beyond what NVIDIA says in its annual report %s." % FR,
    "The context cannot establish more than that NVIDIA's annual report discusses this BIS rule %s." % FR,
])
def test_a_source_limit_does_not_cancel_an_attribution_the_sentence_presupposes(sentence):
    assert misattributed_sentences(sentence, NV) == [sentence]
