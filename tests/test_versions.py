"""Filing-version (supersession) rules — pure, deterministic, no LLM.

Two axes are kept apart on purpose: this module decides which FILING VERSION is
current; risk-lineage validity (Active/Deleted) stays in temporal.py.
"""

import random

import pytest

from semigraph.versions import (
    annual_effective_accessions,
    compute_filing_versions,
    current_quarterly_accession,
)


def f(acc, form, date):
    return {"accession_no": acc, "form": form, "filing_date": date}


def by_acc(versions):
    return {v.accession_no: v for v in versions}


AMD = [
    f("a-23", "10-K", "2023-02-27"), f("a-24", "10-K", "2024-01-31"), f("a-25", "10-K", "2025-02-05"),
    f("a-26", "10-K", "2026-02-04"), f("a-26A", "10-K/A", "2026-02-04"),
    f("q-may", "10-Q", "2026-05-06"), f("q-aug", "10-Q", "2026-08-05"),
]


def test_parsed_amendment_replaces_its_original_as_the_periods_effective_filing():
    v = by_acc(compute_filing_versions(AMD, annual_form="10-K", quarterly_form="10-Q"))
    assert v["a-26A"].status == "current" and v["a-26A"].is_current
    assert v["a-26"].status == "corrected" and v["a-26"].supersede_kind == "corrected"
    assert v["a-26"].superseded_by == "a-26A" and not v["a-26"].is_current


def test_unparsed_amendment_does_not_replace_the_original():
    v = by_acc(compute_filing_versions(AMD, annual_form="10-K", quarterly_form="10-Q", parsed={"a-26", "a-25"}))
    assert v["a-26"].status == "current"
    assert v["a-26A"].status == "amendment" and v["a-26A"].superseded_by is None


def test_prior_periods_are_rolled_forward_not_corrected():
    v = by_acc(compute_filing_versions(AMD, annual_form="10-K", quarterly_form="10-Q"))
    assert (v["a-25"].status, v["a-25"].supersede_kind, v["a-25"].superseded_by) == ("superseded", "rolled", "a-26A")
    assert v["a-23"].superseded_by == "a-24" and v["a-24"].superseded_by == "a-25"


def test_only_the_latest_quarterly_after_the_latest_annual_is_current():
    v = by_acc(compute_filing_versions(AMD, annual_form="10-K", quarterly_form="10-Q"))
    assert v["q-aug"].is_current
    assert (v["q-may"].status, v["q-may"].superseded_by) == ("superseded", "q-aug")


def test_a_new_annual_retires_the_quarterlies_filed_before_it():
    msft = [f("k-25", "10-K", "2025-07-30"), f("q-jan", "10-Q", "2026-01-28"),
            f("q-apr", "10-Q", "2026-04-29"), f("k-26", "10-K", "2026-07-29")]
    v = by_acc(compute_filing_versions(msft, annual_form="10-K", quarterly_form="10-Q"))
    assert v["k-26"].is_current
    assert v["q-apr"].superseded_by == "k-26" and v["q-jan"].superseded_by == "q-apr"
    assert current_quarterly_accession(list(v.values())) is None


def test_foreign_filer_without_quarterlies():
    asml = [f("f-24", "20-F", "2024-02-14"), f("f-25", "20-F", "2025-03-05"), f("f-26", "20-F", "2026-02-25")]
    v = by_acc(compute_filing_versions(asml, annual_form="20-F", quarterly_form=None))
    assert [k for k, x in v.items() if x.is_current] == ["f-26"]


def test_orphan_amendment_is_inert():
    v = by_acc(compute_filing_versions([f("z-A", "10-K/A", "2026-01-01"), f("z", "10-K", "2026-02-01")],
                                       annual_form="10-K", quarterly_form="10-Q"))
    assert v["z-A"].status == "amendment" and v["z"].status == "current"


def test_result_is_independent_of_input_order():
    base = compute_filing_versions(AMD, annual_form="10-K", quarterly_form="10-Q")
    for seed in range(5):
        shuffled = AMD[:]
        random.Random(seed).shuffle(shuffled)
        assert compute_filing_versions(shuffled, annual_form="10-K", quarterly_form="10-Q") == base


def test_effective_annuals_are_period_ordered_and_use_the_amendment():
    versions = compute_filing_versions(AMD, annual_form="10-K", quarterly_form="10-Q")
    assert annual_effective_accessions(versions) == ["a-23", "a-24", "a-25", "a-26A"]


def test_current_quarterly_accession():
    versions = compute_filing_versions(AMD, annual_form="10-K", quarterly_form="10-Q")
    assert current_quarterly_accession(versions) == "q-aug"


def test_empty_and_annual_only_inputs():
    assert compute_filing_versions([], annual_form="10-K", quarterly_form="10-Q") == []
    only = compute_filing_versions([f("k", "10-K", "2026-02-01")], annual_form="10-K", quarterly_form="10-Q")
    assert [v.is_current for v in only] == [True]


def test_unknown_forms_are_ignored_not_fatal():
    v = compute_filing_versions([f("x", "8-K", "2026-03-01"), f("k", "10-K", "2026-02-01")],
                                annual_form="10-K", quarterly_form="10-Q")
    assert [x.accession_no for x in v] == ["k"]


@pytest.mark.parametrize("bad", [{"accession_no": "x", "form": "10-K"}, {"form": "10-K", "filing_date": "2026-01-01"}])
def test_missing_fields_raise_a_clear_error(bad):
    with pytest.raises(ValueError, match="accession_no|filing_date"):
        compute_filing_versions([bad], annual_form="10-K", quarterly_form="10-Q")
