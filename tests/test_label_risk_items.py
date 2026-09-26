"""scripts/label_risk_items.py: pair consecutive annual filings, write blind labeller packets, collect and aggregate labels."""

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "label_risk_items.py"
spec = importlib.util.spec_from_file_location("label_risk_items", SCRIPT)
lri = importlib.util.module_from_spec(spec)
sys.modules["label_risk_items"] = lri
spec.loader.exec_module(lri)

Q1 = "We are subject to privacy laws and statutory fines. These state laws allow for statutory fines for noncompliance."
Q2 = "Our stock price is volatile. Market volatility could affect the value of an investment in our stock."


def items_df():
    rows = []
    for acc, date, texts in (("acc-24", "2024-02-21", [Q1, Q2]), ("acc-25", "2025-02-26", [Q1, Q2 + " Extra."]),
                             ("acc-26", "2026-02-25", [Q1])):
        for n, t in enumerate(texts, 1):
            rows.append({"item_id": f"{acc}:I.1A:i{n:03d}", "accession_no": acc, "ticker": "NVDA", "filing_date": date,
                         "section_id": "I.1A", "headline": t.split(".")[0], "text": t, "form": "10-K"})
    rows.append({"item_id": "x-25:I.1A:i001", "accession_no": "x-25", "ticker": "AMD", "filing_date": "2025-02-05",
                 "section_id": "I.1A", "headline": "Only one filing", "text": "Only one filing.", "form": "10-K"})
    return pd.DataFrame(rows)


def sections_df():
    return pd.DataFrame([
        {"accession_no": "acc-24", "section_id": "I.1A", "text": Q1 + " " + Q2},
        {"accession_no": "acc-25", "section_id": "I.1A", "text": Q1 + " " + Q2 + " Extra."},
        {"accession_no": "acc-26", "section_id": "I.1A", "text": Q1 + " We depend on TSMC for manufacturing capacity."},
        {"accession_no": "x-25", "section_id": "I.1A", "text": "Only one filing."}])


def test_consecutive_pairs_link_neighbouring_annuals_per_ticker_in_date_order():
    pairs = lri.consecutive_pairs(items_df())
    assert [(p["older_accession"], p["newer_accession"]) for p in pairs] == [("acc-24", "acc-25"), ("acc-25", "acc-26")]
    assert pairs[0]["pair_id"] == "NVDA-acc-24-acc-25" and pairs[0]["ticker"] == "NVDA"


def test_packets_for_both_sides_carry_only_items_the_other_text_and_the_rules(tmp_path):
    pairs = lri.consecutive_pairs(items_df())
    written = lri.write_packets(pairs, items_df(), sections_df(), tmp_path)
    assert {p.name for p in written} == {f"{p['pair_id']}.{s}.packet.json" for p in pairs for s in ("older", "newer")}
    older = json.loads((tmp_path / "NVDA-acc-25-acc-26.older.packet.json").read_text(encoding="utf-8"))
    assert [i["item_id"] for i in older["older_items"]] == ["acc-25:I.1A:i001", "acc-25:I.1A:i002"]
    assert older["newer_section_text"].endswith("manufacturing capacity.") and "quote" in older["instructions"]
    data_only = json.dumps({k: v for k, v in older.items() if k != "instructions"}).lower()
    assert "lineage" not in data_only and "algorithm" not in data_only          # nothing from the system under test
    assert "algorithm output" in older["instructions"].lower()                 # and the rules say so
    newer = json.loads((tmp_path / "NVDA-acc-25-acc-26.newer.packet.json").read_text(encoding="utf-8"))
    assert [i["item_id"] for i in newer["items"]] == ["acc-26:I.1A:i001"] and newer["side"] == "newer"


def test_collect_validates_aggregates_and_reports_rejections(tmp_path):
    pairs = lri.consecutive_pairs(items_df())
    lri.write_packets(pairs, items_df(), sections_df(), tmp_path)
    pid = "NVDA-acc-25-acc-26"
    quote = "These state laws allow for statutory fines for noncompliance."
    good = [{"item_id": "acc-25:I.1A:i001", "label": "unchanged", "quote": quote},
            {"item_id": "acc-25:I.1A:i002", "label": "removed", "search_terms": ["volatile", "stock price", "market volatility"]}]
    fabricated = [{"item_id": "acc-25:I.1A:i001", "label": "unchanged", "quote": "Nothing like this sentence appears anywhere at all."},
                  {"item_id": "acc-25:I.1A:i002", "label": "removed", "search_terms": ["volatile", "stock price", "market volatility"]}]
    for name, labels in (("a", good), ("b", good), ("c", fabricated)):
        (tmp_path / f"{pid}.older.{name}.labels.json").write_text(json.dumps(labels), encoding="utf-8")
    report = lri.collect(pid, "older", tmp_path)
    assert report["consensus"] == {"acc-25:I.1A:i001": "unchanged", "acc-25:I.1A:i002": "removed"}
    assert report["rejected"]["c"][0][0] == "acc-25:I.1A:i001" and "quote" in report["rejected"]["c"][0][1]
    assert report["annotators"] == ["a", "b", "c"] and report["needs_adjudication"] == []
    assert (tmp_path / f"{pid}.older.report.json").exists()


