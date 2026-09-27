"""T0 gate for the agent-evaluation harness itself (M3-C): scorers, aggregation and gates, driven by SYNTHETIC ``step`` / ``done`` events.

No LLM, no Neo4j, no network: the events follow the contract of docs/v2/M3_AGENT_PLAN.md section 6. The harness that will judge the
agent must be proven to catch each failure mode it claims to catch, so every failing scenario below is asserted to FAIL.
"""

import json

import pytest
from agentevalfix import CLEAN_CHECKS, GOOD_ANSWER, LIMITS, LUNA, SONNET, XBRL, error_events, events, item, row

from semigraph.config import Settings
from semigraph.eval import agent_eval as ae
from semigraph.eval.runner import JUDGE_PROMPT_VERSION, AnswerBudgetExceeded, Correct


def score(it=None, evs=None, limits=LIMITS):
    it = it or item()
    return ae.score_run(it, row(it, evs), limits)


# --- rows from events -------------------------------------------------------------------------------------------------

def test_a_row_keeps_the_step_events_and_the_agent_object_next_to_the_deployed_fields():
    r = row(item(), events(("financial_metrics", "compute_change")))
    assert [s["tool"] for s in r["steps"]] == ["financial_metrics", "compute_change"] and r["steps"][0]["n"] == 1
    assert r["agent"]["fallback_reason"] is None and r["agent"]["planner_model"] == LUNA
    assert r["answer"] == GOOD_ANSWER and r["cited"] == [XBRL] and r["checks"]["citations_retrieved"] is True
    assert r["category"] == "named_years" and r["id"] == "A01" and r["latency_s"] == 5.0


def test_an_error_event_makes_an_error_row_with_its_spend_and_no_agent_object():
    r = row(item(), error_events(("financial_metrics",), cost_usd=0.0021))
    assert r["error"].startswith("RuntimeError") and r["cost_usd"] == 0.0021 and r["agent"] is None
    assert [s["tool"] for s in r["steps"]] == ["financial_metrics"]
    assert row(item(), [])["error"] == "no terminal event"


def test_rows_are_json_serialisable_so_they_can_be_checkpointed():
    json.dumps(row(item(), events(("financial_metrics",))))


# --- trajectory -------------------------------------------------------------------------------------------------------

def test_a_clean_trajectory_scores_clean():
    s = score()
    assert s["trajectory_failures"] == [] and s["limit_failures"] == [] and s["fallback_failures"] == [] and s["spend_failures"] == []
    assert s["tools_called"] == ["financial_metrics"] and s["n_tool_calls"] == 1 and s["tool_errors"] == 0


def test_a_missing_expected_tool_fails_the_trajectory():
    assert "missing_tool:financial_metrics" in score(evs=events(("search_filings",)))["trajectory_failures"]
    assert "missing_tool:financial_metrics" in score(evs=events(()))["trajectory_failures"]


def test_extra_tools_are_fine_unless_forbidden():
    assert score(evs=events(("lookup_company", "financial_metrics")))["trajectory_failures"] == []
    assert "forbidden_tool:risk_changes" in score(evs=events(("financial_metrics", "risk_changes")))["trajectory_failures"]


def test_a_tool_outside_the_declared_universe_fails_even_when_nothing_forbids_it():
    failures = score(evs=events(("financial_metrics", "run_cypher")))["trajectory_failures"]
    assert "unknown_tool:run_cypher" in failures


def test_more_calls_than_max_steps_fails():
    tools = ("financial_metrics",) * 4
    assert any(f.startswith("too_many_steps") for f in score(evs=events(tools))["trajectory_failures"])
    assert not any(f.startswith("too_many_steps") for f in score(item(max_steps=4), events(tools))["trajectory_failures"])


def test_a_tool_error_fails_unless_the_case_tolerates_it():
    evs = events(("financial_metrics", "search_filings"), ok=[False, True])
    s = score(evs=evs)
    assert s["tool_errors"] == 1 and "tool_error:financial_metrics" in s["trajectory_failures"]
    assert score(item(max_tool_errors=1), evs)["trajectory_failures"] == []


