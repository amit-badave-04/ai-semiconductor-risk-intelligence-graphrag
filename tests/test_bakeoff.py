"""Bake-off harness: prompt reconstruction, checkpointed spend-capped answering, mechanical scoring, judging."""

import json

import pytest

from semigraph.eval import bakeoff as bo
from semigraph.eval.runner import AnswerBudgetExceeded
from semigraph.retrieval.answerer import build_blocks

VALID = ["0001-25-000001:I.1:0001", "0001-25-000001:II.7:0002"]


def retrieval():
    return {"edges": [{"source": "Nvidia", "relation": "DEPENDS_ON", "target": "TSMC", "status": "Active", "chunk_ids": [VALID[0]]}],
            "metrics": [{"company": "Nvidia", "metric": "revenue", "period_start": "2025-01-27", "period_end": "2026-01-25", "value": 215938000000.0}],
            "risks": [{"company": "Nvidia", "category": "Supply", "summary": "Depends on TSMC.", "chunk_id": VALID[0]}],
            "temporal": [], "chunks": [{"chunk_id": VALID[1], "text": "Revenue was $215.9 billion."}]}


def context():
    return build_blocks(retrieval())[1]


def base_row(id_="N2", type_="numeric", q="Revenue FY26?", **over):
    return {"id": id_, "system": "hybrid", "type": type_, "q": q, "context": context(), "valid_ids": VALID,
            "answer": "Sonnet answer", "usage": {"prompt_tokens": 9000, "completion_tokens": 1000}, "cost_usd": 0.028, **over}


BENCH = [{"id": "N2", "type": "numeric", "q": "Revenue FY26?", "expect": {"value": 215938000000}},
         {"id": "D1", "type": "dependency", "q": "Who supplies Nvidia?", "expect": {"any_of": ["TSMC", "Samsung"]}},
         {"id": "T1", "type": "temporal", "q": "Dropped risks?", "judge_notes": "should affirm"},
         {"id": "U1", "type": "refusal", "q": "Samsung revenue?"}]


# --- prompt reconstruction ---

def test_split_context_inverts_build_blocks_exactly():
    blocks, full, _ = build_blocks(retrieval())
    assert bo.split_context(full) == blocks


def test_split_context_refuses_a_context_it_cannot_reproduce():
    with pytest.raises(ValueError, match="context"):
        bo.split_context("EXCERPTS:\nnothing else")


def test_build_prompt_puts_the_question_and_every_block_in_the_answer_template():
    row = base_row()
    p = bo.build_prompt(row["q"], row["context"])
    assert "Revenue FY26?" in p and "Depends on TSMC." in p and "Revenue was $215.9 billion." in p


# --- answering: checkpoint, resume, cap ---

class FakeComplete:
    def __init__(self, text="Revenue was $215.9 billion [%s]." % VALID[1], prompt_tokens=10_000, out=1_000, finish="stop"):
        self.calls, self.text, self.pt, self.out, self.finish = [], text, prompt_tokens, out, finish

    def __call__(self, model, prompt, max_tokens):
        self.calls.append((model, max_tokens))
        return {"text": self.text, "usage": {"prompt_tokens": self.pt, "completion_tokens": self.out},
                "finish_reason": self.finish, "latency_s": 1.5}


def test_answer_candidates_scores_each_row_and_checkpoints(tmp_path):
    fake, path = FakeComplete(), tmp_path / "b.jsonl"
    rows = bo.answer_candidates([base_row(), base_row("D1", "dependency", "Who?")], ["m/one"], fake, path, max_usd=None,
                                price=lambda usage, model: 0.01)
    assert len(rows) == 2 and len(fake.calls) == 2
    r = rows[0]
    assert r["model"] == "m/one" and r["cited"] == [VALID[1]] and r["hallucinated"] == [] and r["cost_usd"] == 0.01
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_answer_candidates_resumes_without_repaying(tmp_path):
    fake, path = FakeComplete(), tmp_path / "b.jsonl"
    args = ([base_row()], ["m/one"], fake, path)
    bo.answer_candidates(*args, max_usd=None, price=lambda u, m: 0.01)
    bo.answer_candidates(*args, max_usd=None, price=lambda u, m: 0.01)
    assert len(fake.calls) == 1


def test_answer_candidates_stops_at_the_spend_cap_and_keeps_what_it_bought(tmp_path):
    fake, path = FakeComplete(), tmp_path / "b.jsonl"
    rows = [base_row(f"Q{i}") for i in range(5)]
    with pytest.raises(AnswerBudgetExceeded):
        bo.answer_candidates(rows, ["m/one"], fake, path, max_usd=0.025, price=lambda u, m: 0.01)
    assert len(fake.calls) == 3  # 0.00, 0.01, 0.02 < cap -> asked; 0.03 >= cap -> refused
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3


