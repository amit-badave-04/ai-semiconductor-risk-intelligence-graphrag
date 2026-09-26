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
            answer_row("U1", "The context does not contain Samsung's revenue.")]
    s = bo.score_mechanical(rows, BENCH)
    assert s["mechanical"] == {"passed": 3, "of": 3} and s["failed_ids"] == []


def test_a_wrong_number_fails_and_is_named():
    s = bo.score_mechanical([answer_row("N2", "It was $190 billion [%s]." % VALID[1], [VALID[1]])], BENCH)
    assert s["mechanical"] == {"passed": 0, "of": 1} and s["failed_ids"] == ["N2"]


def test_citation_validity_counts_hallucinated_ids_and_verifier_failures_are_the_escalation_rate():
    rows = [answer_row("N2", "It was $215.9 billion [%s]." % VALID[1], [VALID[1]]),
            answer_row("D1", "TSMC [0001-25-000001:I.1:0099]", ["0001-25-000001:I.1:0099"], ["0001-25-000001:I.1:0099"]),
            answer_row("U1", "The context does not contain Samsung's revenue.")]
    s = bo.score_mechanical(rows, BENCH)
    assert s["citation_validity"] == pytest.approx(2 / 3)
    assert s["escalation_rate"] == pytest.approx(1 / 3) and s["escalation_ids"] == ["D1"]


def test_a_provider_error_row_fails_mechanically_and_escalates():
    s = bo.score_mechanical([answer_row("N2", "", error="RuntimeError: 400")], BENCH)
    assert s["mechanical"]["passed"] == 0 and s["escalation_rate"] == 1.0


def test_cost_and_latency_averages():
    s = bo.score_mechanical([answer_row("N2", "$215.9 billion [%s]" % VALID[1], [VALID[1]], cost=0.02),
                             answer_row("U1", "The context does not contain Samsung's revenue.", cost=0.04)], BENCH)
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
    assert out == {"open_correct": 1, "open_of": 1, "votes": {"T1": 2}, "errors": {}} and judge.calls == 3


def test_judge_open_survives_a_failing_judge_call():
    def flaky(prompt, model_cls, **kw):
        raise RuntimeError("judge down")

    out = bo.judge_open([answer_row("T1", "x", [VALID[0]])], BENCH, flaky, votes=3)
    assert out["open_correct"] == 0 and out["votes"] == {"T1": 0} and out["errors"] == {"T1": 3}


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
    """Four baseline rows (one per BENCH question) whose saved answers are right, so the baseline passes.

    (The temporal answer says "reworded", not "dropped": since the review an answer that claims a removal without citing
    the temporal block's removed lists is an ``unsupported_removal_claim`` and would count as an escalation.)"""
    good = {"N2": "It was $215.9 billion [%s]." % VALID[1], "D1": "Nvidia depends on TSMC [%s]." % VALID[0],
            "T1": "Yes, several risks were reworded [%s]." % VALID[0], "U1": "The context does not contain Samsung's revenue."}
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
        elif "Dropped risks?" in prompt:
            text = "Yes, several risks were reworded [%s]." % VALID[0]
        else:
            text = "The context does not contain Samsung's revenue."
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


# --- context-aware verifier and reuse of earlier judgements ---

def test_an_uncited_figure_is_an_escalation_even_when_it_is_grounded_but_still_scores_as_correct():
    """XBRL figures are citable ([xbrl:...]) since M1b, so a correct but uncited figure is escalated: the answer must cite it."""
    rows = [answer_row("N2", "Nvidia's revenue was $215.9 billion.", [])]
    without = bo.score_mechanical(rows, BENCH)
    with_ctx = bo.score_mechanical(rows, BENCH, contexts={"N2": context()})
    assert without["escalation_rate"] == 1.0 and with_ctx["escalation_rate"] == 1.0
    assert with_ctx["mechanical"] == {"passed": 1, "of": 1}


def test_run_bakeoff_reuses_a_previous_judgement_instead_of_paying_again(tmp_path):
    first = bo.run_bakeoff(_bench_rows(), BENCH, ["m/good"], complete=_by_question(True), judge=VoteJudge([True]),
                           runs_path=tmp_path / "b.jsonl", max_usd=None, votes=3, price=lambda u, m: 0.001)
    judge = VoteJudge([True])
    again = bo.run_bakeoff(_bench_rows(), BENCH, ["m/good"], complete=_by_question(True), judge=judge,
                           runs_path=tmp_path / "b.jsonl", max_usd=None, votes=3, price=lambda u, m: 0.001, previous=first)
    assert judge.calls == 0 and again["models"]["m/good"]["judged"] == first["models"]["m/good"]["judged"]


