"""The correctness-judge INSTRUMENT (docs/v2/M1B_PLAN.md section E).

The earlier judge could not see the valid citation ids or the data date, graded temporal answers against notes that
assumed the drop layer worked, and had no rule about removal claims or Federal Register rules. These tests pin the new
prompt, the one shared renderer, the misattribution probes (mechanical guard AND judge), errored-vote accounting and the
refusal to use the superseded AI-assigned labels.
"""

import json
from pathlib import Path

import pytest

from semigraph.eval import bakeoff as bo
from semigraph.eval import runner
from semigraph.eval.runner import (
    JUDGE_MAX_TOKENS,
    Correct,
    Faithfulness,
    Recall,
    Relevance,
    SupersededLabelsError,
    load_judge_labels,
    needs_judge,
    render_judge_prompt,
    score_runs,
)

ROOT = Path(__file__).resolve().parents[1]
A1, A2 = "0001045810-26-000021:I.1A:0361", "0001045810-26-000021:I.1A:0364"
FR = "fr:2025-19001"
ITEM = {"id": "T1", "type": "temporal", "q": "Did Nvidia stop disclosing any risk factors?",
        "judge_notes": "VERIFIED: 0 of 23 risk factors were removed."}


# --- the prompt -----------------------------------------------------------------------------------------------

def test_the_prompt_carries_question_notes_as_of_and_the_answer():
    p = render_judge_prompt(ITEM, "No risk factor was removed [%s]." % A1, valid_ids=[A1, A2], as_of="2026-09-25")
    assert ITEM["q"] in p and ITEM["judge_notes"] in p and "2026-09-25" in p and "No risk factor was removed" in p


def test_the_prompt_lists_only_the_cited_valid_ids_and_counts_the_retrieved_ones():
    p = render_judge_prompt(ITEM, "Kept [%s]; also [0001-99-000001:I.1:0001]." % A1, valid_ids=[A1, A2], as_of=None)
    assert A1 in p and A2 not in p and "0001-99-000001:I.1:0001" in p.split("ANSWER:")[1]   # only in the answer text
    assert "2 " in p.split("CITATIONS:")[1].split("\n")[0]                                     # the retrieved count is stated
    assert "mechanically verified" in p and "do NOT judge whether an id exists" in p


def test_the_prompt_states_that_other_bracketed_labels_are_not_citations():
    p = render_judge_prompt(ITEM, "Revenue rose [Reported Metrics].", valid_ids=[A1], as_of=None)
    assert "[Reported Metrics]" in p.split("ANSWER:")[0] and "NOT a citation" in p


def test_the_prompt_states_the_removal_and_federal_register_and_figure_rules():
    p = render_judge_prompt(ITEM, "x", valid_ids=[], as_of=None)
    low = p.lower()
    assert "removed" in low and "dropped" in low and "added" in low and "reworded" in low
    assert "grading notes do not support" in low
    assert "federal register" in low and "fr:" in low and "disclosed" in low
    assert "figure" in low and "contradict" in low


def test_a_missing_as_of_and_missing_notes_are_stated_not_silently_dropped():
    p = render_judge_prompt({"id": "Q", "type": "risk", "q": "q?"}, "a", valid_ids=[], as_of=None)
    assert "not stated" in p and "(none)" in p


def test_a_long_id_list_is_capped_to_keep_the_prompt_small():
    ids = [f"0001-25-000001:I.1A:{n:04d}" for n in range(200)]
    answer = " ".join(f"[{i}]" for i in ids)
    p = render_judge_prompt(ITEM, answer, valid_ids=ids, as_of=None)
    head = p.split("ANSWER:")[0]
    assert head.count("0001-25-000001:I.1A:") <= runner.MAX_JUDGE_IDS and "200" in head


def test_the_prompt_is_small():
    p = render_judge_prompt({**ITEM, "judge_notes": ""}, "", valid_ids=[], as_of="2026-09-25")
    assert len(p) < 2600            # the fixed part of a judge call (cost: it is sent up to 3 times per answer)


def test_the_verdict_schema_gains_unsupported_claims_with_an_empty_default():
    old = Correct.model_validate_json('{"correct": false, "reason": "x"}')       # a verdict saved before this change
    assert old.unsupported_claims == []
    new = Correct(correct=False, reason="r", unsupported_claims=["dropped privacy risk"])
    assert new.unsupported_claims == ["dropped privacy risk"]


# --- one renderer at every call site ---------------------------------------------------------------------------

class Recorder:
    """llm_json-compatible judge that records (prompt, kwargs) and answers every schema (a fixed correctness verdict)."""

    def __init__(self, correct=False):
        self.correct, self.calls = correct, []

    def __call__(self, prompt, model_cls, **kw):
        self.calls.append((prompt, kw))
        if model_cls is Faithfulness:
            return Faithfulness(total_claims=1, supported_claims=1)
        if model_cls is Relevance:
            return Relevance(verdicts=[True])
        if model_cls is Recall:
            return Recall(needed=1, present=1)
        return Correct(correct=self.correct, reason="r")


