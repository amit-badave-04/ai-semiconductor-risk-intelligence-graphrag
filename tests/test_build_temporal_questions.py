"""scripts/build_temporal_questions.py: the 12 temporal benchmark questions, built ONLY from the frozen source-text gold."""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import temporalfix as fx

from semigraph.eval import gold
from semigraph.retrieval.router import needs_strong_model

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_temporal_questions.py"
spec = importlib.util.spec_from_file_location("build_temporal_questions", SCRIPT)
btq = importlib.util.module_from_spec(spec)
sys.modules["build_temporal_questions"] = btq
spec.loader.exec_module(btq)


@pytest.fixture()
def world(tmp_path):
    return fx.build(tmp_path)


def _corpus(world, resolver=fx.period_end):
    return btq.load_corpus(world["items_dir"], world["sections_dir"], resolver)


def _questions(world, **kw):
    doc = json.loads(world["gold"].read_text(encoding="utf-8"))
    return btq.build_questions(doc, _corpus(world), flagship_pair=fx.FLAG, **kw)


# --- helpers ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("end,fy", [(date(2026, 1, 25), 2026), (date(2025, 12, 27), 2025), (date(2025, 8, 28), 2025),
                                    (date(2026, 1, 3), 2025), (date(2025, 12, 31), 2025)])
def test_the_fiscal_year_is_the_year_of_the_period_end_except_for_a_year_ending_in_early_january(end, fy):
    assert btq.fiscal_year_of(end) == fy


def test_a_pair_id_splits_into_the_ticker_and_the_two_accessions():
    assert btq.parse_pair_id(fx.FLAG) == ("NVDA", "0001045810-25-000023", "0001045810-26-000021")
    with pytest.raises(ValueError, match="pair id"):
        btq.parse_pair_id("NVDA-oops")


def test_the_period_resolver_reads_the_latest_period_of_each_accession(tmp_path):
    rows = [{"accn": "A-1", "end": "2024-01-28"}, {"accn": "A-1", "end": "2025-01-26"}, {"accn": "B-2", "end": "2026-01-25"},
            {"accn": "A-1", "end": "2023-01-29"}]
    pd.DataFrame(rows).to_parquet(tmp_path / "X_key_metrics.parquet")
    resolve = btq.xbrl_period_resolver(tmp_path)
    assert resolve("A-1") == date(2025, 1, 26) and resolve("B-2") == date(2026, 1, 25) and resolve("C-3") is None


# --- (a) "did X remove any risk factors" -------------------------------------------------------------------------

def _by_id(questions):
    return {q["id"]: q for q in questions}


def test_the_removed_question_for_the_flagship_pair_says_no_and_names_the_one_new_item(world):
    q = next(q for q in _questions(world) if q["pair_id"] == fx.FLAG and q["subtype"] == "removed_any")
    assert q["q"] == "Did Nvidia remove any risk factors between its FY2025 and FY2026 annual reports?"
    n = q["judge_notes"]
    assert "NO whole risk factor was removed" in n and "0 removed" in n
    assert "Commercial arrangements expose us to counterparty risks." in n and "0001045810-26-000021:I.1A:0003" in n
    assert q["verified_answer"] == "no" and q["facts"]["removed"] == [] and len(q["facts"]["new"]) == 1
    assert "1 new" in n and "3 reworded" in n and "1 unchanged" in n


def test_the_removed_question_for_a_pair_with_one_gold_removed_item_names_it_with_its_older_chunk_ids(world):
    q = next(q for q in _questions(world) if q["pair_id"] == fx.PRIOR and q["subtype"] == "removed_any")
    assert q["verified_answer"] == "yes" and [r["item_id"] for r in q["facts"]["removed"]] == ["0001045810-24-000029:I.1A:i002"]
    assert "0001045810-24-000029:I.1A:0002" in q["judge_notes"] and "risk number 2 may harm our business." in q["judge_notes"]
    assert "exactly 1" in q["judge_notes"]


