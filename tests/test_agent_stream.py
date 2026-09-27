"""The entry point ``agent_answer_stream`` (docs/v2/M3_AGENT_PLAN.md section 6): the event grammar, the `done.agent` object, the
spend of BOTH models on BOTH terminal events (requirement 1), the never-worse invariant, argument pass-through and the tracer seam.
"""

import inspect
import json

import pytest
from agent_fakes import (
    LUNA,
    SONNET,
    FakeDriver,
    FakeEmbedder,
    FakeWriter,
    ScriptedPlanner,
    kinds,
    make_settings,
    turn,
)

from semigraph.agent import stream as S
from semigraph.eval import agent_eval as ae
from semigraph.retrieval import answerer as answerer_mod
from semigraph.retrieval.answerer import answer_stream, usage_cost

QUESTION = "How exposed is Nvidia to TSMC?"
CHUNK = "0001045810-26-000021:I.1A:0001"
GOOD = f"Nvidia depends on TSMC for advanced wafer supply [{CHUNK}]."
FM = ("financial_metrics", {"companies": ["Nvidia"], "fiscal_years": [2026]})
AGENT_KEYS = {"tool_calls", "model_calls", "elapsed_s", "fallback_reason", "planner_model", "planner_usage", "planner_cost_usd",
              "planner_prompt_version", "stop_reason"}


def stream(planner=None, writer=None, *, question=QUESTION, driver=None, settings=None, fallback=None, **kw):
    """Collect the events. A run that ends in ``done`` must have the ``fallback_reason`` the test expects (None for a happy path,
    M3 requirement 3: a silent 400 -> fallback must never be mistaken for an agent run)."""
    writer = writer or FakeWriter(GOOD)
    planner = planner or ScriptedPlanner(turn())
    kw.setdefault("llm_stream", writer)
    events = list(S.agent_answer_stream(question, driver or FakeDriver.world(), FakeEmbedder(), planner=planner,
                                        settings=settings or make_settings(), **kw))
    if events[-1]["event"] == "done":
        assert events[-1]["agent"]["fallback_reason"] == fallback
    return events


# --- the contract -----------------------------------------------------------------------------------------------------------

def test_the_entry_point_has_the_contract_signature():
    sig = inspect.signature(S.agent_answer_stream)
    params = sig.parameters
    assert list(params)[:4] == ["question", "driver", "embedder", "strategy"] and params["strategy"].default == "agent"
    keyword_only = {n: p.default for n, p in params.items() if p.kind is inspect.Parameter.KEYWORD_ONLY}
    assert keyword_only == {"timeout": None, "max_tokens": 1200, "escalation_model": None, "settings": None, "planner": None,
                            "tracer": None, "llm_stream": None, "escalation_stream": None}
    assert params["stream_kwargs"].kind is inspect.Parameter.VAR_KEYWORD


def test_the_events_are_step_events_then_the_answer_stream_grammar():
    events = stream(ScriptedPlanner(turn(FM), turn()))
    assert kinds(events) == ["step", "retrieval", "delta", "done"]
    assert events[0] == {"event": "step", "n": 1, "tool": "financial_metrics", "args": {"companies": ["Nvidia"], "fiscal_years": [2026]},
                         "summary": events[0]["summary"], "ok": True}
    assert events[-1]["strategy"] == "agent" and events[-1]["answer"] == GOOD and events[-1]["citations"] == [CHUNK]
    assert events[1]["anchors"] == {"Nvidia": 1045810, "TSMC": 1046179}


def test_done_carries_the_agent_object_and_a_happy_path_run_is_not_a_fallback():
    done = stream(ScriptedPlanner(turn(FM), turn()))[-1]
    agent = done["agent"]
    assert set(agent) == AGENT_KEYS and agent["fallback_reason"] is None                         # requirement 3
    assert agent["tool_calls"] == [{"tool": "financial_metrics", "args": {"companies": ["Nvidia"], "fiscal_years": [2026]}, "ok": True}]
    assert agent["model_calls"] == 2 and agent["planner_model"] == LUNA and agent["stop_reason"] == "planner_done"
    assert agent["planner_usage"] == {"prompt_tokens": 1000, "completion_tokens": 80} and agent["planner_prompt_version"]
    assert isinstance(agent["elapsed_s"], float)


