"""eval/narration.py: the measured held-out precision per claim class and the wording it licenses (M1B_PLAN L.10)."""

import pytest

from semigraph.eval import narration as nar
from semigraph.eval.rates import FAIL, INSUFFICIENT, PASS

ROW_KEYS = {"claim", "unit", "definition", "k", "n", "precision", "ci", "gate", "threshold", "status", "licensed_wording", "unscored",
            "breakdown"}
GATED = {"removed_item": "item_drop_precision_heldout", "new_item": "item_new_precision_heldout",
         "removed_passage": "passage_drop_precision_heldout", "added_passage": "passage_added_precision_heldout"}


def item_block(tp, fp, **kw):
    return {"tp": tp, "fp": fp, "uncertain": 0, **kw}


def passage_block(tp, fp, confusion=None, n_pairs=6, unverifiable=0):
    return {"n_pairs": n_pairs, "passage": {"tp": tp, "fp": fp, "unverifiable": unverifiable}, "confusion": confusion or {}}


def levels(removed=(1, 2), new=(6, 6), older=(46, 9), newer=(47, 4), **kw):
    items = {"older_removed": {"held_out": item_block(*removed, **kw.get("removed_extra", {}))},
             "newer_new": {"held_out": item_block(*new, **kw.get("new_extra", {}))}}
    passages = {"older": {"held_out": passage_block(*older, **kw.get("older_extra", {}))},
                "newer": {"held_out": passage_block(*newer, **kw.get("newer_extra", {}))}}
    return items, passages


def table(**kw):
    return nar.build_narration(*levels(**kw))


def test_every_claim_class_has_a_row_with_the_fixed_keys():
    t = table()
    assert t["order"] == ["removed_item", "new_item", "unsettled_item", "removed_passage", "added_passage", "reworded_passage"]
    assert list(t["rows"]) == t["order"]
    assert all(set(r) == ROW_KEYS for r in t["rows"].values())
    assert t["split"] == "held_out" and t["threshold"] == 0.9 and t["min_positives"] == 5 and t["notes"]


def test_a_gated_row_carries_k_n_precision_wilson_interval_and_the_plan_threshold():
    r = table(new=(6, 6))["rows"]["new_item"]
    assert (r["k"], r["n"], r["precision"], r["gate"], r["threshold"], r["unit"]) == (6, 12, 0.5, "item_new_precision_heldout", 0.9, "item")
    assert r["ci"] == [0.253778, 0.746222]                      # the interval the report shows for the same counts


@pytest.mark.parametrize("row,kwargs,status", [
    ("removed_item", {"removed": (9, 1)}, PASS), ("removed_item", {"removed": (1, 2)}, FAIL),
    ("new_item", {"new": (6, 6)}, FAIL), ("new_item", {"new": (3, 0)}, INSUFFICIENT), ("new_item", {"new": (5, 0)}, PASS),
    ("removed_passage", {"older": (46, 9)}, FAIL), ("added_passage", {"newer": (47, 4)}, PASS),
    ("added_passage", {"newer": (4, 0)}, INSUFFICIENT)])
def test_the_status_of_a_gated_row_is_the_status_of_the_plan_rate_gate(row, kwargs, status):
    assert table(**kwargs)["rows"][row]["status"] == status


def test_a_passing_class_gets_the_factual_wording_and_any_other_status_gets_the_plans_text_changed_fallback():
    passing, failing = table(removed=(9, 1))["rows"]["removed_item"], table(removed=(1, 2))["rows"]["removed_item"]
    assert passing["licensed_wording"].startswith("No longer appears as a separate risk factor")
    assert failing["licensed_wording"].startswith("'Text changed' only") and "M1B_PLAN B" in failing["licensed_wording"]
    assert "removed" in failing["licensed_wording"] and "No longer appears as a separate" not in failing["licensed_wording"]
    insufficient = table(new=(3, 0))["rows"]["new_item"]
    assert insufficient["status"] == INSUFFICIENT and insufficient["licensed_wording"].startswith("'Text changed' only")