def test_a_paragraph_unit_filer_is_described_in_paragraphs_and_a_missing_fiscal_year_falls_back_to_the_filing_date(world):
    q = next(q for q in _questions(world) if q["pair_id"] == fx.TSM and q["subtype"] == "removed_any")
    assert "paragraph" in q["judge_notes"]
    unresolved = btq.build_questions(json.loads(world["gold"].read_text(encoding="utf-8")), _corpus(world, lambda acc: None),
                                     flagship_pair=fx.FLAG)
    q2 = next(q for q in unresolved if q["pair_id"] == fx.TSM and q["subtype"] == "removed_any")
    assert q2["q"] == "Did TSMC remove any risk factors between its annual report filed 2025-04-17 and its annual report filed 2026-04-16?"


# --- (b) "did X stop disclosing <headline>" ------------------------------------------------------------------------

def test_three_stop_disclosing_questions_use_only_headlines_the_gold_says_are_still_present(world):
    stops = [q for q in _questions(world) if q["subtype"] == "stop_disclosing"]
    assert len(stops) == 3 and all(q["verified_answer"] == "no" for q in stops)
    doc = json.loads(world["gold"].read_text(encoding="utf-8"))
    for q in stops:
        label = doc["pairs"][f"{q['pair_id']}|older"]["labels"][q["facts"]["item_id"]]
        assert label in ("unchanged", "reworded") and q["facts"]["headline"] and q["facts"]["headline"] in q["q"]
        assert q["pair_id"] != fx.TSM                      # paragraph units have no headline to ask about
        assert "not recorded" in q["judge_notes"] and label in q["judge_notes"] and q["facts"]["older_chunk_ids"]


def test_the_choice_is_seeded_and_reproducible(world):
    a = [q["q"] for q in _questions(world, seed=7) if q["subtype"] == "stop_disclosing"]
    assert a == [q["q"] for q in _questions(world, seed=7) if q["subtype"] == "stop_disclosing"]
    assert a != [q["q"] for q in _questions(world, seed=8) if q["subtype"] == "stop_disclosing"]


# --- flagship passages ---------------------------------------------------------------------------------------------

def test_the_flagship_passage_questions_quote_the_gold_removed_sentences_from_the_older_text(world):
    passages = {q["facts"]["key"]: q for q in _questions(world) if q["subtype"] == "passage"}
    assert set(passages) == {"nac", "hk", "ai_diffusion"}
    nac = passages["nac"]
    assert nac["q"].startswith("Does NVIDIA's FY2026 10-K still say that the Notified Advanced Computing (NAC) process")
    assert fx.NAC in nac["judge_notes"] and "SURVIVES" in nac["judge_notes"] and nac["verified_answer"] == "no"
    assert nac["facts"]["sentence_ids"] == ["0001045810-25-000023:I.1A:i000#s001"] and nac["facts"]["labels"] == ["removed"]
    assert fx.HK in passages["hk"]["judge_notes"]
    ai = passages["ai_diffusion"]
    assert fx.UVEU in ai["judge_notes"] and "2 labelled sentences mention 'AI Diffusion'" in ai["judge_notes"]
    assert "1 still present" in ai["judge_notes"]


def test_a_flagship_sentence_the_gold_does_not_call_removed_is_a_loud_error_not_a_quiet_question(world):
    def flip(doc):
        doc["sentences"][f"{fx.FLAG}|older"]["labels"]["0001045810-25-000023:I.1A:i000#s001"] = "present"

    fx.rewrite_gold(world["gold"], flip)
    with pytest.raises(btq.FlagshipMismatch, match="Notified Advanced Computing"):
        _questions(world)


def test_a_flagship_needle_that_matches_no_labelled_sentence_is_a_loud_error(world):
    def drop(doc):
        doc["sentences"][f"{fx.FLAG}|older"]["labels"].pop("0001045810-25-000023:I.1A:i000#s002")
        doc["sentences"][f"{fx.FLAG}|older"]["spans"].pop("0001045810-25-000023:I.1A:i000#s002")

    fx.rewrite_gold(world["gold"], drop)
    with pytest.raises(btq.FlagshipMismatch, match="out of China and Hong Kong"):
        _questions(world)


# --- the set --------------------------------------------------------------------------------------------------------

def test_twelve_questions_with_sequential_ids_and_the_strong_route(world):
    qs = _questions(world)
    assert [q["id"] for q in qs] == [f"T{n}" for n in range(4, 16)] and len(qs) == 12
    assert {q["type"] for q in qs} == {"temporal"} and {q["gold"] for q in qs} == {"source_text"}
    assert [q["subtype"] for q in qs].count("removed_any") == 6
    assert [q["id"] for q in qs if not needs_strong_model(q["q"])] == []