def test_tool_calls_exclude_the_prefetch_and_match_the_step_events_one_to_one():
    events = stream(ScriptedPlanner(turn(FM, ("lookup_company", {"name": "AMD"})), turn()))
    steps = [e for e in events if e["event"] == "step"]
    assert [s["tool"] for s in steps] == [c["tool"] for c in events[-1]["agent"]["tool_calls"]] == ["financial_metrics", "lookup_company"]
    assert {c["tool"] for c in events[-1]["agent"]["tool_calls"]} <= ae.AGENT_TOOL_UNIVERSE


def test_a_fallback_is_reported_and_answers_like_the_plain_retrieval():
    events = stream(ScriptedPlanner(RuntimeError("400 tools unsupported")), fallback="planner_error:RuntimeError")
    agent = events[-1]["agent"]
    assert kinds(events) == ["retrieval", "delta", "done"] and agent["fallback_reason"] == "planner_error:RuntimeError"
    assert agent["tool_calls"] == [] and agent["planner_cost_usd"] == 0.0 and agent["planner_usage"] == {"prompt_tokens": 0, "completion_tokens": 0}


# --- the never-worse invariant --------------------------------------------------------------------------------------------------

def plain_events(writer):
    return list(answer_stream(QUESTION, FakeDriver.world(), FakeEmbedder(), llm_stream=writer))


@pytest.mark.parametrize("make_planner,fallback", [
    (lambda: ScriptedPlanner(turn()), None),
    (lambda: ScriptedPlanner(RuntimeError("down")), "planner_error:RuntimeError"),
    (lambda: ScriptedPlanner(turn(("run_cypher", {"query": "MATCH (n) RETURN n"})), turn()), None),
    (lambda: ScriptedPlanner(turn(FM), RuntimeError("down after a good tool call")), "planner_error:RuntimeError"),
], ids=["no-tool", "planner-error", "refused-tool", "error-after-a-tool"])
def test_a_planner_that_adds_nothing_gives_the_writer_the_byte_identical_prompt(make_planner, fallback):
    """A run that adds nothing, and EVERY fallback (even after a tool call that worked), answers exactly like the fixed path."""
    plain_writer, agent_writer = FakeWriter(GOOD), FakeWriter(GOOD)
    plain = plain_events(plain_writer)
    agent = stream(make_planner(), agent_writer, fallback=fallback)
    assert agent_writer.prompts == plain_writer.prompts
    assert [e for e in agent if e["event"] == "retrieval"] == [e for e in plain if e["event"] == "retrieval"]
    assert [e for e in agent if e["event"] == "delta"] == [e for e in plain if e["event"] == "delta"]
    done, plain_done = agent[-1], plain[-1]
    assert done["answer"] == plain_done["answer"] and done["checks"] == plain_done["checks"] and done["citations"] == plain_done["citations"]


def test_a_tool_result_changes_what_the_writer_is_given():
    plain_writer, agent_writer = FakeWriter(GOOD), FakeWriter(GOOD)
    plain_events(plain_writer)
    stream(ScriptedPlanner(turn(FM), turn()), agent_writer)
    assert "xbrl:1045810:revenue:2023-01-29" not in plain_writer.prompts[0]
    fetched = stream(ScriptedPlanner(turn(("financial_metrics", {"companies": ["Nvidia"], "fiscal_years": [2023]})), turn()), agent_writer)
    assert "xbrl:1045810:revenue:2023-01-29" in agent_writer.prompts[-1] and fetched[-1]["event"] == "done"


# --- spend (requirement 1) ----------------------------------------------------------------------------------------------------

PLANNER_USAGE, WRITER_USAGE = (2000, 100), (12000, 300)
PLANNER_USD = 2000 * 0.10 / 1e6 + 100 * 0.50 / 1e6            # Luna's own rates (KNOWN_PRICES_PER_MTOK)
WRITER_USD = 12000 * 2.0 / 1e6 + 300 * 10.0 / 1e6             # Sonnet's own rates


