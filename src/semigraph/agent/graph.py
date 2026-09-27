"""The LangGraph loop of the agent: ``prefetch -> plan -> tools -> plan ... -> finalize`` (docs/v2/M3_AGENT_PLAN.md section 1).

This is the ONLY module that imports langgraph (``semigraph.agent.stream`` imports this one), so a deployment with the flag off never
needs it. The graph plans the RETRIEVAL only; the answer is streamed afterwards by ``stream.py`` through the one shared
``stream_answer_for_context`` (a sync generator that yields SSE deltas out of a node would buy nothing and cost custom stream writers).

- ``prefetch``: the plain ``hybrid_retrieve`` (the agent can never be worse than the fixed path), plus the planner's first messages.
  A failing prefetch is the fixed path's own failure and propagates.
- ``plan``: one planner call, only while the limits (4 tool calls, 3 model calls, the time budget) allow. Reaching the tool or model
  limit ends planning and the answer uses what was gathered. A planner exception or a budget expiry sets ``fallback_reason`` and ends
  planning, and the answer then uses the PLAIN PREFETCH (see below).
- ``tools``: runs the calls the planner asked for, in order. Calls past the tool limit are neither run nor recorded; refused calls
  (an invented tool, invalid arguments) count as calls; every call gets a ``role: tool`` reply.
- ``finalize``: stamps the planning time. The retrieval dict it leaves is the input of the writer.

State updates are new values, never mutations; the graph runs with ``stream_mode="values"`` so the last consistent state survives a
node bug or the recursion limit. WHENEVER ``fallback_reason`` is set (a planner error, the time budget, a bug, the recursion limit)
the retrieval dict the writer answers from is the untouched PLAIN prefetch (``prefetch_r``): a fallback answers exactly as the fixed
path would, so "fallback_reason is set" and "this is not an agent answer" are the same fact (the eval scores it so). The tool calls
that did run stay in ``tool_calls`` and the step events. The planner's spend lives in a run-local
:class:`~semigraph.agent.state.Ledger`, so a bug cannot lose a paid call.
"""

import json
import logging
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from ..retrieval.answerer import usage_cost
from ..retrieval.retriever import hybrid_retrieve
from . import planner as P
from .sanitize import clip_name
from .state import MIN_CALL_BUDGET_S, AgentState, Ledger, Limits, PlannerTurn, initial_state, recursion_limit_for
from .tools import Toolbox, tool_specs
from .trace import Tracer, as_safe

logger = logging.getLogger("semigraph.agent")


def _now() -> float:
    """The clock the time budget is measured on (a module attribute so the tests can drive a virtual one)."""
    return time.monotonic()


@dataclass(frozen=True)
class AgentResult:
    """What one planning run leaves: the retrieval dict the writer answers from and the facts ``done.agent`` reports."""

    r: dict                         # what the writer answers from: the plain prefetch when ``fallback_reason`` is set, else the merged dict
    tool_calls: list[dict]          # one {"tool", "args", "ok"} per step event, in order; the prefetch is not one
    model_calls: int
    elapsed_s: float                # the planning phase only (prefetch to finalize), never the writer
    fallback_reason: str | None
    stop_reason: str
    planner_usage: dict


@dataclass(frozen=True)
class _Deps:
    question: str
    driver: Any
    embedder: Any
    toolbox: Toolbox
    planner: Callable[..., PlannerTurn]
    planner_model: str
    limits: Limits
    tracer: Tracer
    ledger: Ledger
    specs: list[dict]
    k_chunks: int
    hops: int


def _cost(usage: dict | None, model: str) -> float | None:
    try:
        return usage_cost(usage, model)
    except Exception:  # noqa: BLE001 - tracing detail only; the real accounting is done once, in stream.py
        return None


def _chars(value: Any) -> int:
    return len(json.dumps(value, default=str))


# --- nodes --------------------------------------------------------------------------------------------------------------------

