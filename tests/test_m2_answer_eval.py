"""M2 I6 answer-level harness: dry-run math, paid-mode refusals, deterministic control sample, checkpoint/resume, decision rule.

No database, network or paid API: answering and judging are fakes."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from semigraph.eval.runner import AnswerBudgetExceeded, Correct
from semigraph.retrieval.answerer import build_blocks

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "m2_answer_eval.py"
spec = importlib.util.spec_from_file_location("m2_answer_eval", SCRIPT)
ae = importlib.util.module_from_spec(spec)
sys.modules["m2_answer_eval"] = ae
spec.loader.exec_module(ae)

CID = "0001-25-000001:I.1:0001"


def context():
    r = {"edges": [], "metrics": [], "risks": [], "temporal": [], "chunks": [{"chunk_id": CID, "text": "Leases are a form of financing."}]}
    return build_blocks(r)[1]


def q(id_, type_="B", stratum="headroom", answer="Leases.", split="test"):
    return {"id": id_, "type": type_, "q": f"Question {id_}?", "answer": answer, "quotes": ["quote"], "split": split, "stratum": stratum}


def done_events(text="It is leases [" + CID + "].", cost=0.01):
    return [{"event": "done", "answer": text, "citations": [CID], "hallucinated": [], "finish_reason": "stop",
             "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": cost, "routed": "cheap", "escalated": False,
             "checks": {}}]


def error_events(name="RateLimitError", cost=0.002):
    return [{"event": "error", "detail": f"{name}: slow down", "usage": None, "cost_usd": cost}]


# --- questions -------------------------------------------------------------------------------------------------------

def test_control_sample_is_deterministic_and_order_independent():
    ids = [f"M2-X{i:02d}" for i in range(50)]
    a = ae.control_sample(ids, 15)
    assert a == ae.control_sample(reversed(ids), 15) and len(a) == 15 and a == sorted(a)
    assert a != ae.control_sample(ids, 15, seed="other")


def test_pick_questions_headroom_all_plus_control_and_test_only():
    bench = {"split": "test", "questions": [q(f"Q{i:02d}", split="test") for i in range(30)]}
    strata = {f"Q{i:02d}": ("headroom" if i < 10 else "control") for i in range(30)}
    picked = ae.pick_questions(bench, strata, control_n=5)
    assert [x["stratum"] for x in picked].count("headroom") == 10 and [x["stratum"] for x in picked].count("control") == 5
    assert ae.pick_questions(bench, strata, control_n=5, headroom_limit=4)[0]["id"] == "Q00"
    assert len(ae.pick_questions(bench, strata, control_n=5, headroom_limit=4)) == 9


def test_pick_questions_refuses_a_dev_question():
    bench = {"split": "test", "questions": [q("Q1", split="dev")]}
    with pytest.raises(ValueError, match="test split only"):
        ae.pick_questions(bench, {"Q1": "headroom"})


def test_real_test_split_plan_is_headroom_plus_fifteen():
    questions, est = ae.load_plan(None, 15)
    assert sum(x["stratum"] == "control" for x in questions) == 15
    assert all(x["split"] == "test" for x in questions)
    assert est["answers"] == len(questions) * 2
    assert ae.control_sample((x["id"] for x in questions if x["stratum"] == "control"), 15) == sorted(
        x["id"] for x in questions if x["stratum"] == "control")


def test_mechanical_expect_only_for_short_b_and_c():
    assert ae.mechanical_expect(q("B1", "B", answer="$849 million"))["any_of"] == ["$849 million"]
    assert ae.mechanical_expect(q("A1", "A")) is None
    assert ae.mechanical_expect(q("B2", "B", answer="one two three four five six seven eight nine")) is None


# --- dry run ----------------------------------------------------------------------------------------------------------

def test_estimate_math():
    est = ae.estimate(55, ae.ARMS, 0.0138, votes=3)
    assert est["answers"] == 110 and est["judge_calls_worst_case"] == 330
    assert est["judge_worst_case_usd"] == pytest.approx(330 * 0.01)
    assert est["answers_expected_usd"] == pytest.approx(110 * 0.0138)
    assert est["total_worst_case_usd"] == pytest.approx(110 * 0.0138 * ae.ANSWER_WORST_FACTOR + 3.3)


def test_dry_run_spends_nothing_and_prints_counts(capsys, monkeypatch):
    monkeypatch.setattr(ae, "run_paid", lambda *a, **k: pytest.fail("a dry run must not reach the paid path"))
    assert ae.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run, nothing spent" in out and "arms: off, on" in out and "judge worst case" in out and "worst case" in out


def test_paid_mode_refuses_without_cap_or_confirm(capsys, monkeypatch):
    monkeypatch.setattr(ae, "run_paid", lambda *a, **k: pytest.fail("must refuse before paying"))
    assert ae.main([]) == 2
    assert ae.main(["--max-usd", "5"]) == 2
    assert ae.main(["--confirm-paid"]) == 2
    assert "refusing" in capsys.readouterr().err


def test_refuses_a_cap_below_expected_cost_plus_judge_reserve():
    est = ae.estimate(55, ae.ARMS, 0.0138)
    assert ae.refusals(est, max_usd=1.0, confirm_paid=True)
    assert ae.refusals(est, max_usd=5.0, confirm_paid=True) == []
    assert ae.refusals(est, max_usd=5.0, confirm_paid=False) and ae.refusals(est, max_usd=0, confirm_paid=True)


# --- run, checkpoint, resume, cap, retry ----------------------------------------------------------------------------

def test_run_checkpoints_and_resumes_without_rebuying(tmp_path):
    runs, calls = tmp_path / "runs.jsonl", []

    def answer_one(arm, item):
        calls.append((arm, item["id"]))
        return done_events(), context()

    qs = [q("Q1"), q("Q2")]
    rows = ae.run_arms(qs, ae.ARMS, answer_one, runs, max_usd=1.0, sleep=lambda s: None)
    assert len(rows) == 4 and len(runs.read_text().splitlines()) == 4 and calls[0] == ("off", "Q1")
    assert {r["arm"] for r in rows} == {"off", "on"} and all(isinstance(r["context"], str) for r in rows)
    ae.run_arms(qs + [q("Q3")], ae.ARMS, answer_one, runs, max_usd=1.0, sleep=lambda s: None)
    assert len(calls) == 6                      # only Q3's two answers were bought on the resume


def test_stops_at_the_cap_and_keeps_what_was_bought(tmp_path):
    runs = tmp_path / "runs.jsonl"
    with pytest.raises(AnswerBudgetExceeded):
        ae.run_arms([q("Q1"), q("Q2"), q("Q3")], ae.ARMS, lambda a, i: (done_events(cost=0.02), context()), runs, max_usd=0.05,
                    sleep=lambda s: None)
    assert len(runs.read_text().splitlines()) == 3        # 0.02 x 3 = 0.06 >= 0.05: the fourth is never bought


def test_transient_error_is_retried_and_its_spend_kept(tmp_path):
    attempts = []

    def answer_one(arm, item):
        attempts.append(arm)
        return (error_events(), context()) if len(attempts) < 3 else (done_events(cost=0.01), context())

    rows = ae.run_arms([q("Q1")], ["off"], answer_one, tmp_path / "r.jsonl", max_usd=1.0, sleep=lambda s: None)
    assert len(attempts) == 3 and not rows[0].get("error")
    assert rows[0]["cost_usd"] == pytest.approx(0.002 + 0.002 + 0.01)


def test_persistent_transient_error_is_recorded_not_raised(tmp_path):
    rows = ae.run_arms([q("Q1")], ["off"], lambda a, i: (error_events(), context()), tmp_path / "r.jsonl", max_usd=1.0,
                       sleep=lambda s: None)
    assert rows[0]["error"].startswith("RateLimitError") and rows[0]["cost_usd"] == pytest.approx(0.006)


def test_non_transient_error_is_not_retried(tmp_path):
    calls = []
    ae.run_arms([q("Q1")], ["off"], lambda a, i: (calls.append(1) or error_events("ValueError"), context()), tmp_path / "r.jsonl",
                max_usd=1.0, sleep=lambda s: None)
    assert len(calls) == 1


# --- scoring --------------------------------------------------------------------------------------------------------

class FakeJudge:
    def __init__(self, verdicts):
        self.verdicts, self.calls = verdicts, 0

    def __call__(self, prompt, model_cls, **kw):
        self.calls += 1
        return model_cls(correct=self.verdicts.pop(0), reason="r")


def rows_for(answers, arm="off"):
    return [{"id": i, "arm": arm, "answer": a, "cited": [CID], "hallucinated": [], "valid_ids": [CID], "context": context(),
             "cost_usd": 0.01, "finish_reason": "stop", "checks": {}, **({"error": "boom"} if a is None else {})}
            for i, a in answers.items()]


def test_needle_hit_skips_the_judge_and_a_miss_goes_to_it():
    items = {"B1": q("B1", "B", answer="Leases."), "B2": q("B2", "B", answer="Leases."), "A1": q("A1", "A")}
    judge = FakeJudge([True, True, False,      # B2 (needle miss) majority true
                       False, False, True])    # A1 majority false
    s = ae.score_arm(rows_for({"B1": "It says leases [" + CID + "].", "B2": "Equipment loans [" + CID + "].",
                               "A1": "No idea [" + CID + "]."}), items, judge)
    assert s["correct"] == {"B1": True, "B2": True, "A1": False} and s["needle_hits"] == ["B1"] and judge.calls == 6


def test_error_rows_are_incorrect_and_never_judged():
    judge = FakeJudge([])
    s = ae.score_arm([{**rows_for({"A1": None})[0], "answer": ""}], {"A1": q("A1", "A")}, judge)
    assert s["correct"] == {"A1": False} and judge.calls == 0 and s["errors"] == ["A1"]


def test_caching_judge_replays_votes_on_resume(tmp_path):
    path, inner = tmp_path / "j.jsonl", FakeJudge([True, False, True])
    j1 = ae.caching_judge(path, inner)
    assert [j1("p", Correct).correct for _ in range(3)] == [True, False, True] and inner.calls == 3
    again = FakeJudge([])
    j2 = ae.caching_judge(path, again)
    assert [j2("p", Correct).correct for _ in range(3)] == [True, False, True] and again.calls == 0


# --- the decision rule ----------------------------------------------------------------------------------------------

def outcomes(head_off, head_on, ctrl_off, ctrl_on):
    strata, off, on = {}, {}, {}
    for kind, a, b in (("headroom", head_off, head_on), ("control", ctrl_off, ctrl_on)):
        for n, (x, y) in enumerate(zip(a, b, strict=True)):
            i = f"{kind[0]}{n}"
            strata[i], off[i], on[i] = kind, x, y
    return off, on, strata


def test_decision_passes_with_wins_over_losses_and_one_control_loss():
    off, on, strata = outcomes([0, 0, 0, 1], [1, 1, 1, 0], [1, 1, 1], [1, 1, 0])
    d = ae.decide(off, on, strata)
    assert d["decision"] == "pass" and (d["headroom"]["wins"], d["headroom"]["losses"]) == (3, 1) and d["control"]["losses"] == 1


def test_decision_fails_on_two_headroom_losses_even_with_more_wins():
    off, on, strata = outcomes([0] * 5 + [1, 1], [1] * 5 + [0, 0], [1], [1])
    assert ae.decide(off, on, strata)["decision"] == "fail"


def test_decision_fails_when_wins_do_not_exceed_losses():
    off, on, strata = outcomes([0, 1], [1, 0], [1], [1])
    assert ae.decide(off, on, strata)["decision"] == "fail"


def test_decision_fails_on_two_control_losses():
    off, on, strata = outcomes([0, 0], [1, 1], [1, 1, 1], [0, 0, 1])
    d = ae.decide(off, on, strata)
    assert d["decision"] == "fail" and d["control"]["losses"] == 2 and "no answer value" in d["verdict"]


def test_decision_is_incomplete_when_an_arm_lacks_an_answer():
    off, on, strata = outcomes([0, 0], [1, 1], [1], [1])
    on.pop("h0")
    d = ae.decide(off, on, strata)
    assert d["decision"] == "incomplete" and d["missing_ids"] == ["h0"]


def test_sign_test_and_interval():
    assert ae.sign_test_p(28, 0) == pytest.approx(2 / 2 ** 28)
    assert ae.sign_test_p(0, 0) == 1.0 and ae.sign_test_p(3, 3) == 1.0
    lo, hi = ae.wilson(8, 10)
    assert 0.4 < lo < 0.5 < 0.8 < hi < 1.0 and ae.wilson(0, 0) is None


# --- review fixes (Opus review of I6) ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("needle,answer,hit", [
    ("$2.7 billion", "It fell from $12.7 billion.", False),          # substring of a bigger figure
    ("80%", "Margins reached 180%.", False),
    ("$6,085 million", "Revenue was $16,085 million.", False),
    ("$849 million", "It was $849 million [x].", True),
    ("$849 million", "It was $849 million, up from $700 million.", False),   # another amount: the judge decides
    ("80%", "It was 80% of sales; the answer is 80%.", True),
    ("80%", "It was 80%, versus 62% before.", False),
    ("Leases", "The answer is leases.", True),
    ("Leases", "Releases are different.", False),
    ("advanced computing", "advanced-computing controls", True),
])
def test_needle_hit_uses_boundaries_and_rejects_multi_figure_answers(needle, answer, hit):
    assert ae.needle_hit([needle], answer) is hit


def test_mechanical_expect_has_no_dollar_stripped_variant():
    assert ae.mechanical_expect(q("C1", "C", answer="$2.7 billion"))["any_of"] == ["$2.7 billion"]


def test_a_boundary_false_positive_goes_to_the_judge_not_to_correct():
    items = {"C1": q("C1", "C", answer="$2.7 billion")}
    s = ae.score_arm(rows_for({"C1": "It was $12.7 billion [" + CID + "]."}), items, FakeJudge([False, False, False]))
    assert s["correct"] == {"C1": False} and s["needle_hits"] == []


def walk_keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk_keys(v)


def test_report_has_no_spend_fields():
    questions = [q("Q1", "A", "headroom")]
    s = ae.score_arm(rows_for({"Q1": "x [" + CID + "]."}), {"Q1": questions[0]}, FakeJudge([True, True, True]))
    report = ae.build_report(questions, {"off": s, "on": s}, {"decision": "pass"}, runs_file=Path("runs.jsonl"), headroom_limit=None)
    assert not [k for k in walk_keys(report) if "usd" in k.lower() or k in ("estimate", "cost")]


def test_reranker_must_load_or_the_run_stops():
    assert ae.require_reranker(lambda key: "model", "key") == "model"
    with pytest.raises(SystemExit, match="reranker"):
        ae.require_reranker(lambda key: None, "key")


def test_on_arm_row_without_a_rerank_stage_is_a_deviation_and_blocks_the_decision(tmp_path):
    rows = ae.run_arms([q("Q1")], ["on"], lambda a, i: (done_events(), context(), {"rerank_ok": False}), tmp_path / "r.jsonl",
                       max_usd=1.0, sleep=lambda s: None)
    assert rows[0]["rerank_ok"] is False
    s = ae.score_arm(rows, {"Q1": q("Q1", "A")}, FakeJudge([True, True, True]))
    assert s["rerank_deviations"] == ["Q1"] and "Q1" in ae.unreliable_ids(s)


def test_resume_retries_a_transient_error_row(tmp_path):
    runs, calls = tmp_path / "r.jsonl", []
    ae.run_arms([q("Q1")], ["off"], lambda a, i: (error_events(), context()), runs, max_usd=1.0, sleep=lambda s: None)
    rows = ae.run_arms([q("Q1")], ["off"], lambda a, i: (calls.append(1) or done_events(), context()), runs, max_usd=1.0,
                       sleep=lambda s: None)
    assert len(calls) == 1 and not rows[0].get("error")


def test_resume_counts_earlier_spend_including_superseded_rows(tmp_path):
    runs = tmp_path / "r.jsonl"
    ae.run_arms([q("Q1")], ["off"], lambda a, i: (error_events(cost=0.02), context()), runs, max_usd=1.0, sleep=lambda s: None)
    assert ae.runs_spend(runs) == pytest.approx(0.06)
    with pytest.raises(AnswerBudgetExceeded):
        ae.run_arms([q("Q1")], ["off"], lambda a, i: (done_events(), context()), runs, max_usd=0.05, sleep=lambda s: None)


def test_transient_error_or_judge_error_makes_the_decision_incomplete():
    off, on, strata = outcomes([0, 0], [1, 1], [1], [1])
    d = ae.decide(off, on, strata, unreliable=["h1"])
    assert d["decision"] == "incomplete" and d["unreliable_ids"] == ["h1"]
    items = {"A1": q("A1", "A")}
    rows = [{**rows_for({"A1": "x"})[0]}, ]

    def bad_judge(prompt, model_cls, **kw):
        raise RuntimeError("provider down")

    s = ae.score_arm(rows, items, bad_judge)
    assert "A1" in ae.unreliable_ids(s)
    err = ae.score_arm([{**rows_for({"A1": None})[0], "error": "RateLimitError: x"}], items, FakeJudge([]))
    assert err["transient_errors"] == ["A1"] and "A1" in ae.unreliable_ids(err)


def test_snapshot_must_match_the_strata():
    ae.check_snapshot("snap-1", "snap-1")
    with pytest.raises(SystemExit, match="snapshot"):
        ae.check_snapshot("snap-2", "snap-1")
    with pytest.raises(SystemExit, match="snapshot"):
        ae.check_snapshot("", "snap-1")


def test_run_tag_mismatch_refuses_to_resume(tmp_path):
    runs = tmp_path / "r.jsonl"
    fn = lambda a, i: (done_events(), context())  # noqa: E731
    ae.run_arms([q("Q1")], ["off"], fn, runs, max_usd=1.0, sleep=lambda s: None, run_tag={"frozen": "a", "snapshot": "s"})
    ae.run_arms([q("Q1"), q("Q2")], ["off"], fn, runs, max_usd=1.0, sleep=lambda s: None, run_tag={"frozen": "a", "snapshot": "s"})
    with pytest.raises(SystemExit, match="different run"):
        ae.run_arms([q("Q3")], ["off"], fn, runs, max_usd=1.0, sleep=lambda s: None, run_tag={"frozen": "b", "snapshot": "s"})


def test_one_answer_cannot_overshoot_the_cap(tmp_path):
    calls = []
    with pytest.raises(AnswerBudgetExceeded):
        ae.run_arms([q("Q1")], ["off"], lambda a, i: (calls.append(1) or done_events(), context()), tmp_path / "r.jsonl",
                    max_usd=0.05, answer_worst_usd=0.06, sleep=lambda s: None)
    assert calls == []


def test_missing_cost_report_is_flagged_and_refuses_a_paid_run(tmp_path):
    usd, source = ae.per_answer_usd(tmp_path / "missing.json")
    assert source == "fallback" and usd == ae.ANSWER_CALL_FLOOR_USD
    est = ae.estimate(55, ae.ARMS, usd, source=source)
    assert any("cost report" in p for p in ae.refusals(est, max_usd=50.0, confirm_paid=True))
    assert "fallback" in ae.format_estimate(est, [q("Q", stratum="headroom")])
    assert ae.per_answer_usd()[1] == "deployed eval report"


def test_ledger_caps_the_m2_total_and_stops_after_two_failed_runs(tmp_path):
    led = ae.Ledger(tmp_path / "ledger.jsonl")
    est = ae.estimate(55, ae.ARMS, 0.0138)
    cap, problems = ae.ledger_gate(led, max_usd=20.0, est=est)
    assert cap == ae.M2_CAP_USD and problems == []
    led.append("i6", 14.0, failed=False)
    cap, problems = ae.ledger_gate(led, max_usd=20.0, est=est)
    assert cap == pytest.approx(1.0) and problems            # 1.0 cannot cover expected + judge reserve
    led2 = ae.Ledger(tmp_path / "l2.jsonl")
    led2.append("i6", 0.1, failed=True)
    led2.append("i6", 0.1, failed=True)
    assert any("failed" in p for p in ae.ledger_gate(led2, max_usd=20.0, est=est)[1])
    assert ae.Ledger(tmp_path / "l2.jsonl").spent() == pytest.approx(0.2)    # persisted across instances


# --- S-05: a non-finite --max-usd must not disable the caps ---

@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_non_finite_max_usd_is_rejected_at_the_command_line(bad, capsys):
    with pytest.raises(SystemExit):
        ae.parse_args([f"--max-usd={bad}"])
    assert "finite" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_refusals_and_ledger_gate_reject_non_finite_cap(bad, tmp_path):
    est = ae.estimate(55, ae.ARMS, 0.0138)
    assert ae.refusals(est, max_usd=bad, confirm_paid=True)
    _, problems = ae.ledger_gate(ae.Ledger(tmp_path / "l.jsonl"), max_usd=bad, est=est)
    assert problems


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_run_arms_stops_on_a_non_finite_cap(bad, tmp_path):
    with pytest.raises(AnswerBudgetExceeded):
        ae.run_arms([q("Q1")], ["off"], lambda a, i: (done_events(), context()), tmp_path / "r.jsonl", max_usd=bad,
                    sleep=lambda s: None)


def test_run_arms_stops_on_a_non_finite_recorded_spend(tmp_path):
    runs = tmp_path / "r.jsonl"
    runs.write_text(json.dumps({"arm": "off", "id": "Q0", "cost_usd": float("nan")}) + "\n", encoding="utf-8")
    with pytest.raises(AnswerBudgetExceeded):
        ae.run_arms([q("Q1")], ["off"], lambda a, i: (done_events(), context()), runs, max_usd=5.0, sleep=lambda s: None)


# --- S-06: the live run reads a local graph only ---

def test_live_run_refuses_a_non_local_graph_before_connecting(monkeypatch):
    from types import SimpleNamespace

    from semigraph.graph import client

    monkeypatch.setattr(client, "get_driver", lambda s: pytest.fail("must not connect to a non-local graph"))
    settings = SimpleNamespace(neo4j_uri="neo4j+s://prod.example.com:7687", escalation_model="x")
    with pytest.raises(SystemExit, match="local graph"):
        ae.live_answer_one(settings)


def test_ledger_gate_refuses_when_a_ledger_row_has_a_nan_spend(tmp_path):
    """Verify follow-up: min(max_usd, 15 - nan) is the finite max_usd, so the ledger spend itself must be checked."""
    led = tmp_path / "l.jsonl"
    led.write_text('{"step": "x", "usd": NaN, "failed": false}\n', encoding="utf-8")
    est = ae.estimate(55, ae.ARMS, 0.0138)
    cap, problems = ae.ledger_gate(ae.Ledger(led), max_usd=5.0, est=est)
    assert problems and cap == 0.0


# --- dev split with the served rerank setting (decision 012) -------------------------------------------------------------

def test_defaults_are_the_recorded_test_run():
    args = ae.parse_args([])
    assert (args.split, args.on_arm) == ("test", "frozen")
    assert ae.default_runs_name("test", "frozen") == "m2_answer_eval_runs.jsonl"
    assert ae.report_path("test", "frozen") == ae.OUT_REPORT


def test_dev_served_names_never_collide_with_the_test_run():
    assert ae.default_runs_name("dev", "served") == "m2_answer_eval_dev_served_runs.jsonl"
    assert ae.report_path("dev", "served").name == "m2_answer_eval.dev-served.json"
    names = {ae.default_runs_name(s, a) for s in ae.SPLITS for a in ae.ON_ARMS}
    paths = {ae.report_path(s, a) for s in ae.SPLITS for a in ae.ON_ARMS}
    assert len(names) == 4 and len(paths) == 4


def test_pick_questions_split_dev_refuses_test_questions_and_vice_versa():
    dev = {"split": "dev", "questions": [q("D1", split="dev")]}
    assert ae.pick_questions(dev, {"D1": "headroom"}, split="dev")[0]["id"] == "D1"
    with pytest.raises(ValueError, match="dev split only"):
        ae.pick_questions({"split": "test", "questions": [q("Q1", split="test")]}, {"Q1": "headroom"}, split="dev")
    with pytest.raises(ValueError, match="test split only"):
        ae.pick_questions(dev, {"D1": "headroom"})


def test_real_dev_plan_uses_baseline_context_hit_for_strata():
    questions, est = ae.load_plan(None, 15, "dev", "served")
    report = json.loads(ae.DEV_REPORT.read_text(encoding="utf-8"))["questions"]
    assert all(x["split"] == "dev" for x in questions)
    for x in questions:
        assert (x["stratum"] == "control") == bool(report[x["id"]]["context_hit"])
    assert sum(x["stratum"] == "headroom" for x in questions) == sum(not v["context_hit"] for v in report.values())
    assert sum(x["stratum"] == "control" for x in questions) == 15
    assert est["answers"] == len(questions) * 2


def test_dev_served_dry_run_prints_the_split_arm_and_worst_case(capsys, monkeypatch):
    monkeypatch.setattr(ae, "run_paid", lambda *a, **k: pytest.fail("a dry run must not reach the paid path"))
    assert ae.main(["--split", "dev", "--on-arm", "served", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dev split only" in out and "arms: off, on" in out and "served rerank setting" in out
    assert "worst case" in out and "judge worst case" in out


def test_test_dry_run_text_is_unchanged_for_the_defaults(capsys):
    assert ae.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "test split only" in out and "served" not in out


def test_served_report_names_the_arm_config_and_carries_no_spend():
    questions = [q("Q1", "A", "headroom")]
    s = ae.score_arm(rows_for({"Q1": "x [" + CID + "]."}), {"Q1": questions[0]}, FakeJudge([True, True, True]))
    report = ae.build_report(questions, {"off": s, "on": s}, {"decision": "pass"}, runs_file=Path("r.jsonl"),
                             headroom_limit=None, split="dev", on_arm="served")
    assert report["split"] == "dev" and report["on_arm"] == "served"
    assert report["on_arm_config"]["rerank_max_candidates"] == 25 and "frozen_settings_sha256" not in report
    assert not [k for k in walk_keys(report) if "usd" in k.lower() or k in ("estimate", "cost")]
