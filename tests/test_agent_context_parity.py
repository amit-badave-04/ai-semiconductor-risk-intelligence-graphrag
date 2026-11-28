"""M3 R1 (docs/specs/m3-agent-fix.md): the agent's writer and the verifier see the fixed path's context (detail lines ON), and
fall back to detail lines off only when the context would exceed the per-company allowance of the spend estimate."""

import pytest
from agent_fakes import FakeWriter, ScriptedPlanner, make_settings, turn
from test_agent_seam import _detail_retrieval, _fake_run_agent

from semigraph.agent import merge
from semigraph.agent import stream as S
from semigraph.retrieval import answerer
from semigraph.retrieval.answerer import build_blocks, sources_from_context
from semigraph.retrieval.context_layout import DETAIL_LINES_KEY, YOY_OFF, YOY_PRIOR_IDS_KEY
from semigraph.serve import estimate as est

CHUNK = "0001-25-000001:I.1:0001"
ANSWER = f"Nvidia ships GPUs [{CHUNK}]."


def _prompt_of_agent(monkeypatch, r) -> tuple[str, dict]:
    monkeypatch.setattr(S, "run_agent", _fake_run_agent(r))
    writer = FakeWriter(ANSWER)
    events = list(S.agent_answer_stream("q?", None, None, planner=ScriptedPlanner(turn()), settings=make_settings(), llm_stream=writer))
    assert events[-1]["event"] == "done"
    return writer.prompts[0], events[-1]


def test_agent_context_equals_fixed_when_tools_add_nothing(monkeypatch):
    r = _detail_retrieval()
    agent_prompt, _ = _prompt_of_agent(monkeypatch, r)
    fixed = FakeWriter(ANSWER)
    list(answerer.stream_answer_for_context("q?", r, "agent", llm_stream=fixed))
    assert agent_prompt == fixed.prompts[0]
    assert "SECRETQUOTE" in agent_prompt and "SECRETABSTRACT" in agent_prompt
    out = S.agent_answer("q?", None, None, planner=ScriptedPlanner(turn()), settings=make_settings(), llm_stream=FakeWriter(ANSWER))
    assert out["context"] == build_blocks(r)[1]
    assert DETAIL_LINES_KEY not in out["retrieval"]


def test_the_agent_context_helper_returns_the_same_dict_when_it_fits():
    r = _detail_retrieval()
    assert merge.agent_writer_context(r) is r


def test_detail_lines_fall_back_only_past_allowance(monkeypatch):
    r = _detail_retrieval()
    size = len(build_blocks(r)[1])
    monkeypatch.setattr(merge, "context_allowance", lambda n_companies: size)          # exactly fits: detail stays on
    assert DETAIL_LINES_KEY not in merge.agent_writer_context(r)
    monkeypatch.setattr(merge, "context_allowance", lambda n_companies: size - 1)      # one character over: it falls back
    off = merge.agent_writer_context(r)
    assert off[DETAIL_LINES_KEY] is False and off[YOY_PRIOR_IDS_KEY] == YOY_OFF
    _, context, _ = build_blocks(off)
    assert "SECRETQUOTE" not in context and "SECRETABSTRACT" not in context and "SECRETTITLE rule" in context


def test_the_allowance_is_the_spend_estimates_per_company_figure():
    shared = est.context_chars("agent") - est.agent_company_blocks() * est.AGENT_GRAPH_CHARS_PER_COMPANY
    assert merge.context_allowance(2) == shared + 2 * est.AGENT_GRAPH_CHARS_PER_COMPANY
    assert merge.context_allowance(0) == merge.context_allowance(1)               # a defaulted company still counts as one


def test_a_context_past_the_real_allowance_renders_without_detail(monkeypatch):
    r = _detail_retrieval()
    r["chunks"] = [{"chunk_id": f"0001-25-000001:I.1:{i:04d}", "text": "x" * 40_000} for i in range(10)]
    assert len(build_blocks(r)[1]) > merge.context_allowance(1)
    prompt, _ = _prompt_of_agent(monkeypatch, r)
    assert "SECRETQUOTE" not in prompt and "SECRETABSTRACT" not in prompt


def test_zero_tool_calls_do_not_by_themselves_switch_the_detail_lines_off():
    r = merge.show_periods(_detail_retrieval(), years=[2025])          # what merge_metrics does even for 0 new rows
    assert DETAIL_LINES_KEY not in merge.agent_writer_context(r)


def test_verifier_sources_match_writer_context(monkeypatch):
    """The verifier reads its sources from the context the writer was given: an abstract-only fact is citable and grounded."""
    r = _detail_retrieval()
    prompt, _ = _prompt_of_agent(monkeypatch, r)
    sources = sources_from_context(build_blocks(r)[1])
    assert any("SECRETABSTRACT" in text for text in sources.values())
    assert "SECRETABSTRACT" in prompt
    out = S.agent_answer("q?", None, None, planner=ScriptedPlanner(turn()), settings=make_settings(), llm_stream=FakeWriter(ANSWER))
    assert out["valid_ids"] == build_blocks(r)[2] and out["context"] == build_blocks(r)[1]