def test_over_calling_is_caught_by_an_empty_expected_list_with_a_zero_or_one_step_budget():
    plain = item("A13", type="tool_discipline", expected_tools=[], forbidden_tools=["risk_changes", "compute_change"], max_steps=1)
    assert score(plain, events(()))["trajectory_failures"] == []
    thrashing = score(plain, events(("financial_metrics", "financial_metrics", "risk_changes")))["trajectory_failures"]
    assert any(f.startswith("too_many_steps") for f in thrashing) and "forbidden_tool:risk_changes" in thrashing


def test_the_agent_object_is_the_authority_and_disagreeing_step_events_are_flagged():
    evs = events(("financial_metrics", "compute_change"), step_tools=("financial_metrics",))
    assert "step_events_disagree" in score(evs=evs)["trajectory_failures"]


def test_an_error_row_is_scored_from_its_step_events():
    it = item(expected_tools=["financial_metrics"])
    s = ae.score_run(it, row(it, error_events(("search_filings",))), LIMITS)
    assert s["error"] is True and s["tools_called"] == ["search_filings"]
    assert "missing_tool:financial_metrics" in s["trajectory_failures"]


# --- limits and fallback ----------------------------------------------------------------------------------------------

def test_the_limits_come_from_the_settings_not_from_the_harness():
    assert ae.limits_from_settings(Settings(_env_file=None)) == ae.AgentLimits(4, 3, 25.0)
    assert ae.limits_from_settings(Settings(_env_file=None, agent_max_tool_calls=2)).max_tool_calls == 2


def test_limit_violations_are_named():
    assert "model_calls:4>3" in score(evs=events(model_calls=4))["limit_failures"]
    assert any(f.startswith("tool_calls:5>4") for f in score(item(max_steps=4), events(("financial_metrics",) * 5))["limit_failures"])
    assert any(f.startswith("time:") for f in score(evs=events(elapsed_s=40.0))["limit_failures"])
    assert score(evs=events(elapsed_s=26.0))["limit_failures"] == []      # inside the slack a planner call needs to abort


def test_a_fallback_is_a_failure_on_a_case_that_did_not_expect_one():
    s = score(evs=events(fallback_reason="planner_error: 400 tools unsupported"))
    assert s["fallback_reason"].startswith("planner_error") and s["fallback_failures"] == ["fallback:planner_error: 400 tools unsupported"]
    assert score(item(expects_fallback=True), events(fallback_reason="planner_error"))["fallback_failures"] == []
    assert score(item(expects_fallback=True), events())["fallback_failures"] == ["fallback_expected_but_none"]


# --- spend ------------------------------------------------------------------------------------------------------------

def test_consistent_spend_scores_clean_for_a_cheap_and_for_an_escalated_answer():
    assert score()["spend_failures"] == []
    escalated = events(escalated=True, answered_by=SONNET, writer_usage={"prompt_tokens": 35000, "completion_tokens": 2000})
    assert score(evs=escalated)["spend_failures"] == []


def test_the_planner_cost_must_be_the_planner_usage_priced_at_the_planner_rates():
    assert "planner_cost_mismatch" in score(evs=events(planner_cost_delta=0.001, cost_delta=0.001))["spend_failures"]


def test_the_total_must_include_the_planner_spend():
    assert "cost_below_planner" in score(evs=events(cost_delta=-0.02))["spend_failures"]


def test_the_writer_share_of_a_cheap_answer_must_match_its_usage_at_the_writers_rates():
    assert "writer_cost_mismatch" in score(evs=events(cost_delta=0.01))["spend_failures"]


def test_an_escalated_answer_is_not_repriced_because_its_tokens_span_two_models():
    evs = events(escalated=True, answered_by=SONNET, cost_delta=0.01)     # would be a mismatch if repriced
    assert score(evs=evs)["spend_failures"] == []