def _prefetch_node(d: _Deps):
    def prefetch(state: AgentState) -> dict:
        with d.tracer.span("prefetch") as span:
            r = hybrid_retrieve(d.question, d.driver, d.embedder, k_chunks=d.k_chunks, hops=d.hops)
            span.set(chunks=len(r["chunks"]), metric_rows=len(r["metrics"]), edges=len(r["edges"]))
        try:
            messages = P.initial_messages(d.question, r)
        except Exception as e:  # noqa: BLE001 - past the prefetch the agent degrades, it never errors
            logger.exception("the planner's first messages could not be built")
            return {"r": r, "prefetch_r": r, "messages": [], "fallback_reason": f"agent_error:{type(e).__name__}",
                    "stop_reason": "fallback"}
        return {"r": r, "prefetch_r": r, "messages": messages}

    return prefetch


def _plan_node(d: _Deps):
    def plan(state: AgentState) -> dict:
        ledger, limits = d.ledger, d.limits
        if len(state["steps"]) >= limits.max_tool_calls:
            return {"pending": [], "stop_reason": "tool_limit"}
        if ledger.model_calls >= limits.max_model_calls:
            return {"pending": [], "stop_reason": "model_limit"}
        with d.tracer.span("plan", n=ledger.model_calls + 1) as span:
            remaining = limits.time_budget_s - (_now() - state["t0"])
            if remaining < MIN_CALL_BUDGET_S:
                d.tracer.event("fallback", reason="time_budget")
                return {"pending": [], "fallback_reason": "time_budget", "stop_reason": "time_budget"}
            timeout = min(remaining, limits.planner_call_cap_s)
            span.set(timeout_s=round(timeout, 3))
            ledger.model_calls += 1                      # counted before the call: a call that fails or times out is still a call
            try:
                turn = d.planner(state["messages"], d.specs, timeout=timeout)
                if not isinstance(turn, PlannerTurn):
                    raise TypeError("the planner did not return a PlannerTurn")
            except Exception as e:  # noqa: BLE001 - any planner failure (a 400 on tool calling, a timeout) degrades to the plain retrieval
                reason = f"planner_error:{type(e).__name__}"
                logger.warning("the planner call failed (%s): answering from the plain retrieval", reason)
                span.set(error=type(e).__name__)
                d.tracer.event("fallback", reason=reason)
                return {"pending": [], "fallback_reason": reason, "stop_reason": "fallback"}
            ledger.record(turn.usage)
            d.tracer.generation(name="planner", model=d.planner_model, usage=turn.usage, cost_usd=_cost(turn.usage, d.planner_model),
                                input_chars=_chars(state["messages"]), output_chars=_chars([c.name for c in turn.tool_calls]))
            if not turn.tool_calls:
                return {"pending": [], "stop_reason": "planner_done"}
            return {"messages": [*state["messages"], P.assistant_message(turn)],
                    "pending": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in turn.tool_calls]}

    return plan


def _tools_node(d: _Deps):
    def tools(state: AgentState) -> dict:
        r, steps, messages = state["r"], list(state["steps"]), list(state["messages"])
        expired = False
        for call in state["pending"]:
            if len(steps) >= d.limits.max_tool_calls:
                messages.append(P.tool_message(call["id"], {"error": "the tool call limit is reached"}))
                continue
            if expired or _now() - state["t0"] >= d.limits.time_budget_s:
                expired = True
                messages.append(P.tool_message(call["id"], {"error": "the time budget is used up"}))
                continue
            with d.tracer.span("tool", tool=clip_name(call["name"])) as span:
                outcome = d.toolbox.execute(call["name"], call["arguments"], r)
                span.set(ok=outcome.ok)
            r = outcome.r
            steps.append({"n": len(steps) + 1, "tool": outcome.tool, "args": outcome.args, "summary": outcome.summary, "ok": outcome.ok})
            messages.append(P.tool_message(call["id"], outcome.result))
        update: dict = {"r": r, "steps": steps, "messages": messages, "pending": []}
        if expired:
            d.tracer.event("fallback", reason="time_budget")
            return {**update, "fallback_reason": "time_budget", "stop_reason": "time_budget"}
        if len(steps) >= d.limits.max_tool_calls:
            return {**update, "stop_reason": "tool_limit"}
        if d.ledger.model_calls >= d.limits.max_model_calls:
            return {**update, "stop_reason": "model_limit"}
        return update

    return tools