def test_a_report_is_stamped_with_the_judge_prompt_and_the_grading_notes(tmp_path):
    report = bo.run_bakeoff(_bench_rows(), BENCH, ["m/good"], complete=_by_question(True), judge=VoteJudge([True]),
                            runs_path=tmp_path / "b.jsonl", max_usd=None, votes=3, price=lambda u, m: 0.001)
    assert report["judge_stamp"] == bo.judge_stamp(BENCH, None, None)
    assert bo.judge_stamp(BENCH, None, None) != bo.judge_stamp(BENCH, "2026-09-25", None)               # the as-of date is in the prompt
    changed = [{**b, "judge_notes": "different notes"} if b["id"] == "T1" else b for b in BENCH]
    assert bo.judge_stamp(changed, None, None) != bo.judge_stamp(BENCH, None, None)                    # so are the notes
    assert bo.judge_stamp(BENCH, None, "other/model") != bo.judge_stamp(BENCH, None, None)             # and the judging model


def test_a_judgement_made_under_another_prompt_or_notes_or_without_a_stamp_is_never_reused(tmp_path):
    """The old bakeoff.json holds verdicts from the circular judge: reusing them would defeat the new instrument."""
    first = bo.run_bakeoff(_bench_rows(), BENCH, ["m/good"], complete=_by_question(True), judge=VoteJudge([True]),
                           runs_path=tmp_path / "b.jsonl", max_usd=None, votes=3, price=lambda u, m: 0.001)
    unstamped = {k: v for k, v in first.items() if k != "judge_stamp"}
    stale = {**first, "judge_stamp": "0" * 64}
    for previous in (unstamped, stale):
        judge = VoteJudge([True])
        bo.run_bakeoff(_bench_rows(), BENCH, ["m/good"], complete=_by_question(True), judge=judge, runs_path=tmp_path / "b.jsonl",
                       max_usd=None, votes=3, price=lambda u, m: 0.001, previous=previous)
        assert judge.calls == 6          # baseline + the model, judged again


def test_a_previous_judgement_made_with_a_different_vote_count_is_not_reused(tmp_path):
    first = bo.run_bakeoff(_bench_rows(), BENCH, ["m/good"], complete=_by_question(True), judge=VoteJudge([True]),
                           runs_path=tmp_path / "b.jsonl", max_usd=None, votes=3, price=lambda u, m: 0.001)
    judge = VoteJudge([True])
    bo.run_bakeoff(_bench_rows(), BENCH, ["m/good"], complete=_by_question(True), judge=judge,
                   runs_path=tmp_path / "b.jsonl", max_usd=None, votes=5, price=lambda u, m: 0.001, previous=first)
    assert judge.calls == 10  # baseline + the model, 5 votes each, one open question


# --- the deployed configuration, end to end (answer_stream with router + verifier + escalation) ---

def _fake_answer_stream(script):
    """answer_stream double: script maps a question -> the list of events to emit after 'retrieval'."""
    calls = []

    def stream(question, driver, embedder, strategy="hybrid", **kw):
        calls.append((question, kw))
        yield {"event": "retrieval", "anchors": {}, "counts": {}}
        yield from script[question]
    return stream, calls


def _done(text, cited, *, cost=0.001, routed="cheap", escalated=False, by="cheap/m", reasons=None, hallucinated=()):
    return {"event": "done", "answer": text, "citations": list(cited), "hallucinated": list(hallucinated),
            "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": cost,
            "routed": routed, "escalated": escalated, "answered_by": by, **({"escalation_reasons": reasons} if reasons else {})}