def test_a_provider_error_is_recorded_as_a_failed_row_not_a_crash(tmp_path):
    def boom(model, prompt, max_tokens):
        raise RuntimeError("400 bad request")

    rows = bo.answer_candidates([base_row()], ["m/one"], boom, tmp_path / "b.jsonl", max_usd=None, price=lambda u, m: 0.0)
    assert rows[0]["error"].startswith("RuntimeError") and rows[0]["answer"] == ""


# --- mechanical scoring ---

def answer_row(id_, text, cited=(), hallucinated=(), finish="stop", cost=0.01, error=None):
    return {"model": "m/one", "id": id_, "answer": text, "cited": list(cited), "hallucinated": list(hallucinated),
            "valid_ids": VALID, "finish_reason": finish, "cost_usd": cost, "latency_s": 2.0,
            "usage": {"prompt_tokens": 10_000, "completion_tokens": 1_000}, **({"error": error} if error else {})}


def test_mechanical_scoring_covers_numeric_any_of_and_refusal():
    rows = [answer_row("N2", "It was $215.9 billion [%s]." % VALID[1], [VALID[1]]),
            answer_row("D1", "Nvidia depends on TSMC [%s]." % VALID[0], [VALID[0]]),
            answer_row("U1", "Samsung is not an SEC filer.")]
    s = bo.score_mechanical(rows, BENCH)
    assert s["mechanical"] == {"passed": 3, "of": 3} and s["failed_ids"] == []


def test_a_wrong_number_fails_and_is_named():
    s = bo.score_mechanical([answer_row("N2", "It was $190 billion [%s]." % VALID[1], [VALID[1]])], BENCH)
    assert s["mechanical"] == {"passed": 0, "of": 1} and s["failed_ids"] == ["N2"]


def test_citation_validity_counts_hallucinated_ids_and_verifier_failures_are_the_escalation_rate():
    rows = [answer_row("N2", "It was $215.9 billion [%s]." % VALID[1], [VALID[1]]),
            answer_row("D1", "TSMC [0001-25-000001:I.1:0099]", ["0001-25-000001:I.1:0099"], ["0001-25-000001:I.1:0099"]),
            answer_row("U1", "Samsung is not an SEC filer.")]
    s = bo.score_mechanical(rows, BENCH)
    assert s["citation_validity"] == pytest.approx(2 / 3)
    assert s["escalation_rate"] == pytest.approx(1 / 3) and s["escalation_ids"] == ["D1"]


def test_a_provider_error_row_fails_mechanically_and_escalates():
    s = bo.score_mechanical([answer_row("N2", "", error="RuntimeError: 400")], BENCH)
    assert s["mechanical"]["passed"] == 0 and s["escalation_rate"] == 1.0


def test_cost_and_latency_averages():
    s = bo.score_mechanical([answer_row("N2", "$215.9 billion [%s]" % VALID[1], [VALID[1]], cost=0.02),
                             answer_row("U1", "Samsung is not an SEC filer.", cost=0.04)], BENCH)
    assert s["avg_cost_usd"] == pytest.approx(0.03) and s["avg_latency_s"] == 2.0


# --- judge: majority of N votes over the open questions only ---

class VoteJudge:
    def __init__(self, votes):
        self.votes, self.calls = list(votes), 0

    def __call__(self, prompt, model_cls, **kw):
        v = self.votes[self.calls % len(self.votes)]
        self.calls += 1
        return model_cls(correct=v, reason="scripted")


def test_judge_open_uses_a_majority_and_only_open_questions():
    rows = [answer_row("T1", "Yes, dozens were dropped [%s]." % VALID[0], [VALID[0]]),
            answer_row("N2", "$215.9 billion [%s]" % VALID[1], [VALID[1]])]
    judge = VoteJudge([True, False, True])
    out = bo.judge_open(rows, BENCH, judge, votes=3)
    assert out == {"open_correct": 1, "open_of": 1, "votes": {"T1": 2}} and judge.calls == 3


def test_judge_open_survives_a_failing_judge_call():
    def flaky(prompt, model_cls, **kw):
        raise RuntimeError("judge down")

    out = bo.judge_open([answer_row("T1", "x", [VALID[0]])], BENCH, flaky, votes=3)
    assert out["open_correct"] == 0 and out["votes"] == {"T1": 0}


# --- estimate ---