def test_every_note_is_derived_from_gold_labels_and_stays_small(world):
    for q in _questions(world):
        assert "gold" in q["judge_notes"] and 200 < len(q["judge_notes"]) < 3600
    assert "risk_alignment" not in SCRIPT.read_text(encoding="utf-8")        # nothing reads algorithm output


def test_a_pair_that_is_not_fully_labelled_is_refused(world):
    def drop(doc):
        doc["pairs"][f"{fx.AMD}|older"]["labels"].pop("0000002488-25-000012:I.1A:i000")

    fx.rewrite_gold(world["gold"], drop)
    with pytest.raises(btq.IncompleteGold, match="AMD"):
        _questions(world)


def test_the_output_document_is_deterministic_and_names_the_gold_hash(world, tmp_path):
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    for out in (out1, out2):
        assert btq.main(["--gold", str(world["gold"]), "--items-dir", str(world["items_dir"]), "--sections-dir",
                         str(world["sections_dir"]), "--flagship-pair", fx.FLAG, "--xbrl-dir", str(world["xbrl_dir"]),
                         "--out", str(out)]) == 0
    assert out1.read_bytes() == out2.read_bytes()
    doc = json.loads(out1.read_text(encoding="utf-8"))
    frozen = json.loads(world["gold"].read_text(encoding="utf-8"))
    assert doc["gold_sha256"] == frozen["sha256"] and doc["kind"] == "temporal_questions" and len(doc["questions"]) == 12


def test_a_tampered_or_unfrozen_gold_is_refused(world, tmp_path, capsys):
    tampered = tmp_path / "tampered.json"
    doc = json.loads(world["gold"].read_text(encoding="utf-8"))
    doc["pairs"][f"{fx.FLAG}|older"]["labels"]["0001045810-25-000023:I.1A:i002"] = "removed"
    tampered.write_text(json.dumps(doc), encoding="utf-8")
    args = ["--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]),
            "--xbrl-dir", str(world["xbrl_dir"]), "--out", str(tmp_path / "o.json")]
    assert btq.main(["--gold", str(tampered), *args]) == 2 and "not frozen" in capsys.readouterr().err
    unfrozen = tmp_path / "unfrozen.json"
    unfrozen.write_text(json.dumps({k: v for k, v in doc.items() if k != "sha256"}), encoding="utf-8")
    assert btq.main(["--gold", str(unfrozen), *args]) == 2
    assert not (tmp_path / "o.json").exists()


# --- merge into the benchmark ----------------------------------------------------------------------------------------

LEGACY_BENCH = ('[\r\n  {\r\n    "id": "T1",\r\n    "type": "temporal",\r\n    "q": "Did Nvidia stop disclosing any risk factors?",\r\n'
                '    "judge_notes": "affirms YES \\u2014 dozens dropped"\r\n  },\r\n  {\r\n    "id": "T2",\r\n    "type": "temporal",\r\n'
                '    "q": "What new risks appeared in Meta\'s annual report?",\r\n    "judge_notes": "plausible risks"\r\n  },\r\n'
                '  {\r\n    "id": "T3",\r\n    "type": "temporal",\r\n    "q": "How has Nvidia\'s risk profile evolved?",\r\n'
                '    "judge_notes": "growing emphasis"\r\n  },\r\n  {\r\n    "id": "U1",\r\n    "type": "refusal",\r\n'
                '    "q": "Samsung revenue?"\r\n  }\r\n]')


U1_RAW = '  {\r\n    "id": "U1",\r\n    "type": "refusal",\r\n    "q": "Samsung revenue?"\r\n  }'


def _merge_args(world, tmp_path, *extra):
    a, b = tmp_path / "packaged.json", tmp_path / "toplevel.json"
    for p in (a, b):
        p.write_bytes(LEGACY_BENCH.encode("utf-8"))
    return (["--gold", str(world["gold"]), "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]),
             "--flagship-pair", fx.FLAG, "--xbrl-dir", str(world["xbrl_dir"]), "--out", str(tmp_path / "tq.json"), "--merge",
             "--benchmark", str(a), "--benchmark", str(b), *extra], a, b)


