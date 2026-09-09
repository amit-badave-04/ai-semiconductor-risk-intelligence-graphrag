"""Pure-logic tests for semigraph.eval — no Neo4j, no network, no real LLM.

Covers the notebook 14 programmatic checks (number parsing, refusal
detection), score_runs with a scripted judge, score-aggregation math, and
run_benchmark end-to-end with answer + judge fully mocked (checkpointing,
resume, artifact paths).
"""

import json

import pandas as pd
import pytest

import semigraph.eval.runner as runner_mod
from semigraph.config import Settings
from semigraph.eval import (
    Correct,
    Faithfulness,
    Recall,
    REFUSAL_PAT,
    Relevance,
    parse_numbers,
    run_benchmark,
    score_runs,
    summarize,
)

CID = "0001045810-24-000029:I.1:0001"


# --- programmatic checks (verbatim notebook 14 behavior) ---

@pytest.mark.parametrize("text,expected", [
    ("$60.9 billion", 60.9e9),
    ("60,922 million", 60922e6),
    ("revenue was 215.938 billion USD", 215.938e9),
    ("about 3.5x growth", 3.5),
])
def test_parse_numbers_scales_units(text, expected):
    assert expected in parse_numbers(text)


def test_parse_numbers_ignores_unparseable():
    assert parse_numbers("no digits here") == []


@pytest.mark.parametrize("ans", [
    "The context does not contain a revenue figure for Samsung.",
    "This cannot be determined from the filings.",
    "Samsung is not an SEC filer, so no filings are available.",
])
def test_refusal_pattern_matches_declines(ans):
    assert REFUSAL_PAT.search(ans)


def test_refusal_pattern_rejects_substantive_answer():
    assert not REFUSAL_PAT.search("Nvidia's revenue was $60.9 billion.")


# --- score_runs with a scripted judge ---

def make_run(id_, type_, answer, system="hybrid", hallucinated=(), cited=(CID,),
             context="RELATIONSHIPS:\n(none)\n\nEXCERPTS:\n[x] text",
             chunk_texts=None):
    return {"id": id_, "system": system, "type": type_, "q": "q?", "answer": answer,
            "cited": list(cited), "valid_ids": [CID], "hallucinated": list(hallucinated),
            "context": context,
            "chunk_texts": chunk_texts if chunk_texts is not None else {CID: "some text"}}


class ScriptedJudge:
    """llm_json-compatible double; records prompts per schema."""

    def __init__(self, faithfulness=(3, 4), verdicts=(True, False), correct=True, recall=(2, 2)):
        self.faithfulness = faithfulness
        self.verdicts = list(verdicts)
        self.correct = correct
        self.recall = recall
        self.calls = []

    def __call__(self, prompt, model_cls, **kw):
        self.calls.append((model_cls.__name__, prompt, kw))
        if model_cls is Faithfulness:
            supported, total = self.faithfulness
            return Faithfulness(total_claims=total, supported_claims=supported)
        if model_cls is Relevance:
            return Relevance(verdicts=self.verdicts)
        if model_cls is Recall:
            present, needed = self.recall
            return Recall(needed=needed, present=present)
        return Correct(correct=self.correct, reason="scripted")


BENCH = [
    {"id": "N1", "type": "numeric", "q": "revenue?", "expect": {"value": 60_922_000_000}},
    {"id": "D1", "type": "dependency", "q": "deps?", "expect": {"any_of": ["TSMC", "Samsung"]}},
    {"id": "T1", "type": "temporal", "q": "dropped risks?", "judge_notes": "should affirm"},
    {"id": "U1", "type": "refusal", "q": "samsung revenue?"},
]


def test_score_runs_numeric_within_half_percent_tolerance():
    judge = ScriptedJudge()
    ok = score_runs([make_run("N1", "numeric", "Revenue was $60.9 billion.")], BENCH, judge=judge)
    bad = score_runs([make_run("N1", "numeric", "Revenue was $59 billion.")], BENCH, judge=judge)
    assert ok[0]["correct"] is True
    assert bad[0]["correct"] is False


def test_score_runs_any_of_substring_case_insensitive():
    judge = ScriptedJudge()
    rows = score_runs([make_run("D1", "dependency", "It relies on tsmc foundries.")], BENCH, judge=judge)
    assert rows[0]["correct"] is True


def test_score_runs_open_question_uses_correctness_judge():
    judge = ScriptedJudge(correct=True)
    rows = score_runs([make_run("T1", "temporal", "Yes, 73 lineages were dropped.")], BENCH, judge=judge)
    assert rows[0]["correct"] is True
    assert any(name == "Correct" for name, _, _ in judge.calls)


def test_score_runs_refusal_scored_programmatically_and_skips_faithfulness():
    judge = ScriptedJudge()
    rows = score_runs([make_run("U1", "refusal", "The filings do not contain this; it cannot be determined.")],
                      BENCH, judge=judge)
    assert rows[0]["correct"] is True
    assert "faithfulness" not in rows[0]
    assert not any(name == "Faithfulness" for name, _, _ in judge.calls)


