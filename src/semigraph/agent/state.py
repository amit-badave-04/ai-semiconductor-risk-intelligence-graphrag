"""Shared types of the agent: the limits it runs under, one planner turn, and the LangGraph state.

No langgraph and no litellm here, so the tools, the fakes and the tests can import it freely.
"""

from dataclasses import dataclass, field
from typing import Any, TypedDict

# A planner call gets ``timeout = min(remaining budget, PLANNER_CALL_CAP_S)``: LangGraph runs synchronously here, so the request
# timeout is the ONLY thing that can stop a hung model from holding the whole plan (and an answer slot) past its budget.
PLANNER_CALL_CAP_S = 12.0
# Below this much remaining budget no planner call is started (a time_budget fallback: the answer is the plain retrieval).
MIN_CALL_BUDGET_S = 1.0
# LangGraph counts every node execution: prefetch + (plan + tools) x model calls + finalize is 8 at the default limits. The
# recursion limit is a BACKSTOP against a routing bug (an endless loop), never a limit the loop should meet: 12 at the defaults,
# and never below what ``max_model_calls`` needs when an operator raises it (see :func:`recursion_limit_for`).
RECURSION_LIMIT = 12


@dataclass(frozen=True)
class Limits:
    """What bounds one agent run (plan section 1). ``max_tool_calls`` counts PLANNER-issued tool calls only (the prefetch is not
    one), refused calls included; ``time_budget_s`` covers the planning phase (prefetch to finalize), never the writer."""

    max_tool_calls: int
    max_model_calls: int
    time_budget_s: float
    planner_call_cap_s: float = PLANNER_CALL_CAP_S

    @classmethod
    def from_settings(cls, settings) -> "Limits":
        return cls(int(settings.agent_max_tool_calls), int(settings.agent_max_model_calls), float(settings.agent_time_budget_s))


def recursion_limit_for(limits: Limits) -> int:
    """LangGraph's ``recursion_limit`` for ``limits``: 12 at the defaults, more when ``max_model_calls`` needs more node steps
    (prefetch, then a plan and a tools step per model call, then finalize) so the backstop can never fire before the real limit."""
    return max(RECURSION_LIMIT, 2 * limits.max_model_calls + 4)


@dataclass(frozen=True)
class ToolCall:
    """One tool call the planner model asked for, exactly as it arrived (``arguments`` is the raw JSON text)."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class PlannerTurn:
    """One planner reply: the tool calls it asks for (none = it is done) and the tokens it spent (None = not reported).

    The model's prose, if any, is dropped on purpose: the planner never writes the answer."""

    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict | None = None
    finish_reason: str | None = None


class AgentState(TypedDict, total=False):
    """The LangGraph state. Every node returns a NEW value for the keys it changes (nothing is mutated), so the state a failed
    node leaves behind is always a consistent one."""

    question: str
    t0: float                       # the clock at the start of the run: the budget is measured from here
    r: dict[str, Any]               # the retrieval dict (prefetch, then the tool merges)
    prefetch_r: dict[str, Any]      # the plain hybrid retrieval, never touched: what a FALLBACK answers from
    messages: list[dict]            # the planner conversation
    pending: list[dict]             # the tool calls of the last planner turn, not yet run: {"id", "name", "arguments"}
    steps: list[dict]               # the tool calls run so far: {"n", "tool", "args", "summary", "ok"}
    fallback_reason: str | None     # set when the agent degraded to the plain retrieval (a planner error, the time budget, a bug)
    stop_reason: str | None         # why planning ended: planner_done | tool_limit | model_limit | time_budget | fallback
    elapsed_s: float


@dataclass
class Ledger:
    """What the planner has cost so far, kept OUTSIDE the graph state on purpose. The planner node records a call the moment it
    returns (and counts one BEFORE it is made), so a bug later in the same node, which discards that node's state update, can never
    lose a paid call. It is a run-local counter, never shared and never part of the retrieval data."""

    model_calls: int = 0
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})

    def record(self, usage: dict | None) -> None:
        self.usage = add_usage(self.usage, usage)


def _tokens(usage: dict, key: str) -> int:
    try:
        return max(0, int(usage.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def add_usage(total: dict, usage: dict | None) -> dict:
    """``total`` plus one call's ``usage`` as a new dict (a call that reported no usage, or a malformed one, adds nothing: the
    accounting of a planner call must never be what breaks it)."""
    if not isinstance(usage, dict):
        return dict(total)
    return {"prompt_tokens": total.get("prompt_tokens", 0) + _tokens(usage, "prompt_tokens"),
            "completion_tokens": total.get("completion_tokens", 0) + _tokens(usage, "completion_tokens")}


def initial_state(question: str, t0: float) -> AgentState:
    return {"question": question, "t0": t0, "messages": [], "pending": [], "steps": [], "fallback_reason": None, "stop_reason": None}
