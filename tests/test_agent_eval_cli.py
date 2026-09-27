"""CLI wiring of ``semigraph eval-agent`` (M3-C): a paid run needs an explicit flag and prints its estimate first, --dry-run and offline scoring
(--runs) cost nothing and never touch Neo4j, the embedder or the judge. No network, no LLM: every heavy collaborator is replaced by a
recorder that FAILS the test if a paid or database call is made where none is allowed.
"""

import json
import re

import pytest
from agentevalfix import GOOD_ANSWER, events, row
from typer.testing import CliRunner

from semigraph import cli
from semigraph.artifacts import load_benchmark
from semigraph.config import Settings
from semigraph.eval import agent_eval as ae
from semigraph.eval.runner import JUDGE_PROMPT_VERSION, Correct

runner = CliRunner()
CHUNK = "0001045810-24-000029:I.1A:0162"
TINY = {"version": 1, "questions": [
    {"id": "A13", "type": "tool_discipline", "category": "no_overcall",
     "q": "How much revenue did Nvidia report for the fiscal year ended January 25, 2026?",
     "expect": {"value": 215938000000}, "expect_from": "N2", "expected_tools": [], "forbidden_tools": ["risk_changes"],
     "max_steps": 1, "source": "test fixture"},
    {"id": "A10", "type": "temporal", "category": "risk_change", "q": "Which Nvidia risk factor was removed?",
     "expect": None, "judge_notes_from": ["T7"], "expected_tools": ["risk_changes"], "forbidden_tools": ["compute_change"],
     "max_steps": 3, "source": "test fixture"}]}
BASELINE = {"judge_prompt_version": JUDGE_PROMPT_VERSION, "n": 60, "votes": 3, "avg_cost_usd": 0.0142,
            "judged": {"open_correct": 0, "open_of": 0, "votes": {}}}


class Forbidden:
    """Stands in for a paid or database collaborator: any use fails the test."""

    def __init__(self, name):
        self.name = name

    def __call__(self, *a, **k):
        raise AssertionError(f"{self.name} must not be called here")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A temp working directory with a tiny valid benchmark, a baseline, settings pointing at tmp data, and every collaborator forbidden."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", answer_model="openai/gpt-6-luna",
                        escalation_model="anthropic/claude-sonnet-5")
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    (tmp_path / "bench.json").write_text(json.dumps(TINY), encoding="utf-8")
    (tmp_path / "baseline.json").write_text(json.dumps(BASELINE), encoding="utf-8")
    from semigraph import embeddings, llm
    from semigraph.graph import client

    monkeypatch.setattr(client, "get_driver", Forbidden("get_driver"))
    monkeypatch.setattr(embeddings, "Embedder", Forbidden("Embedder"))
    monkeypatch.setattr(llm, "llm_json", Forbidden("llm_json (the judge)"))
    monkeypatch.setattr(ae, "agent_answer_events", Forbidden("agent_answer_events"))
    return tmp_path


def invoke(*args):
    return runner.invoke(cli.app, ["eval-agent", "--benchmark", "bench.json", "--baseline", "baseline.json", *args])


def saved_runs(tmp_path, *, wrong_answer=False, name="runs.jsonl"):
    items = ae.build_run_set(ae.read_agent_benchmark(tmp_path / "bench.json"), load_benchmark(), include_main=False)
    rows = [row(items[0], events((), answer="Nvidia's revenue was $1.0 billion [xbrl:1045810:revenue:2026-01-25]." if wrong_answer else GOOD_ANSWER)),
            row(items[1], events(("risk_changes",), answer=f"One indebtedness risk factor was removed [{CHUNK}].", cited=(CHUNK,)))]
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# --- dry run and the paid-run guard ---------------------------------------------------------------------------------------------

def test_dry_run_prints_the_plan_and_the_estimate_and_touches_nothing(env):
    result = invoke("--dry-run", "--include-main")
    assert result.exit_code == 0, result.output
    n_main = len(load_benchmark())
    assert f"run set: {2 + n_main} questions ({2} agent + {n_main} main)" in result.output
    assert "estimate" in result.output.lower() and "worst case" in result.output.lower() and "$" in result.output
    assert "no_overcall" in result.output and "risk_change" in result.output and "misattribution" in result.output
    assert not (env / "data").exists() and not (env / "artifacts").exists()


