"""The small shared types of the agent: limits, the planner ledger and the usage arithmetic."""

import pytest
from agent_fakes import make_settings

from semigraph.agent.state import Ledger, Limits, add_usage, initial_state, recursion_limit_for


def test_limits_come_from_the_settings_with_the_time_budget_as_a_float():
    limits = Limits.from_settings(make_settings())
    assert limits == Limits(max_tool_calls=4, max_model_calls=3, time_budget_s=25.0, planner_call_cap_s=12.0)
    assert isinstance(limits.time_budget_s, float)


def test_usage_adds_up_and_ignores_what_it_cannot_read_because_accounting_must_never_be_what_breaks_a_call():
    total = {"prompt_tokens": 10, "completion_tokens": 2}
    assert add_usage(total, {"prompt_tokens": 5, "completion_tokens": 1}) == {"prompt_tokens": 15, "completion_tokens": 3}
    for unreadable in (None, "nope", 7, {}, {"prompt_tokens": "x", "completion_tokens": None}, {"prompt_tokens": -4}):
        assert add_usage(total, unreadable) == total
    assert total == {"prompt_tokens": 10, "completion_tokens": 2}                  # the argument is never mutated


def test_the_ledger_counts_calls_and_sums_usage():
    ledger = Ledger()
    ledger.model_calls += 1
    ledger.record({"prompt_tokens": 7, "completion_tokens": 3})
    ledger.record(None)
    ledger.record({"prompt_tokens": 1, "completion_tokens": 1})
    assert ledger.model_calls == 1 and ledger.usage == {"prompt_tokens": 8, "completion_tokens": 4}


@pytest.mark.parametrize("model_calls,expected", [(0, 12), (3, 12), (4, 12), (6, 16), (10, 24)])
def test_the_recursion_backstop_is_twelve_until_the_model_limit_needs_more(model_calls, expected):
    assert recursion_limit_for(Limits(4, model_calls, 25.0)) == expected


def test_the_initial_state_has_no_retrieval_yet_and_no_fallback():
    state = initial_state("q", 5.0)
    assert state["question"] == "q" and state["t0"] == 5.0 and state["steps"] == [] and state["fallback_reason"] is None
    assert "r" not in state and "prefetch_r" not in state