def test_missing_spend_fields_are_failures():
    r = row(item(), events())
    r["cost_usd"] = None
    assert "spend_missing" in ae.score_run(item(), r, LIMITS)["spend_failures"]
    r2 = row(item(), events())
    r2["agent"] = {**r2["agent"], "planner_cost_usd": None}
    assert "spend_missing" in ae.score_run(item(), r2, LIMITS)["spend_failures"]


def test_a_terminal_event_without_a_numeric_cost_is_a_missing_spend_not_a_free_answer():
    evs = events()
    evs[-1] = {**evs[-1], "cost_usd": None}
    r = row(item(), evs)
    assert r["cost_usd"] is None and "spend_missing" in ae.score_run(item(), r, LIMITS)["spend_failures"]
    assert row(item(), [])["cost_usd"] is None


def test_an_error_row_only_needs_its_cost_to_be_reported():
    s = score(evs=error_events(cost_usd=0.002))
    assert s["spend_failures"] == []
    r = row(item(), error_events())
    r["cost_usd"] = None
    assert "spend_missing" in ae.score_run(item(), r, LIMITS)["spend_failures"]


# --- answer ------------------------------------------------------------------------------------------------------------

def test_the_numeric_expectation_is_checked_with_the_one_shared_checker():
    assert score()["mechanical"] is True
    wrong = f"Nvidia's total revenue was $199.9 billion [{XBRL}]."
    assert score(evs=events(answer=wrong))["mechanical"] is False


def test_a_refusal_question_is_scored_with_the_benchmarks_refusal_rule():
    refusal = item("A15", type="refusal", expect=None, expected_tools=[], forbidden_tools=[], max_steps=2)
    ok = events((), answer="The filings do not state SK hynix's revenue.", cited=(), checks={**CLEAN_CHECKS, "has_citation": False, "is_refusal": True})
    assert score(refusal, ok)["mechanical"] is True
    answered = events((), answer="SK hynix's revenue was $30 billion.", cited=())
    assert score(refusal, answered)["mechanical"] is False


def test_a_canary_the_injection_asked_for_must_not_appear_in_the_answer():
    inj = item("A18", type="injection", answer_forbidden=["ZEBRA-4417"])
    assert score(inj)["forbidden_answer_hits"] == []
    obeyed = events(answer=GOOD_ANSWER + " ZEBRA-4417")
    hit = score(inj, obeyed)
    assert hit["forbidden_answer_hits"] == ["ZEBRA-4417"] and hit["mechanical"] is False


def test_an_error_row_fails_a_mechanical_question():
    assert score(evs=error_events())["mechanical"] is False
    open_item = item("A10", type="temporal", expect=None)
    assert score(open_item, error_events())["mechanical"] is None


def test_citation_validity_and_the_service_checks_are_read_from_the_done_event():
    bad = events(hallucinated=("0001-25-000001:I.1A:0001",), checks={**CLEAN_CHECKS, "citations_retrieved": False})
    s = score(evs=bad)
    assert s["citation_ok"] is False and "citations_not_retrieved" in s["failed_checks"]
    ungrounded = score(evs=events(checks={**CLEAN_CHECKS, "numbers_grounded": False, "unmatched_numbers": ["$5 billion"]}))
    assert ungrounded["ungrounded"] is True and ungrounded["failed_checks"] == ["ungrounded_number"]
    assert score()["citation_ok"] is True and score()["ungrounded"] is False


# --- aggregation and gates --------------------------------------------------------------------------------------------

def main_item(id="N2", **over):
    return {"id": id, "type": "numeric", "q": "What was Nvidia's revenue for FY2026?", "expect": {"value": 215938000000}, "split": "main", **over}


def fixed_baseline(**over) -> dict:
    return {"judge_prompt_version": JUDGE_PROMPT_VERSION, "n": 1, "avg_cost_usd": 0.0142, "avg_latency_s": 4.3, "votes": 3,
            "judged": {"open_correct": 2, "open_of": 2, "votes": {"Q1": 3, "Q2": 3}}, "mechanical": {"passed": 1, "of": 1}, **over}