def test_score_runs_faithfulness_ratio_and_full_context_to_judge():
    judge = ScriptedJudge(faithfulness=(3, 4))
    ctx = "RELATIONSHIPS:\n- Nvidia DEPENDS_ON TSMC\n\nEXCERPTS:\n[c] t"
    rows = score_runs([make_run("N1", "numeric", "Revenue was $60.9 billion.", context=ctx)],
                      BENCH, judge=judge)
    assert rows[0]["faithfulness"] == 3 / 4
    faith_prompt = next(p for name, p, _ in judge.calls if name == "Faithfulness")
    # battle scar: the judge must see the FULL context (graph blocks included)
    assert "Nvidia DEPENDS_ON TSMC" in faith_prompt


def test_score_runs_context_precision_and_haiku_settings():
    judge = ScriptedJudge(verdicts=[True, False])
    rows = score_runs([make_run("N1", "numeric", "Revenue was $60.9 billion.",
                                chunk_texts={"a": "t1", "b": "t2"})],
                      BENCH, judge=judge, critic_model="anthropic/claude-haiku-4-5")
    assert rows[0]["context_precision"] == 0.5
    rel_kw = next(kw for name, _, kw in judge.calls if name == "Relevance")
    # battle scar: relevance runs on the critic model with thinking_off=False
    assert rel_kw["model"] == "anthropic/claude-haiku-4-5"
    assert rel_kw["thinking_off"] is False


def test_score_runs_citation_validity():
    judge = ScriptedJudge()
    rows = score_runs([make_run("N1", "numeric", "Revenue was $60.9 billion.",
                                hallucinated=["bogus:I.1:0000"])], BENCH, judge=judge)
    assert rows[0]["citation_ok"] is False
    assert rows[0]["n_citations"] == 1


# --- aggregation math ---

def test_summarize_aggregation_math():
    scored = pd.DataFrame([
        {"id": "N1", "system": "hybrid", "type": "numeric", "correct": True,
         "faithfulness": 1.0, "context_precision": 0.5, "citation_ok": True, "n_citations": 4},
        {"id": "T1", "system": "hybrid", "type": "temporal", "correct": True,
         "faithfulness": 0.5, "context_precision": 0.25, "citation_ok": True, "n_citations": 6},
        {"id": "N1", "system": "vector", "type": "numeric", "correct": True,
         "faithfulness": 1.0, "context_precision": 0.5, "citation_ok": True, "n_citations": 4},
        {"id": "T1", "system": "vector", "type": "temporal", "correct": False,
         "faithfulness": 1.0, "context_precision": 0.5, "citation_ok": False, "n_citations": 0},
    ])
    report = summarize(scored, n_questions=2)
    assert report["overall"]["correct"] == {"hybrid": 1.0, "vector": 0.5}
    assert report["overall"]["faithfulness"]["hybrid"] == 0.75
    assert report["overall"]["citation_validity"]["vector"] == 0.5
    assert report["by_type"]["hybrid"]["temporal"] == 1.0
    assert report["by_type"]["vector"]["temporal"] == 0.0
    assert report["n_questions"] == 2 and report["scored_runs"] == 4


# --- run_benchmark end to end (answer + judge mocked) ---

@pytest.fixture
def fake_answer(monkeypatch):
    calls = []

    def _answer(question, driver, embedder, strategy="hybrid", llm=None, **kw):
        calls.append((question, strategy))
        return {"answer": f"Revenue was $60.9 billion [{CID}].",
                "cited": {CID}, "valid_ids": {CID}, "hallucinated": set(),
                "context": "RELATIONSHIPS:\n(none)\n\nEXCERPTS:\n[c] t",
                "retrieval": {"chunks": [{"chunk_id": CID, "text": "Revenue was $60,922 million."}]}}

    monkeypatch.setattr(runner_mod, "answer", _answer)
    return calls


def test_run_benchmark_writes_artifacts_and_checkpoints(tmp_path, fake_answer):
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)
    judge = ScriptedJudge(faithfulness=(4, 4), verdicts=[True])
    out = run_benchmark(settings, driver=None, embedder=None, limit=2,
                        judge=judge, artifacts_dir=tmp_path / "artifacts")
    # 2 questions x 2 systems, checkpointed in the notebook's jsonl path
    assert out["results_path"] == settings.processed_dir / "eval_runs.jsonl"
    lines = [json.loads(l) for l in out["results_path"].read_text(encoding="utf-8").splitlines()]
    assert {(l["id"], l["system"]) for l in lines} == {("N1", "hybrid"), ("N1", "vector"),
                                                       ("N2", "hybrid"), ("N2", "vector")}
    assert all("context" in l and "chunk_texts" in l for l in lines)
    assert (tmp_path / "artifacts" / "eval_report.json").exists()
    assert (tmp_path / "artifacts" / "eval_scores.json").exists()
    report = json.loads((tmp_path / "artifacts" / "eval_report.json").read_text(encoding="utf-8"))
    # N1 expects 60.922B (fake answer close enough), N2 expects 215.938B (wrong)
    assert report["overall"]["correct"] == {"hybrid": 0.5, "vector": 0.5}
    assert report["overall"]["citation_validity"] == {"hybrid": 1.0, "vector": 1.0}
    assert report["n_questions"] == 2 and report["scored_runs"] == 4
    assert len(fake_answer) == 4


