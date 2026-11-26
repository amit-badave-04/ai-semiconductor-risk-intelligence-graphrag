"""S-10: cost fields never reach a committed eval report; the removed values go to a git-ignored sidecar."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "strip_costs.py"
spec = importlib.util.spec_from_file_location("strip_costs", SCRIPT)
sc = importlib.util.module_from_spec(spec)
sys.modules["strip_costs"] = sc
spec.loader.exec_module(sc)

REPORT = {
    "n": 3, "avg_cost_usd": 0.014, "total_cost_usd": 0.84, "mechanical": {"passed": 2, "of": 3},
    "estimate": {"questions": 82, "answers_usd_likely": 1.4, "assumptions": {"agent_split_cost_factor": 2.0, "votes": 3,
                                                                              "judge_usd_per_vote": 0.01}},
    "summary": {"cost": {"total_usd": 1.2, "avg_planner_usd": 0.0005, "main_avg_usd": 0.0158}, "spend_inconsistent": {}},
    "rows": [{"id": "A1", "cost_usd": 0.0026, "planner_cost_usd": 0.0008, "latency_s": 3.0}],
    "gates": {"blended_cost": {"passed": True, "detail": "$0.0158 per question vs $0.0140 for the fixed path (ratio 1.13, limit 2)"},
              "spend_consistent": {"passed": True, "detail": "0 runs with inconsistent spend"},
              "p95_latency": {"passed": False, "detail": "p95 15.6s (limit 15s)"}},
    "limits": {"max_cost_usd_per_question": 0.5},
}


def test_strip_removes_every_cost_and_usd_key_at_any_depth():
    out, removed = sc.strip_report(REPORT)
    flat = json.dumps(out)
    for key in ("avg_cost_usd", "total_cost_usd", "answers_usd_likely", "judge_usd_per_vote", "agent_split_cost_factor",
                "total_usd", "avg_planner_usd", "main_avg_usd", "cost_usd", "planner_cost_usd"):
        assert f'"{key}"' not in flat, key
    assert "cost" not in out["summary"]
    assert out["rows"] == [{"id": "A1", "latency_s": 3.0}]
    assert removed["/total_cost_usd"] == 0.84 and removed["/rows/0/cost_usd"] == 0.0026


def test_strip_does_not_mutate_its_input():
    before = json.dumps(REPORT, sort_keys=True)
    sc.strip_report(REPORT)
    assert json.dumps(REPORT, sort_keys=True) == before


def test_strip_keeps_gate_thresholds_and_limits_but_not_dollar_figures():
    out, removed = sc.strip_report(REPORT)
    assert out["gates"]["blended_cost"]["passed"] is True                   # a gate NAME containing 'cost' is kept
    detail = out["gates"]["blended_cost"]["detail"]
    assert "$0.0158" not in detail and "$0.0140" not in detail
    assert "ratio 1.13, limit 2" in detail
    assert out["gates"]["p95_latency"] == REPORT["gates"]["p95_latency"]
    assert out["gates"]["spend_consistent"] == REPORT["gates"]["spend_consistent"]
    assert out["limits"] == REPORT["limits"]                                # a limit named in the definitions stays
    assert removed["/gates/blended_cost/detail"] == REPORT["gates"]["blended_cost"]["detail"]


def test_strip_keeps_non_cost_data():
    out, _ = sc.strip_report(REPORT)
    assert out["n"] == 3 and out["mechanical"] == REPORT["mechanical"] and out["estimate"]["questions"] == 82
    assert out["estimate"]["assumptions"] == {"votes": 3}


def test_strip_file_writes_stripped_report_and_sidecar_and_is_idempotent(tmp_path):
    report, side = tmp_path / "r.json", tmp_path / "side" / "r.costs.json"
    report.write_text(json.dumps(REPORT), encoding="utf-8")
    sc.strip_file(report, side)
    first = report.read_text(encoding="utf-8")
    sidecar = json.loads(side.read_text(encoding="utf-8"))
    assert sidecar["removed"]["/avg_cost_usd"] == 0.014 and "avg_cost_usd" not in first
    sc.strip_file(report, side)                                              # second run: nothing left, nothing clobbered
    assert report.read_text(encoding="utf-8") == first
    assert json.loads(side.read_text(encoding="utf-8")) == sidecar


def test_cli_strips_in_place_and_defaults_the_sidecar_under_data_processed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = tmp_path / "r.json"
    report.write_text(json.dumps(REPORT), encoding="utf-8")
    assert sc.main([str(report), "--sidecar-dir", str(tmp_path / "data" / "processed")]) == 0
    assert "avg_cost_usd" not in report.read_text(encoding="utf-8")
    assert (tmp_path / "data" / "processed" / "r.costs.json").exists()


def test_data_processed_is_gitignored():
    r = subprocess.run(["git", "check-ignore", "-q", "data/processed/x.costs.json"], cwd=ROOT)
    assert r.returncode == 0


def test_committed_reports_carry_no_spend():
    for name in ("eval_report.m2-deployed.json", "eval_report.agent.m2.json"):
        text = (ROOT / "artifacts" / name).read_text(encoding="utf-8")
        assert "_usd" not in text and "cost_usd" not in text and "$0." not in text, name