def test_a_removed_label_for_an_item_still_in_the_newer_text_is_rejected_in_collect(tmp_path):
    pairs = lri.consecutive_pairs(items_df())
    lri.write_packets(pairs, items_df(), sections_df(), tmp_path)
    pid = "NVDA-acc-25-acc-26"
    wrong = [{"item_id": "acc-25:I.1A:i001", "label": "removed", "search_terms": ["privacy", "fines", "laws"]}]
    (tmp_path / f"{pid}.older.a.labels.json").write_text(json.dumps(wrong), encoding="utf-8")
    report = lri.collect(pid, "older", tmp_path)
    assert report["consensus"] == {} and "still" in report["rejected"]["a"][0][1]
    assert report["unlabelled"] == ["acc-25:I.1A:i001", "acc-25:I.1A:i002"]


def test_development_pairs_are_flagged_so_held_out_pairs_stay_untouched():
    assert lri.split_of("NVDA-0001045810-25-000023-0001045810-26-000021") == "development"
    assert lri.split_of("NVDA-acc-24-acc-25") == "held_out"


QUALITY = {"acc-24": {"low_coverage": False, "section_suspect": False, "coverage": 0.96},
           "acc-25": {"low_coverage": True, "section_suspect": False, "coverage": 0.71},
           "acc-26": {"low_coverage": False, "section_suspect": False, "coverage": 0.96}}


def test_pairs_with_an_untrustworthy_side_are_marked_not_compared_and_get_no_packets(tmp_path):
    pairs = lri.consecutive_pairs(items_df(), QUALITY)
    assert [(p["comparable"], p["not_compared_reason"] is None) for p in pairs] == [(False, False), (False, False)]
    assert "older" in pairs[1]["not_compared_reason"] or "71" in pairs[1]["not_compared_reason"]
    assert lri.write_packets(pairs, items_df(), sections_df(), tmp_path) == []


def test_without_quality_data_pairs_stay_comparable_for_backwards_compatibility():
    assert all(p["comparable"] for p in lri.consecutive_pairs(items_df()))


# --- the risk section is chosen by the items' section id (the lake holds several sections per accession) ---------

def with_business_first(sections):
    business = pd.DataFrame([{"accession_no": a, "section_id": "I.1", "text": f"Item 1. Business text of {a}."}
                             for a in sections["accession_no"].unique()])
    return pd.concat([business, sections], ignore_index=True)


def test_packets_carry_the_risk_section_of_each_filing_not_the_first_section_listed(tmp_path):
    sections = with_business_first(sections_df())
    lri.write_packets(lri.consecutive_pairs(items_df()), items_df(), sections, tmp_path)
    older = json.loads((tmp_path / "NVDA-acc-25-acc-26.older.packet.json").read_text(encoding="utf-8"))
    newer = json.loads((tmp_path / "NVDA-acc-25-acc-26.newer.packet.json").read_text(encoding="utf-8"))
    assert older["newer_section_text"].endswith("manufacturing capacity.") and "Business" not in older["newer_section_text"]
    assert newer["other_section_text"].startswith(Q1) and "Business" not in newer["other_section_text"]


def test_the_risk_section_is_a_unique_lookup_or_a_loud_error():
    sections = with_business_first(sections_df())
    assert lri._risk_section_text(items_df(), sections, "acc-24") == Q1 + " " + Q2
    with pytest.raises(KeyError, match="no section text"):
        lri._risk_section_text(items_df(), sections[sections["section_id"] != "I.1A"], "acc-24")
    mixed = items_df()
    mixed.loc[mixed["item_id"] == "acc-24:I.1A:i001", "section_id"] = "II.7"
    with pytest.raises(KeyError, match="which section"):
        lri._risk_section_text(mixed, sections, "acc-24")


def test_collect_reports_an_unreadable_label_file_by_name(tmp_path):
    lri.write_packets(lri.consecutive_pairs(items_df()), items_df(), sections_df(), tmp_path)
    (tmp_path / "NVDA-acc-25-acc-26.older.a.labels.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError, match="a.labels.json is not valid JSON"):
        lri.collect("NVDA-acc-25-acc-26", "older", tmp_path)
    (tmp_path / "NVDA-acc-25-acc-26.older.a.labels.json").write_text('{"item_id": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="JSON list"):
        lri.collect("NVDA-acc-25-acc-26", "older", tmp_path)


def test_newer_side_packets_carry_their_own_coherent_instructions(tmp_path):
    pairs = lri.consecutive_pairs(items_df())
    lri.write_packets(pairs, items_df(), sections_df(), tmp_path)
    newer = json.loads((tmp_path / "NVDA-acc-25-acc-26.newer.packet.json").read_text(encoding="utf-8"))["instructions"]
    assert "carried" in newer and "new:" in newer and "OLDER text" in newer
    assert "unchanged" not in newer and "merged" not in newer and "removed" not in newer      # the older-side vocabulary
    older = json.loads((tmp_path / "NVDA-acc-25-acc-26.older.packet.json").read_text(encoding="utf-8"))["instructions"]
    assert "unchanged" in older and "removed" in older and "carried" not in older
