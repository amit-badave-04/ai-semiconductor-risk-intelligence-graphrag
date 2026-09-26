"""The benchmark grew from 20 to 60 questions (docs/v2/M1B_PLAN.md section E): +24 numeric gold, +4 misattribution probes and
the 12 source-text temporal questions T4..T15 that ``scripts/build_temporal_questions.py --merge`` appends from the frozen gold."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from semigraph.artifacts import load_benchmark
from semigraph.eval import bakeoff as bo
from semigraph.eval.expect import check_expectation
from semigraph.eval.runner import needs_judge
from semigraph.retrieval.ids import FR_ID_PATTERN

ROOT = Path(__file__).resolve().parents[1]
PACKAGED = ROOT / "src" / "semigraph" / "artifacts" / "benchmark.json"
TOP_LEVEL = ROOT / "artifacts" / "benchmark.json"
ORIGINAL_IDS = ["N1", "N2", "N3", "N4", "D1", "D2", "D3", "D4", "R1", "R2", "R3", "T1", "T2", "T3", "Q1", "Q2", "Q3", "M1", "U1", "U2"]
# sha256 over the original 20 (id, question) pairs: ids and text must never change (notes may, for T1-T3, see the plan)
ORIGINAL_SHA = "5fbc17c3dd47cee95aad4c0ea694106c1b75473b101dd384499288f02fc4e2e8"


def _bench():
    return load_benchmark()


def test_the_packaged_copy_and_the_top_level_copy_are_byte_identical():
    assert PACKAGED.read_bytes() == TOP_LEVEL.read_bytes()


def test_the_benchmark_has_48_questions_with_unique_ids_in_order():
    bench = _bench()
    ids = [b["id"] for b in bench]
    assert len(bench) >= 48 and len(ids) == len(set(ids))
    assert ids[:20] == ORIGINAL_IDS
    assert ids[20:44] == [f"NG{n:02d}" for n in range(1, 25)] and ids[44:48] == ["X1", "X2", "X3", "X4"]


def test_the_original_twenty_questions_keep_their_ids_and_text():
    original = [(b["id"], b["type"], b["q"]) for b in _bench()[:20]]
    digest = hashlib.sha256(json.dumps(original, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert digest == ORIGINAL_SHA


def test_the_numeric_gold_questions_are_appended_as_they_are_in_the_gold_file():
    gold = json.loads((ROOT / "artifacts" / "gold" / "numeric_questions.json").read_text(encoding="utf-8"))
    by_id = {b["id"]: b for b in _bench()}
    assert len(gold) == 24
    for g in gold:
        b = by_id[g["id"]]
        for key in ("type", "q", "expect", "gold", "subtype", "window", "metric_ids"):
            assert b[key] == g[key], (g["id"], key)


def test_numeric_gold_questions_are_mechanical_and_never_judged():
    for b in (b for b in _bench() if b["id"].startswith("NG")):
        assert bo._mechanical_kind(b) and not needs_judge(b) and b["type"] == "numeric"


def test_the_four_misattribution_probes_are_mechanical_plus_judge():
    probes = [b for b in _bench() if b["type"] == "misattribution"]
    assert [p["id"] for p in probes] == ["X1", "X2", "X3", "X4"]
    for p in probes:
        assert bo._mechanical_kind(p) and needs_judge(p)
        assert list(p["expect"]) == ["not_company_disclosure"] and p["expect"]["not_company_disclosure"]
        assert re.search(FR_ID_PATTERN, p["judge_notes"]) and "VERIFIED" in p["judge_notes"] and len(p["judge_notes"]) < 2200
        # the guard is runnable on the probe itself
        assert check_expectation(p["expect"], "It is a Federal Register rule; the company's filings do not discuss it.")


def test_the_temporal_notes_no_longer_assert_what_the_audit_found_false():
    """The legacy T1 note said 'dozens of risk lineages were dropped': under the new judge rule (a removal claim is correct only
    if the notes support it) that note alone would grade the wrong served T1 answer correct."""
    by_id = {b["id"]: b for b in _bench()}
    for tid in ("T1", "T2", "T3"):
        notes = by_id[tid]["judge_notes"]
        assert "dozens" not in notes.lower() and "source-text" in notes and "VERIFIED" in notes, tid
        assert by_id[tid]["type"] == "temporal"
    assert "NO whole risk factor was removed" in by_id["T1"]["judge_notes"] and "Notified Advanced Computing" in by_id["T1"]["judge_notes"]
    assert "No risk factor is new" in by_id["T2"]["judge_notes"]


def test_the_probe_notes_name_only_federal_register_documents_that_exist_in_the_lake():
    lake = ROOT / "data" / "raw" / "federal_register_bis_rules.json"
    if not lake.exists():
        pytest.skip("the Federal Register cache is not present")
    known = {r["document_number"] for r in json.loads(lake.read_text(encoding="utf-8"))["results"]}
    for p in (b for b in _bench() if b["type"] == "misattribution"):
        cited = {m[3:] for m in re.findall(rf"\[({FR_ID_PATTERN})\]", p["judge_notes"])}
        assert cited and cited <= known, (p["id"], cited - known)


def test_the_merged_temporal_questions_are_exactly_the_gold_derived_file_and_name_its_hash():
    doc = json.loads((ROOT / "artifacts" / "gold" / "temporal_questions.json").read_text(encoding="utf-8"))
    frozen = json.loads((ROOT / "artifacts" / "gold" / "risk_items_gold.json").read_text(encoding="utf-8"))["sha256"]
    assert doc["gold_sha256"] == frozen
    bench = _bench()
    by_id = {b["id"]: b for b in bench}
    assert [b["id"] for b in bench[48:]] == [f"T{n}" for n in range(4, 16)] == [q["id"] for q in doc["questions"]]
    for q in doc["questions"]:
        assert by_id[q["id"]] == q and q["gold"] == "source_text" and frozen[:8] in q["judge_notes"]
    for tid in ("T1", "T2", "T3"):                       # the legacy questions' notes come from the same gold, same hash
        assert by_id[tid]["judge_notes"] == doc["legacy_notes"][tid] and frozen[:8] in by_id[tid]["judge_notes"]
        assert "not yet frozen" not in by_id[tid]["judge_notes"]
