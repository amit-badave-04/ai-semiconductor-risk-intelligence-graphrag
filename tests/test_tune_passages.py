"""scripts/tune_passages.py: the development-only sweep, now able to score with recorded band answers (``--verdicts``) and to
sweep ``reword_confident`` (``--band``). No model is called and the real lake is not needed (synthetic pair, hand-made gold)."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_passage_bands import BAND, HK, TARIFF, engine
from test_passages import P, X

from semigraph.graph import adjudicate as adj
from semigraph.graph import passage_adjudicate as pad
from semigraph.graph import passages
from semigraph.graph.passages import PassageParams

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tune_passages.py"
spec = importlib.util.spec_from_file_location("tune_passages", SCRIPT)
tp = importlib.util.module_from_spec(spec)
sys.modules["tune_passages"] = tp
spec.loader.exec_module(tp)

MODEL = "openai/gpt-6-luna"


def synthetic():
    return engine([[P[0], HK[0], X[0]]], [[P[0], HK[1]]])


def as_pair(so, sn):
    """The dict ``load_pairs`` builds, from the synthetic filings (rows and decisions as in ``test_passage_bands``)."""
    from test_passages import CARRIED_NEW, REWORDED_OLD, decisions, filing

    _, o_rows = filing("o", [[P[0], HK[0], X[0]]])
    _, n_rows = filing("n", [[P[0], HK[1]]])
    return {"pair_id": "PAIR", "older": o_rows, "newer": n_rows, "o_text": so, "n_text": sn, "o_spans": [], "n_spans": [],
            "result": decisions(REWORDED_OLD, CARRIED_NEW)}


def gold_for(so):
    def span(text):
        start = so.index(text)
        return ["o0", start, start + len(text)]
    return {"PAIR|older": {"labels": {"o0#s000": "present", "o0#s001": "removed", "o0#s002": "removed"},
                           "spans": {"o0#s000": span(P[0]), "o0#s001": span(HK[0]), "o0#s002": span(X[0])}}}


def records_for(pp, verdict="different"):
    band = next(b for b in pp.band_sentences() if b.side == "older")
    record = {"key": pad.task_key(band.text_hash, band.other_hash, MODEL), "model": MODEL, "verdict": verdict, "candidate": None,
              "quote": None, "candidates": []}
    return {record["key"]: record}


def test_the_band_grid_parses_numbers_and_none():
    assert tp.parse_band("none,0.5, 0.6") == (None, 0.5, 0.6)
    assert tp.parse_band("0.6") == (0.6,)
    with pytest.raises(argparse.ArgumentTypeError):
        tp.parse_band(" , ")


def test_the_grid_sweeps_the_band_first_and_fast_keeps_the_shipped_values_of_everything_else():
    full, fast = tp.build_grid((None, 0.6)), tp.build_grid((None, 0.6), fast=True)
    assert list(full)[0] == "reword_confident" and full["reword_confident"] == (None, 0.6) and full["reword_min"] == tp.GRID["reword_min"]
    base = PassageParams()
    assert fast == {"reword_confident": (None, 0.6), "reword_min": (base.reword_min,), "partial_min": (base.partial_min,),
                    "present_min_ratio": (base.present_min_ratio,), "decompose_uncertain": (True,),
                    "suppress_added_with_counterpart": (True,), "adjudicate_below_band": (False,)}


def test_the_below_switch_is_the_last_swept_parameter_and_parses_on_off_and_lists():
    assert list(tp.build_grid((0.5,), fast=True, below=(False, True)))[-1] == "adjudicate_below_band"
    assert tp.build_grid((0.5,), fast=True, below=(False, True))["adjudicate_below_band"] == (False, True)
    assert tp.parse_switch("off") == (False,) and tp.parse_switch("ON") == (True,) and tp.parse_switch("off, on,off") == (False, True)
    for bad in ("maybe", " , "):
        with pytest.raises(argparse.ArgumentTypeError):
            tp.parse_switch(bad)


def test_the_defaults_describe_the_shipped_setting_and_the_current_prompt_version():
    args = tp.build_parser().parse_args([])
    assert args.band == (PassageParams().reword_confident,) == (0.50,)          # the shipped edge (commit cbc89ff), not 0.6
    assert args.below_band == (False,) and args.prompt_version == pad.PROMPT_VERSION == "pas-v3"
    on = tp.build_parser().parse_args(["--below-band", "off,on", "--prompt-version", "pas-v2", "--band", "none,0.5"])
    assert on.below_band == (False, True) and on.prompt_version == "pas-v2" and on.band == (None, 0.5)


def test_without_answers_the_band_sentence_stays_reworded_and_the_gold_removal_is_missed():
    pp, so, sn = synthetic()
    pair = as_pair(so, sn)
    params = PassageParams(**BAND)
    no_answers = tp.evaluate([pair], gold_for(so), params)
    assert (no_answers["tp"], no_answers["fn"], no_answers["band"]) == (1, 1, 0)         # X[0] found, the lookalike missed
    assert no_answers["reworded_as_positive"] == 0


def test_recorded_answers_are_applied_with_the_code_rules_and_counted():
    pp, so, sn = synthetic()
    pair = as_pair(so, sn)
    params = PassageParams(**BAND)
    bought = tp.evaluate([pair], gold_for(so), params, records_for(pp), MODEL)
    assert (bought["tp"], bought["fp"], bought["fn"]) == (2, 0, 0) and bought["precision"] == 1.0 and bought["recall"] == 1.0
    assert bought["band"] == 2 and bought["answered"] == 1 and bought["applied"] == 1            # the newer lookalike was never asked


def test_answers_of_another_model_do_nothing():
    pp, so, sn = synthetic()
    other = tp.evaluate([as_pair(so, sn)], gold_for(so), PassageParams(**BAND), records_for(pp), "vendor/other")
    assert other["applied"] == 0 and other["tp"] == 1


def test_a_setting_without_a_band_ignores_the_answers():
    pp, so, sn = synthetic()
    off = tp.evaluate([as_pair(so, sn)], gold_for(so), PassageParams(**{**BAND, "reword_confident": None}), records_for(pp), MODEL)
    assert off["band"] == 0 and off["tp"] == 1


# --------------------------------------------------------------------------- the below zone

def tariff_case():
    """Older [P0, TARIFF0, X1]: TARIFF0 has a paraphrase in the newer section (gold: reworded), X1 has none (gold: removed). Both are
    BELOW sentences (no counterpart at the floor) when the flag is on."""
    from test_passages import CARRIED_NEW, REWORDED_OLD, decisions, filing

    so, o_rows = filing("o", [[P[0], TARIFF[0], X[1]]])
    sn, n_rows = filing("n", [[P[0], TARIFF[1]]])
    pair = {"pair_id": "PAIR", "older": o_rows, "newer": n_rows, "o_text": so, "n_text": sn, "o_spans": [], "n_spans": [],
            "result": decisions(REWORDED_OLD, CARRIED_NEW)}

    def span(text):
        start = so.index(text)
        return ["o0", start, start + len(text)]

    gold_entry = {"PAIR|older": {"labels": {"o0#s000": "present", "o0#s001": "reworded", "o0#s002": "removed"},
                                 "spans": {"o0#s000": span(P[0]), "o0#s001": span(TARIFF[0]), "o0#s002": span(X[1])}}}
    return pair, gold_entry, so, sn


def below_records(pair, version=pad.PROMPT_VERSION, x1="different"):
    """Recorded answers: TARIFF0 -> same (naming the newer paraphrase, quoting the stretch that passes the relatedness floor), X1 -> ``x1``."""
    from test_passage_bands import TARIFF_QUOTE

    params = PassageParams(**{**BAND, "adjudicate_below_band": True})
    pp = passages.PairPassages(pair["older"], pair["newer"], pair["result"], pair["o_text"], pair["n_text"], [], [], params)
    sn = pair["n_text"]
    start = sn.index(TARIFF[1])
    cand = {"text": TARIFF[1], "start": start, "end": start + len(TARIFF[1])}
    records = {}
    for band in pp.band_sentences(candidates=False):
        if band.side != "older":
            continue
        answer = ({"verdict": "same", "candidate": 1, "quote": TARIFF_QUOTE} if band.text == TARIFF[0] else {"verdict": x1, "candidate": None, "quote": None})
        key = pad.task_key(band.text_hash, band.other_hash, MODEL, version)
        records[key] = {"key": key, "model": MODEL, "prompt_version": version, "candidates": [cand], **answer}
    return records


def test_a_verified_below_answer_removes_a_false_removal_from_the_development_score():
    pair, gold_entry, _, _ = tariff_case()
    records = below_records(pair)
    off = tp.evaluate([pair], gold_entry, PassageParams(**BAND), records, MODEL)
    on = tp.evaluate([pair], gold_entry, PassageParams(**{**BAND, "adjudicate_below_band": True}), records, MODEL)
    assert (off["tp"], off["fp"], off["fn"], off["precision"]) == (1, 1, 0, 0.5) and off["below"] == 0        # X1 found, TARIFF0 a false removal
    assert (on["tp"], on["fp"], on["fn"], on["precision"], on["recall"]) == (1, 0, 0, 1.0, 1.0)
    assert (on["below"], on["below_answered"], on["applied"]) == (3, 2, 2)                                   # 2 older answered, the newer one not asked


def test_a_below_setting_without_answers_scores_exactly_like_no_below_setting():
    pair, gold_entry, _, _ = tariff_case()
    off = tp.evaluate([pair], gold_entry, PassageParams(**BAND), {}, MODEL)
    on = tp.evaluate([pair], gold_entry, PassageParams(**{**BAND, "adjudicate_below_band": True}), {}, MODEL)
    assert {k: on[k] for k in ("tp", "fp", "fn")} == {k: off[k] for k in ("tp", "fp", "fn")}
    unmatched = tp.evaluate([pair], gold_entry, PassageParams(**{**BAND, "adjudicate_below_band": True}), {"x": {"model": MODEL}}, MODEL)
    assert (unmatched["tp"], unmatched["fp"], unmatched["below_answered"]) == (1, 1, 0)


def test_a_different_answer_on_a_true_removal_keeps_it_found():
    pair, gold_entry, _, _ = tariff_case()
    on = tp.evaluate([pair], gold_entry, PassageParams(**{**BAND, "adjudicate_below_band": True}), below_records(pair), MODEL)
    assert on["tp"] == 1 and on["fn"] == 0                                                  # X1's `different` keeps its removal


def test_the_prompt_version_selects_which_recorded_answers_are_used():
    pair, gold_entry, _, _ = tariff_case()
    legacy = below_records(pair, version=pad.LEGACY_PROMPT_VERSION)
    params = PassageParams(**{**BAND, "adjudicate_below_band": True})
    assert tp.evaluate([pair], gold_entry, params, legacy, MODEL)["below_answered"] == 0                 # default: pas-v3, none found
    found = tp.evaluate([pair], gold_entry, params, legacy, MODEL, pad.LEGACY_PROMPT_VERSION)
    assert found["below_answered"] == 2 and found["fp"] == 0


def test_a_missing_prompt_version_is_reported_next_to_the_wrong_model_warning(tmp_path, capsys):
    path = tmp_path / "answers.jsonl"
    path.write_text(json.dumps({"key": "k", "model": MODEL, "prompt_version": "pas-v2", "verdict": "different"}) + "\n", encoding="utf-8")
    tp.load_records(path, MODEL, "pas-v3")
    assert "prompt version 'pas-v3'" in capsys.readouterr().err
    tp.load_records(path, MODEL, "pas-v2")
    assert capsys.readouterr().err == ""


def test_the_records_file_is_read_and_a_missing_file_or_a_wrong_model_is_reported(tmp_path, capsys):
    with pytest.raises(SystemExit, match="not found"):
        tp.load_records(tmp_path / "nope.jsonl", MODEL)
    path = tmp_path / "answers.jsonl"
    path.write_text(json.dumps({"key": "k", "model": "vendor/x", "verdict": "different"}) + "\n", encoding="utf-8")
    assert set(tp.load_records(path, MODEL)) == {"k"}
    assert "no recorded answer is for model" in capsys.readouterr().err
    assert isinstance(adj.Checkpoint(path).records["k"], dict)
    tp.load_records(path, "vendor/x")
    assert capsys.readouterr().err == ""