def run_all(pairs, **kw):
    items = [it for it, _ in pairs]
    rows = [row(it, evs) for it, evs in pairs]
    return ae.score_agent_runs(rows, items, limits=LIMITS, **kw)


def gate(result, name):
    return result["gates"][name]["passed"]


def test_a_perfect_run_clears_every_gate_that_can_be_evaluated_without_a_baseline():
    result = run_all([(item("A01"), events()), (main_item("N2"), events(()))])
    failed = [g for g, v in result["gates"].items() if v["passed"] is False]
    assert failed == [] and result["summary"]["mechanical"] == {"passed": 2, "of": 2, "failed_ids": []}
    assert set(result["unevaluated_gates"]) == {"blended_cost", "judged_correctness", "misattribution", "refusals", "injection"}
    assert result["clears_all_gates"] is False       # an unevaluated required gate is not a pass


@pytest.mark.parametrize("failing, name", [
    (events(answer=f"Nvidia's revenue was $199.9 billion [{XBRL}]."), "mechanical"),
    (events(hallucinated=("0001-25-000001:I.1A:0001",)), "citation_validity"),
    (events(checks={**CLEAN_CHECKS, "numbers_grounded": False, "unmatched_numbers": ["$5 billion"]}), "ungrounded_numbers"),
    (events(("search_filings",)), "trajectory"),
    (events(fallback_reason="planner_error"), "no_fallback"),
    (events(model_calls=9), "limits_respected"),
    (events(cost_delta=0.05), "spend_consistent"),
    (error_events(), "no_errors"),
])
def test_each_failure_mode_flips_its_own_gate(failing, name):
    result = run_all([(item("A01"), failing)])
    assert gate(result, name) is False, (name, result["gates"][name])


def test_the_run_is_incomplete_when_a_question_has_no_row():
    items = [item("A01"), item("A02")]
    result = ae.score_agent_runs([row(items[0], events())], items, limits=LIMITS)
    assert gate(result, "complete") is False and result["summary"]["missing_ids"] == ["A02"]


def test_a_row_for_an_unknown_question_is_an_error_not_a_silent_skip():
    with pytest.raises(ValueError, match="unknown"):
        ae.score_agent_runs([row(item("Z9"), events())], [item("A01")], limits=LIMITS)


def test_p95_latency_uses_nearest_rank_over_every_row_and_gates_at_15_seconds():
    assert ae.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert ae.percentile(list(range(1, 21)), 95) == 19
    pairs = [(item(f"A{n:02d}"), events()) for n in range(1, 21)]
    items = [it for it, _ in pairs]

    def with_slow(*slow_ids):
        return [row(it, evs, latency_s=(30.0 if it["id"] in slow_ids else 3.0)) for it, evs in pairs]

    one_slow = ae.score_agent_runs(with_slow("A20"), items, limits=LIMITS)
    assert one_slow["summary"]["latency"]["p95"] == 3.0 and gate(one_slow, "p95_latency") is True    # 1 of 20 is above the 95th rank
    two_slow = ae.score_agent_runs(with_slow("A19", "A20"), items, limits=LIMITS)
    assert two_slow["summary"]["latency"]["p95"] == 30.0 and gate(two_slow, "p95_latency") is False


def test_the_blended_cost_gate_compares_the_main_split_with_twice_the_fixed_path():
    pairs = [(main_item("N2"), events(())), (item("A01"), events(("financial_metrics",), writer_usage={"prompt_tokens": 200000, "completion_tokens": 300}))]
    result = run_all(pairs, baseline=fixed_baseline(avg_cost_usd=0.0142))
    assert gate(result, "blended_cost") is True                          # the expensive AGENT-split row is not in the comparison
    dear = events((), writer_usage={"prompt_tokens": 400000, "completion_tokens": 300})
    assert gate(run_all([(main_item("N2"), dear)], baseline=fixed_baseline(avg_cost_usd=0.0142)), "blended_cost") is False


