"""scripts/tune_passages.py: the development-only sweep, now able to score with recorded band answers (``--verdicts``) and to
sweep ``reword_confident`` (``--band``). No model is called and the real lake is not needed (synthetic pair, hand-made gold)."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from test_passage_bands import BAND, HK, engine
from test_passages import P, X

from semigraph.graph import adjudicate as adj
from semigraph.graph import passage_adjudicate as pad
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
                    "suppress_added_with_counterpart": (True,)}


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
