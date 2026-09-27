"""The LangGraph loop: prefetch -> plan -> tools -> plan ... -> finalize (docs/v2/M3_AGENT_PLAN.md section 1).

What is pinned here: the planner only ADDS to the prefetch (a call for a pair/company the prefetch already covers UNIONS with
it, never replaces the whole thing, M3 finding #4); the limits (4 tool calls, 3 model calls, the time budget) hold whatever the
planner asks for; every planner call gets ``timeout = min(remaining budget, cap)`` (requirement 4); a planner exception, a
budget expiry or a bug in the loop degrades to the plain prefetch ONLY when no tool call has already succeeded, else it
finalizes with what was gathered instead (M3 finding #10) -- and never raises either way (only a failing PREFETCH raises: that
is the fixed path's own failure); step events and ``tool_calls`` are one-to-one.
"""

import copy
import json

import pytest
from agent_fakes import (
    NVDA_ACC,
    FakeDriver,
    FakeEmbedder,
    ScriptedPlanner,
    make_settings,
    turn,
)

from semigraph.agent import graph as G
from semigraph.agent import merge as M
from semigraph.agent.state import Limits
from semigraph.agent.tools import tool_specs
from semigraph.retrieval.retriever import hybrid_retrieve

QUESTION = "Compare Nvidia and AMD revenue over time."
LIMITS = Limits(max_tool_calls=4, max_model_calls=3, time_budget_s=25.0)
FM = ("financial_metrics", {"companies": ["TSMC"]})
LOOKUP = ("lookup_company", {"name": "TSMC"})


def drain(gen):
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def run(planner, *, driver=None, question=QUESTION, limits=LIMITS, tracer=None, fallback=None, **kw):
    """Run the loop. Every run must end as the test says: ``fallback`` is the expected ``fallback_reason``, None for a happy path
    (M3 requirement 3: a silent 400 -> fallback must never be mistaken for an agent run)."""
    driver = driver or FakeDriver.world()
    gen = G.run_agent(question, driver, FakeEmbedder(), planner=planner, planner_model="openai/gpt-6-luna", limits=limits,
                      tracer=tracer, **kw)
    events, result = drain(gen)
    assert result.fallback_reason == fallback
    return events, result, driver


def prefetch_r(question=QUESTION):
    return hybrid_retrieve(question, FakeDriver.world(), FakeEmbedder())


