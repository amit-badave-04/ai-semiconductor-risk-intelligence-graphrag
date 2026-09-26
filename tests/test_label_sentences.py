"""scripts/label_risk_items.py, sentence layer: sentence packets, collect-sentences, and freeze over both gold layers."""

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from semigraph.eval import gold
from semigraph.parsing import risk_item_quality

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "label_risk_items.py"
spec = importlib.util.spec_from_file_location("label_risk_items_sent", SCRIPT)
lri = importlib.util.module_from_spec(spec)
sys.modules["label_risk_items_sent"] = lri
spec.loader.exec_module(lri)

OLDER, NEWER = "acc-25", "acc-26"
PID = f"NVDA-{OLDER}-{NEWER}"
OLD_I0 = ("Export licence requirements for China may reduce our sales. The NAC process resulted in no approvals for China at "
          "all this year. We transitioned some operations out of China and Hong Kong during the year.")
OLD_I1 = ("Our stock price is volatile and may decline. Market volatility could affect the value of an investment in our "
          "stock significantly.")
OLD_I2 = ("We depend on third parties for wafer supply. These suppliers could fail to meet our demand forecasts in the "
          "coming quarters.")
NAC_REWORD_QUOTE = "The NAC process resulted in very few approvals for China this year."
NEW_J0 = ("Export licence requirements for China may reduce our sales. New H20 licensing rules apply to shipments to China "
          "since April. " + NAC_REWORD_QUOTE)
NEW_J1 = OLD_I1
EXPORT_QUOTE = "Export licence requirements for China may reduce our sales."
H20_QUOTE = "New H20 licensing rules apply to shipments to China since April."
VOLATILITY_QUOTE = "Market volatility could affect the value of an investment in our stock significantly."


def item_id(acc, n):
    return f"{acc}:I.1A:i{n:03d}"


def sid(acc, n, s):
    return f"{item_id(acc, n)}#s{s:03d}"


def build_frames(older_texts=(OLD_I0, OLD_I1, OLD_I2), newer_texts=(NEW_J0, NEW_J1)):
    """items + sections frames; every accession also has a Business section listed FIRST (as in the real lake)."""
    item_rows, section_rows = [], []
    for acc, date, texts in ((OLDER, "2025-02-26", older_texts), (NEWER, "2026-02-25", newer_texts)):
        section, cursor = "", 0
        for n, text in enumerate(texts):
            item_rows.append({"item_id": item_id(acc, n), "accession_no": acc, "ticker": "NVDA", "filing_date": date,
                              "section_id": "I.1A", "headline": text.split(".")[0], "text": text, "form": "10-K",
                              "char_start": cursor, "char_end": cursor + len(text)})
            section += text + "\n\n"
            cursor += len(text) + 2
        section_rows.append({"accession_no": acc, "section_id": "I.1", "text": "Item 1 Business. We design chips."})
        section_rows.append({"accession_no": acc, "section_id": "I.1A", "text": section})
    return pd.DataFrame(item_rows), pd.DataFrame(section_rows)


def item_report_full(side, consensus):
    return {"pair_id": PID, "side": side, "split": "held_out", "annotators": ["a"], "consensus": consensus,
            "needs_adjudication": [], "unlabelled": [], "pairwise_agreement": 1.0, "alpha": 1.0, "rejected": {}, "votes": {}}


def write_item_report(out, side, consensus):
    (out / f"{PID}.{side}.report.json").write_text(json.dumps(item_report_full(side, consensus)), encoding="utf-8")


OLDER_CONSENSUS = {item_id(OLDER, 0): "reworded", item_id(OLDER, 1): "unchanged", item_id(OLDER, 2): "removed"}
NEWER_CONSENSUS = {item_id(NEWER, 0): "carried", item_id(NEWER, 1): "carried"}


def pairs_for(items, quality=None):
    return lri.consecutive_pairs(items, quality)


def make_packets(tmp_path, **kw):
    items, sections = build_frames()
    write_item_report(tmp_path, "older", OLDER_CONSENSUS)
    write_item_report(tmp_path, "newer", NEWER_CONSENSUS)
    written = lri.write_sentence_packets(pairs_for(items), items, sections, tmp_path, **kw)
    return items, sections, written