def test_run_benchmark_resumes_from_checkpoint(tmp_path, fake_answer):
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)
    judge = ScriptedJudge(faithfulness=(4, 4), verdicts=[True])
    run_benchmark(settings, None, None, limit=1, judge=judge,
                  artifacts_dir=tmp_path / "artifacts")
    n_first = len(fake_answer)
    run_benchmark(settings, None, None, limit=1, judge=judge,
                  artifacts_dir=tmp_path / "artifacts")
    # second pass answered nothing new — all (id, system) pairs checkpointed
    assert len(fake_answer) == n_first == 2


# --- Agentic_Evals-derived additions: context recall, cost/latency, judge override, rescore ---

def test_score_runs_context_recall_from_grading_notes_on_critic_model():
    judge = ScriptedJudge(recall=(1, 2))
    [row] = score_runs([make_run("T1", "temporal", "yes, dropped")], BENCH, judge=judge,
                       critic_model="haiku")
    assert row["context_recall"] == 0.5
    recall_calls = [c for c in judge.calls if c[0] == "Recall"]
    assert len(recall_calls) == 1 and "should affirm" in recall_calls[0][1]
    assert recall_calls[0][2]["model"] == "haiku" and recall_calls[0][2]["thinking_off"] is False


def test_score_runs_context_recall_uses_expect_and_skips_refusals():
    judge = ScriptedJudge(recall=(2, 2))
    rows = score_runs([make_run("N1", "numeric", "$60.9 billion"),
                       make_run("U1", "refusal", "The context does not contain that.")], BENCH, judge=judge)
    assert rows[0]["context_recall"] == 1.0 and "context_recall" not in rows[1]
    assert "60922000000" in [c for c in judge.calls if c[0] == "Recall"][0][1]


def test_score_runs_carries_cost_and_latency():
    run = {**make_run("N1", "numeric", "$60.9 billion"), "latency_s": 12.5, "cost_usd": 0.05}
    [row] = score_runs([run], BENCH, judge=ScriptedJudge())
    assert row["latency_s"] == 12.5 and row["cost_usd"] == 0.05


def test_summarize_adds_recall_cost_latency_and_judge_model():
    scored = pd.DataFrame([
        {"id": "N1", "system": "hybrid", "type": "numeric", "correct": True, "faithfulness": 1.0,
         "context_precision": 0.5, "context_recall": 1.0, "citation_ok": True, "n_citations": 4,
         "cost_usd": 0.04, "latency_s": 10.0},
        {"id": "T1", "system": "hybrid", "type": "temporal", "correct": False, "faithfulness": 1.0,
         "context_precision": 0.5, "context_recall": 0.5, "citation_ok": True, "n_citations": 2,
         "cost_usd": 0.06, "latency_s": 20.0},
    ])
    report = summarize(scored, n_questions=2, judge_model="anthropic/claude-haiku-4-5")
    assert report["overall"]["context_recall"]["hybrid"] == 0.75
    assert report["overall"]["avg_cost_usd"]["hybrid"] == 0.05
    assert report["overall"]["avg_latency_s"]["hybrid"] == 15.0
    assert report["judge_model"] == "anthropic/claude-haiku-4-5"
    legacy = summarize(scored.drop(columns=["context_recall", "cost_usd", "latency_s"]), n_questions=2)
    assert "context_recall" not in legacy["overall"] and legacy["judge_model"] is None


def test_run_benchmark_records_latency_and_cost_per_run(tmp_path, fake_answer):
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)
    out = run_benchmark(settings, None, None, limit=1, judge=ScriptedJudge(),
                        artifacts_dir=tmp_path / "artifacts")
    line = json.loads(out["results_path"].read_text(encoding="utf-8").splitlines()[0])
    assert isinstance(line["latency_s"], float) and line["latency_s"] >= 0
    assert line["usage"] is None and line["cost_usd"] is None  # fake answer exposes no usage


def test_run_benchmark_rescore_reuses_runs_with_other_judge(tmp_path, fake_answer):
    settings = Settings(data_dir=tmp_path / "data", _env_file=None)
    run_benchmark(settings, None, None, limit=1, judge=ScriptedJudge(), artifacts_dir=tmp_path / "a")
    n_answers = len(fake_answer)
    judge2 = ScriptedJudge(correct=False)
    out = run_benchmark(settings, None, None, limit=1, judge=judge2, artifacts_dir=tmp_path / "a",
                        judge_model="anthropic/claude-haiku-4-5", rescore=True, report_suffix=".haiku")
    assert len(fake_answer) == n_answers  # nothing re-answered
    assert out["report_path"].name == "eval_report.haiku.json"
    assert out["report"]["judge_model"].endswith("haiku-4-5")
    assert all(c[2].get("model") == "anthropic/claude-haiku-4-5"
               for c in judge2.calls if c[0] in ("Faithfulness", "Correct"))
    assert (tmp_path / "a" / "eval_report.json").exists()  # primary report untouched