def test_merge_appends_t4_onwards_keeps_the_original_bytes_and_keeps_both_copies_identical(world, tmp_path):
    args, a, b = _merge_args(world, tmp_path)
    assert btq.main(args) == 0
    assert a.read_bytes() == b.read_bytes() and b"\r\n" in a.read_bytes() and not a.read_bytes().endswith(b"\n")
    merged = json.loads(a.read_text(encoding="utf-8"))
    assert [e["id"] for e in merged] == ["T1", "T2", "T3", "U1"] + [f"T{n}" for n in range(4, 16)]
    assert U1_RAW in a.read_bytes().decode("utf-8")                           # an untouched entry keeps its exact bytes


def test_merge_replaces_the_legacy_t1_t2_t3_notes_with_gold_derived_ones_but_keeps_ids_and_questions(world, tmp_path):
    args, a, _ = _merge_args(world, tmp_path)
    btq.main(args)
    merged = {e["id"]: e for e in json.loads(a.read_text(encoding="utf-8"))}
    for tid in ("T1", "T3"):
        n = merged[tid]["judge_notes"]
        assert "dozens" not in n and "NO whole risk factor was removed" in n and "exactly 1" in n and "FY2024" in n
        assert "Notified Advanced Computing" in n and "FY2023" in n and "not in the gold" in n
        assert n.count("Correct answer:") == 1           # one verdict line: notes of different questions are never stitched together
    assert "No risk factor is new" in merged["T2"]["judge_notes"] and "Meta" in merged["T2"]["judge_notes"]
    assert merged["T1"]["q"] == "Did Nvidia stop disclosing any risk factors?" and merged["T1"]["type"] == "temporal"


def test_the_document_carries_the_legacy_notes_and_merge_writes_exactly_those(world, tmp_path):
    args, a, _ = _merge_args(world, tmp_path)
    assert btq.main(args) == 0
    doc = json.loads((tmp_path / "tq.json").read_text(encoding="utf-8"))
    merged = {e["id"]: e for e in json.loads(a.read_bytes().decode("utf-8"))}
    assert set(doc["legacy_notes"]) == {"T1", "T2", "T3"}
    for tid in ("T1", "T2", "T3"):
        assert merged[tid]["judge_notes"] == doc["legacy_notes"][tid]


# --- sentence-level gold in the notes ------------------------------------------------------------------------------------------

def test_the_notes_state_the_sentence_level_gold_counts_the_sample_and_the_quotes(world):
    q = next(q for q in _questions(world) if q["pair_id"] == fx.FLAG and q["subtype"] == "removed_any")
    n = q["judge_notes"]
    assert "a seeded SAMPLE of 1 of 4 risk factors" in n and "3 removed, 0 reworded, 1 present" in n
    for needle in ("Notified Advanced Computing", "out of China and Hong Kong", "Universal Verified End Users"):
        assert needle in n
    assert "1 added" in n and "New licensing requirements" in n            # the newer side
    assert "not verified" in n                                            # what the sample does not cover


def test_a_pair_without_sentence_gold_says_that_no_sentence_claim_is_verified(world):
    q = next(q for q in _questions(world) if q["pair_id"] == fx.AMD and q["subtype"] == "removed_any")
    assert "Sentence level: not labelled for this pair" in q["judge_notes"]


def _records(n_removed, extra=()):
    rows = [{"sentence_id": f"i:{k:03d}", "item_id": "i", "label": "removed", "text": f"Removed sentence number {k} of the filing."}
            for k in range(n_removed)]
    return rows + [{"sentence_id": f"i:{900 + k}", "item_id": "i", "label": "removed", "text": t} for k, t in enumerate(extra)]


def test_the_sentence_list_is_capped_with_a_count_and_the_named_passages_come_first():
    named = "The Notified Advanced Computing process has not resulted in approvals for exports of products to China."
    s = btq.sentence_summary(_records(12, [named]), "older", total_items=30, unit="risk factor", priority=("Notified Advanced Computing",))
    assert "13 removed" in s and "and 5 more" in s
    assert s.index("Notified Advanced Computing") < s.index("Removed sentence number 0")
    assert s.count('"') == 2 * btq.SENTENCES_LISTED