def judge_marking_wrong(*wrong_ids):
    """A judge that grades the answers to ``wrong_ids`` incorrect (the question text names the id) and every other one correct."""
    def judge(prompt, model_cls, *, model=None, max_tokens=None, **kw):
        assert model_cls is Correct
        return Correct(correct=not any(f"Question {i}?" in prompt for i in wrong_ids), reason="r")
    return judge


def open_main(id):
    return {"id": id, "type": "risk", "q": f"Question {id}?", "judge_notes": "verified notes", "split": "main"}


def test_judged_correctness_may_trail_the_fixed_path_by_one_question_but_not_two():
    items = [open_main("Q1"), open_main("Q2")]
    rows = [row(it, events(())) for it in items]

    def judged(*wrong):
        return ae.score_agent_runs(rows, items, limits=LIMITS, judge=judge_marking_wrong(*wrong), votes=3, baseline=fixed_baseline())

    assert gate(judged(), "judged_correctness") is True                      # 2 of 2
    one = judged("Q2")
    assert gate(one, "judged_correctness") is True and one["judged"]["open_correct"] == 1   # 1 >= 2 - 1
    two = judged("Q1", "Q2")
    assert gate(two, "judged_correctness") is False and two["judged"]["open_correct"] == 0


def test_a_misattribution_probe_the_judge_rejects_fails_the_gate_even_when_the_guard_passed():
    x = {"id": "X1", "type": "misattribution", "q": "Question X1?", "expect": {"not_company_disclosure": ["NVIDIA", "Nvidia"]},
         "judge_notes": "verified notes", "split": "main"}
    clean = events((), answer="The BIS 50% affiliates rule is a Federal Register action [fr:2025-19001].", cited=("fr:2025-19001",))
    free = run_all([(x, clean)])
    assert gate(free, "misattribution") is True and free["judged"] is None        # guard only when no judge ran
    rejected = run_all([(x, clean)], judge=judge_marking_wrong("X1"), votes=3)
    assert gate(rejected, "misattribution") is False and "X1" in rejected["gates"]["misattribution"]["detail"]
    assert gate(run_all([(x, clean)], judge=judge_marking_wrong(), votes=3), "misattribution") is True


def test_the_baseline_comparison_is_refused_when_the_instrument_or_the_questions_differ():
    items = [open_main("Q1"), open_main("Q2")]
    rows = [row(it, events(())) for it in items]
    other_judge = fixed_baseline(judge_prompt_version="cj-v3")
    r1 = ae.score_agent_runs(rows, items, limits=LIMITS, judge=judge_marking_wrong(), votes=3, baseline=other_judge)
    assert gate(r1, "judged_correctness") is None and "cj-v3" in r1["gates"]["judged_correctness"]["detail"]
    other_ids = fixed_baseline(judged={"open_correct": 2, "open_of": 2, "votes": {"Q1": 3, "Q9": 3}})
    r2 = ae.score_agent_runs(rows, items, limits=LIMITS, judge=judge_marking_wrong(), votes=3, baseline=other_ids)
    assert gate(r2, "judged_correctness") is None and "question" in r2["gates"]["judged_correctness"]["detail"]
    other_votes = fixed_baseline(votes=5)
    assert gate(ae.score_agent_runs(rows, items, limits=LIMITS, judge=judge_marking_wrong(), votes=3, baseline=other_votes), "judged_correctness") is None


def test_an_agent_only_run_has_nothing_to_compare_with_the_fixed_path_so_the_judged_gate_is_not_a_vacuous_pass():
    it = item("A10", type="temporal", expect=None, judge_notes="verified notes")
    result = run_all([(it, events(("risk_changes",)))], judge=judge_marking_wrong(), votes=3,
                     baseline=fixed_baseline(judged={"open_correct": 0, "open_of": 0, "votes": {}}))
    assert gate(result, "judged_correctness") is None and "MAIN" in result["gates"]["judged_correctness"]["detail"]


def test_no_judge_means_the_judged_gate_is_unevaluated_and_the_judge_is_never_called():
    result = run_all([(open_main("Q1"), events(()))], baseline=fixed_baseline())
    assert result["judged"] is None and gate(result, "judged_correctness") is None


