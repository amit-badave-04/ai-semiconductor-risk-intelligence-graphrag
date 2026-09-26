"""graph/item_pairs.py: which consecutive annual filings are paired for risk-item alignment, and their risk-section text."""

import pandas as pd
import pytest

from semigraph.graph.item_pairs import consecutive_pairs, risk_section_text


def items_df():
    rows = []
    for ticker, acc, date in (("NVDA", "n-24", "2024-02-21"), ("NVDA", "n-25", "2025-02-26"), ("NVDA", "n-26", "2026-02-25"),
                              ("AMD", "a-25", "2025-02-05"), ("AMD", "a-26", "2026-02-04"), ("MU", "m-26", "2026-10-07")):
        for n in (1, 2):
            rows.append({"item_id": f"{acc}:I.1A:i{n:03d}", "accession_no": acc, "ticker": ticker, "filing_date": date,
                         "section_id": "I.1A", "headline": f"h{n}", "text": f"h{n}. body {acc}"})
    return pd.DataFrame(rows)


GOOD = {"low_coverage": False, "section_suspect": False, "coverage": 0.96}
QUALITY = {"n-24": GOOD, "n-25": GOOD, "n-26": GOOD, "a-25": {**GOOD, "low_coverage": True, "coverage": 0.71},
           "a-26": GOOD, "m-26": GOOD}


def test_neighbouring_annuals_are_paired_per_ticker_in_filing_date_order_and_a_lone_filing_has_no_pair():
    pairs = consecutive_pairs(items_df())
    assert [(p["ticker"], p["older_accession"], p["newer_accession"]) for p in pairs] == [
        ("AMD", "a-25", "a-26"), ("NVDA", "n-24", "n-25"), ("NVDA", "n-25", "n-26")]
    assert pairs[1]["pair_id"] == "NVDA-n-24-n-25"
    assert (pairs[1]["older_date"], pairs[1]["newer_date"]) == ("2024-02-21", "2025-02-26")


def test_every_pair_is_comparable_when_no_quality_record_set_is_given():
    assert all(p["comparable"] and p["not_compared_reason"] is None for p in consecutive_pairs(items_df()))


def test_a_pair_with_an_untrustworthy_side_is_not_compared_and_says_which_side_and_why():
    by_id = {p["pair_id"]: p for p in consecutive_pairs(items_df(), QUALITY)}
    assert by_id["NVDA-n-25-n-26"]["comparable"] is True and by_id["NVDA-n-25-n-26"]["not_compared_reason"] is None
    amd = by_id["AMD-a-25-a-26"]
    assert amd["comparable"] is False and amd["not_compared_reason"].startswith("older filing") and "71.0%" in amd["not_compared_reason"]


def test_a_filing_with_no_quality_record_makes_its_pair_not_compared():
    quality = {k: v for k, v in QUALITY.items() if k != "n-24"}
    pair = next(p for p in consecutive_pairs(items_df(), quality) if p["pair_id"] == "NVDA-n-24-n-25")
    assert pair["comparable"] is False and "no quality record" in pair["not_compared_reason"]


def test_pairs_come_back_sorted_by_pair_id_whatever_the_row_order():
    shuffled = items_df().sample(frac=1.0, random_state=3).reset_index(drop=True)
    assert consecutive_pairs(shuffled) == consecutive_pairs(items_df())


def sections_df():
    return pd.DataFrame([
        {"accession_no": "n-25", "section_id": "I.1", "text": "Business text"},
        {"accession_no": "n-25", "section_id": "I.1A", "text": "The risk section."},
    ])


def test_the_risk_section_is_the_one_the_filings_own_items_were_cut_from():
    assert risk_section_text(items_df(), sections_df(), "n-25") == "The risk section."


def test_a_missing_section_or_an_ambiguous_section_id_is_a_loud_error():
    with pytest.raises(KeyError, match="no section text"):
        risk_section_text(items_df(), sections_df()[sections_df()["section_id"] != "I.1A"], "n-25")
    mixed = items_df()
    mixed.loc[mixed["item_id"] == "n-25:I.1A:i001", "section_id"] = "II.7"
    with pytest.raises(KeyError, match="which section"):
        risk_section_text(mixed, sections_df(), "n-25")