def test_estimate_scales_with_candidates_and_the_judge_budget():
    est = bo.estimate([base_row(), base_row("D1")], [("m/one", 1.0, 2.0), ("m/two", 10.0, 20.0)], votes=3, open_questions=7)
    per_m = lambda i, o: 2 * (9000 * i / 1e6 + 1300 * o / 1e6)  # noqa: E731 — two rows of 9000 prompt tokens
    assert est["answers_usd"] == pytest.approx(per_m(1.0, 2.0) + per_m(10.0, 20.0))
    assert est["judge_worst_case_usd"] == pytest.approx(2 * 7 * 3 * bo.JUDGE_CALL_USD)
    assert est["total_worst_case_usd"] == pytest.approx(est["answers_usd"] + est["judge_worst_case_usd"] + est["baseline_rejudge_usd"])


def test_report_round_trips_as_json():
    json.dumps(bo.score_mechanical([answer_row("N2", "$215.9 billion [%s]" % VALID[1], [VALID[1]])], BENCH))


# --- gates and orchestration ---

GOOD_SCORE = {"mechanical": {"passed": 2, "of": 2}, "citation_validity": 1.0, "escalation_rate": 0.1, "errors": 0}


def test_free_gates_pass_a_clean_score_and_name_every_failure():
    assert bo.passes_free_gates(GOOD_SCORE) == []
    bad = {**GOOD_SCORE, "mechanical": {"passed": 1, "of": 2}, "citation_validity": 0.95, "escalation_rate": 0.2, "errors": 1}
    failed = bo.passes_free_gates(bad)
    assert len(failed) == 4 and any("mechanical" in f for f in failed) and any("escalation" in f for f in failed)


def _bench_rows():
    """Four baseline rows (one per BENCH question) whose saved answers are right, so the baseline passes."""
    good = {"N2": "It was $215.9 billion [%s]." % VALID[1], "D1": "Nvidia depends on TSMC [%s]." % VALID[0],
            "T1": "Yes, several risks were dropped [%s]." % VALID[0], "U1": "Samsung is not an SEC filer."}
    rows = []
    for b in BENCH:
        text = good[b["id"]]
        rows.append(base_row(b["id"], b["type"], b["q"], answer=text, cited=sorted(set(__import__("re").findall(r"\[([^\]]+)\]", text))),
                             hallucinated=[], latency_s=3.0))
    return rows


def _by_question(good: bool):
    def complete(model, prompt, max_tokens):
        if "Revenue FY26?" in prompt:
            text = ("It was $215.9 billion [%s]." % VALID[1]) if good else ("It was $190 billion [%s]." % VALID[1])
        elif "Who supplies" in prompt:
            text = "Nvidia depends on TSMC [%s]." % VALID[0]
        elif "Dropped" in prompt:
            text = "Yes, several risks were dropped [%s]." % VALID[0]
        else:
            text = "Samsung is not an SEC filer."
        return {"text": text, "finish_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 100}, "latency_s": 1.0}
    return complete


def test_run_bakeoff_judges_only_models_that_clear_the_free_gates(tmp_path):
    def complete(model, prompt, max_tokens):
        return _by_question(model == "m/good")(model, prompt, max_tokens)

    judge = VoteJudge([True])
    report = bo.run_bakeoff(_bench_rows(), BENCH, ["m/good", "m/bad"], complete=complete, judge=judge,
                            runs_path=tmp_path / "b.jsonl", max_usd=None, votes=3, price=lambda u, m: 0.001)
    assert report["models"]["m/good"]["gates_failed"] == [] and report["models"]["m/good"]["judged"]["open_correct"] == 1
    assert report["models"]["m/bad"]["gates_failed"] and report["models"]["m/bad"]["judged"] is None
    assert report["baseline"]["judged"]["open_correct"] == 1
    assert judge.calls == 3 + 3          # the good model and the baseline; the failing model costs no judge calls
    assert report["models"]["m/good"]["clears_all_gates"] is True and report["models"]["m/bad"]["clears_all_gates"] is False


def test_a_model_below_the_baseline_on_open_questions_does_not_clear_the_gates(tmp_path):
    votes_by_call = iter([True, True, True, False, False, False])  # baseline judged first (3 True), then the candidate (3 False)

    def judge(prompt, model_cls, **kw):
        return model_cls(correct=next(votes_by_call), reason="scripted")

    report = bo.run_bakeoff(_bench_rows(), BENCH, ["m/good"], complete=_by_question(True), judge=judge,
                            runs_path=tmp_path / "b.jsonl", max_usd=None, votes=3, price=lambda u, m: 0.001)
    assert report["baseline"]["judged"]["open_correct"] == 1
    assert report["models"]["m/good"]["judged"]["open_correct"] == 0
    assert report["models"]["m/good"]["clears_all_gates"] is False