class VirtualClock:
    """A monotonic clock the tests advance: a slow planner is a call that advances it, so nothing sleeps."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    fake = VirtualClock()
    monkeypatch.setattr(G, "_now", fake)
    return fake


# --- the shape of a run -----------------------------------------------------------------------------------------------------

def test_a_planner_that_calls_no_tool_leaves_the_prefetch_exactly_as_it_is():
    planner = ScriptedPlanner(turn())
    events, result, _ = run(planner)
    assert events == [] and result.r == prefetch_r()
    assert result.tool_calls == [] and result.model_calls == 1 and result.fallback_reason is None
    assert result.stop_reason == "planner_done" and result.planner_usage == {"prompt_tokens": 500, "completion_tokens": 40}


def test_one_tool_call_then_done_merges_the_result_and_emits_one_step_event():
    planner = ScriptedPlanner(turn(FM), turn())
    events, result, driver = run(planner)
    assert [e["event"] for e in events] == ["step"]
    step = events[0]
    assert step["n"] == 1 and step["tool"] == "financial_metrics" and step["ok"] is True and step["args"] == {"companies": ["TSMC"]}
    assert step["summary"].startswith("financial_metrics:")
    assert any(m["cik"] == 1046179 for m in result.r["metrics"]) and result.r["anchors"]["TSMC"] == 1046179
    assert result.tool_calls == [{"tool": "financial_metrics", "args": {"companies": ["TSMC"]}, "ok": True}]
    assert result.model_calls == 2 and result.planner_usage == {"prompt_tokens": 1000, "completion_tokens": 80}
    assert result.stop_reason == "planner_done" and result.fallback_reason is None


def test_step_events_and_tool_calls_are_one_to_one_in_the_same_order_and_numbered_from_one():
    planner = ScriptedPlanner(turn(LOOKUP, FM), turn(("relationships", {"companies": ["TSMC"]})), turn())
    events, result, _ = run(planner)
    assert [e["n"] for e in events] == [1, 2, 3]
    assert [e["tool"] for e in events] == [c["tool"] for c in result.tool_calls] == ["lookup_company", "financial_metrics", "relationships"]
    assert [e["ok"] for e in events] == [c["ok"] for c in result.tool_calls]


def test_parallel_calls_in_one_turn_run_in_order_and_each_gets_its_tool_message():
    planner = ScriptedPlanner(turn(LOOKUP, FM), turn())
    run(planner)
    second = planner.calls[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool", "tool"]
    assert [c["id"] for c in second[2]["tool_calls"]] == [m["tool_call_id"] for m in second[3:]] == ["call_0", "call_1"]
    assert json.loads(second[3]["content"])["company"] == "TSMC"


def test_every_planner_call_is_given_the_tool_schemas_and_the_question():
    planner = ScriptedPlanner(turn(FM), turn())
    run(planner)
    assert all(c["tools"] == tool_specs() for c in planner.calls)
    assert QUESTION in planner.calls[0]["messages"][1]["content"]


# --- the limits ---------------------------------------------------------------------------------------------------------------

def test_calls_beyond_the_tool_limit_are_neither_run_nor_recorded_and_the_planner_is_not_asked_again():
    planner = ScriptedPlanner(turn(*[LOOKUP] * 6), turn(FM))
    events, result, driver = run(planner)
    assert len(events) == len(result.tool_calls) == 4
    assert driver.names().count("annual_pairs") == 4                     # the prefetch reads no annual pairs: the 4 lookups only
    assert len(planner.calls) == 1 and result.model_calls == 1 and result.stop_reason == "tool_limit"


def test_the_tool_limit_holds_across_turns():
    planner = ScriptedPlanner(turn(LOOKUP, LOOKUP), turn(LOOKUP, LOOKUP, LOOKUP), turn(FM))
    events, result, _ = run(planner)
    assert len(events) == 4 and result.stop_reason == "tool_limit" and len(planner.calls) == 2


def test_the_model_limit_holds_for_a_planner_that_never_stops():
    planner = ScriptedPlanner(turn(LOOKUP), repeat=True)
    events, result, _ = run(planner)
    assert len(planner.calls) == 3 == result.model_calls and len(events) == 3 and result.stop_reason == "model_limit"


def test_the_limits_come_from_the_settings():
    limits = Limits.from_settings(make_settings(agent_max_tool_calls=2, agent_max_model_calls=1, agent_time_budget_s=9))
    assert limits == Limits(2, 1, 9.0)
    events, result, _ = run(ScriptedPlanner(turn(LOOKUP), repeat=True), limits=limits)
    assert len(events) == 1 and result.model_calls == 1


def test_a_zero_limit_means_the_planner_is_never_called():
    """An operator can set ``AGENT_MAX_TOOL_CALLS=0`` or ``AGENT_MAX_MODEL_CALLS=0`` to keep the agent path but switch planning off."""
    for limits, reason in ((Limits(0, 3, 25.0), "tool_limit"), (Limits(4, 0, 25.0), "model_limit")):
        planner = ScriptedPlanner(turn(FM))
        events, result, _ = run(planner, limits=limits)
        assert planner.calls == [] and events == [] and result.model_calls == 0 and result.stop_reason == reason
        assert result.r == prefetch_r() and result.planner_usage == {"prompt_tokens": 0, "completion_tokens": 0}


def test_relationships_edges_are_capped_across_the_whole_run_not_per_call():
    """M3 finding #7: the 40-edge cap is per RUN, not per call -- four ``relationships`` calls, each returning 20 brand-new
    distinct edges (a per-call cap would let each one add up to 40, ~2.5x the intended context growth over the run), add
    at most ``merge.MAX_EDGES_ADDED`` edges COMBINED."""
    calls = {"n": 0}

    def company_edges(params):
        calls["n"] += 1
        return [{"source": "Nvidia", "relation": "DEPENDS_ON", "target": f"call{calls['n']}-{i}", "status": "Active",
                 "quote": None, "chunk_ids": []} for i in range(20)]

    driver = FakeDriver.world(company_edges=company_edges, rule_edges=lambda p: [])
    call = ("relationships", {"companies": ["Nvidia"]})
    planner = ScriptedPlanner(turn(call), turn(call), turn(call), turn(call), turn())
    events, result, _ = run(planner, driver=driver, limits=Limits(4, 5, 25.0))
    assert len(result.tool_calls) == 4 and all(c["ok"] for c in result.tool_calls)
    prefetch_edges = 20                                        # the prefetch's OWN company_edges call: not a tool call, not capped
    added = len(result.r["edges"]) - prefetch_edges
    assert added == M.MAX_EDGES_ADDED                          # exactly 40, not 20 (a per-call cap stuck too low) or 80 (uncapped)


def test_refused_calls_count_toward_the_limit_and_are_recorded_as_failed():
    bad = ("run_cypher", {"query": "MATCH (n) DETACH DELETE n"})
    planner = ScriptedPlanner(turn(bad, bad, bad, bad, bad))
    events, result, driver = run(planner)
    assert [e["ok"] for e in events] == [False] * 4 and [e["tool"] for e in events] == ["run_cypher"] * 4
    assert result.stop_reason == "tool_limit" and all(c["ok"] is False for c in result.tool_calls)
    assert set(driver.names()) <= {"company_edges", "rule_edges", "metrics", "active_risks", "temporal", "passages", "excerpts"}


def test_a_failing_tool_does_not_stop_the_loop():
    class Flaky(FakeDriver):
        def answer(self, query, params, *, timeout=None):
            if params.get("ids") == [1046179]:
                raise RuntimeError("neo4j is down")
            return super().answer(query, params, timeout=timeout)

    driver = Flaky(**FakeDriver.world().layers)
    planner = ScriptedPlanner(turn(FM), turn(("financial_metrics", {"companies": ["AMD"]})), turn())
    events, result, _ = run(planner, driver=driver)
    assert [e["ok"] for e in events] == [False, True] and result.fallback_reason is None
    assert json.loads(planner.calls[1]["messages"][-1]["content"]) == {"error": "the tool failed", "type": "RuntimeError"}


# --- fallback: never worse than the fixed path, but never a full discard once a tool call succeeded (M3 finding #10) -------

def test_a_planner_exception_with_zero_successful_tool_calls_falls_back_to_the_prefetch_and_says_why():
    events, result, _ = run(ScriptedPlanner(RuntimeError("400: tools are not supported with reasoning")), fallback='planner_error:RuntimeError')
    assert events == [] and result.r == prefetch_r() and result.tool_calls == []
    assert result.fallback_reason == "planner_error:RuntimeError" and result.stop_reason == "fallback" and result.model_calls == 1
    assert result.planner_usage == {"prompt_tokens": 0, "completion_tokens": 0}


def test_a_planner_exception_after_a_successful_tool_call_keeps_the_merged_result_not_the_plain_prefetch():
    """M3 finding #10: plan section 1 says exceeding a LIMIT goes to finalize with what was gathered; a fallback is not
    fundamentally different once a tool call has already succeeded -- discarding it would throw away real, already-fetched
    data for no reason. ``fallback_reason`` is still reported (for the eval and the audit trail), but ``r`` is the merged
    dict, not ``prefetch_r()``."""
    events, result, _ = run(ScriptedPlanner(turn(FM), ValueError("boom")), fallback="planner_error:ValueError")
    assert result.fallback_reason == "planner_error:ValueError" and len(events) == 1 and len(result.tool_calls) == 1
    assert result.tool_calls[0]["ok"] is True
    assert result.r != prefetch_r() and any(m["cik"] == 1046179 for m in result.r["metrics"]) and result.model_calls == 2


def test_a_bug_in_the_loop_is_a_fallback_not_an_error_and_the_planners_spend_is_still_counted(monkeypatch):
    monkeypatch.setattr(G.P, "assistant_message", lambda planner_turn: 1 / 0)
    events, result, _ = run(ScriptedPlanner(turn(FM)), fallback='agent_error:ZeroDivisionError')
    assert result.fallback_reason == "agent_error:ZeroDivisionError" and result.r == prefetch_r() and result.stop_reason == "fallback"
    assert result.model_calls == 1 and result.planner_usage == {"prompt_tokens": 500, "completion_tokens": 40}   # the call was paid for


def test_planner_messages_that_cannot_be_built_are_a_fallback_and_the_planner_is_never_called(monkeypatch):
    def broken(question, r):
        raise FileNotFoundError("agent_planner.txt is missing from the image")

    monkeypatch.setattr(G.P, "initial_messages", broken)
    planner = ScriptedPlanner(turn(FM))
    events, result, _ = run(planner, fallback="agent_error:FileNotFoundError")
    assert planner.calls == [] and events == [] and result.r == prefetch_r() and result.stop_reason == "fallback"


def test_a_planner_that_returns_something_that_is_not_a_turn_is_a_planner_error():
    _, result, _ = run(ScriptedPlanner(lambda messages, timeout: object()), fallback='planner_error:TypeError')
    assert result.fallback_reason == "planner_error:TypeError" and result.r == prefetch_r() and result.model_calls == 1


def test_a_failing_prefetch_is_the_fixed_paths_own_failure_and_propagates():
    class Down(FakeDriver):
        def answer(self, query, params, *, timeout=None):
            raise ConnectionError("neo4j unreachable")

    planner = ScriptedPlanner(turn())
    with pytest.raises(ConnectionError):
        run(planner, driver=Down())
    assert planner.calls == []


def test_the_recursion_limit_is_a_fallback_that_keeps_the_tool_call_that_already_succeeded():
    events, result, _ = run(ScriptedPlanner(turn(FM), repeat=True), recursion_limit=4, fallback="recursion_limit")
    assert result.fallback_reason == "recursion_limit" and len(result.tool_calls) == 1 and result.tool_calls[0]["ok"] is True
    assert result.r != prefetch_r() and any(m["cik"] == 1046179 for m in result.r["metrics"])
    assert result.stop_reason == "fallback"


def test_the_recursion_backstop_is_12_at_the_defaults_and_never_below_what_the_model_limit_needs():
    from semigraph.agent.state import recursion_limit_for

    assert recursion_limit_for(LIMITS) == 12 and recursion_limit_for(Limits(4, 6, 25.0)) == 16 and recursion_limit_for(Limits(12, 10, 25.0)) == 24
    events, result, _ = run(ScriptedPlanner(turn(LOOKUP), repeat=True), limits=Limits(12, 10, 25.0))
    assert result.model_calls == 10 and result.stop_reason == "model_limit" and len(events) == 10       # the backstop did not fire early


def test_the_prefetch_dict_is_never_mutated_by_a_tool_heavy_run(monkeypatch):
    seen = {}
    real = G.hybrid_retrieve

    def spy(*args, **kwargs):
        seen["r"] = real(*args, **kwargs)
        seen["copy"] = copy.deepcopy(seen["r"])
        return seen["r"]

    monkeypatch.setattr(G, "hybrid_retrieve", spy)
    planner = ScriptedPlanner(turn(FM, ("risk_changes", {"companies": ["AMD"], "fiscal_years": [2024, 2025]})),
                              turn(("search_filings", {"query": "x", "companies": ["AMD"], "k": 10}),
                                   ("compute_change", {"company": "Nvidia", "metric": "revenue", "from_period_end": "2024-01-28",
                                                       "to_period_end": "2026-01-25"})))
    _, result, _ = run(planner)
    assert seen["r"] == seen["copy"] and result.r is not seen["r"] and len(result.tool_calls) == 4


def test_the_prefetch_uses_the_callers_k_chunks_and_hops():
    _, result, driver = run(ScriptedPlanner(turn()), k_chunks=3, hops=1)
    assert driver.params_of("excerpts")[0]["k"] == 3 and len(result.r["chunks"]) == 3


# --- the time budget (requirement 4) -----------------------------------------------------------------------------------------

def slow(clock, seconds):
    """A planner that takes ``seconds`` of the virtual clock, or raises a timeout at its own ``timeout``."""
    def call(messages, timeout):
        clock.t += min(seconds, timeout)
        if seconds > timeout:
            raise TimeoutError("planner call timed out")
        return turn()
    return call


def test_a_planner_call_gets_min_of_the_remaining_budget_and_the_per_call_cap(clock):
    planner = ScriptedPlanner(slow(clock, 1))
    run(planner)
    assert planner.calls[0]["timeout"] == 12.0                              # cap < remaining (25 s)
    planner = ScriptedPlanner(slow(clock, 1))
    run(planner, limits=Limits(4, 3, 5.0))
    assert planner.calls[0]["timeout"] == 5.0                               # remaining (5 s) < cap


def test_a_tool_calls_timeout_is_min_of_the_remaining_budget_and_the_tool_call_cap(clock):
    """M3 finding #3: the LOOP (not just ``Toolbox.execute`` in isolation) passes a per-call timeout down to the Cypher
    queries a tool call makes -- a regression back to ``timeout=None`` here would pass every ``Toolbox``-level test but
    leave every REAL tool call in a run unbounded again. The prefetch's own query is untouched (``None``: the fixed
    path's behaviour), only the TOOL call's gets the budget."""
    def spend_then_metrics(messages, timeout):
        clock.t += 3.0
        return turn(FM)

    planner = ScriptedPlanner(spend_then_metrics, turn())
    events, result, driver = run(planner, limits=Limits(4, 3, 8.0))
    assert driver.timeouts_of("metrics") == [None, 5.0]         # prefetch (untouched), then min(8 - 3, tool_call_cap_s=12)


def test_the_timeout_shrinks_with_the_remaining_budget(clock):
    def slow_tool_turn(messages, timeout):
        clock.t += 9.0
        return turn(LOOKUP)

    planner = ScriptedPlanner(slow_tool_turn, slow(clock, 0))
    run(planner)
    assert [round(c["timeout"], 3) for c in planner.calls] == [12.0, 12.0]  # 25 - 9 = 16 remaining: still above the cap
    planner = ScriptedPlanner(slow_tool_turn, slow_tool_turn, slow(clock, 0))
    run(planner, limits=Limits(4, 3, 25.0))
    assert [round(c["timeout"], 3) for c in planner.calls] == [12.0, 12.0, 7.0]   # 25 - 18 = 7 remaining: below the cap


def test_a_slow_model_falls_back_inside_the_budget(clock):
    planner = ScriptedPlanner(slow(clock, 300))                               # a hung model: would take five minutes
    events, result, _ = run(planner, fallback='planner_error:TimeoutError')
    assert result.fallback_reason == "planner_error:TimeoutError" and result.r == prefetch_r()
    assert result.elapsed_s == 12.0 <= LIMITS.time_budget_s                   # abandoned at the per-call cap, not at 300 s


def test_a_slow_model_against_a_short_budget_is_abandoned_at_the_budget(clock):
    events, result, _ = run(ScriptedPlanner(slow(clock, 300)), limits=Limits(4, 3, 5.0), fallback='planner_error:TimeoutError')
    assert result.fallback_reason == "planner_error:TimeoutError" and result.elapsed_s == 5.0


def test_an_expired_budget_after_a_successful_tool_call_finalizes_with_what_was_gathered_not_the_plain_prefetch(clock):
    """M3 finding #10 (the spec inconsistency): plan section 1 says exceeding a limit goes to finalize with what was
    gathered; the code used to treat ANY time-budget expiry as a full discard of tool calls that had already succeeded.
    ``fallback_reason`` is still ``"time_budget"`` (still reported, for the eval and the audit trail), but ``r`` keeps the
    FM call's merged metrics -- it is NOT ``prefetch_r()``. (The zero-successful-calls case, where the budget expires
    before any pending call gets to run, is ``test_a_budget_that_expires_between_tool_calls_skips_the_rest`` below.)"""
    def tools_then_expire(messages, timeout):
        clock.t += 24.5                                                       # 0.5 s left: below the minimum for a new call
        return turn(FM)

    planner = ScriptedPlanner(tools_then_expire, turn())
    events, result, _ = run(planner, fallback='time_budget')
    assert len(planner.calls) == 1 and result.fallback_reason == "time_budget" and result.stop_reason == "time_budget"
    assert len(events) == 1 and len(result.tool_calls) == 1 and result.tool_calls[0]["ok"] is True
    assert result.r != prefetch_r() and any(m["cik"] == 1046179 for m in result.r["metrics"])


def test_a_limit_is_not_a_fallback_and_answers_from_what_was_gathered():
    events, result, _ = run(ScriptedPlanner(turn(FM), repeat=True), limits=Limits(1, 3, 25.0))
    assert result.stop_reason == "tool_limit" and result.fallback_reason is None
    assert any(m["cik"] == 1046179 for m in result.r["metrics"]) and result.r != prefetch_r()


def test_a_budget_that_expires_between_tool_calls_skips_the_rest_and_falls_back_since_nothing_succeeded(clock):
    """The zero-successful-tool-calls case of M3 finding #10: the budget is already spent before EITHER pending call gets
    to run, so nothing was gathered worth keeping and the answer is the untouched prefetch."""
    def spend_then_call_two(messages, timeout):
        clock.t += 25.0
        return turn(FM, LOOKUP)

    events, result, _ = run(ScriptedPlanner(spend_then_call_two), fallback='time_budget')
    assert events == [] and result.fallback_reason == "time_budget" and result.tool_calls == [] and result.r == prefetch_r()


def test_elapsed_time_covers_the_planning_phase_only(clock):
    def one_second(messages, timeout):
        clock.t += 1.0
        return turn()

    _, result, _ = run(ScriptedPlanner(one_second))
    assert result.elapsed_s == 1.0


# --- tracing ------------------------------------------------------------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.log = []

    def span(self, name, **attrs):
        log = self.log

        class Span:
            def __enter__(self_):
                log.append(("span", name, attrs))
                return self_

            def __exit__(self_, *exc):
                return False

            def set(self_, **more):
                log.append(("set", name, more))

        return Span()

    def event(self, name, **attrs):
        self.log.append(("event", name, attrs))

    def generation(self, **kw):
        self.log.append(("generation", kw))

    def flush(self):
        self.log.append(("flush",))


def test_the_loop_reports_spans_generations_and_the_fallback_to_the_tracer():
    tracer = Recorder()
    run(ScriptedPlanner(turn(FM), RuntimeError("x")), tracer=tracer, fallback="planner_error:RuntimeError")
    names = [entry[1] for entry in tracer.log if entry[0] == "span"]
    assert names == ["prefetch", "plan", "tool", "plan"]
    generations = [e[1] for e in tracer.log if e[0] == "generation"]
    assert len(generations) == 1 and generations[0]["model"] == "openai/gpt-6-luna" and generations[0]["usage"] == {
        "prompt_tokens": 500, "completion_tokens": 40}
    assert generations[0]["cost_usd"] == pytest.approx(500 * 0.10 / 1e6 + 40 * 0.50 / 1e6)
    assert ("event", "fallback", {"reason": "planner_error:RuntimeError"}) in tracer.log


def test_an_unknown_tool_names_carries_invalid_in_the_tracer_span_not_the_raw_planner_text():
    """R2 (serving-seam security) review: the tracer span used to record the planner's raw, possibly question-steered tool
    NAME before the toolbox validated it. It must instead carry ``"invalid"`` for anything outside the declared set."""
    tracer = Recorder()
    run(ScriptedPlanner(turn(("run_cypher", {"query": "MATCH (n) DETACH DELETE n"})), turn()), tracer=tracer)
    tool_spans = [entry[2] for entry in tracer.log if entry[0] == "span" and entry[1] == "tool"]
    assert tool_spans == [{"tool": "invalid"}]


def test_a_tracer_that_raises_does_not_change_the_run():
    class Boom:
        def span(self, *a, **k):
            raise RuntimeError("no")

        def event(self, *a, **k):
            raise RuntimeError("no")

        def generation(self, **k):
            raise RuntimeError("no")

        def flush(self):
            raise RuntimeError("no")

    events, result, _ = run(ScriptedPlanner(turn(FM), turn()), tracer=Boom())
    assert len(events) == 1 and result.fallback_reason is None


def test_the_planner_is_shown_the_previous_result_as_counts_and_years_only():
    planner = ScriptedPlanner(turn(("risk_changes", {"companies": ["Nvidia"], "fiscal_years": [2024, 2025]})), turn())
    run(planner, driver=FakeDriver.world())
    tool_message = json.loads(planner.calls[1]["messages"][-1]["content"])
    assert tool_message["comparisons"][0]["newer_fiscal_year"] == 2025 and NVDA_ACC["n25"] not in json.dumps(tool_message)