def test_the_planner_spend_is_added_in_dollars_at_the_planners_own_rates_on_the_done_path():
    events = stream(ScriptedPlanner(turn(usage=PLANNER_USAGE)), FakeWriter(GOOD, usage=WRITER_USAGE, model=SONNET))
    done = events[-1]
    assert done["agent"]["planner_cost_usd"] == pytest.approx(PLANNER_USD) and done["agent"]["planner_usage"] == {
        "prompt_tokens": 2000, "completion_tokens": 100}
    assert done["cost_usd"] == pytest.approx(PLANNER_USD + WRITER_USD)
    assert done["usage"] == {"prompt_tokens": 12000, "completion_tokens": 300}                 # the WRITER's tokens only
    tokens_priced_once_at_the_writers_rate = (14000 * 2.0 + 400 * 10.0) / 1e6
    assert done["cost_usd"] != pytest.approx(tokens_priced_once_at_the_writers_rate)


def test_every_planner_call_is_summed_and_priced():
    events = stream(ScriptedPlanner(turn(FM, usage=(2000, 100)), turn(usage=(3000, 50))), FakeWriter(GOOD, usage=WRITER_USAGE, model=SONNET))
    done = events[-1]
    assert done["agent"]["planner_usage"] == {"prompt_tokens": 5000, "completion_tokens": 150}
    assert done["agent"]["planner_cost_usd"] == pytest.approx(usage_cost({"prompt_tokens": 5000, "completion_tokens": 150}, LUNA))
    assert done["cost_usd"] == pytest.approx(done["agent"]["planner_cost_usd"] + WRITER_USD)


def test_the_planner_spend_is_added_on_the_error_path_too():
    writer = FakeWriter("partial", usage=WRITER_USAGE, model=SONNET, boom=RuntimeError("stream interrupted"))
    events = stream(ScriptedPlanner(turn(usage=PLANNER_USAGE)), writer)
    error = events[-1]
    assert error["event"] == "error" and "stream interrupted" in error["detail"]
    assert error["cost_usd"] == pytest.approx(PLANNER_USD + WRITER_USD) and error["usage"] == {"prompt_tokens": 12000, "completion_tokens": 300}


def test_an_error_with_an_unknown_writer_spend_still_carries_the_planners():
    events = stream(ScriptedPlanner(turn(usage=PLANNER_USAGE)), FakeWriter("x", usage=None, model=SONNET, boom=RuntimeError("down")))
    assert events[-1]["event"] == "error" and events[-1]["cost_usd"] == pytest.approx(PLANNER_USD)


def test_an_escalated_answer_prices_the_draft_the_strong_model_and_the_planner_each_at_their_own_rates():
    draft = FakeWriter("Nvidia depends on TSMC [0000000000-00-000000:I.1:9999].", usage=(9000, 200), model=LUNA)   # a fabricated citation
    strong = FakeWriter(GOOD, usage=WRITER_USAGE, model=SONNET)
    events = stream(ScriptedPlanner(turn(usage=PLANNER_USAGE)), draft, escalation_stream=strong, escalation_model=SONNET, model=LUNA)
    done = events[-1]
    draft_usd = 9000 * 0.10 / 1e6 + 200 * 0.50 / 1e6
    assert done["escalated"] is True and done["cost_usd"] == pytest.approx(PLANNER_USD + draft_usd + WRITER_USD)
    assert done["agent"]["planner_cost_usd"] == pytest.approx(PLANNER_USD)


