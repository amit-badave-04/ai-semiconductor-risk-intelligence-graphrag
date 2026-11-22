"""M3 step 0: the shared surfaces the agent plugs into (docs/v2/M3_AGENT_PLAN.md section 1).

The agent is OPT-IN: ``strategy=agent`` is accepted only when ``AGENT_ENABLED`` is set, ``stream_answer_for_context`` is the ONE
draft / verify / escalate implementation both paths share, the agent package is imported lazily (langgraph is not in the slim serve
image unless the agent is enabled), and code-computed change lines ride in the METRICS block as data (no template change).
"""

import inspect
import sys
import types

import pytest
from fastapi import HTTPException

from semigraph.config import Settings
from semigraph.retrieval import answerer
from semigraph.retrieval.answerer import build_blocks, template_fingerprint
from semigraph.serve import guard, routes


def test_the_agent_is_off_by_default_with_the_plans_limits():
    s = Settings(_env_file=None)
    assert s.agent_enabled is False and s.agent_planner_model == "openai/gpt-6-luna"
    assert (s.agent_max_tool_calls, s.agent_max_model_calls, s.agent_time_budget_s) == (4, 3, 25)
    assert s.langfuse_sample_rate == 0.1 and s.langfuse_public_key == "" and s.langfuse_host == ""
    assert "langfuse_secret_key" not in repr(s) and "sk-lf" not in repr(Settings(_env_file=None, langfuse_secret_key="sk-lf-x"))


def test_strategy_agent_is_refused_unless_the_agent_is_enabled():
    with pytest.raises(HTTPException) as e:
        guard.validate_strategy("agent")
    assert e.value.status_code == 400 and "agent" not in str(e.value.detail)
    assert guard.validate_strategy("agent", agent_enabled=True) == "agent"
    assert guard.validate_strategy("Agent", agent_enabled=True) == "agent"
    assert guard.validate_strategy("", agent_enabled=True) == "hybrid" and guard.validate_strategy("vector") == "vector"


def test_the_error_of_an_enabled_agent_lists_the_agent_as_an_option():
    with pytest.raises(HTTPException) as e:
        guard.validate_strategy("nonsense", agent_enabled=True)
    assert "agent" in str(e.value.detail) and "hybrid" in str(e.value.detail)


def _retrieval(**layers):
    base = {"anchors": {}, "edges": [], "metrics": [], "risks": [], "temporal": [], "temporal_pairs": [],
            "temporal_passages": [], "chunks": []}
    return {**base, **layers}


def test_computed_lines_are_appended_to_the_metrics_block_as_data_and_change_nothing_without_them():
    plain, _, _ = build_blocks(_retrieval())
    assert plain.metrics_block == "(none)"
    computed = "computed: +65.5% vs fiscal year ended 2025-01-26 (change +85,441,000,000 USD)"
    blocks, context, _ = build_blocks(_retrieval(computed=[computed]))
    assert blocks.metrics_block == computed and computed in context
    assert build_blocks(_retrieval(computed=[])) == build_blocks(_retrieval())


def test_the_template_is_not_touched_by_the_seam():
    """Computed lines are DATA in METRICS: the agent must never change the prompt template (seeded examples, the cache and the
    eval baselines are keyed to it). A deliberate template change updates this pin AND re-runs the benchmark."""
    assert template_fingerprint() == "fbed8caa9e"


def test_stream_answer_for_context_is_what_answer_stream_delegates_to():
    """answer_stream = retrieval + stream_answer_for_context; a caller with its OWN retrieval dict gets the same events."""
    events = list(answerer.stream_answer_for_context(
        "q?", _retrieval(chunks=[{"chunk_id": "0001-25-000001:I.1:0001", "text": "Nvidia depends on TSMC."}]), "agent",
        llm_stream=lambda prompt: iter(["Nvidia depends on TSMC [0001-25-000001:I.1:0001]."])))
    assert [e["event"] for e in events][0] == "retrieval" and events[-1]["event"] == "done"
    assert events[-1]["strategy"] == "agent" and events[-1]["citations"] == ["0001-25-000001:I.1:0001"]
    assert events[-1]["checks"]["citations_retrieved"] is True


def test_the_route_streams_through_the_agent_only_for_strategy_agent_and_imports_it_lazily(monkeypatch):
    sentinel = object()
    stub = types.ModuleType("semigraph.agent.stream_async")
    stub.aagent_answer_stream = sentinel
    pkg = types.ModuleType("semigraph.agent")
    monkeypatch.setitem(sys.modules, "semigraph.agent", pkg)
    monkeypatch.setitem(sys.modules, "semigraph.agent.stream_async", stub)
    assert routes._stream_fn("agent") is sentinel
    assert routes._stream_fn("hybrid") is routes.aanswer_stream and routes._stream_fn("vector") is routes.aanswer_stream
    assert routes._stream_fn("hybrid", True) is routes.astream_workspace_answer   # a workspace ask never gets the agent
    assert routes._stream_fn("agent", True) is routes.astream_workspace_answer