def test_dry_run_can_be_agent_only_or_limited(env):
    assert "run set: 2 questions (2 agent + 0 main)" in invoke("--dry-run", "--agent-only").output
    assert "run set: 1 question (1 agent + 0 main)" in invoke("--dry-run", "--agent-only", "--limit", "1").output


def test_a_live_run_refuses_without_confirm_paid_after_printing_the_estimate(env):
    result = invoke("--agent-only")
    assert result.exit_code == 2 and "--confirm-paid" in result.output and "estimate" in result.output.lower()
    assert not (env / "data").exists()


def test_a_live_run_needs_an_explicit_spend_cap(env):
    result = invoke("--agent-only", "--confirm-paid")
    assert result.exit_code == 2 and "--max-usd" in result.output


def test_a_malformed_benchmark_is_refused_before_anything_is_bought(env):
    doc = json.loads((env / "bench.json").read_text(encoding="utf-8"))
    doc["questions"][0]["expected_tools"] = ["run_cypher"]
    (env / "bench.json").write_text(json.dumps(doc), encoding="utf-8")
    result = invoke("--dry-run", "--agent-only")
    assert result.exit_code == 2 and "run_cypher" in result.output


def test_a_planner_or_writer_without_a_price_is_refused_rather_than_estimated_blind(env, monkeypatch):
    monkeypatch.setattr(cli, "_settings", lambda: Settings(_env_file=None, data_dir=env / "data", agent_planner_model="mystery/model",
                                                           answer_model="openai/gpt-6-luna", escalation_model="anthropic/claude-sonnet-5"))
    result = invoke("--dry-run", "--agent-only")
    assert result.exit_code == 2 and "no price" in result.output


# --- offline scoring of a saved runs file -----------------------------------------------------------------------------------------

def test_saved_runs_are_scored_offline_for_free_and_a_report_is_written(env):
    saved_runs(env)
    result = invoke("--agent-only", "--runs", "runs.jsonl")
    assert result.exit_code == 0, result.output
    assert re.search(r"PASS\s+mechanical", result.output) and re.search(r"PASS\s+trajectory", result.output)
    assert re.search(r"n/a\s+judged_correctness", result.output) and "judge" in result.output.lower() and "skipped" in result.output.lower()
    report = json.loads((env / "artifacts" / "eval_report.agent.json").read_text(encoding="utf-8"))
    assert set(report) >= {"summary", "gates", "rows", "judged", "clears_all_gates", "estimate"} and report["judged"] is None
    assert report["summary"]["n"] == 2 and report["gates"]["mechanical"]["passed"] is True


def test_a_failing_gate_is_named_and_the_exit_code_says_so(env):
    saved_runs(env, wrong_answer=True)
    result = invoke("--agent-only", "--runs", "runs.jsonl")
    assert result.exit_code == 4 and re.search(r"FAIL\s+mechanical", result.output), result.output


def test_runs_may_be_named_inside_the_processed_directory(env):
    processed = env / "data" / "processed"
    processed.mkdir(parents=True)
    saved_runs(env, name="elsewhere.jsonl").replace(processed / "saved.jsonl")
    assert invoke("--agent-only", "--runs", "saved.jsonl").exit_code == 0


def test_a_missing_runs_file_is_a_usage_error(env):
    result = invoke("--agent-only", "--runs", "nope.jsonl")
    assert result.exit_code == 2 and "nope.jsonl" in result.output


def test_the_judge_runs_offline_only_when_paid_calls_are_confirmed(env, monkeypatch):
    saved_runs(env)
    from semigraph import llm

    seen = []

    def judge(prompt, model_cls, **kw):
        seen.append(prompt)
        return Correct(correct=True, reason="ok")

    monkeypatch.setattr(llm, "llm_json", judge)
    result = invoke("--agent-only", "--runs", "runs.jsonl", "--confirm-paid", "--max-usd", "1.0")
    assert result.exit_code == 0, result.output
    assert len(seen) == 3 and all("[T7]" in p for p in seen)                       # A10 only, three votes, notes resolved from T7
    report = json.loads((env / "artifacts" / "eval_report.agent.json").read_text(encoding="utf-8"))
    assert report["judged"]["open_of"] == 1 and report["judged"]["open_correct"] == 1


