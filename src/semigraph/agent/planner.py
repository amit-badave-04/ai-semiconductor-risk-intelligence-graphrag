"""The planner: one cheap model call that chooses read-only lookups (never the answer).

``LiteLLMPlanner`` is the live implementation of the injectable ``planner(messages, tools, *, timeout) -> PlannerTurn`` the loop
calls; tests inject a scripted one. Three findings are load-bearing here and each is pinned by a test:

- ``openai/gpt-6-luna`` rejects function tools on chat completions (a 400) unless ``reasoning_effort="none"``
  (``artifacts/agent_tool_probe.json``), so the call ALWAYS goes through ``completion_params(model, budget,
  reasoning_effort="none")`` (a no-op for the models that have no such parameter);
- the request timeout is the only thing that stops a hung model inside the plan's time budget (LangGraph runs synchronously here),
  and ``num_retries=0`` keeps LiteLLM (which maps it onto the provider client's own retry count) from retrying a timed-out call
  past that budget;
- the reply's prose is dropped: only tool calls and token usage are read.

What the model is first shown is the question (the user's own text, marked untrusted in the system prompt) and the allowlisted
summary of the prefetch (:func:`semigraph.agent.sanitize.prefetch_summary`).
"""

import json
from collections.abc import Mapping

import litellm

from ..artifacts import read_prompt
from ..config import get_settings
from ..llm_shape import completion_params, provider_kwargs
from .sanitize import prefetch_summary
from .state import PlannerTurn, ToolCall

# Bumped whenever ``prompts/agent_planner.txt`` changes; surfaced in ``done.agent`` so an eval run says which prompt planned it.
PLANNER_PROMPT_VERSION = "1"
PLANNER_MAX_TOKENS = 400          # a tool call or two: the planner never writes prose


def system_prompt() -> str:
    return read_prompt("agent_planner")


def initial_messages(question: str, r: Mapping) -> list[dict]:
    """The system prompt, then the question and the summary of what the plain retrieval already holds."""
    summary = json.dumps(prefetch_summary(r), separators=(",", ":"))
    user = f"QUESTION (untrusted user text):\n{question}\n\nALREADY RETRIEVED (plain hybrid retrieval; counts and ids only):\n{summary}"
    return [{"role": "system", "content": system_prompt()}, {"role": "user", "content": user}]


def assistant_message(turn: PlannerTurn) -> dict:
    """The planner's turn as an assistant message (tool calls only: its prose is never kept)."""
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                           for c in turn.tool_calls]}


def tool_message(call_id: str, result: Mapping) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)}


class LiteLLMPlanner:
    """Calls ``litellm.completion`` with the tool schemas. Raises whatever the provider raises: the loop turns any planner
    exception into a fallback to the plain retrieval."""

    def __init__(self, model: str, max_tokens: int = PLANNER_MAX_TOKENS):
        self.model, self.max_tokens = model, max_tokens

    def __call__(self, messages: list[dict], tools: list[dict], *, timeout: float) -> PlannerTurn:
        response = litellm.completion(
            model=self.model, messages=messages, tools=tools, tool_choice="auto", timeout=timeout, num_retries=0,
            **completion_params(self.model, self.max_tokens, reasoning_effort="none"),
            **provider_kwargs(self.model, get_settings()))
        choice = response.choices[0]
        calls = tuple(ToolCall(id=call.id or f"call_{i}", name=call.function.name, arguments=call.function.arguments or "{}")
                      for i, call in enumerate(getattr(choice.message, "tool_calls", None) or []))
        usage = getattr(response, "usage", None)
        return PlannerTurn(tool_calls=calls, finish_reason=choice.finish_reason,
                           usage={"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens} if usage else None)