def test_every_twin_a_route_can_stream_through_takes_the_paid_call_meter_as_its_own_keyword():
    """The runtime hands the ask's meter to whichever twin the route resolved (``twin(..., meter=...)``). A twin without
    that keyword would fail every ask, and one that took it through ``**kwargs`` would hand the meter to a model stream.
    The same keyword runs through the writer functions the twins call, so an injected stream never receives it."""
    from semigraph.agent.stream_async import aagent_answer_stream
    from semigraph.retrieval import answerer_async

    twins = [routes.aanswer_stream, routes.astream_workspace_answer, aagent_answer_stream,
             answerer_async.astream_answer_for_context, answerer_async.astream_answer_for_prompt]
    for twin in twins:
        meter = inspect.signature(twin).parameters["meter"]
        assert meter.kind is inspect.Parameter.KEYWORD_ONLY and meter.default is None, twin.__name__
    assert inspect.signature(routes.aanswer_stream).parameters["limiters"].kind is inspect.Parameter.KEYWORD_ONLY


# --- M2a R6b / S14: the detail lines (edge quotes, rule abstracts) are the plain hybrid ask's; the agent switches them off -------------

DETAIL_EDGE = {"source": "Nvidia", "relation": "SUPPLIES_TO", "target": "AMD", "status": "Active", "chunk_ids": ["0001-25-000001:I.1:0001"],
               "quote": "SECRETQUOTE we ship GPUs.", "source_cik": 1045810, "target_cik": 2,
               "evidence": [{"id": "0001-25-000001:I.1:0001", "is_current": True, "filing_date": "2026-01-01"}]}
DETAIL_RULE = {"source": "Nvidia", "relation": "AFFECTED_BY", "target": "SECRETTITLE rule", "status": "Active", "quote": None,
               "chunk_ids": [], "rule_id": "2026-12345", "date": "2026-01-02", "link_method": "keyword",
               "abstract": "SECRETABSTRACT what it does."}


def _detail_retrieval():
    return _retrieval(anchors={"Nvidia": 1045810}, edges=[DETAIL_EDGE, DETAIL_RULE],
                      chunks=[{"chunk_id": "0001-25-000001:I.1:0001", "text": "Nvidia ships GPUs."}])


def _fake_run_agent(r):
    from semigraph.agent.graph import AgentResult

    def run_agent(*args, **kwargs):
        if False:
            yield
        return AgentResult(r=r, tool_calls=[], model_calls=1, elapsed_s=0.0, fallback_reason=None, stop_reason="done",
                           planner_usage={"prompt_tokens": 10, "completion_tokens": 1})
    return run_agent


def test_the_agent_writer_gets_no_quote_or_abstract_lines_and_the_hybrid_writer_gets_them(monkeypatch):
    from agent_fakes import FakeWriter, ScriptedPlanner, make_settings, turn

    from semigraph.agent import stream as S

    monkeypatch.setattr(S, "run_agent", _fake_run_agent(_detail_retrieval()))
    agent_writer = FakeWriter("Nvidia ships GPUs [0001-25-000001:I.1:0001].")
    events = list(S.agent_answer_stream("q?", None, None, planner=ScriptedPlanner(turn()), settings=make_settings(),
                                        llm_stream=agent_writer))
    assert events[-1]["event"] == "done"
    prompt = agent_writer.prompts[0]
    assert "SECRETQUOTE" not in prompt and "SECRETABSTRACT" not in prompt and "evidence [" not in prompt
    assert "SECRETTITLE rule" in prompt                       # R2 and the rule line itself stay
    hybrid_writer = FakeWriter("Nvidia ships GPUs [0001-25-000001:I.1:0001].")
    list(answerer.stream_answer_for_context("q?", _detail_retrieval(), "hybrid", llm_stream=hybrid_writer))
    assert "SECRETQUOTE" in hybrid_writer.prompts[0] and "SECRETABSTRACT" in hybrid_writer.prompts[0]


def test_agent_answer_rebuilds_the_context_with_the_switch_off(monkeypatch):
    from agent_fakes import FakeWriter, ScriptedPlanner, make_settings, turn

    from semigraph.agent import stream as S

    monkeypatch.setattr(S, "run_agent", _fake_run_agent(_detail_retrieval()))
    out = S.agent_answer("q?", None, None, planner=ScriptedPlanner(turn()), settings=make_settings(),
                         llm_stream=FakeWriter("Nvidia ships GPUs [0001-25-000001:I.1:0001]."))
    assert "SECRETQUOTE" not in out["context"] and "SECRETABSTRACT" not in out["context"]
    assert "detail_lines" not in out["retrieval"]             # the switch is applied for rendering only


def test_the_async_agent_writer_is_handed_the_switched_off_retrieval():
    """The async twin builds its writer call from ``plan.r`` at one place; it must go through the same switch."""
    from semigraph.agent import stream_async

    assert "question, without_detail_lines(plan.r), strategy" in inspect.getsource(stream_async)


def test_the_planner_view_never_receives_quotes_abstracts_or_titles():
    import json

    from semigraph.agent import sanitize

    r = _detail_retrieval()
    views = [sanitize.view_edges(r["edges"]), sanitize.prefetch_summary(r)]
    dumped = json.dumps(views)
    assert "SECRETQUOTE" not in dumped and "SECRETABSTRACT" not in dumped and "SECRETTITLE" not in dumped
    assert "we ship GPUs" not in dumped and "what it does" not in dumped