def test_no_judge_skips_it_even_when_paid_calls_are_confirmed(env):
    saved_runs(env)
    assert invoke("--agent-only", "--runs", "runs.jsonl", "--confirm-paid", "--no-judge").exit_code == 0


def test_the_judge_needs_max_usd_even_offline_with_runs(env):
    """--max-usd was not required for the judge in --runs mode, so it never capped the judge's spend there (M3 R3 review,
    MEDIUM finding 7): a confirmed judge run without --max-usd must be refused before anything is bought."""
    saved_runs(env)
    result = invoke("--agent-only", "--runs", "runs.jsonl", "--confirm-paid")
    assert result.exit_code == 2 and "--max-usd" in result.output


def test_the_judges_worst_case_over_max_usd_is_refused_before_anything_is_bought(env):
    """A10 alone is judged: 1 question x 3 votes x JUDGE_CALL_USD; a --max-usd below that must refuse (M3 R3 review, MEDIUM
    finding 7), mirroring the bakeoff command's own up-front refusal. ``env`` already forbids the judge (llm_json): if the
    refusal did not fire first, that stand-in would raise instead."""
    saved_runs(env)
    tiny = ae.JUDGE_CALL_USD * 3 * 1 - 0.001
    result = invoke("--agent-only", "--runs", "runs.jsonl", "--confirm-paid", "--max-usd", f"{tiny:.6f}")
    assert result.exit_code == 2 and "judge" in result.output.lower() and "--max-usd" in result.output


def test_a_missing_baseline_leaves_the_comparison_gates_unevaluated_instead_of_failing(env):
    saved_runs(env)
    result = runner.invoke(cli.app, ["eval-agent", "--benchmark", "bench.json", "--baseline", "absent.json", "--agent-only", "--runs", "runs.jsonl"])
    assert result.exit_code == 0 and re.search(r"n/a\s+blended_cost", result.output) and "absent.json" in result.output


# --- a confirmed live run -----------------------------------------------------------------------------------------------------------

