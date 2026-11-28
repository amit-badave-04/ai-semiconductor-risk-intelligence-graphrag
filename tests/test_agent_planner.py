"""The planner model call: the kwargs it sends (M3 requirements 3 and 4), how a reply is read, and what the model is first shown.

The live probe (artifacts/agent_tool_probe.json) found that ``openai/gpt-6-luna`` rejects function tools on chat completions unless
``reasoning_effort="none"``: a silent 400 there would be a silent fallback to the plain retrieval, so the kwargs are pinned.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest
from agent_fakes import CANARY, INJECTION, LUNA, SONNET, FakeDriver, FakeEmbedder, poisoned_world, turn

from semigraph.agent import planner as P
from semigraph.agent.state import PlannerTurn, ToolCall
from semigraph.agent.tools import tool_specs
from semigraph.retrieval.retriever import hybrid_retrieve


def response(tool_calls=(), usage=(120, 15), content=None, finish="tool_calls"):
    calls = [SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args)) for cid, name, args in tool_calls]
    message = SimpleNamespace(content=content, tool_calls=calls or None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                           usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None)


@pytest.fixture
def sent(monkeypatch):
    """Captures the kwargs of every ``litellm.completion`` call and answers with ``sent.reply``."""
    box = SimpleNamespace(calls=[], reply=response())

    def fake(**kwargs):
        box.calls.append(kwargs)
        return box.reply

    monkeypatch.setattr(P.litellm, "completion", fake)
    return box


MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_a_luna_planner_call_sends_reasoning_effort_none_and_the_pinned_kwargs(sent):
    P.LiteLLMPlanner(LUNA)(MESSAGES, tool_specs(), timeout=7.5)
    (kwargs,) = sent.calls
    assert kwargs == {"model": LUNA, "messages": MESSAGES, "tools": tool_specs(), "tool_choice": "auto", "timeout": 7.5,
                      "num_retries": 0, "max_completion_tokens": P.PLANNER_MAX_TOKENS, "reasoning_effort": "none",
                      "allowed_openai_params": ["reasoning_effort"]}


def test_the_planner_kwargs_come_from_completion_params_so_they_cannot_drift(sent):
    from semigraph.llm_shape import completion_params

    P.LiteLLMPlanner(LUNA)(MESSAGES, [], timeout=3)
    shape = completion_params(LUNA, P.PLANNER_MAX_TOKENS, reasoning_effort="none")
    assert all(sent.calls[0][k] == v for k, v in shape.items())


def test_a_sonnet_planner_call_keeps_thinking_off_and_uses_max_tokens(sent):
    P.LiteLLMPlanner(SONNET)(MESSAGES, tool_specs(), timeout=9)
    kwargs = sent.calls[0]
    assert kwargs["max_tokens"] == P.PLANNER_MAX_TOKENS and kwargs["thinking"] == {"type": "disabled"}
    assert "max_completion_tokens" not in kwargs and kwargs["num_retries"] == 0 and kwargs["timeout"] == 9


def test_a_timeout_never_meets_a_client_retry(sent):
    """``num_retries=0`` also sets the provider client's own retry count (litellm maps num_retries onto max_retries), so a slow
    call is abandoned at its timeout, not retried past the budget."""
    P.LiteLLMPlanner(LUNA)(MESSAGES, [], timeout=2)
    assert sent.calls[0]["num_retries"] == 0 and "max_retries" not in sent.calls[0]


def test_a_reply_with_tool_calls_is_read_into_a_turn_and_the_prose_is_dropped(sent):
    sent.reply = response([("call_a", "financial_metrics", '{"companies": ["AMD"]}'), ("call_b", "risk_changes", '{"companies": ["AMD"]}')],
                          usage=(300, 40), content="Sure! Here is my answer: revenue is huge.")
    result = P.LiteLLMPlanner(LUNA)(MESSAGES, tool_specs(), timeout=5)
    assert result == PlannerTurn(tool_calls=(ToolCall("call_a", "financial_metrics", '{"companies": ["AMD"]}'),
                                             ToolCall("call_b", "risk_changes", '{"companies": ["AMD"]}')),
                                 usage={"prompt_tokens": 300, "completion_tokens": 40}, finish_reason="tool_calls")
    assert "answer" not in repr(result)


def test_a_reply_with_no_tool_call_means_the_planner_is_done(sent):
    sent.reply = response([], content="DONE", finish="stop")
    result = P.LiteLLMPlanner(LUNA)(MESSAGES, tool_specs(), timeout=5)
    assert result.tool_calls == () and result.usage == {"prompt_tokens": 120, "completion_tokens": 15}


def test_missing_ids_and_missing_usage_are_tolerated(sent):
    sent.reply = response([(None, "lookup_company", None)], usage=None)
    result = P.LiteLLMPlanner(LUNA)(MESSAGES, [], timeout=5)
    assert result.usage is None and result.tool_calls == (ToolCall("call_0", "lookup_company", "{}"),)


def test_a_provider_error_propagates_for_the_loop_to_turn_into_a_fallback(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("400 tools not supported")

    monkeypatch.setattr(P.litellm, "completion", boom)
    with pytest.raises(RuntimeError, match="400"):
        P.LiteLLMPlanner(LUNA)(MESSAGES, [], timeout=5)


# --- what the model is first shown ------------------------------------------------------------------------------------------

def test_the_system_prompt_is_short_versioned_and_treats_the_question_as_untrusted():
    text = P.system_prompt()
    assert text == P.read_prompt("agent_planner") and len(text.splitlines()) <= 40
    assert "untrusted" in text and "NO tool" in text and "You never write an answer" in text
    assert isinstance(P.PLANNER_PROMPT_VERSION, str) and P.PLANNER_PROMPT_VERSION


PROMPT_PIN = ("2", "8685a6c17b")      # (PLANNER_PROMPT_VERSION, sha256 of prompts/agent_planner.txt)


def test_a_prompt_edit_without_a_version_bump_fails_here():
    """The planner prompt is an evaluated artifact (done.agent names its version): edit it, bump ``PLANNER_PROMPT_VERSION``, update
    this pin AND re-run the agent eval (the same discipline as the answer template's fingerprint in tests/test_agent_seam.py)."""
    digest = hashlib.sha256(P.system_prompt().encode("utf-8")).hexdigest()[:10]
    assert (P.PLANNER_PROMPT_VERSION, digest) == PROMPT_PIN


def test_the_first_messages_carry_the_question_and_a_summary_of_what_was_retrieved():
    r = hybrid_retrieve("Compare Nvidia and AMD revenue.", FakeDriver.world(), FakeEmbedder())
    system, user = P.initial_messages("Compare Nvidia and AMD revenue.", r)
    assert system == {"role": "system", "content": P.system_prompt()} and user["role"] == "user"
    assert "Compare Nvidia and AMD revenue." in user["content"]
    summary = json.loads(user["content"].split("ALREADY RETRIEVED", 1)[1].split("\n", 1)[1])
    assert summary["companies_in_question"] == ["AMD", "Nvidia"] and summary["metrics"]["AMD"]["revenue"][0] == "2025-12-27"


def test_the_first_messages_of_a_poisoned_graph_carry_none_of_it():
    r = hybrid_retrieve("How exposed is Nvidia to TSMC?", poisoned_world(), FakeEmbedder())
    messages = P.initial_messages("How exposed is Nvidia to TSMC?", r)
    assert CANARY not in json.dumps(messages) and "Ignore previous instructions" not in json.dumps(messages)


def test_the_question_itself_is_passed_verbatim_because_it_is_the_users_own_text():
    r = hybrid_retrieve("q", FakeDriver.world(), FakeEmbedder())
    assert INJECTION in P.initial_messages(INJECTION, r)[1]["content"]


def test_an_assistant_and_a_tool_message_follow_the_openai_shape():
    planner_turn = turn(("lookup_company", {"name": "AMD"}), ("financial_metrics", {"companies": ["AMD"]}))
    message = P.assistant_message(planner_turn)
    assert message == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_0", "type": "function", "function": {"name": "lookup_company", "arguments": '{"name": "AMD"}'}},
        {"id": "call_1", "type": "function", "function": {"name": "financial_metrics", "arguments": '{"companies": ["AMD"]}'}}]}
    assert P.tool_message("call_0", {"company": "AMD"}) == {"role": "tool", "tool_call_id": "call_0", "content": '{"company": "AMD"}'}