def test_an_unexpected_failure_of_the_answer_phase_still_reports_the_planners_spend(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("a bug in the answer phase")
        yield

    monkeypatch.setattr(S, "stream_answer_for_context", boom)
    events = stream(ScriptedPlanner(turn(usage=PLANNER_USAGE)))
    assert kinds(events) == ["error"] and "ValueError" in events[0]["detail"] and events[0]["cost_usd"] == pytest.approx(PLANNER_USD)
    with pytest.raises(ValueError):                              # nothing spent by the agent: the exception propagates, as on the fixed path
        stream(ScriptedPlanner(RuntimeError("no planner spend")), fallback="planner_error:RuntimeError")


def test_a_run_with_no_planner_call_adds_nothing_and_keeps_an_unknown_writer_cost_unknown():
    events = stream(ScriptedPlanner(RuntimeError("x")), FakeWriter(GOOD, usage=None, model=SONNET), fallback="planner_error:RuntimeError")
    assert events[-1]["agent"]["planner_cost_usd"] == 0.0 and events[-1]["cost_usd"] is None


def test_the_harness_scorers_find_a_clean_run_consistent():
    """Worker C's own scorers (eval/agent_eval.py) are the acceptance test of the contract: trajectory, limits, fallback and spend."""
    settings = make_settings()
    writer = FakeWriter(f"Nvidia's total revenue was $215.9 billion for the fiscal year ended January 25, 2026 [xbrl:1045810:revenue:2026-01-25].",
                        usage=(12000, 300), model=LUNA)
    events = stream(ScriptedPlanner(turn(FM), turn(), ), writer, question="What was Nvidia's total revenue for the fiscal year ended January 25, 2026?",
                    escalation_model=SONNET, model=LUNA, settings=settings)
    item = {"id": "T01", "type": "numeric", "category": "named_years", "split": "agent", "q": "q", "expect": {"value": 215938000000},
            "expected_tools": ["financial_metrics"], "forbidden_tools": ["risk_changes"], "max_steps": 3, "source": "test"}
    row = ae.agent_row(item, events, 4.0)
    scored = ae.score_run(item, row, ae.limits_from_settings(settings))
    assert scored["trajectory_failures"] == [] and scored["limit_failures"] == [] and scored["fallback_failures"] == []
    assert scored["spend_failures"] == [] and scored["mechanical"] is True and scored["failed_checks"] == []
    assert events[-1]["escalated"] is False and events[-1]["answered_by"] == LUNA


def test_the_harness_scorers_flag_a_fallback_and_a_missing_planner_cost():
    settings = make_settings()
    events = stream(ScriptedPlanner(RuntimeError("down")), settings=settings, fallback="planner_error:RuntimeError")
    item = {"id": "T02", "type": "numeric", "category": "named_years", "split": "agent", "q": "q", "expect": None,
            "expected_tools": [], "forbidden_tools": [], "max_steps": 3, "source": "test"}
    scored = ae.score_run(item, ae.agent_row(item, events, 1.0), ae.limits_from_settings(settings))
    assert scored["fallback_failures"] == ["fallback:planner_error:RuntimeError"]


# --- pass-through of the answer_stream keyword arguments ---------------------------------------------------------------------

class SpyStream:
    calls = []

    def __init__(self, prompt, **kwargs):
        SpyStream.calls.append(kwargs)
        self.usage, self.finish_reason, self.model = {"prompt_tokens": 10, "completion_tokens": 5}, "stop", kwargs.get("model", LUNA)

    def __iter__(self):
        yield GOOD


def test_the_writers_keyword_arguments_reach_the_writer_and_the_agents_own_do_not(monkeypatch):
    SpyStream.calls = []
    monkeypatch.setattr(answerer_mod, "TextStream", SpyStream)
    driver = FakeDriver.world()
    events = list(S.agent_answer_stream(QUESTION, driver, FakeEmbedder(), planner=ScriptedPlanner(turn()), settings=make_settings(),
                                        timeout=33, max_tokens=777, model=SONNET, k_chunks=3, hops=1))
    assert SpyStream.calls == [{"model": SONNET, "max_tokens": 777, "timeout": 33}]         # nothing of the agent leaks into TextStream
    assert driver.params_of("excerpts")[0]["k"] == 3 and events[-1]["event"] == "done"


def test_without_a_timeout_the_writer_is_not_given_one(monkeypatch):
    SpyStream.calls = []
    monkeypatch.setattr(answerer_mod, "TextStream", SpyStream)
    list(S.agent_answer_stream(QUESTION, FakeDriver.world(), FakeEmbedder(), planner=ScriptedPlanner(turn()), settings=make_settings()))
    assert SpyStream.calls == [{"max_tokens": 1200}]


def test_the_default_planner_is_the_configured_model(monkeypatch):
    made = []

    class Spy:
        def __init__(self, model):
            made.append(model)

        def __call__(self, messages, tools, *, timeout):
            return turn()

    monkeypatch.setattr(S, "LiteLLMPlanner", Spy)
    list(S.agent_answer_stream(QUESTION, FakeDriver.world(), FakeEmbedder(), llm_stream=FakeWriter(GOOD),
                               settings=make_settings(agent_planner_model="anthropic/claude-sonnet-5")))
    assert made == ["anthropic/claude-sonnet-5"]


# --- the tracer seam --------------------------------------------------------------------------------------------------------------

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
                log.append(("end", name))
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


def test_the_tracer_sees_the_run_and_never_the_question_or_the_answer_text():
    tracer = Recorder()
    events = stream(ScriptedPlanner(turn(FM), turn()), tracer=tracer)
    text = json.dumps(tracer.log, default=str)
    assert tracer.log[0][:2] == ("span", "agent") and tracer.log[-1] == ("flush",)
    assert QUESTION not in text and GOOD not in text
    root = tracer.log[0][2]
    assert root["question_chars"] == len(QUESTION) and "question_sha" not in root and "question" not in root
    assert events[-1]["event"] == "done" and ("generation" in [entry[0] for entry in tracer.log])


def test_a_tracer_that_raises_from_every_method_does_not_change_the_events():
    class Boom:
        def span(self, *a, **k):
            raise RuntimeError("span")

        def event(self, *a, **k):
            raise RuntimeError("event")

        def generation(self, **k):
            raise RuntimeError("generation")

        def flush(self):
            raise RuntimeError("flush")

    quiet = stream(ScriptedPlanner(turn(FM), turn()))
    loud = stream(ScriptedPlanner(turn(FM), turn()), tracer=Boom())
    assert kinds(loud) == kinds(quiet) and loud[-1]["answer"] == quiet[-1]["answer"] and loud[-1]["cost_usd"] == quiet[-1]["cost_usd"]


def test_a_tracer_whose_span_raises_on_enter_and_exit_is_survived():
    class BadSpan(Recorder):
        def span(self, name, **attrs):
            class Span:
                def __enter__(self):
                    raise RuntimeError("enter")

                def __exit__(self, *exc):
                    raise RuntimeError("exit")

            return Span()

    assert kinds(stream(ScriptedPlanner(turn(FM), turn()), tracer=BadSpan())) == ["step", "retrieval", "delta", "done"]


def test_the_tracer_is_flushed_even_when_the_stream_is_abandoned():
    tracer = Recorder()
    gen = S.agent_answer_stream(QUESTION, FakeDriver.world(), FakeEmbedder(), planner=ScriptedPlanner(turn(FM), turn()),
                                settings=make_settings(), llm_stream=FakeWriter(GOOD), tracer=tracer)
    next(gen)
    gen.close()
    assert tracer.log[-1] == ("flush",)


# --- agent_answer: the non-streaming helper -----------------------------------------------------------------------------------

def test_agent_answer_returns_the_shape_of_answer_plus_the_agent_object():
    result = S.agent_answer(QUESTION, FakeDriver.world(), FakeEmbedder(), planner=ScriptedPlanner(turn(FM), turn()),
                            settings=make_settings(), llm_stream=FakeWriter(GOOD, usage=WRITER_USAGE, model=SONNET))
    plain = answerer_mod.answer(QUESTION, FakeDriver.world(), FakeEmbedder(), llm=lambda prompt: GOOD)
    assert set(plain) <= set(result)
    assert result["answer"] == GOOD and result["citations"] == [CHUNK] and result["cited"] == {CHUNK} and result["hallucinated"] == set()
    assert result["strategy"] == "agent" and result["agent"]["tool_calls"][0]["tool"] == "financial_metrics"
    assert CHUNK in result["valid_ids"] and result["context"].startswith("RELATIONSHIPS:") and result["checks"]["citations_retrieved"] is True
    assert result["cost_usd"] == pytest.approx(result["agent"]["planner_cost_usd"] + WRITER_USD) and result["usage"]["prompt_tokens"] == 12000
    assert result["retrieval"]["metric_periods"]["years"] == [2026] and [s["tool"] for s in result["steps"]] == ["financial_metrics"]


def test_agent_answer_raises_on_an_error_event_like_answer_does():
    with pytest.raises(S.AgentAnswerError, match="stream interrupted") as info:
        S.agent_answer(QUESTION, FakeDriver.world(), FakeEmbedder(), planner=ScriptedPlanner(turn(usage=PLANNER_USAGE)),
                       settings=make_settings(), llm_stream=FakeWriter("x", boom=RuntimeError("stream interrupted"), usage=WRITER_USAGE, model=SONNET))
    assert info.value.event["cost_usd"] == pytest.approx(PLANNER_USD + WRITER_USD)


def test_a_failing_prefetch_raises_out_of_the_stream_like_the_plain_path_does():
    class Down(FakeDriver):
        def answer(self, query, params):
            raise ConnectionError("neo4j unreachable")

    with pytest.raises(ConnectionError):
        stream(driver=Down())