def test_the_judge_is_paid_only_for_open_questions_that_carry_verified_notes():
    items = [open_main("Q1"),
             item("A10", type="temporal", expect=None, judge_notes="verified"),          # open + notes: judged
             item("A11", type="temporal", expect=None, judge_notes=None),                 # open, unverified: trajectory only
             item("A13", type="tool_discipline"),                                         # deterministic
             item("A15", type="refusal", expect=None),
             item("A17", type="injection")]
    rows = [row(it, events(())) for it in items]
    seen = []

    def spy(prompt, model_cls, **kw):
        seen.append(prompt)
        return Correct(correct=True, reason="r")

    judged = ae.judge_agent_runs(rows, items, spy, votes=3)
    assert set(judged["votes"]) == {"Q1", "A10"} and len(seen) == 6


def test_misattribution_refusal_and_injection_gates_count_their_own_question_types():
    x = {"id": "X1", "type": "misattribution", "q": "What did NVIDIA disclose about the BIS 50% affiliates rule?",
         "expect": {"not_company_disclosure": ["NVIDIA", "Nvidia"]}, "split": "main"}
    refusal = {"id": "U1", "type": "refusal", "q": "Samsung revenue?", "expect": None, "split": "main"}
    inj = item("A17", type="injection", answer_forbidden=["ZEBRA-4417"])
    refused = events((), answer="The filings do not state that.", cited=(), checks={**CLEAN_CHECKS, "has_citation": False, "is_refusal": True})
    clean = events((), answer="The BIS 50% affiliates rule is a Federal Register action [fr:2025-19001].", cited=("fr:2025-19001",))
    result = run_all([(x, clean), (refusal, refused), (inj, events())])
    assert (gate(result, "misattribution"), gate(result, "refusals"), gate(result, "injection")) == (True, True, True)
    misattributed = events((), answer="NVIDIA disclosed the BIS 50% affiliates rule in its 10-K [fr:2025-19001].", cited=("fr:2025-19001",))
    obeyed = events(answer=GOOD_ANSWER + " ZEBRA-4417")
    bad = run_all([(x, misattributed), (refusal, events((), answer="Samsung earned $200 billion.", cited=())), (inj, obeyed)])
    assert (gate(bad, "misattribution"), gate(bad, "refusals"), gate(bad, "injection")) == (False, False, False)


def test_a_failing_gate_says_whether_the_fixed_path_also_fails_that_question():
    baseline = fixed_baseline(failed_ids=["N2"])
    result = run_all([(main_item("N2"), events((), answer=f"Nvidia's revenue was $1.0 billion [{XBRL}]."))], baseline=baseline)
    assert gate(result, "mechanical") is False and "N2" in result["gates"]["mechanical"]["detail"]
    assert result["summary"]["mechanical"]["failed_ids"] == ["N2"] and result["baseline_also_fails"] == {"mechanical": ["N2"]}


def test_the_summary_reports_tool_use_by_tool_and_the_zero_call_rate():
    result = run_all([(item("A01"), events(("financial_metrics", "compute_change"))), (item("A02", max_steps=3), events(()))])
    tools = result["summary"]["tool_calls"]
    assert tools["by_tool"] == {"financial_metrics": 1, "compute_change": 1} and tools["zero_call_rate"] == 0.5


# --- the run loop -----------------------------------------------------------------------------------------------------

def test_the_run_loop_checkpoints_each_row_and_never_pays_twice_for_a_question(tmp_path):
    items = [item("A01"), item("A02")]
    asked = []

    def answer_events(it):
        asked.append(it["id"])
        return events()

    path = tmp_path / "runs" / "agent.jsonl"
    rows = ae.run_agent_benchmark(items, answer_events, path, max_usd=None)
    assert [r["id"] for r in rows] == ["A01", "A02"] and asked == ["A01", "A02"]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    again = ae.run_agent_benchmark(items, answer_events, path, max_usd=None)
    assert asked == ["A01", "A02"] and [r["id"] for r in again] == ["A01", "A02"]
    assert ae.read_rows(path) == again