def _finalize(state: AgentState) -> dict:
    return {"elapsed_s": round(_now() - state["t0"], 3), "stop_reason": state.get("stop_reason") or "planner_done"}


def _after_prefetch(state: AgentState) -> str:
    return "finalize" if state.get("stop_reason") else "plan"


def _after_plan(state: AgentState) -> str:
    return "tools" if state.get("pending") else "finalize"


def _after_tools(state: AgentState) -> str:
    return "finalize" if state.get("stop_reason") or state.get("fallback_reason") else "plan"


def _build_graph(d: _Deps):
    graph = StateGraph(AgentState)
    graph.add_node("prefetch", _prefetch_node(d))
    graph.add_node("plan", _plan_node(d))
    graph.add_node("tools", _tools_node(d))
    graph.add_node("finalize", _finalize)
    graph.add_edge(START, "prefetch")
    graph.add_conditional_edges("prefetch", _after_prefetch, ["plan", "finalize"])
    graph.add_conditional_edges("plan", _after_plan, ["tools", "finalize"])
    graph.add_conditional_edges("tools", _after_tools, ["plan", "finalize"])
    graph.add_edge("finalize", END)
    return graph.compile()


# --- the runner ---------------------------------------------------------------------------------------------------------------

def _result(state: dict, ledger: Ledger) -> AgentResult:
    """The outcome of a run. A fallback answers from the untouched prefetch (``prefetch_r``); the tool calls that did run are
    still reported."""
    fallback = state.get("fallback_reason")
    steps = state.get("steps", [])
    return AgentResult(r=state["prefetch_r"] if fallback else state["r"],
                       tool_calls=[{"tool": s["tool"], "args": s["args"], "ok": s["ok"]} for s in steps],
                       model_calls=ledger.model_calls, elapsed_s=state.get("elapsed_s", round(_now() - state["t0"], 3)),
                       fallback_reason=fallback, stop_reason=state.get("stop_reason") or "planner_done",
                       planner_usage=dict(ledger.usage))


def run_agent(question: str, driver, embedder, *, planner: Callable[..., PlannerTurn], planner_model: str, limits: Limits,
              tracer: Tracer | None = None, k_chunks: int = 8, hops: int = 2,
              recursion_limit: int | None = None) -> Generator[dict, None, AgentResult]:
    """Run the planning graph; yield one ``step`` event per tool call as its turn completes and RETURN the :class:`AgentResult`.

    Raises only when the prefetch itself fails. Any later failure (a planner error, the time budget, a bug in a node, the recursion
    limit) ends planning with a ``fallback_reason`` and the plain prefetch as the result's ``r``. ``recursion_limit`` defaults to
    :func:`~semigraph.agent.state.recursion_limit_for` (12 at the default limits)."""
    tracer = as_safe(tracer)
    ledger = Ledger()
    deps = _Deps(question, driver, embedder, Toolbox(driver, embedder, question), planner, planner_model, limits, tracer, ledger,
                 tool_specs(), k_chunks, hops)
    config = {"recursion_limit": recursion_limit or recursion_limit_for(limits)}
    state: dict = dict(initial_state(question, _now()))
    emitted = 0
    try:
        for state in _build_graph(deps).stream(state, config, stream_mode="values"):
            for step in state.get("steps", [])[emitted:]:
                yield {"event": "step", **step}
                emitted += 1
    except Exception as e:  # noqa: BLE001 - see the docstring: only a missing prefetch is allowed to raise
        if "r" not in state:
            raise
        reason = "recursion_limit" if isinstance(e, GraphRecursionError) else f"agent_error:{type(e).__name__}"
        logger.warning("the agent loop failed (%s): answering from the plain retrieval", reason, exc_info=reason != "recursion_limit")
        tracer.event("fallback", reason=reason)
        state = {**state, "fallback_reason": state.get("fallback_reason") or reason, "stop_reason": "fallback",
                 "elapsed_s": round(_now() - state["t0"], 3)}
    return _result(state, ledger)
