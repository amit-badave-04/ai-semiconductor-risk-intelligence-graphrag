"""The LangGraph loop of the agent: ``prefetch -> plan -> tools -> plan ... -> finalize`` (docs/v2/M3_AGENT_PLAN.md section 1).

This is the ONLY module that imports langgraph (``semigraph.agent.stream`` imports this one), so a deployment with the flag off never
needs it. The graph plans the RETRIEVAL only; the answer is streamed afterwards by ``stream.py`` through the one shared
``stream_answer_for_context`` (a sync generator that yields SSE deltas out of a node would buy nothing and cost custom stream writers).

- ``prefetch``: the plain ``hybrid_retrieve`` (the agent can never be worse than the fixed path), plus the planner's first messages.
  A failing prefetch is the fixed path's own failure and propagates.
- ``plan``: one planner call, only while the limits (4 tool calls, 3 model calls, the time budget) allow. Reaching the tool or model
  limit ends planning and the answer uses what was gathered. A planner exception or a budget expiry sets ``fallback_reason`` and ends
  planning (see below for what the answer uses).
- ``tools``: runs the calls the planner asked for, in order. Calls past the tool limit are neither run nor recorded; refused calls
  (an invented tool, invalid arguments) count as calls; every call gets a ``role: tool`` reply.
- ``finalize``: stamps the planning time. The retrieval dict it leaves is the input of the writer.

State updates are new values, never mutations; the graph runs with ``stream_mode="values"`` so the last consistent state survives a
node bug or the recursion limit. ``fallback_reason`` (a planner error, the time budget, a bug, the recursion limit) is always
REPORTED for the eval and the audit trail, but it is NOT always a full discard (M3 finding #10, resolving the plan/config
inconsistency the review found: plan section 1 already said exceeding a LIMIT goes to finalize with what was gathered; the code
used to treat a time-budget expiry as a full discard regardless): the retrieval dict the writer answers from is the untouched
PLAIN prefetch (``prefetch_r``) only when NO tool call has succeeded yet (a planner exception or timeout before any call, an
unsupported-tool-calling 400, a time-budget expiry before any call succeeded); once at least one tool call has already
succeeded, a later fallback (of any reason) still finalizes with what was gathered (the merged retrieval dict is always a
superset of the pairs and notices the prefetch itself would have shown -- see :func:`~semigraph.agent.merge.merge_temporal`),
``fallback_reason`` staying set so the eval and the audit trail still know planning did not finish cleanly. The tool calls that
did run stay in ``tool_calls`` and the step events either way. The planner's spend lives in a run-local
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
from . import merge as M
from . import planner as P
from .state import MIN_CALL_BUDGET_S, AgentState, Ledger, Limits, PlannerTurn, initial_state, recursion_limit_for
from .tools import TOOL_NAMES, Toolbox, tool_specs
from .trace import Tracer, as_safe

logger = logging.getLogger("semigraph.agent")


def _now() -> float:
    """The clock the time budget is measured on (a module attribute so the tests can drive a virtual one)."""
    return time.monotonic()


@dataclass(frozen=True)
class AgentResult:
    """What one planning run leaves: the retrieval dict the writer answers from and the facts ``done.agent`` reports."""

    r: dict                         # what the writer answers from: the plain prefetch ONLY when fallback_reason is set AND no
                                     # tool call has succeeded yet; the merged dict otherwise (M3 finding #10)
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
        edges_added = state.get("edges_added", 0)
        expired = False
        for call in state["pending"]:
            if len(steps) >= d.limits.max_tool_calls:
                messages.append(P.tool_message(call["id"], {"error": "the tool call limit is reached"}))
                continue
            remaining = d.limits.time_budget_s - (_now() - state["t0"])
            if expired or remaining <= 0:
                expired = True
                messages.append(P.tool_message(call["id"], {"error": "the time budget is used up"}))
                continue
            name = call["name"]
            # The tracer span carries the tool NAME only after it is known to be one of the declared tools (a planner
            # (or a hijacked one) can send anything): recording the raw, possibly question-steered name as a span
            # attribute before the toolbox validates it would put unvalidated planner text in the trace (R2 review).
            span_tool = name if isinstance(name, str) and name in TOOL_NAMES else "invalid"
            timeout = min(remaining, d.limits.tool_call_cap_s)
            edges_budget = max(0, M.MAX_EDGES_ADDED - edges_added)
            with d.tracer.span("tool", tool=span_tool) as span:
                outcome = d.toolbox.execute(name, call["arguments"], r, timeout=timeout, edges_budget=edges_budget)
                span.set(ok=outcome.ok)
            edges_added += max(0, len(outcome.r.get("edges", [])) - len(r.get("edges", [])))
            r = outcome.r
            steps.append({"n": len(steps) + 1, "tool": outcome.tool, "args": outcome.args, "summary": outcome.summary, "ok": outcome.ok})
            messages.append(P.tool_message(call["id"], outcome.result))
        update: dict = {"r": r, "steps": steps, "messages": messages, "pending": [], "edges_added": edges_added}
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
    """The outcome of a run (M3 finding #10). A fallback with ZERO successful tool calls answers from the untouched
    prefetch (``prefetch_r``): nothing was gathered worth keeping. A fallback with AT LEAST ONE successful tool call
    finalizes with what was gathered (the merged ``r``) instead -- ``fallback_reason`` is still reported, but the merged
    context (a superset of the prefetch's own pairs/notices) is what the writer answers from. The tool calls that did run
    are reported either way."""
    fallback = state.get("fallback_reason")
    steps = state.get("steps", [])
    any_tool_succeeded = any(s.get("ok") for s in steps)
    use_prefetch = bool(fallback) and not any_tool_succeeded
    return AgentResult(r=state["prefetch_r"] if use_prefetch else state["r"],
                       tool_calls=[{"tool": s["tool"], "args": s["args"], "ok": s["ok"]} for s in steps],
                       model_calls=ledger.model_calls, elapsed_s=state.get("elapsed_s", round(_now() - state["t0"], 3)),
                       fallback_reason=fallback, stop_reason=state.get("stop_reason") or "planner_done",
                       planner_usage=dict(ledger.usage))


def run_agent(question: str, driver, embedder, *, planner: Callable[..., PlannerTurn], planner_model: str, limits: Limits,
              tracer: Tracer | None = None, k_chunks: int = 8, hops: int = 2,
              recursion_limit: int | None = None, ledger: Ledger | None = None) -> Generator[dict, None, AgentResult]:
    """Run the planning graph; yield one ``step`` event per tool call as its turn completes and RETURN the :class:`AgentResult`.

    Raises only when the prefetch itself fails. Any later failure (a planner error, the time budget, a bug in a node, the recursion
    limit) ends planning with a ``fallback_reason``; the result's ``r`` is the plain prefetch only when no tool call had already
    succeeded, else the merged dict gathered so far (M3 finding #10, see :func:`_result`). ``recursion_limit`` defaults to
    :func:`~semigraph.agent.state.recursion_limit_for` (12 at the default limits). ``ledger`` is normally created here, but
    a caller (``stream.py``, M3 finding #6) may pass its OWN so it can still read the planner's accrued spend if the
    generator is abandoned (``GeneratorExit``) before this function ever returns -- a local ``Ledger`` would be lost with
    the abandoned generator frame."""
    tracer = as_safe(tracer)
    ledger = ledger if ledger is not None else Ledger()
    deps = _Deps(question, driver, embedder, Toolbox(driver, embedder, question, clock=_now), planner, planner_model, limits, tracer, ledger,
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
