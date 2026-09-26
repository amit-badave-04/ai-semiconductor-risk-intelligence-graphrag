"""scripts/judge_acceptance.py: the instrument acceptance test (the new judge must grade the served WRONG T1 and T3 answers incorrect).

Nothing here calls a model: the paid path takes an injected fake judge, and the default (dry-run) path must not reach one.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import temporalfix as fx

import semigraph.eval.runner as runner
from semigraph.eval.runner import Correct

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "judge_acceptance.py"
spec = importlib.util.spec_from_file_location("judge_acceptance", SCRIPT)
ja = importlib.util.module_from_spec(spec)
sys.modules["judge_acceptance"] = ja
spec.loader.exec_module(ja)

T1_ANSWER = "## Yes - several risk factors were dropped\n- Data privacy risk [0001045810-25-000023:I.1A:0257]\n- NAC [Reported Metrics]"
T3_ANSWER = "## Evolution\nRevenue rose [Reported Metrics]. Dropped lineages: privacy fines [0001045810-26-000021:I.1A:0344]."
EXAMPLES = {"source": "x", "snapshot_id": "snap-20260924-7feaaf9bfe", "examples": [
    {"id": "T1", "type": "temporal", "question": "Did Nvidia stop disclosing any risk factors?", "answer": T1_ANSWER,
     "citations": ["0001045810-25-000023:I.1A:0257"], "hallucinated": []},
    {"id": "T3", "type": "temporal", "question": "How has Nvidia's risk profile evolved?", "answer": T3_ANSWER,
     "citations": ["0001045810-26-000021:I.1A:0344"], "hallucinated": []},
    {"id": "N1", "type": "numeric", "question": "revenue?", "answer": "x", "citations": [], "hallucinated": []}]}


class FakeJudge:
    """llm_json-compatible fake: ``verdicts`` maps a question fragment to correct/incorrect; ``fail_on`` raises for that fragment."""

    def __init__(self, correct=(), fail_on=(), fail_first_only=False):
        self.correct, self.fail_on, self.calls, self.fail_first_only = tuple(correct), tuple(fail_on), [], fail_first_only

    def __call__(self, prompt, model_cls, **kw):
        self.calls.append((prompt, kw))
        if any(f in prompt for f in self.fail_on) and (not self.fail_first_only or len(self.calls) == 1):
            raise RuntimeError("provider down")
        return model_cls(correct=any(c in prompt for c in self.correct), reason="scripted",
                         unsupported_claims=["dropped privacy risk"])


def _write_temporal(tmp_path, world):
    """Run the real builder on the fixture gold so the acceptance script reads a genuine temporal questions file."""
    spec_b = importlib.util.spec_from_file_location("btq_for_acceptance", ROOT / "scripts" / "build_temporal_questions.py")
    btq = importlib.util.module_from_spec(spec_b)
    sys.modules["btq_for_acceptance"] = btq
    spec_b.loader.exec_module(btq)
    out = tmp_path / "temporal.json"
    assert btq.main(["--gold", str(world["gold"]), "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]),
                     "--xbrl-dir", str(world["xbrl_dir"]), "--flagship-pair", fx.FLAG, "--out", str(out)]) == 0
    return out


@pytest.fixture()
def setup(tmp_path):
    world = fx.build(tmp_path)
    examples = tmp_path / "examples.json"
    examples.write_text(json.dumps(EXAMPLES), encoding="utf-8")
    temporal = _write_temporal(tmp_path, world)
    base = ["--examples", str(examples), "--temporal", str(temporal), "--gold", str(world["gold"]),
            "--report", str(tmp_path / "report.json")]
    return base, tmp_path / "report.json"


# --- helpers ------------------------------------------------------------------------------------------------------

def test_the_as_of_date_is_the_snapshot_date_of_the_examples_file():
    assert ja.snapshot_as_of("snap-20260924-7feaaf9bfe") == "2026-09-24"
    with pytest.raises(ValueError, match="snapshot"):
        ja.snapshot_as_of("not-a-snapshot")


def test_the_notes_are_the_legacy_notes_the_temporal_file_carries_for_that_question(tmp_path, setup):
    base, _ = setup
    doc = json.loads((tmp_path / "temporal.json").read_text(encoding="utf-8"))
    notes = ja.notes_from_temporal(doc, "T1")
    assert notes == doc["legacy_notes"]["T1"]
    assert "NO whole risk factor was removed" in notes and "exactly 1" in notes and "Notified Advanced Computing" in notes
    assert notes.count("Correct answer:") == 1               # never several questions' verdicts stitched together
    with pytest.raises(ValueError, match="T9"):
        ja.notes_from_temporal(doc, "T9")


def test_the_acceptance_notes_are_byte_equal_to_the_notes_production_scoring_will_use(setup, tmp_path):
    """One source of truth: after `build_temporal_questions.py --merge`, the benchmark's T1/T3 judge_notes ARE the acceptance notes."""
    spec_b = importlib.util.spec_from_file_location("btq_merge", ROOT / "scripts" / "build_temporal_questions.py")
    btq = importlib.util.module_from_spec(spec_b)
    sys.modules["btq_merge"] = btq
    spec_b.loader.exec_module(btq)
    (tmp_path / "w").mkdir()
    world = fx.build(tmp_path / "w")
    bench = tmp_path / "bench.json"
    bench.write_bytes(json.dumps([{"id": "T1", "type": "temporal", "q": "q", "judge_notes": "old"},
                                  {"id": "T3", "type": "temporal", "q": "q", "judge_notes": "old"}], indent=2).encode("utf-8"))
    out = tmp_path / "tq2.json"
    assert btq.main(["--gold", str(world["gold"]), "--items-dir", str(world["items_dir"]), "--sections-dir", str(world["sections_dir"]),
                     "--xbrl-dir", str(world["xbrl_dir"]), "--flagship-pair", fx.FLAG, "--out", str(out), "--merge",
                     "--benchmark", str(bench)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    merged = {e["id"]: e for e in json.loads(bench.read_text(encoding="utf-8"))}
    for tid in ("T1", "T3"):
        assert ja.notes_from_temporal(doc, tid) == merged[tid]["judge_notes"]


# --- dry run (the default) ------------------------------------------------------------------------------------------

def test_the_default_is_a_dry_run_that_prints_the_prompts_and_the_cost_and_calls_nothing(setup, capsys, monkeypatch):
    base, report = setup
    monkeypatch.setattr(runner, "llm_json", lambda *a, **k: pytest.fail("the dry run reached the judge"))
    assert ja.main(base) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "GRADING NOTES" in out and "T1" in out and "T3" in out
    assert "0.06" in out and "6 judge calls" in out
    assert "0001045810-25-000023:I.1A:0257" in out and "2026-09-24" in out
    assert not report.exists()


def test_a_missing_temporal_file_is_a_clear_error_that_points_at_the_builder(setup, capsys):
    base, _ = setup
    args = [a if a != base[base.index("--temporal") + 1] else "/nope/temporal.json" for a in base]
    assert ja.main(args) == 2
    assert "build_temporal_questions.py" in capsys.readouterr().err


def test_a_temporal_file_that_does_not_match_the_frozen_gold_is_refused(setup, tmp_path, capsys):
    base, _ = setup
    doc = json.loads((tmp_path / "temporal.json").read_text(encoding="utf-8"))
    doc["gold_sha256"] = "0" * 64
    (tmp_path / "temporal.json").write_text(json.dumps(doc), encoding="utf-8")
    assert ja.main(base) == 2 and "gold" in capsys.readouterr().err


# --- the paid path (only with --go, and only through judge_open) ------------------------------------------------------

def test_both_wrong_answers_graded_incorrect_is_a_pass(setup, capsys):
    base, report = setup
    judge = FakeJudge(correct=())
    assert ja.main([*base, "--go"], judge=judge) == 0
    assert len(judge.calls) == 6 and all(kw["max_tokens"] == runner.JUDGE_MAX_TOKENS for _, kw in judge.calls)
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert doc["verdict"] == "PASS" and doc["as_of"] == "2026-09-24" and doc["votes"] == 3
    assert set(doc["results"]) == {"T1", "T3"} and doc["results"]["T1"][0]["unsupported_claims"] == ["dropped privacy risk"]
    assert "PASS" in capsys.readouterr().out


def test_the_judge_is_shown_the_notes_the_valid_ids_and_the_as_of_date(setup):
    base, _ = setup
    judge = FakeJudge()
    ja.main([*base, "--go"], judge=judge)
    prompt = judge.calls[0][0]
    assert "NO whole risk factor was removed" in prompt and "0001045810-25-000023:I.1A:0257" in prompt and "2026-09-24" in prompt
    assert "Data privacy risk" in prompt and "mechanically verified" in prompt


def test_one_answer_graded_correct_fails_the_instrument(setup, capsys):
    base, report = setup
    judge = FakeJudge(correct=("How has Nvidia's risk profile evolved?",))
    assert ja.main([*base, "--go"], judge=judge) == 1
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"] == "FAIL" and "FAIL" in capsys.readouterr().out


def test_an_errored_vote_makes_the_result_inconclusive_never_a_pass(setup, capsys):
    """An errored vote counts as 'not correct' in judge_open; for an acceptance test where 'incorrect' is the good outcome that
    would be a false PASS, so any error is INCONCLUSIVE with its own exit code."""
    base, report = setup
    judge = FakeJudge(correct=(), fail_on=("Did Nvidia stop disclosing",), fail_first_only=True)
    assert ja.main([*base, "--go"], judge=judge) == 3
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"] == "INCONCLUSIVE" and "INCONCLUSIVE" in capsys.readouterr().out


def test_the_report_records_the_prompt_and_notes_hashes(setup):
    base, report = setup
    ja.main([*base, "--go"], judge=FakeJudge())
    doc = json.loads(report.read_text(encoding="utf-8"))
    assert len(doc["prompt_sha256"]) == 64 and set(doc["notes_sha256"]) == {"T1", "T3"}


def test_go_and_dry_run_together_are_refused(setup, capsys):
    base, _ = setup
    with pytest.raises(SystemExit):
        ja.main([*base, "--go", "--dry-run"])


# --- notes from the benchmark (the production path) ---------------------------------------------------------------------

def test_notes_can_come_from_the_merged_benchmark_instead(setup, tmp_path, capsys):
    base, _ = setup
    bench = tmp_path / "bench.json"
    bench.write_text(json.dumps([{"id": "T1", "type": "temporal", "q": "q", "judge_notes": "BENCH NOTES ONE"},
                                 {"id": "T3", "type": "temporal", "q": "q", "judge_notes": "BENCH NOTES THREE"}]), encoding="utf-8")
    assert ja.main([*base, "--notes", "benchmark", "--benchmark", str(bench)]) == 0
    out = capsys.readouterr().out
    assert "BENCH NOTES ONE" in out and "BENCH NOTES THREE" in out


def test_the_script_never_imports_the_paid_judge_before_go():
    src = SCRIPT.read_text(encoding="utf-8")
    assert src.index("LITELLM_LOCAL_MODEL_COST_MAP") < src.index("from semigraph")     # offline-safe before any litellm import
    assert not hasattr(ja, "llm_json")                                                 # imported lazily inside main, on --go only