def test_the_passage_wording_never_says_the_company_dropped_the_risk_even_when_it_passes():
    passing = table(older=(20, 1))["rows"]["removed_passage"]
    assert passing["status"] == PASS and "wording was not found" in passing["licensed_wording"]
    assert "never: the company dropped the risk factor" in passing["licensed_wording"]


def test_unscored_predictions_are_reported_per_class_and_null_when_the_report_predates_the_count():
    t = table(removed_extra={"unlabelled_positive": 4}, older_extra={"unverifiable": 3})
    assert t["rows"]["removed_item"]["unscored"] == 4 and t["rows"]["removed_passage"]["unscored"] == 3
    assert t["rows"]["new_item"]["unscored"] is None


def test_the_passage_rows_list_the_gold_labels_of_the_sentences_the_alignment_called_removed():
    confusion = {"present": {"present": 400}, "reworded": {"removed": 12, "reworded": 9, "present": 9}, "removed": {"removed": 71}}
    r = table(older_extra={"confusion": confusion})["rows"]["removed_passage"]
    assert r["breakdown"] == {"sentences_predicted_removed_by_gold_label": {"removed": 71, "reworded": 12}}


def test_an_unsettled_item_has_no_precision_and_shows_the_gold_breakdown_instead():
    r = table(removed_extra={"uncertain": 8, "uncertain_gold_removed": 3})["rows"]["unsettled_item"]
    assert (r["precision"], r["ci"], r["gate"], r["threshold"], r["k"], r["n"]) == (None, None, None, None, None, 8)
    assert r["status"] == nar.REPORTED and r["breakdown"] == {"gold_removed": 3, "gold_still_present": 5}
    assert "not verified as removed and not verified as present" in r["licensed_wording"]
    assert table()["rows"]["unsettled_item"]["breakdown"] is None            # no gold breakdown in an older report


def test_a_gold_item_the_alignment_made_no_decision_about_is_not_an_unsettled_item():
    r = table(removed_extra={"uncertain": 8, "unpredicted": 2, "uncertain_gold_removed": 3})["rows"]["unsettled_item"]
    assert r["n"] == 6 and r["breakdown"] == {"gold_removed": 3, "gold_still_present": 3}


def test_a_reworded_passage_is_scored_on_the_older_confusion_column_in_sentences_with_the_strict_share_beside_it():
    confusion = {"present": {"present": 427, "reworded": 4}, "removed": {"removed": 71, "reworded": 1},
                 "reworded": {"present": 9, "removed": 12, "reworded": 9}}
    r = table(older_extra={"confusion": confusion})["rows"]["reworded_passage"]
    assert (r["unit"], r["n"], r["k"], r["status"], r["gate"]) == ("sentence", 14, 13, nar.REPORTED, None)
    assert r["precision"] == pytest.approx(13 / 14)
    assert r["breakdown"]["strict"]["k"] == 9 and r["breakdown"]["strict"]["precision"] == pytest.approx(9 / 14)
    assert r["breakdown"]["sentences_predicted_reworded_by_gold_label"] == {"present": 4, "removed": 1, "reworded": 9}


def test_without_held_out_sentence_gold_the_passage_rows_are_insufficient_not_perfect():
    items, passages = levels()
    passages["newer"]["held_out"] = passage_block(0, 0, n_pairs=0)
    r = nar.build_narration(items, passages)["rows"]["added_passage"]
    assert (r["n"], r["k"], r["precision"], r["ci"], r["status"]) == (0, 0, None, None, INSUFFICIENT)


def test_the_printed_table_has_one_line_per_class_plus_a_header():
    lines = nar.format_narration(table())
    assert len(lines) == 7 and all(any(k in line for line in lines) for k in nar.build_narration(*levels())["rows"])
    assert any("0.500 [0.25,0.75]" in line for line in lines)