def test_a_long_quote_is_clipped_and_a_side_without_removals_says_so():
    s = btq.sentence_summary(_records(0, ["x" * 500]), "older", total_items=3, unit="risk factor")
    assert "..." in s and len(s) < 500
    none = btq.sentence_summary([{"sentence_id": "i:0", "item_id": "i", "label": "present", "text": "kept"}], "newer", total_items=3,
                                unit="risk factor")
    assert "No sentence was labelled added" in none and "1 present" in none


def test_a_second_merge_changes_nothing(world, tmp_path):
    args, a, b = _merge_args(world, tmp_path)
    btq.main(args)
    first = a.read_bytes()
    assert btq.main(args) == 0 and a.read_bytes() == first == b.read_bytes()


def test_merge_refuses_an_id_that_belongs_to_a_different_kind_of_question(world, tmp_path, capsys):
    args, a, b = _merge_args(world, tmp_path)
    clash = LEGACY_BENCH[:-len("\r\n]")] + ',\r\n  {\r\n    "id": "T4",\r\n    "type": "numeric",\r\n    "q": "x"\r\n  }\r\n]'
    for p in (a, b):
        p.write_bytes(clash.encode("utf-8"))
    assert btq.main(args) == 2 and "T4" in capsys.readouterr().err
    assert a.read_bytes() == clash.encode("utf-8")


def test_merge_refuses_a_gold_that_is_not_frozen_and_touches_nothing(world, tmp_path):
    args, a, b = _merge_args(world, tmp_path)
    doc = json.loads(world["gold"].read_text(encoding="utf-8"))
    doc["pairs"][f"{fx.FLAG}|older"]["labels"]["0001045810-25-000023:I.1A:i002"] = "removed"
    world["gold"].write_text(json.dumps(doc), encoding="utf-8")
    assert btq.main(args) == 2 and a.read_bytes() == LEGACY_BENCH.encode("utf-8") == b.read_bytes()


def test_the_dry_run_writes_nothing(world, tmp_path, capsys):
    out = tmp_path / "dry.json"
    assert btq.main(["--gold", str(world["gold"]), "--items-dir", str(world["items_dir"]), "--sections-dir",
                     str(world["sections_dir"]), "--flagship-pair", fx.FLAG, "--xbrl-dir", str(world["xbrl_dir"]),
                     "--out", str(out), "--dry-run"]) == 0
    assert not out.exists() and "12 questions" in capsys.readouterr().out


# --- every question is labelled with the split its pair belongs to (M1B_PLAN L.10) -----------------------------------------------

def test_every_question_is_labelled_as_development_split_because_only_development_pairs_feed_it(world):
    qs = _questions(world)
    assert len(qs) == 12 and {q["split"] for q in qs} == {"development"}


def test_the_label_comes_from_the_split_the_gold_records_for_the_pair_never_from_a_constant(world):
    doc = json.loads(world["gold"].read_text(encoding="utf-8"))
    facts = {q["pair_id"] for q in btq.build_questions(doc, _corpus(world), flagship_pair=fx.FLAG)}
    assert facts and all(doc["pairs"][f"{p}|older"]["split"] == "development" for p in facts)
    with pytest.raises(ValueError, match="development"):
        btq.label_split({"id": "T4", "pair_id": next(iter(facts))}, {"pairs": {f"{next(iter(facts))}|older": {"split": "held_out"}}})


def test_the_output_document_and_both_benchmark_copies_carry_the_development_label(world, tmp_path):
    args, a, b = _merge_args(world, tmp_path)
    assert btq.main(args) == 0
    doc = json.loads((tmp_path / "tq.json").read_text(encoding="utf-8"))
    assert doc["split"] == "development" and "not held-out" in doc["split_note"] and "L.10" in doc["split_note"]
    assert {q["split"] for q in doc["questions"]} == {"development"}
    assert a.read_bytes() == b.read_bytes()
    merged = {e["id"]: e for e in json.loads(a.read_bytes().decode("utf-8"))}
    assert {merged[f"T{n}"]["split"] for n in range(4, 16)} == {"development"}
    assert "split" not in merged["T1"] and "split" not in merged["U1"]           # the legacy entries keep their exact shape