GOOD_OLDER = [
    {"sentence_id": sid(OLDER, 0, 0), "label": "present", "quote": EXPORT_QUOTE},
    {"sentence_id": sid(OLDER, 0, 1), "label": "removed", "search_terms": ["NAC process", "approvals for China", "no approvals"]},
    {"sentence_id": sid(OLDER, 0, 2), "label": "removed", "search_terms": ["Hong Kong", "transitioned some operations", "operations out"]},
    {"sentence_id": sid(OLDER, 1, 0), "label": "present", "quote": OLD_I1.split(".")[0] + "."},
    {"sentence_id": sid(OLDER, 1, 1), "label": "present", "quote": VOLATILITY_QUOTE}]


def write_labels(out, side, name, labels):
    (out / f"{PID}.{side}.sent.{name}.labels.json").write_text(json.dumps(labels), encoding="utf-8")


# --- sentence packets --------------------------------------------------------------------------------------------

def test_sentence_packets_refuse_when_the_item_level_reports_are_missing(tmp_path):
    items, sections = build_frames()
    with pytest.raises(lri.MissingItemReports) as err:
        lri.write_sentence_packets(pairs_for(items), items, sections, tmp_path)
    message = str(err.value)
    assert f"{PID}.older.report.json" in message and f"{PID}.newer.report.json" in message
    assert "collect" in message and not list(tmp_path.iterdir())            # nothing written on a refusal
    write_item_report(tmp_path, "older", OLDER_CONSENSUS)
    with pytest.raises(lri.MissingItemReports, match="newer"):
        lri.write_sentence_packets(pairs_for(items), items, sections, tmp_path)
    assert lri.write_sentence_packets(pairs_for(items), items, sections, tmp_path, sides=("older",))


def test_sentence_packets_are_written_for_both_sides_with_a_separate_sampling_manifest(tmp_path):
    _, _, written = make_packets(tmp_path)
    assert {p.name for p in written} == {f"{PID}.{s}.sent.{kind}.json" for s in ("older", "newer") for kind in ("packet", "sample")}
    packet = json.loads((tmp_path / f"{PID}.older.sent.packet.json").read_text(encoding="utf-8"))
    assert set(packet) == {"pair", "side", "items", "other_section_text", "instructions"}
    assert packet["pair"] == {"pair_id": PID, "ticker": "NVDA", "split": "held_out", "side": "older"}
    assert [i["item_id"] for i in packet["items"]] == [item_id(OLDER, 0), item_id(OLDER, 1)]      # the removed item i002 is not sampled
    assert [s["sentence_id"] for s in packet["items"][0]["sentences"]] == [sid(OLDER, 0, n) for n in range(3)]
    assert packet["other_section_text"] == NEW_J0 + "\n\n" + NEW_J1 + "\n\n"
    data_only = json.dumps({k: v for k, v in packet.items() if k != "instructions"}).lower()
    for leaked in ("consensus", "reworded", "unchanged", "removed", "seed", "eligible", "algorithm", "lineage"):
        assert leaked not in data_only
    newer = json.loads((tmp_path / f"{PID}.newer.sent.packet.json").read_text(encoding="utf-8"))
    assert newer["side"] == "newer" and newer["other_section_text"].startswith("Export licence") and "OLDER" in newer["instructions"]
    assert "NAC process" in newer["other_section_text"]                                         # the OLDER text


def test_the_manifest_records_the_seed_the_flagship_and_every_sentence_offset(tmp_path):
    make_packets(tmp_path, seed=5, k=6, max_sentences=100)
    manifest = json.loads((tmp_path / f"{PID}.older.sent.sample.json").read_text(encoding="utf-8"))
    assert manifest["seed"] == 5 and manifest["rng_key"] == f"5|{PID}|older" and manifest["k"] == 6
    assert manifest["flagship_item_id"] == item_id(OLDER, 0) and manifest["dropped_items"] == [] and manifest["n_sentences"] == 5
    first = manifest["items"][0]["sentences"][0]
    assert (first["section_start"], first["section_end"]) == (first["start"], first["end"])       # item 0 starts the section
    assert manifest["items"][1]["char_start"] == len(OLD_I0) + 2
    assert manifest["pair"]["pair_id"] == PID


