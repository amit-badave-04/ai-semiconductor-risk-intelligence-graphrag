"""M3 R6 (docs/specs/m3-agent-fix.md): per-span timings ride on done.agent and into the eval rows; the planner's summary shows the
run's own tool-call room. Timings carry names and seconds only, never question or answer text (S5)."""

import json

from agent_fakes import FakeDriver, FakeEmbedder, FakeWriter, ScriptedPlanner, make_settings, turn

from semigraph.agent import graph as G
from semigraph.agent import stream as S
from semigraph.agent.state import Limits
from semigraph.eval import agent_eval as ae

QUESTION = "How exposed is Nvidia to TSMC? SECRET-QUESTION-TOKEN"
CHUNK = "0001045810-26-000021:I.1A:0001"
GOOD = f"Nvidia depends on TSMC for advanced wafer supply [{CHUNK}]. SECRET-ANSWER-TOKEN"
FM = ("financial_metrics", {"companies": ["Nvidia"], "fiscal_years": [2026]})
LIMITS = Limits(max_tool_calls=4, max_model_calls=3, time_budget_s=25.0)


def _events(planner):
    return list(S.agent_answer_stream(QUESTION, FakeDriver.world(), FakeEmbedder(), planner=planner, settings=make_settings(),
                                      llm_stream=FakeWriter(GOOD)))


def test_run_agent_records_prefetch_planner_and_tool_spans():
    gen = G.run_agent(QUESTION, FakeDriver.world(), FakeEmbedder(), planner=ScriptedPlanner(turn(FM), turn()),
                      planner_model="openai/gpt-6-luna", limits=LIMITS)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        result = stop.value
    assert [(t["kind"], t["name"]) for t in result.timings] == [
        ("prefetch", "prefetch"), ("planner", "planner"), ("tool", "financial_metrics"), ("planner", "planner")]
    assert all(set(t) == {"kind", "name", "s"} and isinstance(t["s"], (int, float)) and t["s"] >= 0 for t in result.timings)


def test_done_agent_carries_timings_without_question_or_answer_text():
    done = _events(ScriptedPlanner(turn(FM), turn()))[-1]
    assert done["event"] == "done" and len(done["agent"]["timings"]) == 4
    blob = json.dumps(done["agent"]["timings"])
    assert "SECRET-QUESTION-TOKEN" not in blob and "SECRET-ANSWER-TOKEN" not in blob and "Nvidia" not in blob


def test_eval_rows_carry_span_timings():
    events = _events(ScriptedPlanner(turn(FM), turn()))
    item = {"id": "T", "type": "numeric", "category": "named_years", "split": "agent", "q": QUESTION, "expect": None,
            "expected_tools": [], "forbidden_tools": [], "max_steps": 3, "source": "test"}
    row = ae.agent_row(item, events, 1.0)
    assert [t["kind"] for t in row["agent"]["timings"]] == ["prefetch", "planner", "tool", "planner"]


def test_a_refused_tool_name_is_never_recorded_raw():
    gen = G.run_agent(QUESTION, FakeDriver.world(), FakeEmbedder(), planner=ScriptedPlanner(turn(("EVIL-NAME", {})), turn()),
                      planner_model="m", limits=LIMITS)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        result = stop.value
    assert "EVIL-NAME" not in json.dumps(result.timings)


def test_the_planners_first_message_shows_the_tool_call_room():
    planner = ScriptedPlanner(turn())
    _events(planner)
    user = planner.calls[0]["messages"][1]["content"]
    assert '"tool_calls":4' in user