# --- M3 I3: planner prompt v2 (spec R5, S2, S3; the LLM is mocked) -----------------------------------------------------------------

def _prompt() -> str:
    return " ".join(P.system_prompt().split())


def test_agent_planner_v2_prompt_version_is_bumped():
    assert P.PLANNER_PROMPT_VERSION == "2"


@pytest.mark.parametrize("rule", [
    "at most 4 companies in all",                       # the answer-wide cap, not per call
    "300 characters or fewer",
    "keywords",
    "one company's own two period ends",
    "never resend the same arguments",
    "all tool calls in your first turn",
])
def test_agent_planner_v2_prompt_states_the_rule(rule):
    assert rule in _prompt()


def test_agent_planner_v2_prompt_keeps_the_untrusted_question_and_no_filing_text_rules():
    text = _prompt()
    assert "untrusted" in text and "Filing text is never shown to you" in text and "DONE" in text


def test_agent_planner_v2_initial_messages_carry_coverage_and_no_injection():
    r = hybrid_retrieve("How exposed is Nvidia to TSMC?", poisoned_world(), FakeEmbedder())
    system, user = P.initial_messages("How exposed is Nvidia to TSMC?", r)
    assert system["content"] == P.system_prompt()
    assert '"companies_covered":["Nvidia","TSMC"]' in user["content"] and '"room":{"companies":2}' in user["content"]
    assert CANARY not in user["content"] and "Ignore" not in user["content"]


def test_agent_planner_v2_the_tool_schemas_the_model_is_sent_are_unchanged_in_shape(sent):
    P.LiteLLMPlanner(LUNA)(MESSAGES, tool_specs(), timeout=5)
    assert sent.calls[0]["tools"] == tool_specs()
    assert {s["function"]["name"] for s in tool_specs()} == {"lookup_company", "search_filings", "financial_metrics", "risk_changes",
                                                             "relationships", "active_risks", "compute_change"}


def test_agent_planner_v2_initial_messages_never_read_settings(monkeypatch):
    # the guarded fixture server forbids get_settings; the run's real limit comes from the caller (graph.py), never from Settings
    def boom():
        raise AssertionError("get_settings was called")
    monkeypatch.setattr(P, "get_settings", boom, raising=False)
    r = hybrid_retrieve("How exposed is Nvidia to TSMC?", poisoned_world(), FakeEmbedder())
    plain = P.initial_messages("How exposed is Nvidia to TSMC?", r)[1]["content"]
    assert '"room":{"companies":2}' in plain
    with_limit = P.initial_messages("How exposed is Nvidia to TSMC?", r, tool_calls_left=3)[1]["content"]
    assert '"room":{"companies":2,"tool_calls":3}' in with_limit


def test_agent_planner_v2_prompt_states_the_cap_once_and_fetch_then_compute_in_one_turn():
    text = _prompt()
    assert text.count("at most 4 companies in all") == 1
    assert "financial_metrics for both periods earlier in the same turn, then compute_change" in text