def test_a_confirmed_live_run_answers_checkpoints_scores_and_closes_the_driver(env, monkeypatch):
    from semigraph import embeddings
    from semigraph.graph import client

    closed = []

    class FakeDriver:
        def close(self):
            closed.append(True)

    def fake_events(driver, embedder, *, model, escalation_model, **kw):
        assert (model, escalation_model) == ("openai/gpt-6-luna", "anthropic/claude-sonnet-5")
        return lambda item: events(item["expected_tools"])

    monkeypatch.setattr(client, "get_driver", lambda settings=None: FakeDriver())
    monkeypatch.setattr(embeddings, "Embedder", lambda: object())
    monkeypatch.setattr(ae, "agent_answer_events", fake_events)
    result = invoke("--agent-only", "--confirm-paid", "--max-usd", "1.0", "--no-judge")
    assert result.exit_code == 0, result.output
    assert closed == [True]
    logged = [json.loads(line) for line in (env / "data" / "processed" / "eval_agent.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in logged] == ["A13", "A10"]
    assert (env / "artifacts" / "eval_report.agent.json").exists()


def test_a_live_run_without_an_escalation_model_is_refused(env, monkeypatch):
    monkeypatch.setattr(cli, "_settings", lambda: Settings(_env_file=None, data_dir=env / "data", answer_model="openai/gpt-6-luna", escalation_model=""))
    result = invoke("--agent-only", "--confirm-paid", "--max-usd", "1.0")
    assert result.exit_code == 2 and "escalation" in result.output.lower()


_TINY_ESTIMATE = {"questions": 2, "judged_questions": 0, "votes": 3, "answers_usd_likely": 0.0, "answers_usd_worst_case": 0.0,
                  "planner_usd_likely": 0.0, "planner_usd_worst_case": 0.0, "judge_usd_worst_case": 0.0,
                  "total_likely_usd": 0.0, "total_worst_case_usd": 0.0, "assumptions": {}}


def test_the_spend_cap_stops_a_live_run_with_its_own_exit_code(env, monkeypatch):
    """The up-front worst-case estimate is mocked to a value BELOW --max-usd (finding 11's new refusal is about THAT estimate,
    tested separately below), isolating the ORIGINAL guarantee this test covers: the ACTUAL running spend still trips --max-usd
    mid-run and is reported with its own exit code."""
    from semigraph import embeddings
    from semigraph.graph import client

    class FakeDriver:
        def close(self):
            pass

    monkeypatch.setattr(client, "get_driver", lambda settings=None: FakeDriver())
    monkeypatch.setattr(embeddings, "Embedder", lambda: object())
    monkeypatch.setattr(ae, "agent_answer_events", lambda *a, **k: (lambda item: events(item["expected_tools"])))
    monkeypatch.setattr(ae, "estimate_agent_run", lambda *a, **k: dict(_TINY_ESTIMATE))
    result = invoke("--agent-only", "--confirm-paid", "--max-usd", "0.0001", "--no-judge")
    assert result.exit_code == 5 and "cap" in result.output.lower()


def test_a_live_run_refuses_up_front_when_the_worst_case_exceeds_max_usd_instead_of_getting_cut_off(env, monkeypatch):
    """M3 R3 review, LOW finding 11: the REAL (unmocked) worst-case estimate for a live run must be compared to --max-usd BEFORE
    the first question is answered, mirroring bakeoff's up-front refusal, instead of starting and being cut off mid-run."""
    from semigraph import embeddings
    from semigraph.graph import client

    asked = []

    class FakeDriver:
        def close(self):
            pass

    def fake_events(driver, embedder, *, model, escalation_model, **kw):
        def answer(item):
            asked.append(item["id"])
            return events(item["expected_tools"])
        return answer

    monkeypatch.setattr(client, "get_driver", lambda settings=None: FakeDriver())
    monkeypatch.setattr(embeddings, "Embedder", lambda: object())
    monkeypatch.setattr(ae, "agent_answer_events", fake_events)
    result = invoke("--agent-only", "--confirm-paid", "--max-usd", "0.0001", "--no-judge")
    assert result.exit_code == 2 and asked == []                      # refused before the first question, not cut off mid-run
    assert "worst case" in result.output.lower()


def test_a_live_run_refuses_when_no_baseline_report_exists_to_estimate_from(env, monkeypatch):
    """M3 R3 review, MEDIUM finding 8: a confirmed LIVE run must never silently proceed with no cost estimate."""
    monkeypatch.setattr(cli, "_DEFAULT_AGENT_BASELINE", env / "does-not-exist.json")
    result = runner.invoke(cli.app, ["eval-agent", "--benchmark", "bench.json", "--agent-only", "--confirm-paid", "--max-usd", "1.0", "--no-judge"])
    assert result.exit_code == 2
    assert "baseline" in result.output.lower() or "estimated" in result.output.lower()


def test_the_default_baseline_path_is_anchored_to_the_repository_not_the_cwd():
    assert cli._DEFAULT_AGENT_BASELINE.is_absolute()
    assert cli._DEFAULT_AGENT_BASELINE.name == "eval_report.v2d-deployed.json"
    assert cli._DEFAULT_AGENT_BASELINE.parent.name == "artifacts"


def test_a_live_run_from_a_different_cwd_still_finds_the_default_baseline_and_estimates_first(env, monkeypatch):
    """The default --baseline path resolves relative to the repository (like agent_eval.AGENT_BENCHMARK_PATH), not the CWD: a
    live run started with no --baseline flag, from ``env``'s own temp CWD, must still find it and print an estimate before
    spending (M3 R3 review, MEDIUM finding 8)."""
    real_baseline = env / "real_baseline.json"
    real_baseline.write_text(json.dumps(BASELINE), encoding="utf-8")
    monkeypatch.setattr(cli, "_DEFAULT_AGENT_BASELINE", real_baseline)
    from semigraph import embeddings
    from semigraph.graph import client

    class FakeDriver:
        def close(self):
            pass

    monkeypatch.setattr(client, "get_driver", lambda settings=None: FakeDriver())
    monkeypatch.setattr(embeddings, "Embedder", lambda: object())
    monkeypatch.setattr(ae, "agent_answer_events", lambda *a, **k: (lambda item: events(item["expected_tools"])))
    result = runner.invoke(cli.app, ["eval-agent", "--benchmark", "bench.json", "--agent-only", "--confirm-paid", "--max-usd", "1.0", "--no-judge"])
    assert result.exit_code == 0, result.output
    assert "estimate" in result.output.lower()
