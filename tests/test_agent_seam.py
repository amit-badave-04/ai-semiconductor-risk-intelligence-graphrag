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
    assert template_fingerprint() == "4d0a62f5a0"


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