def test_the_cap_and_k_options_reach_the_sampler(tmp_path):
    make_packets(tmp_path, k=1, max_sentences=320)
    manifest = json.loads((tmp_path / f"{PID}.older.sent.sample.json").read_text(encoding="utf-8"))
    assert [i["item_id"] for i in manifest["items"]] == [item_id(OLDER, 0)]


def test_a_pair_that_is_not_comparable_gets_no_sentence_packets_and_needs_no_reports(tmp_path):
    items, sections = build_frames()
    quality = {OLDER: {"low_coverage": True, "coverage": 0.5}, NEWER: {"low_coverage": False, "coverage": 0.97}}
    assert lri.write_sentence_packets(pairs_for(items, quality), items, sections, tmp_path) == []


def test_item_offsets_that_do_not_match_the_section_text_fail_loudly(tmp_path):
    items, sections = build_frames()
    bad = items.copy()
    bad.loc[bad["item_id"] == item_id(OLDER, 1), "char_start"] += 3
    write_item_report(tmp_path, "older", OLDER_CONSENSUS)
    write_item_report(tmp_path, "newer", NEWER_CONSENSUS)
    with pytest.raises(ValueError, match="offsets"):
        lri.write_sentence_packets(pairs_for(items), bad, sections, tmp_path)


def test_report_consensus_for_items_missing_from_the_parquet_is_ignored(tmp_path):
    items, sections = build_frames()
    write_item_report(tmp_path, "older", {**OLDER_CONSENSUS, "gone:I.1A:i009": "unchanged"})
    write_item_report(tmp_path, "newer", NEWER_CONSENSUS)
    assert lri.write_sentence_packets(pairs_for(items), items, sections, tmp_path)


# --- collect-sentences -------------------------------------------------------------------------------------------

def test_collect_sentences_validates_aggregates_and_reports_rejections_and_unlabelled(tmp_path):
    make_packets(tmp_path)
    fabricated = [dict(GOOD_OLDER[0], quote="Nvidia will never face any export licence requirement, ever again.")] + GOOD_OLDER[1:4]
    for name, labels in (("a", GOOD_OLDER), ("b", GOOD_OLDER), ("c", fabricated)):
        write_labels(tmp_path, "older", name, labels)
    report = lri.collect_sentences(PID, "older", tmp_path)
    assert report["kind"] == "sentences" and report["annotators"] == ["a", "b", "c"] and report["side"] == "older"
    assert report["consensus"] == {g["sentence_id"]: g["label"] for g in GOOD_OLDER}
    assert report["needs_adjudication"] == [] and report["unlabelled"] == []
    assert report["rejected"]["c"][0][0] == sid(OLDER, 0, 0) and "not found" in report["rejected"]["c"][0][1]
    assert set(report["votes"][sid(OLDER, 0, 0)]) == {"a", "b"}                          # c's rejected label casts no vote
    assert report["spans"][sid(OLDER, 0, 1)][0] == item_id(OLDER, 0) and len(report["spans"]) == 5
    assert report["sampling"]["seed"] == gold.SENTENCE_SAMPLE_SEED and report["sampling"]["flagship_item_id"] == item_id(OLDER, 0)
    assert "items" not in report["sampling"]
    assert report["split"] == "held_out" and 0.0 < report["pairwise_agreement"] <= 1.0
    assert (tmp_path / f"{PID}.older.sent.report.json").exists()


def test_collect_sentences_marks_disputed_and_unlabelled_sentences(tmp_path):
    make_packets(tmp_path)
    reworded = dict(GOOD_OLDER[1], label="reworded", quote=NAC_REWORD_QUOTE)
    assert gold.SENT_REWORDED_MIN_SIM <= gold.sentence_similarity(
        "The NAC process resulted in no approvals for China at all this year.", NAC_REWORD_QUOTE) < gold.SENT_VERBATIM_SIM
    write_labels(tmp_path, "older", "a", GOOD_OLDER[:2])
    write_labels(tmp_path, "older", "b", [GOOD_OLDER[0], reworded])
    report = lri.collect_sentences(PID, "older", tmp_path)
    assert report["consensus"] == {sid(OLDER, 0, 0): "present"}
    assert report["needs_adjudication"] == [sid(OLDER, 0, 1)]
    assert report["unlabelled"] == [sid(OLDER, 0, 2), sid(OLDER, 1, 0), sid(OLDER, 1, 1)]
    assert report["votes"][sid(OLDER, 0, 1)] == {"a": "removed", "b": "reworded"}


