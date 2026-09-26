"""Source-text gold for 'was this risk item still in the newer annual filing?': validation, consensus, freeze, scoring.

The point of this module is that a label can be REJECTED by machine: a quote must literally occur in the source
text, and a 'removed' label is contradicted if the item's own headline is still there.
"""

import json
from pathlib import Path

import pytest

from semigraph.eval import gold

OLD = [
    {"item_id": "a:I.1A:i001", "headline": "We are subject to privacy laws and statutory fines",
     "text": "We are subject to privacy laws and statutory fines. These state laws allow for statutory fines for noncompliance."},
    {"item_id": "a:I.1A:i002", "headline": "Export licence requirements for China may reduce our sales",
     "text": "Export licence requirements for China may reduce our sales. The NAC process resulted in no approvals for China."},
    {"item_id": "a:I.1A:i003", "headline": "Our stock price is volatile",
     "text": "Our stock price is volatile. Market volatility could affect the value of an investment in our stock."},
]
NEW_TEXT = ("Risk factors. We are subject to privacy laws and statutory fines. These state laws allow for statutory fines "
            "for noncompliance. Our stock price is volatile and could decline. Market volatility could affect the value "
            "of an investment in our stock. We depend on TSMC for manufacturing capacity.")
LONG_QUOTE_1 = "These state laws allow for statutory fines for noncompliance."
LONG_QUOTE_3 = "Market volatility could affect the value of an investment in our stock."


def label(item_id, kind, **kw):
    return {"item_id": item_id, "label": kind, **kw}


# --- validation ---

def test_a_quote_that_occurs_in_the_source_text_is_accepted():
    v = gold.validate_annotation([label("a:I.1A:i001", "unchanged", quote=LONG_QUOTE_1)], OLD, NEW_TEXT, side="older")
    assert v.accepted == {"a:I.1A:i001": "unchanged"} and not v.rejected


def test_normalisation_ignores_case_whitespace_and_smart_quotes():
    q = "  these STATE laws allow for statutory fines\nfor noncompliance. "
    v = gold.validate_annotation([label("a:I.1A:i001", "reworded", quote=q)], OLD, NEW_TEXT, side="older")
    assert "a:I.1A:i001" in v.accepted


def test_a_fabricated_quote_is_rejected():
    v = gold.validate_annotation([label("a:I.1A:i001", "reworded", quote="Nvidia will pay no fines whatsoever, ever.")],
                                 OLD, NEW_TEXT, side="older")
    assert not v.accepted and v.rejected[0][0] == "a:I.1A:i001" and "quote" in v.rejected[0][1]


def test_a_quote_that_is_too_short_to_prove_anything_is_rejected():
    v = gold.validate_annotation([label("a:I.1A:i003", "merged", quote="stock price")], OLD, NEW_TEXT, side="older")
    assert not v.accepted and "short" in v.rejected[0][1]


def test_removed_needs_search_terms_and_the_headline_must_really_be_absent():
    ok = gold.validate_annotation([label("a:I.1A:i002", "removed", search_terms=["NAC", "licence", "China approvals"])],
                                  OLD, NEW_TEXT, side="older")
    assert ok.accepted == {"a:I.1A:i002": "removed"}
    no_terms = gold.validate_annotation([label("a:I.1A:i002", "removed", search_terms=[])], OLD, NEW_TEXT, side="older")
    assert not no_terms.accepted and "search_terms" in no_terms.rejected[0][1]
    # item 1's headline IS in the newer text: calling it removed is contradicted by the source
    wrong = gold.validate_annotation([label("a:I.1A:i001", "removed", search_terms=["privacy", "fines", "laws"])],
                                     OLD, NEW_TEXT, side="older")
    assert not wrong.accepted and "still" in wrong.rejected[0][1]


def test_unknown_items_and_labels_are_rejected_and_unlabelled_items_are_reported():
    v = gold.validate_annotation([label("zzz", "removed", search_terms=["a", "b", "c"]),
                                  label("a:I.1A:i003", "maybe", quote=LONG_QUOTE_3)], OLD, NEW_TEXT, side="older")
    assert not v.accepted and {r[0] for r in v.rejected} == {"zzz", "a:I.1A:i003"}
    assert v.missing == ["a:I.1A:i001", "a:I.1A:i002", "a:I.1A:i003"]