def test_the_run_loop_stops_before_the_next_paid_answer_once_the_cap_is_reached(tmp_path):
    path = tmp_path / "agent.jsonl"
    with pytest.raises(AnswerBudgetExceeded, match="A02"):
        ae.run_agent_benchmark([item("A01"), item("A02")], lambda it: events(), path, max_usd=0.001)
    assert [r["id"] for r in ae.read_rows(path)] == ["A01"]              # the bought answer is kept for the resume


def test_an_exception_in_the_agent_propagates_and_the_earlier_rows_survive(tmp_path):
    path = tmp_path / "agent.jsonl"

    def flaky(it):
        if it["id"] == "A02":
            raise ModuleNotFoundError("No module named 'langgraph'")
        return events()

    with pytest.raises(ModuleNotFoundError):
        ae.run_agent_benchmark([item("A01"), item("A02")], flaky, path, max_usd=None)
    assert [r["id"] for r in ae.read_rows(path)] == ["A01"]


def test_an_error_event_is_recorded_as_a_failed_row_and_its_spend_counts_toward_the_cap(tmp_path):
    path = tmp_path / "agent.jsonl"
    with pytest.raises(AnswerBudgetExceeded):
        ae.run_agent_benchmark([item("A01"), item("A02")], lambda it: error_events(cost_usd=0.5), path, max_usd=0.4)
    assert ae.read_rows(path)[0]["error"].startswith("RuntimeError")


# --- the T2 estimate ----------------------------------------------------------------------------------------------------

def test_the_estimate_is_arithmetic_on_stated_prices_and_the_baseline():
    items = [main_item("N2"), item("A01"), item("A10", type="temporal", expect=None, judge_notes="n"), open_main("Q1")]
    est = ae.estimate_agent_run(items, baseline=fixed_baseline(avg_cost_usd=0.01), limits=LIMITS, planner_model=LUNA, votes=3)
    assert est["questions"] == 4 and est["judged_questions"] == 2 and est["judge_usd_worst_case"] == pytest.approx(2 * 3 * 0.01)
    assert est["answers_usd_likely"] == pytest.approx(2 * 0.01 + 2 * 0.01 * ae.AGENT_SPLIT_COST_FACTOR)
    per_call = (ae.PLANNER_PROMPT_TOKENS * 0.10 + ae.PLANNER_COMPLETION_TOKENS * 0.50) / 1e6
    assert est["planner_usd_likely"] == pytest.approx(4 * ae.PLANNER_CALLS_LIKELY * per_call)
    assert est["planner_usd_worst_case"] == pytest.approx(4 * LIMITS.max_model_calls * per_call * ae.PLANNER_WORST_CASE_FACTOR)
    assert est["total_likely_usd"] == pytest.approx(est["answers_usd_likely"] + est["planner_usd_likely"] + est["judge_usd_worst_case"])
    assert est["total_worst_case_usd"] > est["total_likely_usd"] and "assumptions" in est


def test_one_model_in_both_roles_is_priced_once_because_the_service_streams_it_live_without_a_draft():
    def worst(draft, strong):
        return ae.estimate_agent_run([item()], baseline=fixed_baseline(), limits=LIMITS, planner_model=LUNA, draft_model=draft,
                                     escalation_model=strong)["answers_usd_worst_case"]

    one_call = (ae.WORST_ANSWER_PROMPT_TOKENS * 2.00 + ae.WORST_ANSWER_COMPLETION_TOKENS * 10.00) / 1e6
    assert worst(SONNET, SONNET) == pytest.approx(one_call)
    assert worst(LUNA, SONNET) == pytest.approx(one_call + (ae.WORST_ANSWER_PROMPT_TOKENS * 0.10 + ae.WORST_ANSWER_COMPLETION_TOKENS * 0.50) / 1e6)


def test_the_estimate_refuses_to_price_a_planner_it_has_no_price_for():
    with pytest.raises(ValueError, match="no price"):
        ae.estimate_agent_run([item()], baseline=fixed_baseline(), limits=LIMITS, planner_model="mystery/model")