def test_collect_sentences_needs_the_manifest_and_a_readable_label_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="sentence-packets"):
        lri.collect_sentences(PID, "older", tmp_path)
    make_packets(tmp_path)
    (tmp_path / f"{PID}.older.sent.a.labels.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="a.labels.json"):
        lri.collect_sentences(PID, "older", tmp_path)
    (tmp_path / f"{PID}.older.sent.a.labels.json").write_text(json.dumps({"labels": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON list"):
        lri.collect_sentences(PID, "older", tmp_path)


def test_the_newer_side_uses_present_reworded_and_added(tmp_path):
    make_packets(tmp_path)
    good_newer = [
        {"sentence_id": sid(NEWER, 0, 0), "label": "present", "quote": EXPORT_QUOTE},
        {"sentence_id": sid(NEWER, 0, 1), "label": "added", "search_terms": ["H20 licensing", "April shipments", "licensing rules"]},
        {"sentence_id": sid(NEWER, 1, 0), "label": "present", "quote": OLD_I1.split(".")[0] + "."},
        {"sentence_id": sid(NEWER, 1, 1), "label": "present", "quote": VOLATILITY_QUOTE}]
    write_labels(tmp_path, "newer", "a", good_newer)
    report = lri.collect_sentences(PID, "newer", tmp_path)
    assert report["consensus"][sid(NEWER, 0, 1)] == "added" and report["annotators"] == ["a"]
    wrong_side = [dict(good_newer[1], label="removed")]
    write_labels(tmp_path, "newer", "b", wrong_side)
    assert "unknown label 'removed'" in lri.collect_sentences(PID, "newer", tmp_path)["rejected"]["b"][0][1]


# --- glob collisions between the item layer and the sentence layer -----------------------------------------------

def test_item_level_collect_ignores_sentence_label_files(tmp_path):
    items, sections = build_frames()
    lri.write_packets(pairs_for(items), items, sections, tmp_path)
    write_labels(tmp_path, "older", "a", GOOD_OLDER)
    item_labels = [{"item_id": item_id(OLDER, 0), "label": "unchanged", "quote": EXPORT_QUOTE}]
    (tmp_path / f"{PID}.older.b.labels.json").write_text(json.dumps(item_labels), encoding="utf-8")
    report = lri.collect(PID, "older", tmp_path)
    assert report["annotators"] == ["b"] and report["rejected"] == {}


# --- freeze over both layers -------------------------------------------------------------------------------------

def test_freeze_without_sentence_reports_still_works_and_has_an_empty_sentences_layer(tmp_path):
    (tmp_path / f"{PID}.older.report.json").write_text(json.dumps(item_report_full("older", OLDER_CONSENSUS)), encoding="utf-8")
    target = tmp_path / "out" / "gold.json"
    digest = lri.freeze_gold(tmp_path, target)
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["sentences"] == {} and doc["kind"] == "risk_items_gold" and doc["sha256"] == digest
    assert doc["pairs"][f"{PID}|older"]["labels"] == OLDER_CONSENSUS and gold.verify_frozen(target)


def test_freeze_with_no_reports_at_all_is_an_empty_but_valid_gold(tmp_path):
    target = tmp_path / "gold.json"
    lri.freeze_gold(tmp_path, target)
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["pairs"] == {} and doc["sentences"] == {} and gold.verify_frozen(target)


def test_freeze_merges_the_sentence_consensus_with_spans_and_hashes_both_layers(tmp_path):
    make_packets(tmp_path)
    for name in ("a", "b"):
        write_labels(tmp_path, "older", name, GOOD_OLDER)
    lri.collect_sentences(PID, "older", tmp_path)
    (tmp_path / f"{PID}.older.report.json").write_text(json.dumps(item_report_full("older", OLDER_CONSENSUS)), encoding="utf-8")
    target = tmp_path / "out" / "gold.json"
    lri.freeze_gold(tmp_path, target)
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert set(doc["pairs"]) == {f"{PID}|older", f"{PID}|newer"}                     # a sentence report is not an item report
    assert doc["pairs"][f"{PID}|older"]["labels"] == OLDER_CONSENSUS               # and does not overwrite the item layer
    entry = doc["sentences"][f"{PID}|older"]
    assert entry["labels"] == {g["sentence_id"]: g["label"] for g in GOOD_OLDER} and entry["split"] == "held_out"
    assert set(entry["spans"]) == set(entry["labels"]) and entry["n_sampled_sentences"] == 5 and entry["n_unresolved"] == 0
    assert entry["sampling"]["seed"] == gold.SENTENCE_SAMPLE_SEED and "alpha" in entry and "pairwise_agreement" in entry
    records = gold.gold_sentence_records(entry)                                       # scoring works from the frozen gold alone
    assert {r["sentence_id"] for r in records} == set(entry["labels"])
    assert gold.verify_frozen(target)
    tampered = json.loads(target.read_text(encoding="utf-8"))
    tampered["sentences"][f"{PID}|older"]["labels"][sid(OLDER, 0, 1)] = "present"
    target.write_text(json.dumps(tampered), encoding="utf-8")
    assert gold.verify_frozen(target) is False                                        # the sentence layer is hashed too


def test_freeze_applies_adjudicated_sentence_labels_and_counts_the_unresolved(tmp_path):
    make_packets(tmp_path)
    write_labels(tmp_path, "older", "a", GOOD_OLDER[:2])
    write_labels(tmp_path, "older", "b", [GOOD_OLDER[0], dict(GOOD_OLDER[1], label="reworded", quote=NAC_REWORD_QUOTE)])
    report = lri.collect_sentences(PID, "older", tmp_path)
    disputed = report["needs_adjudication"]
    assert disputed == [sid(OLDER, 0, 1)]
    target = tmp_path / "out" / "gold.json"
    lri.freeze_gold(tmp_path, target)
    entry = json.loads(target.read_text(encoding="utf-8"))["sentences"][f"{PID}|older"]
    assert sid(OLDER, 0, 1) not in entry["labels"] and entry["n_unresolved"] == 4
    lri.freeze_gold(tmp_path, target, adjudicated={sid(OLDER, 0, 1): "removed", "unrelated#s000": "removed"})
    entry = json.loads(target.read_text(encoding="utf-8"))["sentences"][f"{PID}|older"]
    assert entry["labels"][sid(OLDER, 0, 1)] == "removed" and entry["n_unresolved"] == 3
    assert "unrelated#s000" not in entry["labels"]
    with pytest.raises(ValueError, match="adjudicated"):
        lri.freeze_gold(tmp_path, target, adjudicated={sid(OLDER, 0, 1): "added"})     # `added` is not an older-side label


# --- the CLI end to end ------------------------------------------------------------------------------------------

@pytest.fixture()
def lake(tmp_path):
    items, sections = build_frames()
    items_dir, sections_dir, out = tmp_path / "items", tmp_path / "sections", tmp_path / "gold"
    items_dir.mkdir()
    sections_dir.mkdir()
    items.to_parquet(items_dir / "NVDA_risk_items.parquet")
    sections.to_parquet(sections_dir / "NVDA_section_texts.parquet")
    filings = [{"accession_no": a, "coverage": 0.97, "low_coverage": False, "section_suspect": False} for a in (OLDER, NEWER)]
    risk_item_quality.write_quality(risk_item_quality.quality_path_for(items_dir, "NVDA"), "NVDA", filings)
    return {"args": ["--items-dir", str(items_dir), "--sections-dir", str(sections_dir), "--out", str(out)], "out": out,
            "tmp": tmp_path}


def run(lake, *argv):
    return lri.main([argv[0], *argv[1:], *lake["args"]])


def test_cli_full_round_trip_packets_collect_sentence_packets_collect_sentences_freeze(lake, capsys):
    out = lake["out"]
    assert run(lake, "packets", "--pair-id", PID) == 0
    older_labels = [{"item_id": item_id(OLDER, 0), "label": "reworded", "quote": EXPORT_QUOTE},
                    {"item_id": item_id(OLDER, 1), "label": "unchanged", "quote": OLD_I1[:60]},
                    {"item_id": item_id(OLDER, 2), "label": "removed", "search_terms": ["wafer supply", "third parties", "demand forecasts"]}]
    newer_labels = [{"item_id": item_id(NEWER, 0), "label": "carried", "quote": EXPORT_QUOTE},
                    {"item_id": item_id(NEWER, 1), "label": "carried", "quote": OLD_I1[:60]}]
    for side, labels in (("older", older_labels), ("newer", newer_labels)):
        (out / f"{PID}.{side}.a.labels.json").write_text(json.dumps(labels), encoding="utf-8")
        assert run(lake, "collect", "--pair-id", PID, "--side", side) == 0
    capsys.readouterr()
    assert run(lake, "sentence-packets", "--pair-id", PID) == 0
    printed = capsys.readouterr().out
    assert f"{PID}.older.sent.packet.json" in printed and f"{PID}.newer.sent.packet.json" in printed
    write_labels(out, "older", "a", GOOD_OLDER)
    assert run(lake, "collect-sentences", "--pair-id", PID, "--side", "older") == 0
    summary = capsys.readouterr().out
    assert '"annotators": [' in summary and '"needs_adjudication": []' in summary
    target = lake["tmp"] / "artifacts" / "risk_items_gold.json"
    assert run(lake, "freeze", "--out-file", str(target)) == 0
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert set(doc["pairs"]) == {f"{PID}|older", f"{PID}|newer"} and set(doc["sentences"]) == {f"{PID}|older"}
    assert doc["pairs"][f"{PID}|older"]["labels"][item_id(OLDER, 2)] == "removed" and gold.verify_frozen(target)
    assert capsys.readouterr().out.strip().endswith(doc["sha256"])


def test_cli_sentence_packets_refuse_clearly_without_item_reports_and_without_a_pair_selection(lake, capsys):
    assert run(lake, "sentence-packets", "--pair-id", PID) == 2
    err = capsys.readouterr().err
    assert "collect" in err and f"{PID}.older.report.json" in err
    with pytest.raises(SystemExit) as exit_info:
        run(lake, "sentence-packets")
    assert exit_info.value.code == 2 and "--pair-id" in capsys.readouterr().err


def test_cli_sentence_packets_reject_an_unknown_pair_and_skip_a_pair_that_is_not_comparable(lake, capsys):
    assert run(lake, "sentence-packets", "--pair-id", "NVDA-nope-nope") == 2
    assert "unknown pair" in capsys.readouterr().err
    filings = [{"accession_no": OLDER, "coverage": 0.5, "low_coverage": True, "section_suspect": False},
               {"accession_no": NEWER, "coverage": 0.97, "low_coverage": False, "section_suspect": False}]
    risk_item_quality.write_quality(risk_item_quality.quality_path_for(Path(lake["args"][1]), "NVDA"), "NVDA", filings)
    assert run(lake, "sentence-packets", "--pair-id", PID) == 2
    captured = capsys.readouterr()
    assert "NOT COMPARED" in captured.err and "no comparable pair" in captured.err


def test_cli_dev_flag_selects_the_development_pairs_and_side_can_be_restricted(lake, monkeypatch, capsys):
    monkeypatch.setattr(lri, "DEV_PAIRS", frozenset({PID}))
    lake["out"].mkdir(parents=True, exist_ok=True)
    write_item_report(lake["out"], "older", OLDER_CONSENSUS)
    assert run(lake, "sentence-packets", "--dev", "--side", "older", "--k", "1", "--max-sentences", "50", "--seed", "3") == 0
    names = capsys.readouterr().out.splitlines()
    assert len(names) == 2 and all(".older.sent." in n for n in names)
    manifest = json.loads((lake["out"] / f"{PID}.older.sent.sample.json").read_text(encoding="utf-8"))
    assert manifest["seed"] == 3 and manifest["k"] == 1 and manifest["max_sentences"] == 50
    assert json.loads((lake["out"] / f"{PID}.older.sent.packet.json").read_text(encoding="utf-8"))["pair"]["split"] == "development"


def test_cli_collect_sentences_reports_a_missing_manifest_as_an_error_exit(lake, capsys):
    assert run(lake, "collect-sentences", "--pair-id", PID, "--side", "older") == 2
    assert "sentence-packets" in capsys.readouterr().err


def test_cli_collect_sentences_prints_the_agreement_summary_and_the_rejections(lake, capsys):
    out = lake["out"]
    out.mkdir(parents=True, exist_ok=True)
    write_item_report(out, "older", OLDER_CONSENSUS)
    write_item_report(out, "newer", NEWER_CONSENSUS)
    assert run(lake, "sentence-packets", "--pair-id", PID, "--side", "older") == 0
    write_labels(out, "older", "a", GOOD_OLDER)
    write_labels(out, "older", "b", [dict(GOOD_OLDER[0], quote="This sentence never appears in the newer filing at all.")])
    capsys.readouterr()
    assert run(lake, "collect-sentences", "--pair-id", PID, "--side", "older") == 0
    printed = capsys.readouterr().out
    assert '"annotators"' in printed and '"pairwise_agreement"' in printed and "b: 1 label(s) rejected by machine" in printed


def test_cli_packets_accept_the_dev_flag_and_a_ticker_filter_and_report_pairs_not_compared(lake, monkeypatch, capsys):
    monkeypatch.setattr(lri, "DEV_PAIRS", frozenset({PID}))
    assert run(lake, "packets", "--dev", "--ticker", "nvda") == 0
    assert len(capsys.readouterr().out.splitlines()) == 2
    items_dir = Path(lake["args"][1])
    filings = [{"accession_no": OLDER, "coverage": 0.5, "low_coverage": True, "section_suspect": False},
               {"accession_no": NEWER, "coverage": 0.97, "low_coverage": False, "section_suspect": False}]
    risk_item_quality.write_quality(risk_item_quality.quality_path_for(items_dir, "NVDA"), "NVDA", filings)
    assert run(lake, "packets", "--ticker", "NVDA") == 0
    captured = capsys.readouterr()
    assert "NOT COMPARED" in captured.err and captured.out.strip() == ""


def test_cli_collect_reports_a_missing_packet_or_a_bad_label_file_as_an_error_exit(lake, capsys):
    assert run(lake, "collect", "--pair-id", PID, "--side", "older") == 2
    assert "packet.json" in capsys.readouterr().err
    assert run(lake, "packets", "--pair-id", PID) == 0
    (lake["out"] / f"{PID}.older.a.labels.json").write_text("{oops", encoding="utf-8")
    capsys.readouterr()
    assert run(lake, "collect", "--pair-id", PID, "--side", "older") == 2
    assert "a.labels.json" in capsys.readouterr().err


def test_cli_freeze_says_how_many_entries_of_each_layer_it_froze(lake, capsys):
    lake["out"].mkdir(parents=True, exist_ok=True)
    write_item_report(lake["out"], "older", OLDER_CONSENSUS)
    target = lake["tmp"] / "frozen.json"
    assert run(lake, "freeze", "--out-file", str(target)) == 0
    captured = capsys.readouterr()
    assert "frozen 1 item entries and 0 sentence entries" in captured.err
    assert captured.out.strip() == json.loads(target.read_text(encoding="utf-8"))["sha256"]


def test_sentence_packets_need_item_offsets_in_the_parquet(tmp_path):
    items, sections = build_frames()
    write_item_report(tmp_path, "older", OLDER_CONSENSUS)
    write_item_report(tmp_path, "newer", NEWER_CONSENSUS)
    with pytest.raises(ValueError, match="char_start"):
        lri.write_sentence_packets(pairs_for(items), items.drop(columns=["char_start", "char_end"]), sections, tmp_path)


def test_a_disputed_largest_item_stays_the_flagship_through_the_report_votes(tmp_path):
    items, sections = build_frames()
    disputed = {item_id(OLDER, 1): "unchanged", item_id(OLDER, 2): "removed"}             # i000 (the largest) has no consensus
    report = {**item_report_full("older", disputed),
              "needs_adjudication": [item_id(OLDER, 0)],
              "votes": {item_id(OLDER, 0): {"a": "reworded", "b": "merged", "c": "unchanged"}}}
    (tmp_path / f"{PID}.older.report.json").write_text(json.dumps(report), encoding="utf-8")
    lri.write_sentence_packets(pairs_for(items), items, sections, tmp_path, sides=("older",))
    manifest = json.loads((tmp_path / f"{PID}.older.sent.sample.json").read_text(encoding="utf-8"))
    assert manifest["flagship_item_id"] == item_id(OLDER, 0) and manifest["flagship_note"] is None
    assert manifest["eligibility_basis"] == {item_id(OLDER, 0): "vote_majority", item_id(OLDER, 1): "consensus"}
    assert manifest["eligible_item_ids"] == [item_id(OLDER, 0), item_id(OLDER, 1)]
    packet = json.loads((tmp_path / f"{PID}.older.sent.packet.json").read_text(encoding="utf-8"))
    assert "vote_majority" not in json.dumps(packet) and "eligib" not in json.dumps({k: v for k, v in packet.items() if k != "instructions"})


# --- contested sentences (a second check disagreed on a boundary case): frozen, but never scored -------------------------

def test_a_contested_sentence_is_frozen_flagged_and_left_out_of_the_scoring_records(tmp_path):
    make_packets(tmp_path)
    write_labels(tmp_path, "older", "a", GOOD_OLDER)
    write_labels(tmp_path, "older", "b", GOOD_OLDER)
    lri.collect_sentences(PID, "older", tmp_path)
    target = tmp_path / "out" / "gold.json"
    lri.freeze_gold(tmp_path, target, contested=[sid(OLDER, 0, 1), "unrelated#s000"])
    entry = json.loads(target.read_text(encoding="utf-8"))["sentences"][f"{PID}|older"]
    assert entry["contested"] == [sid(OLDER, 0, 1)]                                   # only ids of this side's labels
    assert sid(OLDER, 0, 1) in entry["labels"]                                         # still frozen with its label
    records = gold.gold_sentence_records(entry)
    assert sid(OLDER, 0, 1) not in {r["sentence_id"] for r in records} and len(records) == len(entry["labels"]) - 1


def test_the_freeze_command_reads_adjudications_and_contested_ids_from_a_json_file(tmp_path):
    make_packets(tmp_path)
    write_labels(tmp_path, "older", "a", GOOD_OLDER[:2])
    write_labels(tmp_path, "older", "b", [GOOD_OLDER[0], dict(GOOD_OLDER[1], label="reworded", quote=NAC_REWORD_QUOTE)])
    lri.collect_sentences(PID, "older", tmp_path)
    adjudications = tmp_path / "adj.json"
    adjudications.write_text(json.dumps({"sentences": {sid(OLDER, 0, 1): "removed"}, "contested_sentences": [sid(OLDER, 0, 0)]}),
                             encoding="utf-8")
    target = tmp_path / "gold.json"
    assert lri.main(["freeze", "--out", str(tmp_path), "--out-file", str(target), "--adjudications", str(adjudications)]) == 0
    entry = json.loads(target.read_text(encoding="utf-8"))["sentences"][f"{PID}|older"]
    assert entry["labels"][sid(OLDER, 0, 1)] == "removed" and entry["contested"] == [sid(OLDER, 0, 0)]


def test_item_adjudications_may_be_keyed_by_side_when_one_item_id_sits_on_two_sides(tmp_path):
    """An item is the newer item of one pair and the older item of the next: its id alone cannot carry two labels."""
    older_rep = {"pair_id": "P1", "side": "older", "split": "held_out", "annotators": ["a", "b"], "consensus": {},
                 "needs_adjudication": ["x:i1"], "unlabelled": [], "pairwise_agreement": 0.5, "alpha": 0.0, "rejected": {},
                 "votes": {"x:i1": {"a": "reworded", "b": "unchanged"}}}
    newer_rep = {**older_rep, "pair_id": "P0", "side": "newer", "votes": {"x:i1": {"a": "new", "b": "carried"}}}
    for rep in (older_rep, newer_rep):
        (tmp_path / f"{rep['pair_id']}.{rep['side']}.report.json").write_text(json.dumps(rep), encoding="utf-8")
    target = tmp_path / "gold.json"
    lri.freeze_gold(tmp_path, target, {"older|x:i1": "reworded", "newer|x:i1": "carried"})
    pairs = json.loads(target.read_text(encoding="utf-8"))["pairs"]
    assert pairs["P1|older"]["labels"] == {"x:i1": "reworded"} and pairs["P0|newer"]["labels"] == {"x:i1": "carried"}
