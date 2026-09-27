"""scripts/verify_temporal.py: the alignment's drop precision/recall against the FROZEN source-text gold, per split."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# A pipeline-script test: it needs pandas (the lake fixtures) which the API image does not ship. The ``unit`` CI job runs it; the
# ``serve-shipped`` job's ``tests/test_verify*.py`` selection also matches this file by name and skips it here, with this reason.
pytest.importorskip("pandas", reason="scripts/verify_temporal.py is pipeline code; the serve-shipped dependency set has no pandas")

import verifyfix as vf  # noqa: E402
from verifyfix import PairSpec  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_temporal.py"
spec = importlib.util.spec_from_file_location("verify_temporal", SCRIPT)
vt = importlib.util.module_from_spec(spec)
sys.modules["verify_temporal"] = vt
spec.loader.exec_module(vt)

HELD_OLD, HELD_NEW = "0000000001-25-000011", "0000000001-26-987654"
DEV_OLD, DEV_NEW = "0000000002-25-000033", "0000000002-26-765432"
X_OLD, X_NEW = "0000000003-25-000044", "0000000003-26-654321"
UNPINNED = "--allow-unpinned-gold"          # the fixtures are not the two pinned gold files (gold.KNOWN_GOLD_SHA256)
TODAY = "2026-09-30"


def held(**kw):
    """8 older items: 6 gold-removed then 2 reworded; the alignment finds 5 of the 6, misses one, and wrongly drops one reworded."""
    base = dict(ticker="HLD", older_acc=HELD_OLD, newer_acc=HELD_NEW, split="held_out",
                older_gold=["removed"] * 6 + ["reworded"] * 2,
                older_pred=["removed"] * 5 + ["reworded", "removed", "reworded"])
    return PairSpec(**{**base, **kw})


def dev(**kw):
    base = dict(ticker="DEV", older_acc=DEV_OLD, newer_acc=DEV_NEW, split="development",
                older_gold=["removed", "reworded", "reworded"], older_pred=["removed", "reworded", "reworded"])
    return PairSpec(**{**base, **kw})


def run(tmp_path, specs, *extra):
    tmp_path.mkdir(parents=True, exist_ok=True)
    world = vf.build(tmp_path, specs)
    out = tmp_path / "temporal_eval.json"
    args = ["--gold", str(world["gold"]), "--alignment", str(world["alignment"]), "--items-dir", str(world["items_dir"]),
            "--sections-dir", str(world["sections_dir"]), "--out", str(out), UNPINNED, *extra]
    code = vt.main(args)
    return code, (json.loads(out.read_text(encoding="utf-8")) if out.exists() else None), world


# --- statistics -------------------------------------------------------------------------------------------------------

def test_the_wilson_interval_matches_the_textbook_value_and_handles_the_extremes():
    lo, hi = vt.wilson_interval(5, 6)
    assert lo == pytest.approx(0.4365, abs=1e-3) and hi == pytest.approx(0.9699, abs=1e-3)
    assert vt.wilson_interval(0, 0) is None
    assert vt.wilson_interval(10, 10)[1] == pytest.approx(1.0) and vt.wilson_interval(0, 10)[0] == pytest.approx(0.0)


@pytest.mark.parametrize("k,n,threshold,status", [
    (5, 6, 0.90, "FAIL"), (5, 6, 0.80, "PASS"), (3, 3, 0.80, "INSUFFICIENT-DATA"), (4, 4, 0.90, "INSUFFICIENT-DATA"),
    (0, 3, 0.80, "FAIL"), (0, 0, 0.80, "INSUFFICIENT-DATA"), (5, 5, 0.90, "PASS"), (4, 5, 0.90, "FAIL"),
    (0, 1, 0.80, "INSUFFICIENT-DATA"),          # one miss is not evidence against 0.80 (the exact tail is 0.2)
    (0, 2, 0.80, "FAIL"),                       # two misses in a row: 0.2 ** 2 = 0.04 < 0.05
    (2, 3, 0.80, "INSUFFICIENT-DATA")])
def test_a_rate_gate_never_passes_on_fewer_than_five_and_fails_early_only_on_significant_evidence(k, n, threshold, status):
    assert vt.rate_status(k, n, threshold) == status


# --- item level, per split ------------------------------------------------------------------------------------------------

def test_development_and_held_out_pairs_are_scored_separately(tmp_path):
    code, report, _ = run(tmp_path, [held(), dev()])
    assert code == 0
    ho, dv = (report["item_level"]["older_removed"][s] for s in ("held_out", "development"))
    assert (ho["tp"], ho["fp"], ho["fn"], ho["gold_positive"]) == (5, 1, 1, 6)
    assert (dv["tp"], dv["fp"], dv["fn"], dv["gold_positive"]) == (1, 0, 0, 1)
    assert ho["precision"] == pytest.approx(5 / 6) and ho["recall"] == pytest.approx(5 / 6)
    assert ho["precision_ci"][0] < 5 / 6 < ho["precision_ci"][1]


def test_false_positive_and_false_negative_ids_are_listed(tmp_path):
    _, report, _ = run(tmp_path, [held()])
    ho = report["item_level"]["older_removed"]["held_out"]
    assert ho["false_positive_ids"] == [f"{HELD_OLD}:I.1A:i006"] and ho["false_negative_ids"] == [f"{HELD_OLD}:I.1A:i005"]


def test_the_item_gates_use_the_held_out_split_only(tmp_path):
    _, report, _ = run(tmp_path, [held(), dev()])
    g = report["gates"]
    assert g["item_drop_precision_heldout"]["status"] == "FAIL" and g["item_drop_precision_heldout"]["n"] == 6
    assert g["item_drop_recall_heldout"]["status"] == "PASS" and g["item_drop_recall_heldout"]["n"] == 6


def test_a_gate_with_fewer_than_five_positives_is_insufficient_data_and_states_n(tmp_path):
    few = held(older_gold=["removed"] * 3 + ["reworded"] * 5, older_pred=["removed"] * 3 + ["reworded"] * 5)
    _, report, _ = run(tmp_path, [few])
    g = report["gates"]
    assert g["item_drop_precision_heldout"]["status"] == "INSUFFICIENT-DATA" and g["item_drop_precision_heldout"]["n"] == 3
    assert g["item_drop_recall_heldout"]["status"] == "INSUFFICIENT-DATA" and g["item_drop_recall_heldout"]["n"] == 3
    assert report["overall"] in ("INSUFFICIENT-DATA", "FAIL")


def test_the_newer_side_scores_new_items_as_the_positive_class(tmp_path):
    spec_ = held(older_gold=["reworded"] * 8, older_pred=["reworded"] * 8,
                 newer_gold=["carried"] * 6 + ["new"] * 2, newer_pred=["carried"] * 5 + ["new"] * 3)
    _, report, _ = run(tmp_path, [spec_])
    n = report["item_level"]["newer_new"]["held_out"]
    assert (n["tp"], n["fp"], n["fn"], n["gold_positive"]) == (2, 1, 0, 2)
    assert n["false_positive_ids"] == [f"{HELD_NEW}:I.1A:i005"]


def test_an_unknown_label_is_listed_never_silently_mapped_and_counts_as_uncertain(tmp_path, capsys):
    spec_ = held(older_pred=["removed"] * 5 + ["mystery", "removed", "reworded"])
    _, report, _ = run(tmp_path, [spec_])
    assert report["unknown_labels"] == {"older": {"mystery": 1}}
    ho = report["item_level"]["older_removed"]["held_out"]
    assert ho["uncertain"] == 1 and ho["fn"] == 1 and ho["false_negative_ids"] == [f"{HELD_OLD}:I.1A:i005"]
    assert "mystery" in capsys.readouterr().out


def test_uncertain_predictions_are_reported_as_a_rate_not_as_errors(tmp_path):
    spec_ = held(older_pred=["removed"] * 5 + ["uncertain", "reworded", "reworded"])
    _, report, _ = run(tmp_path, [spec_])
    ho = report["item_level"]["older_removed"]["held_out"]
    assert ho["uncertain"] == 1 and ho["uncertain_rate"] == pytest.approx(1 / 8) and ho["fp"] == 0


def test_a_pair_the_alignment_did_not_compare_is_excluded_and_named(tmp_path):
    spec_ = dev(comparable=False, reason="older filing's risk items cover only 71.0% of its risk section text", older_pred=None)
    _, report, _ = run(tmp_path, [held(), spec_])
    assert report["item_level"]["older_removed"]["development"]["n_pairs"] == 0
    excluded = report["coverage"]["excluded"]
    assert excluded == [{"pair_id": spec_.pair_id, "reason": "older filing's risk items cover only 71.0% of its risk section text"}]
    assert report["gates"]["unit_coverage"]["status"] == "PASS"


# --- coverage gate ----------------------------------------------------------------------------------------------------------

def test_a_compared_pair_under_ninety_percent_unit_coverage_fails_the_gate(tmp_path):
    _, report, _ = run(tmp_path, [held(coverage=(0.85, 0.97))])
    g = report["gates"]["unit_coverage"]
    assert g["status"] == "FAIL" and g["detail"]["below_threshold"] == [{"pair_id": held().pair_id, "older": 0.85, "newer": 0.97}]


# --- false-drop guard ---------------------------------------------------------------------------------------------------------

def test_a_removed_item_whose_headline_is_still_in_the_newer_section_is_a_false_drop(tmp_path):
    headline = vf.headline("older", 0)                      # the first removed item's headline, verbatim in the newer text
    _, report, _ = run(tmp_path, [held(newer_extra_text=headline)])
    g = report["false_drop_guard"]
    assert g["status"] == "FAIL" and g["violations"] == [{"item_id": f"{HELD_OLD}:I.1A:i000", "headline": headline,
                                                          "pair_id": held().pair_id, "score": 100.0}]
    assert report["gates"]["false_drop_guard"]["status"] == "FAIL"


def test_a_headline_glued_to_a_longer_sentence_is_still_caught_by_containment(tmp_path):
    headline = vf.headline("older", 0)
    glued = f"{headline} Then some unrelated long sentence continues here for many many many words indeed without a break."
    _, report, _ = run(tmp_path, [held(newer_extra_text=glued)])
    assert [v["item_id"] for v in report["false_drop_guard"]["violations"]] == [f"{HELD_OLD}:I.1A:i000"]


def test_no_false_drop_passes_when_removed_items_were_checked(tmp_path):
    _, report, _ = run(tmp_path, [held()])
    g = report["false_drop_guard"]
    assert g["status"] == "PASS" and g["n_checked"] == 6 and g["violations"] == []


def test_the_guard_reports_insufficient_data_when_nothing_was_removed(tmp_path):
    _, report, _ = run(tmp_path, [held(older_gold=["reworded"] * 8, older_pred=["reworded"] * 8)])
    assert report["false_drop_guard"]["status"] == "INSUFFICIENT-DATA" and report["false_drop_guard"]["n_checked"] == 0


def test_paragraph_units_without_a_headline_are_skipped_and_counted(tmp_path):
    _, report, _ = run(tmp_path, [held(paragraph=True)])
    g = report["false_drop_guard"]
    assert g["n_checked"] == 0 and g["skipped_no_headline"] == 6


# --- passages ------------------------------------------------------------------------------------------------------------------

PASSAGE_GOLD = [(0, 1, "removed"), (0, 2, "removed"), (1, 1, "present")]


def test_passages_are_scored_against_the_sentence_gold_per_split(tmp_path):
    partial = held(gold_sentences=PASSAGE_GOLD, passages=[{"kind": "removed", "item": 0, "sentence": 1}])
    _, report, _ = run(tmp_path, [partial])
    p = report["passage_level"]["older"]["held_out"]
    assert (p["sentence"]["tp"], p["sentence"]["fp"], p["sentence"]["fn"]) == (1, 0, 1)
    assert (p["passage"]["tp"], p["passage"]["fp"], p["passage"]["n_gold_runs"], p["passage"]["recalled"]) == (1, 0, 1, 0)
    g = report["gates"]["passage_drop_recall_heldout"]
    assert g["status"] == "INSUFFICIENT-DATA" and g["n"] == 1


def test_a_run_of_gold_removed_sentences_is_recalled_when_most_of_it_is_covered(tmp_path):
    full = held(gold_sentences=PASSAGE_GOLD, passages=[{"kind": "removed", "item": 0, "sentence": 1},
                                                       {"kind": "removed", "item": 0, "sentence": 2}])
    _, report, _ = run(tmp_path, [full])
    p = report["passage_level"]["older"]["held_out"]
    assert p["passage"]["recalled"] == 1 and p["passage"]["precision"] == 1.0 and p["sentence"]["recall"] == 1.0


def test_a_false_positive_passage_is_listed_by_its_sentence_id(tmp_path):
    wrong = held(gold_sentences=PASSAGE_GOLD, passages=[{"kind": "removed", "item": 1, "sentence": 1}])
    _, report, _ = run(tmp_path, [wrong])
    p = report["passage_level"]["older"]["held_out"]
    assert p["sentence"]["false_positive_ids"] == [f"{HELD_OLD}:I.1A:i001#s001"]


def test_item_relative_passage_offsets_are_refused_rather_than_scored_as_zero_recall(tmp_path, capsys):
    bad = held(gold_sentences=[(1, 1, "removed"), (1, 2, "removed")],       # item 1 does not start at 0: relative != section offsets
               passages=[{"kind": "removed", "item": 1, "sentence": 1}, {"kind": "removed", "item": 1, "sentence": 2}],
               item_relative_offsets=True)
    code, report, _ = run(tmp_path, [bad])
    assert code == 2 and report is None
    err = capsys.readouterr().err
    assert "offsets" in err and "section" in err


def test_passage_offsets_that_match_the_section_text_are_accepted(tmp_path):
    ok = held(gold_sentences=PASSAGE_GOLD, passages=[{"kind": "removed", "item": 0, "sentence": 1}])
    code, report, _ = run(tmp_path, [ok])
    assert code == 0 and report["passage_offsets"] == {"checked": 1, "matched": 1}


# --- the NVDA flagship -----------------------------------------------------------------------------------------------------------

NEEDLE_HIT = f"Sentence A of {X_OLD[-6:]} item 0"
NEEDLE_MISS = "Notified Advanced Computing"


def flagship(**kw):
    base = dict(ticker="FLG", older_acc=X_OLD, newer_acc=X_NEW, split="development", older_gold=["reworded"] * 3,
                older_pred=["reworded"] * 3, newer_gold=["carried"] * 3, newer_pred=["carried"] * 3,
                gold_sentences=[(0, 1, "removed")], passages=[{"kind": "removed", "item": 0, "sentence": 1}])
    return PairSpec(**{**base, **kw})


def test_the_named_needles_must_all_be_in_removed_passages_of_the_flagship_pair(tmp_path):
    spec_ = flagship()
    _, report, _ = run(tmp_path / "a", [spec_], "--flagship-pair", spec_.pair_id, "--needle", NEEDLE_HIT)
    g = report["gates"]["nvda_flagship_passages"]
    assert g["status"] == "PASS" and g["detail"]["must_hit"] == {NEEDLE_HIT: True}
    _, report, _ = run(tmp_path / "b", [spec_], "--flagship-pair", spec_.pair_id, "--needle", NEEDLE_HIT, "--needle", NEEDLE_MISS)
    g = report["gates"]["nvda_flagship_passages"]
    assert g["status"] == "FAIL" and g["detail"]["must_hit"] == {NEEDLE_HIT: True, NEEDLE_MISS: False}


def test_a_flagship_pair_the_alignment_did_not_produce_is_insufficient_data(tmp_path):
    _, report, _ = run(tmp_path, [flagship(older_pred=None, comparable=False, reason="not compared", passages=[])],
                       "--flagship-pair", flagship().pair_id, "--needle", NEEDLE_HIT)
    assert report["gates"]["nvda_flagship_passages"]["status"] == "INSUFFICIENT-DATA"
    assert report["gates"]["nvda_flagship_items"]["status"] == "INSUFFICIENT-DATA"


def test_the_flagship_item_check_fails_on_a_false_removed_item_or_a_missed_new_item(tmp_path):
    good = flagship(newer_gold=["carried", "carried", "new"], newer_pred=["carried", "carried", "new"])
    _, report, _ = run(tmp_path / "a", [good], "--flagship-pair", good.pair_id, "--needle", NEEDLE_HIT)
    assert report["gates"]["nvda_flagship_items"]["status"] == "PASS"
    bad = flagship(older_pred=["reworded", "removed", "reworded"], newer_gold=["carried", "carried", "new"],
                   newer_pred=["carried", "carried", "carried"])
    _, report, _ = run(tmp_path / "b", [bad], "--flagship-pair", bad.pair_id, "--needle", NEEDLE_HIT)
    g = report["gates"]["nvda_flagship_items"]
    assert g["status"] == "FAIL" and g["detail"]["false_removed"] == 1 and g["detail"]["missed_new"] == 1


# --- integrity, determinism, reporting ---------------------------------------------------------------------------------------------

def test_a_gold_whose_hash_does_not_verify_is_refused_before_anything_is_read(tmp_path, capsys):
    world = vf.build(tmp_path, [held()])
    doc = json.loads(world["gold"].read_text(encoding="utf-8"))
    doc["pairs"][f"{held().pair_id}|older"]["labels"][f"{HELD_OLD}:I.1A:i000"] = "reworded"      # tampered after the freeze
    world["gold"].write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "o.json"
    code = vt.main(["--gold", str(world["gold"]), "--alignment", str(world["alignment"]), "--items-dir", str(world["items_dir"]),
                    "--sections-dir", str(world["sections_dir"]), "--out", str(out), UNPINNED])
    assert code == 2 and "sha256" in capsys.readouterr().err and not out.exists()


def test_the_report_is_deterministic_and_names_its_inputs(tmp_path):
    world = vf.build(tmp_path, [held(), dev()])
    args = ["--gold", str(world["gold"]), "--alignment", str(world["alignment"]), "--items-dir", str(world["items_dir"]),
            "--sections-dir", str(world["sections_dir"]), UNPINNED, "--inspection-date", TODAY]
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert vt.main([*args, "--out", str(a)]) == 0 and vt.main([*args, "--out", str(b)]) == 0
    assert a.read_bytes() == b.read_bytes()
    doc = json.loads(a.read_text(encoding="utf-8"))
    frozen = json.loads(world["gold"].read_text(encoding="utf-8"))
    assert doc["inputs"]["gold_sha256"] == frozen["sha256"] and set(doc["inputs"]["alignment_sha256"]) == {
        "ZZ_decisions.parquet", "ZZ_pairs.parquet", "ZZ_passages.parquet"}
    assert [p["pair_id"] for p in doc["pairs"]] == sorted(p["pair_id"] for p in doc["pairs"])


def test_the_held_out_bias_caveat_is_in_the_report_and_the_printed_table(tmp_path, capsys):
    _, report, _ = run(tmp_path, [held()])
    assert any("biased upward" in c for c in report["caveats"])
    out = capsys.readouterr().out
    assert "biased upward" in out and "held_out" in out and "item_drop_precision_heldout" in out and "FAIL" in out


def test_strict_turns_a_failed_or_insufficient_gate_into_a_nonzero_exit(tmp_path):
    code, _, _ = run(tmp_path / "a", [held()], "--strict")
    assert code == 1
    few = held(older_gold=["removed"] * 3 + ["reworded"] * 5, older_pred=["removed"] * 3 + ["reworded"] * 5)
    code, report, _ = run(tmp_path / "b", [few], "--strict")
    assert code in (1, 3) and report["overall"] != "PASS"


def test_a_missing_alignment_directory_is_a_clear_error(tmp_path, capsys):
    world = vf.build(tmp_path, [held()])
    code = vt.main(["--gold", str(world["gold"]), "--alignment", str(tmp_path / "nope"), "--items-dir", str(world["items_dir"]),
                    "--sections-dir", str(world["sections_dir"]), "--out", str(tmp_path / "o.json"), UNPINNED])
    assert code == 2 and "alignment" in capsys.readouterr().err


def test_the_script_reads_no_graph_and_makes_no_model_call():
    src = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("neo4j", "litellm", "llm_json", "requests", "urllib"):
        assert forbidden not in src


# --- several gold files (development + held-out) -----------------------------------------------------------------------------------

def _split_gold(world, tmp_path):
    """Re-freeze the fixture gold as two files, one per split (the real layout: dev gold now, held-out gold later)."""
    doc = json.loads(world["gold"].read_text(encoding="utf-8"))
    doc.pop("sha256")
    paths = {}
    for split in ("development", "held_out"):
        part = {"kind": doc["kind"],
                "pairs": {k: v for k, v in doc["pairs"].items() if v["split"] == split},
                "sentences": {k: v for k, v in doc["sentences"].items() if v["split"] == split}}
        paths[split] = tmp_path / f"gold_{split}.json"
        vt.gold.freeze(part, paths[split])
    return paths


def test_several_gold_files_give_the_same_scores_as_one_and_each_entrys_split_decides(tmp_path):
    specs = [held(gold_sentences=PASSAGE_GOLD, passages=[{"kind": "removed", "item": 0, "sentence": 1}]), dev()]
    code, one, world = run(tmp_path / "one", specs)
    parts = _split_gold(world, tmp_path)
    out = tmp_path / "two.json"
    args = ["--gold", str(parts["development"]), "--gold", str(parts["held_out"]), "--alignment", str(world["alignment"]),
            "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]), "--out", str(out), UNPINNED]
    assert code == 0 and vt.main(args) == 0
    two = json.loads(out.read_text(encoding="utf-8"))
    for key in ("item_level", "passage_level", "gates", "pairs", "false_drop_guard", "coverage"):
        assert two[key] == one[key], key
    assert two["item_level"]["older_removed"]["held_out"]["tp"] == 5 and two["item_level"]["older_removed"]["development"]["tp"] == 1


def test_the_report_names_every_gold_file_with_its_hash(tmp_path):
    _, _, world = run(tmp_path / "one", [held(), dev()])
    parts = _split_gold(world, tmp_path)
    out = tmp_path / "two.json"
    assert vt.main(["--gold", str(parts["development"]), "--gold", str(parts["held_out"]), "--alignment", str(world["alignment"]),
                    "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]), "--out", str(out), UNPINNED]) == 0
    inputs = json.loads(out.read_text(encoding="utf-8"))["inputs"]
    on_disk = {p.name: json.loads(p.read_text(encoding="utf-8"))["sha256"] for p in parts.values()}
    assert inputs["gold_files"] == on_disk and len(inputs["gold_sha256"]) == 64


def test_one_gold_file_that_does_not_verify_stops_the_run_and_is_named(tmp_path, capsys):
    _, _, world = run(tmp_path / "one", [held(), dev()])
    parts = _split_gold(world, tmp_path)
    doc = json.loads(parts["held_out"].read_text(encoding="utf-8"))
    doc["pairs"][f"{held().pair_id}|older"]["labels"][f"{HELD_OLD}:I.1A:i000"] = "reworded"
    parts["held_out"].write_text(json.dumps(doc), encoding="utf-8")
    out = tmp_path / "bad.json"
    code = vt.main(["--gold", str(parts["development"]), "--gold", str(parts["held_out"]), "--alignment", str(world["alignment"]),
                    "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]), "--out", str(out), UNPINNED])
    assert code == 2 and "gold_held_out.json" in capsys.readouterr().err and not out.exists()


def test_a_pair_side_present_in_two_gold_files_is_refused(tmp_path, capsys):
    _, _, world = run(tmp_path / "one", [held(), dev()])
    out = tmp_path / "dup.json"
    code = vt.main(["--gold", str(world["gold"]), "--gold", str(world["gold"]), "--alignment", str(world["alignment"]),
                    "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]), "--out", str(out), UNPINNED])
    assert code == 2 and "more than one gold file" in capsys.readouterr().err and not out.exists()


def test_the_report_states_the_longest_passage_so_a_stale_alignment_is_recognisable(tmp_path):
    """An alignment built with the legacy 1200-character passage cap looks different from a tuned (450) one: say which one it is."""
    partial = held(gold_sentences=PASSAGE_GOLD, passages=[{"kind": "removed", "item": 0, "sentence": 1}])
    _, report, world = run(tmp_path, [partial])
    import pandas as pd

    frame = pd.read_parquet(world["alignment"] / "ZZ_passages.parquet")
    assert report["inputs"]["alignment_max_passage_chars"] == int(frame["text"].str.len().max())
    _, empty, _ = run(tmp_path / "e", [held()])
    assert empty["inputs"]["alignment_max_passage_chars"] is None


# --- the new-item and added-passage precision gates (M1B_PLAN H, L.3) -----------------------------------------------------------

def _new_side(tp, fp):
    """8 newer items (gold: 2 carried then 6 new): the alignment calls ``tp + fp`` of them new, tp of them right."""
    gold_labels = ["carried"] * 2 + ["new"] * 6
    pred = ["carried"] * 8
    for i in range(2, 2 + tp):
        pred[i] = "new"
    for i in range(fp):
        pred[i] = "new"
    return dict(older_gold=["reworded"] * 8, older_pred=["reworded"] * 8, newer_gold=gold_labels, newer_pred=pred)


def test_the_new_item_precision_gate_reads_the_held_out_newer_side_and_states_n(tmp_path):
    _, report, _ = run(tmp_path / "a", [held(**_new_side(tp=3, fp=2))])
    g = report["gates"]["item_new_precision_heldout"]
    assert (g["status"], g["n"], g["threshold"], g["value"]) == ("FAIL", 5, 0.90, 0.6) and g["interval"][0] < 0.6 < g["interval"][1]
    _, report, _ = run(tmp_path / "b", [held(**_new_side(tp=5, fp=0))])
    assert report["gates"]["item_new_precision_heldout"]["status"] == "PASS"


def test_the_new_item_gate_is_insufficient_under_five_predicted_new_items_and_never_read_from_development_pairs(tmp_path):
    _, report, _ = run(tmp_path / "a", [held(**_new_side(tp=3, fp=0))])
    g = report["gates"]["item_new_precision_heldout"]
    assert g["status"] == "INSUFFICIENT-DATA" and g["n"] == 3
    _, report, _ = run(tmp_path / "b", [dev(**_new_side(tp=2, fp=3))])                  # a development pair is not the gate
    assert report["gates"]["item_new_precision_heldout"]["n"] == 0


def test_a_failing_new_item_gate_fails_the_overall_verdict_although_every_other_gate_is_only_insufficient(tmp_path):
    _, report, _ = run(tmp_path, [held(**_new_side(tp=3, fp=2))])
    statuses = {n: g["status"] for n, g in report["gates"].items() if n != "item_new_precision_heldout"}
    assert "FAIL" not in statuses.values()
    assert report["overall"] == "FAIL"


ADDED_GOLD = [(i, 1, "added") for i in range(4)] + [(4, 1, "present"), (5, 1, "present")]


def _added(n_passages, **kw):
    return held(older_gold=["reworded"] * 8, older_pred=["reworded"] * 8, gold_sentences_newer=ADDED_GOLD,
                passages=[{"kind": "added", "item": i, "sentence": 1} for i in range(n_passages)], **kw)


def test_the_added_passage_precision_gate_is_scored_on_the_newer_side_of_the_held_out_sentence_gold(tmp_path):
    _, report, _ = run(tmp_path / "a", [_added(6)])                                       # 4 of 6 passages cover gold-added sentences
    g = report["gates"]["passage_added_precision_heldout"]
    assert (g["status"], g["n"], g["threshold"]) == ("FAIL", 6, 0.90) and g["value"] == pytest.approx(4 / 6)
    p = report["passage_level"]["newer"]["held_out"]["passage"]
    assert (p["tp"], p["fp"]) == (4, 2)
    _, report, _ = run(tmp_path / "b", [_added(4)])                                       # 4 of 4: too few to pass
    assert report["gates"]["passage_added_precision_heldout"]["status"] == "INSUFFICIENT-DATA"


def test_the_added_passage_gate_passes_with_five_correct_passages_and_joins_the_overall_verdict(tmp_path):
    good = held(older_gold=["reworded"] * 8, older_pred=["reworded"] * 8, gold_sentences_newer=[(i, 1, "added") for i in range(5)],
                passages=[{"kind": "added", "item": i, "sentence": 1} for i in range(5)])
    _, report, _ = run(tmp_path / "a", [good])
    assert report["gates"]["passage_added_precision_heldout"]["status"] == "PASS"
    _, failing, _ = run(tmp_path / "b", [_added(6)])
    assert failing["overall"] == "FAIL" and failing["gates"]["item_drop_precision_heldout"]["status"] != "FAIL"


def test_without_held_out_sentence_gold_the_added_passage_gate_is_insufficient_not_passed(tmp_path):
    _, report, _ = run(tmp_path, [held()])
    g = report["gates"]["passage_added_precision_heldout"]
    assert g["status"] == "INSUFFICIENT-DATA" and g["n"] == 0


# --- the narration table ---------------------------------------------------------------------------------------------------------

def test_the_report_carries_the_narration_table_and_its_gated_rows_agree_with_the_gates(tmp_path):
    _, report, _ = run(tmp_path, [held(**_new_side(tp=3, fp=2))])
    rows = report["narration"]["rows"]
    assert report["narration"]["order"] == ["removed_item", "new_item", "unsettled_item", "removed_passage", "added_passage",
                                            "reworded_passage"] and set(rows) == set(report["narration"]["order"])
    for row, gate in (("removed_item", "item_drop_precision_heldout"), ("new_item", "item_new_precision_heldout"),
                      ("removed_passage", "passage_drop_precision_heldout"), ("added_passage", "passage_added_precision_heldout")):
        assert rows[row]["gate"] == gate and rows[row]["status"] == report["gates"][gate]["status"], row
        assert rows[row]["n"] == report["gates"][gate]["n"]
    assert rows["new_item"]["licensed_wording"].startswith("'Text changed' only")


def test_the_printed_report_shows_the_narration_table_and_the_new_gates(tmp_path, capsys):
    run(tmp_path, [held()])
    out = capsys.readouterr().out
    assert "licensed wording" in out and "removed_passage" in out and "item_new_precision_heldout" in out
    assert "passage_added_precision_heldout" in out


def test_score_items_counts_predicted_removals_the_gold_cannot_verify_and_the_gold_of_the_unsettled_items():
    truth = {"a": "removed", "b": "reworded", "c": "removed", "d": "reworded"}
    pred = {"a": "removed", "b": "removed", "c": "uncertain", "d": "uncertain", "e": "removed", "f": "removed", "g": "reworded"}
    block = vt.score_items(truth, pred)
    assert block["unlabelled_positive"] == 2 and block["uncertain"] == 2 and block["uncertain_gold_removed"] == 1
    merged = vt._merge_item_blocks([block, block])
    assert merged["unlabelled_positive"] == 4 and merged["uncertain_gold_removed"] == 2


def test_a_gold_item_with_no_decision_is_uncertain_for_the_rate_but_not_an_unsettled_item():
    block = vt.score_items({"a": "removed", "b": "removed", "c": "reworded"}, {"a": "uncertain", "c": "uncertain"})     # b: no decision
    assert block["uncertain"] == 3 and block["unpredicted"] == 1 and block["uncertain_gold_removed"] == 1


def test_the_narration_counts_only_real_uncertain_labels_as_unsettled_and_notes_unknown_labels(tmp_path):
    spec_ = held(older_pred=["removed"] * 5 + ["mystery", "uncertain", "reworded"])
    _, report, _ = run(tmp_path, [spec_])
    row = report["narration"]["rows"]["unsettled_item"]
    assert row["n"] == 2 and row["breakdown"] == {"gold_removed": 1, "gold_still_present": 1}       # the unknown label counts as uncertain
    assert any("unknown_labels" in note for note in report["narration"]["notes"])


# --- each side is scored under ITS OWN split ---------------------------------------------------------------------------------------

def test_each_side_of_a_pair_is_scored_under_its_own_split(tmp_path):
    crossed = held(older_gold=["reworded"] * 8, older_pred=["reworded"] * 8, newer_gold=["carried"] * 6 + ["new"] * 2,
                   newer_pred=["carried"] * 5 + ["new"] * 3, newer_split="development")
    _, report, _ = run(tmp_path, [crossed])
    assert report["item_level"]["older_removed"]["held_out"]["n_pairs"] == 1
    assert report["item_level"]["newer_new"]["held_out"]["n_pairs"] == 0
    dv = report["item_level"]["newer_new"]["development"]
    assert (dv["n_pairs"], dv["tp"], dv["fp"]) == (1, 2, 1)
    (entry,) = report["pairs"]
    assert entry["split"] == "held_out" and entry["split_newer"] == "development"


def test_a_pair_whose_sides_share_a_split_reports_it_twice(tmp_path):
    _, report, _ = run(tmp_path, [held(), dev()])
    assert {(p["split"], p["split_newer"]) for p in report["pairs"]} == {("held_out", "held_out"), ("development", "development")}


# --- pinning the gold by constant (gold.KNOWN_GOLD_SHA256) ---------------------------------------------------------------------------------

def _run_without_flag(tmp_path, world, gold_paths=None, extra=()):
    out = tmp_path / "pinned.json"
    args = []
    for path in gold_paths or [world["gold"]]:
        args += ["--gold", str(path)]
    args += ["--alignment", str(world["alignment"]), "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]),
             "--out", str(out), *extra]
    return vt.main(args), out


def test_a_gold_that_is_not_pinned_is_refused_unless_the_flag_is_passed(tmp_path, capsys):
    _, _, world = run(tmp_path / "w", [held(), dev()])
    code, out = _run_without_flag(tmp_path, world)
    err = capsys.readouterr().err
    assert code == 2 and not out.exists()
    assert "gold.json" in err and "not pinned" in err and "--allow-unpinned-gold" in err


def test_the_flag_lets_an_unpinned_gold_through_and_the_report_records_the_comparison(tmp_path):
    _, report, world = run(tmp_path, [held(), dev()])
    pins = report["inputs"]["gold_pins"]
    assert report["inputs"]["allow_unpinned_gold"] is True and set(pins) == {"gold.json"}
    frozen = json.loads(world["gold"].read_text(encoding="utf-8"))
    assert pins["gold.json"] == {"kind": "risk_items_gold", "split": "mixed", "sha256": frozen["sha256"], "pinned_sha256": None,
                                 "status": "unpinned"}
    assert report["inputs"]["all_gold_pinned"] is False and any("not pinned" in c for c in report["caveats"])


def test_a_gold_whose_role_is_pinned_needs_no_flag_and_is_recorded_as_pinned(tmp_path, monkeypatch):
    _, _, world = run(tmp_path / "w", [held(), dev()])
    parts = _split_gold(world, tmp_path)
    for split, path in parts.items():
        monkeypatch.setitem(vt.gold.KNOWN_GOLD_SHA256, ("risk_items_gold", split), json.loads(path.read_text(encoding="utf-8"))["sha256"])
    code, out = _run_without_flag(tmp_path, world, [parts["development"], parts["held_out"]])
    report = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and report["inputs"]["allow_unpinned_gold"] is False and report["inputs"]["all_gold_pinned"] is True
    assert {p["status"] for p in report["inputs"]["gold_pins"].values()} == {"pinned"}
    assert not any("not pinned" in c for c in report["caveats"])


def test_a_role_pinned_to_another_hash_is_refused_and_says_so_even_though_the_file_verifies(tmp_path, capsys):
    _, _, world = run(tmp_path / "w", [held(), dev()])
    parts = _split_gold(world, tmp_path)                        # verifies, but (risk_items_gold, development) is pinned to the real gold
    code, out = _run_without_flag(tmp_path, world, [parts["development"], parts["held_out"]])
    err = capsys.readouterr().err
    assert code == 2 and not out.exists() and "differs from the pinned sha256" in err and "gold_development.json" in err
    code, out = _run_without_flag(tmp_path, world, [parts["development"], parts["held_out"]], extra=[UNPINNED])
    pins = json.loads(out.read_text(encoding="utf-8"))["inputs"]["gold_pins"]
    assert code == 0 and {p["status"] for p in pins.values()} == {"mismatch"}


def test_the_two_real_gold_files_are_pinned_and_load_without_the_flag():
    root = Path(__file__).resolve().parents[1] / "artifacts" / "gold"
    doc = vt.load_gold([root / "risk_items_gold.json", root / "risk_items_gold_heldout.json"])
    assert {p["status"] for p in doc["pins"].values()} == {"pinned"}
    assert set(doc["pins"]) == {"risk_items_gold.json", "risk_items_gold_heldout.json"}
    assert {p["split"] for p in doc["pins"].values()} == {"development", "held_out"}


def test_a_tampered_gold_is_refused_by_its_own_hash_before_the_pin_is_looked_at(tmp_path, capsys):
    _, _, world = run(tmp_path / "w", [held(), dev()])
    doc = json.loads(world["gold"].read_text(encoding="utf-8"))
    doc["pairs"][f"{held().pair_id}|older"]["labels"][f"{HELD_OLD}:I.1A:i000"] = "reworded"
    world["gold"].write_text(json.dumps(doc), encoding="utf-8")
    code, _ = _run_without_flag(tmp_path, world, extra=[UNPINNED])
    assert code == 2 and "sha256 does not verify" in capsys.readouterr().err


# --- the held-out inspection history (M3) ------------------------------------------------------------------------------------------------

def _inspection_run(world, out, date=TODAY, alignment=None):
    args = ["--gold", str(world["gold"]), "--alignment", str(alignment or world["alignment"]), "--items-dir", str(world["items_dir"]),
            "--sections-dir", str(world["sections_dir"]), "--out", str(out), UNPINNED, "--inspection-date", date]
    code = vt.main(args)
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_the_first_run_of_a_fresh_file_records_the_2026_09_26_inspection_and_this_run(tmp_path):
    _, _, world = run(tmp_path / "w", [held(), dev()])
    code, report = _inspection_run(world, tmp_path / "fresh.json")
    first, this = report["held_out_inspections"]
    assert code == 0 and first == vt.FIRST_HELD_OUT_INSPECTION and first["date"] == "2026-09-26"
    assert "false-positive and false-negative" in first["viewed"] and "not a blind test" in first["consequence"]
    inputs = report["inputs"]
    assert (this["date"], this["gold_files"]) == (TODAY, inputs["gold_files"])
    assert this["alignment_digest"] == vt.alignment_digest(inputs["alignment_sha256"])
    assert this["held_out_pairs"] == {"item": 1, "sentence": 0} and "held-out" in this["viewed"]


def test_a_rerun_on_the_same_inputs_is_byte_identical_and_appends_nothing(tmp_path):
    _, _, world = run(tmp_path / "w", [held(), dev()])
    out = tmp_path / "o.json"
    _inspection_run(world, out)
    before = out.read_bytes()
    _inspection_run(world, out, date="2026-10-15")            # another day, same alignment and gold
    assert out.read_bytes() == before and len(json.loads(before)["held_out_inspections"]) == 2


def test_a_run_on_a_different_alignment_appends_an_entry_and_keeps_the_earlier_ones_untouched(tmp_path):
    _, _, world = run(tmp_path / "w", [held(), dev()])
    out = tmp_path / "o.json"
    _, one = _inspection_run(world, out)
    _, _, world2 = run(tmp_path / "w2", [held(older_pred=["removed"] * 6 + ["reworded"] * 2), dev()])
    _, two = _inspection_run(world, out, date="2026-10-20", alignment=world2["alignment"])
    history = two["held_out_inspections"]
    assert history[:2] == one["held_out_inspections"] and len(history) == 3 and history[2]["date"] == "2026-10-20"
    assert history[2]["alignment_digest"] != history[1]["alignment_digest"]


def test_a_development_only_run_records_no_new_inspection(tmp_path):
    _, _, world = run(tmp_path / "w", [dev()])
    _, report = _inspection_run(world, tmp_path / "o.json")
    assert report["held_out_inspections"] == [vt.FIRST_HELD_OUT_INSPECTION]


def test_an_existing_history_is_carried_over_and_an_unreadable_file_is_never_overwritten(tmp_path, capsys):
    _, _, world = run(tmp_path / "w", [held(), dev()])
    out = tmp_path / "o.json"
    out.write_text('{"held_out_inspections": [{"date": "2026-09-27", "alignment_digest": "x", "gold_files": {}}]}', encoding="utf-8")
    _, report = _inspection_run(world, out)
    assert [e["date"] for e in report["held_out_inspections"]] == ["2026-09-26", "2026-09-27", TODAY]
    out.write_text("{not json", encoding="utf-8")
    code = vt.main(["--gold", str(world["gold"]), "--alignment", str(world["alignment"]), "--items-dir", str(world["items_dir"]),
                    "--sections-dir", str(world["sections_dir"]), "--out", str(out), UNPINNED])
    assert code == 2 and out.read_text(encoding="utf-8") == "{not json" and "inspection" in capsys.readouterr().err


def test_the_alignment_digest_depends_on_every_file_hash_and_not_on_their_order():
    a, b = {"x.parquet": "1", "y.parquet": "2"}, {"y.parquet": "2", "x.parquet": "1"}
    assert vt.alignment_digest(a) == vt.alignment_digest(b) != vt.alignment_digest({"x.parquet": "1", "y.parquet": "3"})