def test_newer_side_labels_use_carried_and_new_against_the_older_text():
    newer = [{"item_id": "b:I.1A:i001", "headline": "We depend on TSMC for manufacturing capacity",
              "text": "We depend on TSMC for manufacturing capacity."},
             {"item_id": "b:I.1A:i002", "headline": "Our stock price is volatile and could decline",
              "text": "Our stock price is volatile and could decline. Market volatility could affect the value of an investment in our stock."}]
    older_text = " ".join(o["text"] for o in OLD)
    v = gold.validate_annotation(
        [label("b:I.1A:i001", "new", search_terms=["TSMC", "manufacturing", "capacity"]),
         label("b:I.1A:i002", "carried", quote=LONG_QUOTE_3)], newer, older_text, side="newer")
    assert v.accepted == {"b:I.1A:i001": "new", "b:I.1A:i002": "carried"}


# --- consensus and agreement ---

def test_majority_needs_a_strict_majority():
    assert gold.majority_label(["removed", "removed", "reworded"]) == "removed"
    assert gold.majority_label(["removed", "reworded", "merged"]) is None
    assert gold.majority_label(["removed"]) == "removed"


def test_aggregate_returns_consensus_disagreements_and_statistics():
    a = {"i1": "removed", "i2": "unchanged", "i3": "reworded"}
    b = {"i1": "removed", "i2": "unchanged", "i3": "merged"}
    c = {"i1": "removed", "i2": "reworded", "i3": "removed"}
    agg = gold.aggregate({"a": a, "b": b, "c": c})
    assert agg.consensus == {"i1": "removed", "i2": "unchanged"}
    assert agg.needs_adjudication == ["i3"]
    assert 0.0 < agg.pairwise_agreement < 1.0 and -1.0 <= agg.alpha <= 1.0


def test_krippendorff_alpha_matches_the_hand_computed_case_and_the_extremes():
    # two coders, four units: (A,A) (A,B) (B,B) (B,B)  -> alpha = 1 - 2 / (30/7) = 0.5333
    assert gold.krippendorff_alpha({"c1": {"u1": "A", "u2": "A", "u3": "B", "u4": "B"},
                                    "c2": {"u1": "A", "u2": "B", "u3": "B", "u4": "B"}}) == pytest.approx(0.5333, abs=1e-3)
    same = {"c1": {"u1": "A", "u2": "B"}, "c2": {"u1": "A", "u2": "B"}}
    assert gold.krippendorff_alpha(same) == pytest.approx(1.0)
    assert gold.krippendorff_alpha({"c1": {"u1": "A", "u2": "A"}, "c2": {"u1": "A", "u2": "A"}}) == pytest.approx(1.0)


def test_missing_labels_are_tolerated_in_the_statistics():
    agg = gold.aggregate({"a": {"i1": "removed", "i2": "unchanged"}, "b": {"i1": "removed"}, "c": {"i1": "removed", "i2": "unchanged"}})
    assert agg.consensus == {"i1": "removed", "i2": "unchanged"}


# --- freezing ---

def test_freeze_writes_deterministic_json_and_the_hash_detects_edits(tmp_path):
    g = {"pair": "nvda-fy25-fy26", "labels": {"i2": "removed", "i1": "unchanged"}}
    p1, p2 = tmp_path / "g1.json", tmp_path / "g2.json"
    h1 = gold.freeze(g, p1)
    h2 = gold.freeze({"labels": {"i1": "unchanged", "i2": "removed"}, "pair": "nvda-fy25-fy26"}, p2)
    assert h1 == h2 and p1.read_text(encoding="utf-8") == p2.read_text(encoding="utf-8")
    assert gold.verify_frozen(p1) is True
    doc = json.loads(p1.read_text(encoding="utf-8"))
    doc["labels"]["i1"] = "removed"
    p1.write_text(json.dumps(doc), encoding="utf-8")
    assert gold.verify_frozen(p1) is False


# --- scoring an algorithm against the gold ---

def test_scoring_reports_drop_precision_and_recall_and_treats_uncertain_separately():
    g = {"i1": "removed", "i2": "removed", "i3": "unchanged", "i4": "reworded", "i5": "merged", "i6": "removed"}
    p = {"i1": "removed", "i2": "unchanged", "i3": "removed", "i4": "reworded", "i5": "merged", "i6": "uncertain"}
    m = gold.score_predictions(g, p)
    assert (m.true_drops, m.false_drops, m.missed_drops, m.uncertain) == (1, 1, 1, 1)
    assert m.drop_precision == pytest.approx(0.5) and m.drop_recall == pytest.approx(1 / 3)
    assert m.n == 6 and m.uncertain_rate == pytest.approx(1 / 6)


def test_merged_and_reworded_count_as_present_not_dropped():
    m = gold.score_predictions({"i1": "merged", "i2": "reworded"}, {"i1": "removed", "i2": "removed"})
    assert m.false_drops == 2 and m.true_drops == 0 and m.drop_precision == 0.0


def test_scoring_with_no_predicted_drops_has_undefined_precision_reported_as_none():
    m = gold.score_predictions({"i1": "removed"}, {"i1": "unchanged"})
    assert m.drop_precision is None and m.drop_recall == 0.0