def _run(id_="T1", type_="temporal", answer="No risk factor was removed [%s]." % A1, valid=(A1, A2)):
    return {"id": id_, "system": "hybrid", "type": type_, "q": "q", "answer": answer, "cited": [A1],
            "valid_ids": list(valid), "hallucinated": [], "context": "ctx", "chunk_texts": {}}


def test_score_runs_uses_the_shared_renderer_and_the_larger_token_budget():
    judge = Recorder(correct=True)
    rows = score_runs([_run()], [ITEM], judge=judge, as_of="2026-09-25")
    prompt, kw = judge.calls[0]
    assert prompt == render_judge_prompt(ITEM, _run()["answer"], valid_ids=[A1, A2], as_of="2026-09-25")
    assert kw["max_tokens"] == JUDGE_MAX_TOKENS >= 600 and rows[0]["correct"] is True


def test_judge_open_uses_the_same_renderer_and_token_budget():
    judge = Recorder(correct=True)
    row = {**_run(), "model": "m"}
    out = bo.judge_open([row], [ITEM], judge, votes=3, as_of="2026-09-25")
    assert {c[0] for c in judge.calls} == {render_judge_prompt(ITEM, row["answer"], valid_ids=[A1, A2], as_of="2026-09-25")}
    assert all(c[1]["max_tokens"] == JUDGE_MAX_TOKENS for c in judge.calls) and out["open_correct"] == 1


def test_no_call_site_formats_the_judge_prompt_by_hand():
    """``render_judge_prompt`` is the only place that fills the template (cli.py reaches it through bakeoff.judge_open)."""
    eval_dir = ROOT / "src" / "semigraph" / "eval"
    counts = {p.name: p.read_text(encoding="utf-8").count("JUDGE_PROMPT.format") for p in eval_dir.glob("*.py")}
    assert counts["runner.py"] == 1 and sum(counts.values()) == 1, counts
    assert "JUDGE_PROMPT.format" not in (ROOT / "src" / "semigraph" / "cli.py").read_text(encoding="utf-8")


# --- errored votes are recorded separately (an error must never look like a graded verdict) ----------------------

def test_judge_open_records_errored_votes_separately_and_still_counts_them_as_not_correct():
    seen = []

    def flaky(prompt, model_cls, **kw):
        seen.append(1)
        if len(seen) == 2:
            raise RuntimeError("provider down")
        return model_cls(correct=True, reason="r")

    out = bo.judge_open([_run()], [ITEM], flaky, votes=3)
    assert out["votes"] == {"T1": 2} and out["errors"] == {"T1": 1} and out["open_correct"] == 1


def test_judge_open_detail_returns_each_vote_with_its_unsupported_claims_or_error():
    seen = []

    def judge(prompt, model_cls, **kw):
        seen.append(1)
        if len(seen) == 3:
            raise RuntimeError("boom")
        return model_cls(correct=False, reason=f"r{len(seen)}", unsupported_claims=["dropped privacy risk"])

    out = bo.judge_open([_run()], [ITEM], judge, votes=3, detail=True)
    votes = out["details"]["T1"]
    assert votes[0] == {"correct": False, "reason": "r1", "unsupported_claims": ["dropped privacy risk"]}
    assert votes[2]["error"].startswith("RuntimeError") and "correct" not in votes[2]
    assert "details" not in bo.judge_open([_run()], [ITEM], Recorder(), votes=1)


# --- misattribution probes: the guard AND the judge --------------------------------------------------------------

PROBE = {"id": "X1", "type": "misattribution", "q": "What did NVIDIA disclose about the BIS 50% affiliates rule?",
         "expect": {"not_company_disclosure": ["NVIDIA", "Nvidia"]},
         "judge_notes": "The rule is a BIS Federal Register action; NVIDIA's filings do not discuss it."}
M1 = {"id": "M1", "type": "numeric", "q": "growth?", "expect": {"any_of": ["155"]}, "judge_notes": "about +155B"}
HONEST = "The rule is a BIS action [%s]. NVIDIA's filings do not mention it." % FR
BAD = "NVIDIA disclosed the affiliates rule in its 10-K [%s]." % FR


def test_a_misattribution_probe_needs_the_judge_but_a_numeric_question_with_notes_does_not():
    assert needs_judge(PROBE) and not needs_judge(M1)
    assert needs_judge({"id": "T1", "type": "temporal", "q": "q"}) and not needs_judge({"id": "U1", "type": "refusal", "q": "q"})
    assert not needs_judge({"id": "N1", "type": "numeric", "q": "q", "expect": {"value": 1}})


def test_the_bakeoff_judges_the_probes_and_only_the_probes_among_the_mechanical_questions():
    judge = Recorder(correct=True)
    rows = [{**_run("X1", "misattribution", HONEST), "model": "m"}, {**_run("M1", "numeric", "155 billion"), "model": "m"}]
    out = bo.judge_open(rows, [PROBE, M1], judge, votes=1)
    assert out["open_of"] == 1 and list(out["votes"]) == ["X1"]