def test_run_deployed_records_the_route_cost_and_checkpoints(tmp_path, monkeypatch):
    stream, calls = _fake_answer_stream({
        "Revenue FY26?": [_done("It was $215.9 billion.", [])],
        "Dropped risks?": [_done("Yes, several.", [VALID[0]], routed="strong", by="strong/m", cost=0.03)]})
    monkeypatch.setattr(bo, "answer_stream", stream)
    bench = [b for b in BENCH if b["id"] in ("N2", "T1")]
    rows = bo.run_deployed(bench, None, None, tmp_path / "d.jsonl", model="cheap/m", escalation_model="strong/m", max_usd=None)
    assert [r["id"] for r in rows] == ["N2", "T1"] and rows[1]["routed"] == "strong" and rows[1]["cost_usd"] == 0.03
    assert calls[0][1]["model"] == "cheap/m" and calls[0][1]["escalation_model"] == "strong/m" and calls[0][1]["max_tokens"] == bo.ANSWER_MAX_TOKENS
    assert len((tmp_path / "d.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    bo.run_deployed(bench, None, None, tmp_path / "d.jsonl", model="cheap/m", escalation_model="strong/m", max_usd=None)
    assert len(calls) == 2  # resumed: nothing bought twice


def test_run_deployed_stops_at_the_cap(tmp_path, monkeypatch):
    stream, calls = _fake_answer_stream({b["q"]: [_done("x", [], cost=0.02)] for b in BENCH})
    monkeypatch.setattr(bo, "answer_stream", stream)
    with pytest.raises(AnswerBudgetExceeded):
        bo.run_deployed(BENCH, None, None, tmp_path / "d.jsonl", model="c/m", escalation_model="s/m", max_usd=0.03)
    assert len(calls) == 2


def test_run_deployed_turns_a_stream_error_into_a_failed_row(tmp_path, monkeypatch):
    stream, _ = _fake_answer_stream({"Revenue FY26?": [{"event": "error", "detail": "boom", "usage": None, "cost_usd": 0.004}]})
    monkeypatch.setattr(bo, "answer_stream", stream)
    [row] = bo.run_deployed([BENCH[0]], None, None, tmp_path / "d.jsonl", model="c/m", escalation_model="s/m", max_usd=None)
    assert row["error"] == "boom" and row["answer"] == "" and row["cost_usd"] == 0.004


def test_score_deployed_counts_routes_escalations_and_judges_the_open_questions():
    rows = [{"id": "N2", "answer": "It was $215.9 billion.", "cited": [], "hallucinated": [], "valid_ids": [], "finish_reason": "stop",
             "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.001, "latency_s": 2.0, "routed": "cheap", "escalated": False},
            {"id": "D1", "answer": "TSMC [%s]" % VALID[0], "cited": [VALID[0]], "hallucinated": [], "valid_ids": VALID, "finish_reason": "stop",
             "usage": None, "cost_usd": 0.03, "latency_s": 9.0, "routed": "cheap", "escalated": True, "answered_by": "strong/m"},
            {"id": "T1", "answer": "Yes [%s]" % VALID[0], "cited": [VALID[0]], "hallucinated": [], "valid_ids": VALID, "finish_reason": "stop",
             "usage": None, "cost_usd": 0.03, "latency_s": 9.0, "routed": "strong", "escalated": False}]
    s = bo.score_deployed(rows, BENCH, VoteJudge([True]), votes=3)
    assert s["routes"] == {"cheap": 2, "strong": 1} and s["escalated"] == 1
    assert s["mechanical"] == {"passed": 2, "of": 2} and s["judged"]["open_correct"] == 1
    assert s["total_cost_usd"] == pytest.approx(0.061) and s["avg_cost_usd"] == pytest.approx(0.061 / 3)


# --- saved (pre-M1b) contexts stay readable, but old dropped-lineage text is never relabelled "text-verified" ---

LEGACY = ("RELATIONSHIPS:\n- Nvidia DEPENDS_ON TSMC (status=Active) [0001-25-000001:I.1:0001]\n\nMETRICS:\n(none)\n\n"
          "ACTIVE RISKS:\n(none)\n\nDROPPED RISK LINEAGES:\n- Nvidia dropped a China risk [0001-25-000001:I.1:0001]\n\n"
          "EXCERPTS:\n[0001-25-000001:II.7:0002]\nRevenue was $215.9 billion.\n")


def test_a_pre_m1b_context_is_split_with_external_and_temporal_blocks_empty():
    blocks = bo.split_context(LEGACY)
    assert blocks.edges_block.startswith("- Nvidia DEPENDS_ON TSMC")
    assert blocks.external_block == "(none)" and blocks.temporal_block == "(none)"
    assert "China risk" not in bo.build_prompt("Q?", LEGACY)      # never re-rendered under the "text-verified" heading


def test_the_mechanical_score_hands_the_verifier_the_cited_sources_and_the_question(monkeypatch):
    seen = {}

    def spy(text, cited, valid, finish, context=None, *, sources=None, question=None):
        seen.update(sources=sources, question=question)
        return []

    monkeypatch.setattr(bo, "verify_answer", spy)
    row = {**base_row(), "cited": [VALID[1]], "hallucinated": False, "finish_reason": "stop", "latency_s": 1.0, "error": None,
           "answer": "Revenue was $215.9 billion [%s]." % VALID[1]}
    bo.score_mechanical([row], BENCH, contexts={"N2": context()})
    assert seen["question"] == "Revenue FY26?" and "Revenue was $215.9 billion." in seen["sources"][VALID[1]]