# --- the labeller packet ---

def test_packet_carries_the_items_the_full_newer_text_and_the_rules():
    packet = gold.build_packet({"older": "a", "newer": "b"}, OLD, NEW_TEXT)
    assert [i["item_id"] for i in packet["older_items"]] == [o["item_id"] for o in OLD]
    assert packet["newer_section_text"] == NEW_TEXT
    assert "removed" in packet["instructions"] and "quote" in packet["instructions"]
    assert "algorithm" not in json.dumps(packet).lower() or "never" in packet["instructions"].lower()


# --- remaining item-level edges ---

def test_an_unknown_side_and_a_duplicate_label_are_rejected_by_the_item_validator():
    with pytest.raises(ValueError, match="side"):
        gold.validate_annotation([], OLD, NEW_TEXT, side="sideways")
    dup = [label("a:I.1A:i001", "unchanged", quote=LONG_QUOTE_1), label("a:I.1A:i001", "unchanged", quote=LONG_QUOTE_1)]
    v = gold.validate_annotation(dup, OLD, NEW_TEXT, side="older")
    assert v.accepted == {"a:I.1A:i001": "unchanged"} and "duplicate" in v.rejected[0][1]


def test_majority_of_no_votes_is_none_and_freeze_refuses_a_gold_that_already_carries_a_hash(tmp_path):
    assert gold.majority_label([]) is None
    with pytest.raises(ValueError, match="sha256"):
        gold.freeze({"labels": {}, "sha256": "x"}, tmp_path / "g.json")


# --- pinning: verify_frozen trusts the hash stored inside the file, so the two frozen gold files are pinned by constant ---

REPO_GOLD = Path(__file__).resolve().parents[1] / "artifacts" / "gold"


def _sentence(split):
    return {"split": split, "labels": {}, "spans": {}}


def _gold_file(tmp_path, pairs_split, sentences_split=None, kind="risk_items_gold"):
    doc = {"kind": kind, "pairs": {"p|older": {"split": pairs_split, "labels": {}}},
           "sentences": {} if sentences_split is None else {"p|older": _sentence(sentences_split)}}
    path = tmp_path / f"{kind}_{pairs_split}.json"
    gold.freeze(doc, path)
    return path


def test_the_two_frozen_gold_files_still_hash_to_their_pinned_constants():
    for name, split in (("risk_items_gold.json", "development"), ("risk_items_gold_heldout.json", "held_out")):
        path = REPO_GOLD / name
        assert gold.verify_frozen(path)
        pin = gold.check_pinned(path)
        assert pin["status"] == "pinned" and pin["split"] == split and pin["kind"] == "risk_items_gold"
        assert pin["sha256"] == pin["pinned_sha256"] == gold.KNOWN_GOLD_SHA256[("risk_items_gold", split)]
    assert len(set(gold.KNOWN_GOLD_SHA256.values())) == 2


def test_a_file_whose_role_has_no_pin_is_unpinned_and_a_pinned_role_with_another_hash_is_a_mismatch(tmp_path):
    new_kind = gold.check_pinned(_gold_file(tmp_path, "held_out", kind="something_new"))
    assert new_kind["status"] == "unpinned" and new_kind["pinned_sha256"] is None
    reused_role = gold.check_pinned(_gold_file(tmp_path, "held_out"))          # role (risk_items_gold, held_out) is pinned to another hash
    assert reused_role["status"] == "mismatch" and reused_role["pinned_sha256"] == gold.KNOWN_GOLD_SHA256[("risk_items_gold", "held_out")]
    assert reused_role["sha256"] != reused_role["pinned_sha256"]


def test_the_split_of_a_file_comes_from_all_its_entries_and_a_mixed_file_is_never_pinned(tmp_path):
    assert gold.gold_split({"pairs": {"a": {"split": "development"}}, "sentences": {"b": {"split": "development"}}}) == "development"
    assert gold.gold_split({"pairs": {"a": {"split": "development"}}, "sentences": {"b": {"split": "held_out"}}}) == "mixed"
    assert gold.gold_split({"pairs": {}, "sentences": {}}) == "empty"
    assert gold.check_pinned(_gold_file(tmp_path, "development", "held_out"))["status"] == "unpinned"


def test_a_pin_is_matched_by_the_hash_recorded_in_the_file_so_a_pinned_hash_needs_the_real_content(tmp_path, monkeypatch):
    path = _gold_file(tmp_path, "development")
    recorded = json.loads(path.read_text(encoding="utf-8"))["sha256"]
    monkeypatch.setitem(gold.KNOWN_GOLD_SHA256, ("risk_items_gold", "development"), recorded)
    assert gold.check_pinned(path)["status"] == "pinned"