def test_mechanical_scoring_counts_a_probe_by_its_guard_only():
    ok = {**_run("X1", "misattribution", HONEST), "model": "m", "finish_reason": "stop", "cost_usd": 0.0,
          "usage": None, "latency_s": 1.0}
    bad = {**ok, "answer": BAD, "cited": []}
    assert bo.score_mechanical([ok], [PROBE])["mechanical"] == {"passed": 1, "of": 1}
    assert bo.score_mechanical([bad], [PROBE])["failed_ids"] == ["X1"]


def _correctness_calls(judge):
    return [prompt for prompt, _ in judge.calls if "GRADING NOTES" in prompt and "Judge whether the ANSWER" in prompt]


def test_score_runs_a_probe_is_correct_only_when_the_guard_and_the_judge_agree():
    judge_yes, judge_no = Recorder(correct=True), Recorder(correct=False)
    assert score_runs([_run("X1", "misattribution", HONEST)], [PROBE], judge=judge_yes)[0]["correct"] is True
    assert score_runs([_run("X1", "misattribution", HONEST)], [PROBE], judge=judge_no)[0]["correct"] is False
    assert len(_correctness_calls(judge_yes)) == 1
    failing_guard = score_runs([_run("X1", "misattribution", BAD)], [PROBE], judge=judge_yes)
    assert failing_guard[0]["correct"] is False
    assert len(_correctness_calls(judge_yes)) == 1          # the failed guard bought no second correctness call


def test_score_runs_skips_the_correctness_call_when_the_guard_already_failed():
    judge = Recorder(correct=True)
    score_runs([_run("X1", "misattribution", BAD)], [PROBE], judge=judge)
    assert _correctness_calls(judge) == []


def test_a_numeric_question_with_notes_is_still_scored_mechanically_without_the_judge():
    judge = Recorder(correct=False)
    row = score_runs([_run("M1", "numeric", "about 155 billion")], [M1], judge=judge)[0]
    assert row["correct"] is True and _correctness_calls(judge) == []


# --- the superseded AI-assigned labels ------------------------------------------------------------------------

def test_the_28_ai_labels_are_marked_superseded_and_otherwise_intact():
    doc = json.loads((ROOT / "artifacts" / "judge_labels.json").read_text(encoding="utf-8"))
    assert doc["status"] == "superseded"
    assert "labelled by AI annotators who saw only the retrieved context" in doc["superseded_reason"]
    assert "docs/v2/REVIEW_2026-09-26.md" in doc["superseded_reason"]
    assert len(doc["labels"]) == 28 and doc["counts"] == {"correct": 21, "incorrect": 6, "contested": 1}


def test_the_loader_refuses_superseded_labels_unless_asked():
    path = ROOT / "artifacts" / "judge_labels.json"
    with pytest.raises(SupersededLabelsError, match="superseded"):
        load_judge_labels(path)
    assert len(load_judge_labels(path, include_superseded=True)["labels"]) == 28


def test_the_loader_accepts_labels_that_are_not_superseded(tmp_path):
    p = tmp_path / "labels.json"
    p.write_text(json.dumps({"labels": [{"id": "Q1", "label": "correct"}]}), encoding="utf-8")
    assert load_judge_labels(p)["labels"][0]["id"] == "Q1"


# --- the data as-of date ----------------------------------------------------------------------------------------------

def test_data_as_of_is_the_newest_lake_date_in_iso_form_or_none_for_an_empty_lake(monkeypatch):
    from datetime import date

    import semigraph.snapshot as snap

    monkeypatch.setattr(snap, "newest_lake_date", lambda settings: date(2026, 9, 25))
    assert runner.data_as_of(object()) == "2026-09-25"
    monkeypatch.setattr(snap, "newest_lake_date", lambda settings: None)
    assert runner.data_as_of(object()) is None


def test_run_benchmark_tells_the_judge_the_data_date(tmp_path, monkeypatch):
    """The judge prompt of a real benchmark run carries the lake's as-of date, not 'not stated'."""
    from types import SimpleNamespace

    import semigraph.snapshot as snap

    monkeypatch.setattr(snap, "newest_lake_date", lambda settings: __import__("datetime").date(2026, 9, 25))
    prompts = []

    def judge(prompt, model_cls, **kw):
        prompts.append(prompt)
        return Recorder(correct=True)(prompt, model_cls, **kw)

    settings = SimpleNamespace(processed_dir=tmp_path, llm_model="m", critic_model="c")
    runs_file = tmp_path / "runs.jsonl"
    runs_file.write_text(json.dumps(_run()) + "\n", encoding="utf-8")
    monkeypatch.setattr(runner, "load_benchmark", lambda: [ITEM])
    runner.run_benchmark(settings, driver=None, embedder=None, systems=("hybrid",), runs_file="runs.jsonl", rescore=True,
                         judge=judge, artifacts_dir=tmp_path / "out")
    assert any("DATA AS OF: 2026-09-25" in p for p in prompts)
