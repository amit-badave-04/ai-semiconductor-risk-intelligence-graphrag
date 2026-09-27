"""The entry points of the agent: ``agent_answer_stream`` (imported lazily by ``serve/routes.py``) and ``agent_answer``.

``agent_answer_stream`` has the signature and the event grammar of ``retrieval.answerer.answer_stream``, plus ``step`` events (one per
tool call, BEFORE the ``retrieval`` event) and an ``agent`` object on ``done``. The planning runs first (:mod:`semigraph.agent.graph`);
the answer is then written by the ONE shared ``stream_answer_for_context``, so the verifier, the router, the escalation and the
``checks`` are the same implementation for both paths, and an agent that adds nothing hands the writer the byte-identical prompt.

Spend (the ledger and the daily ceiling read ``usage`` and ``cost_usd`` of the terminal event): the planner's cost is folded into
BOTH ``done`` and ``error``, as DOLLARS priced at the planner's own rates and ADDED to the writer's dollars (which the writer path
already prices per model); tokens are never summed across models and priced once. ``usage`` stays the WRITER's usage; the planner's
tokens are in ``agent.planner_usage``. A writer cost that is unknown stays unknown unless the planner cost something.

Tracing (:mod:`semigraph.agent.trace`): the tracer is wrapped so it can never raise, and it is given lengths, counts and ids, never
the question, the answer or a hash of either (an unsalted hash of a question is linkable; the tracer of ``serve/tracing.py`` salts its own).
"""

import logging
from collections.abc import Generator, Iterator
from typing import Any

from ..config import get_settings
from ..retrieval.answerer import build_blocks, stream_answer_for_context, usage_cost
from . import planner as P
from .graph import AgentResult, run_agent
from .planner import LiteLLMPlanner
from .state import Limits
from .trace import as_safe

logger = logging.getLogger("semigraph.agent")

_PREFETCH_ARGS = ("k_chunks", "hops")      # answer_stream's retrieval arguments: the agent's prefetch takes them, the writer must not
_ROUTING_KEYS = ("finish_reason", "escalated", "answered_by", "routed", "escalation_reasons")


class AgentAnswerError(RuntimeError):
    """``agent_answer`` ended in an ``error`` event (the streaming path reports it as an event; ``answer()`` raises)."""

    def __init__(self, event: dict):
        super().__init__(event.get("detail") or "the answer failed")
        self.event = event


def _agent_info(plan: AgentResult, planner_model: str, planner_cost: float) -> dict:
    """The ``agent`` object of ``done`` (plan section 6, plus the prompt version and why planning stopped)."""
    return {"tool_calls": plan.tool_calls, "model_calls": plan.model_calls, "elapsed_s": plan.elapsed_s,
            "fallback_reason": plan.fallback_reason, "planner_model": planner_model, "planner_usage": plan.planner_usage,
            "planner_cost_usd": planner_cost, "planner_prompt_version": P.PLANNER_PROMPT_VERSION, "stop_reason": plan.stop_reason}


def _total_cost(writer_cost: float | None, planner_cost: float) -> float | None:
    if writer_cost is None and not planner_cost:
        return None
    return round((writer_cost or 0.0) + planner_cost, 6)


def _fold_spend(event: dict, planner_cost: float, agent: dict) -> dict:
    """``event`` with the planner's dollars added to ``cost_usd`` when it is terminal (and, on ``done``, the ``agent`` object)."""
    if event["event"] == "done":
        return {**event, "cost_usd": _total_cost(event.get("cost_usd"), planner_cost), "agent": agent}
    if event["event"] == "error":
        return {**event, "cost_usd": _total_cost(event.get("cost_usd"), planner_cost)}
    return event


def _planner_cost(plan: AgentResult, planner_model: str) -> float:
    return round(usage_cost(plan.planner_usage, planner_model) or 0.0, 6)


def _run(question: str, driver, embedder, strategy: str, *, timeout, max_tokens, escalation_model, settings, planner, tracer,
         llm_stream, escalation_stream, stream_kwargs: dict) -> Generator[dict, None, tuple[dict, dict]]:
    """Every event of one run; RETURNS ``(the final retrieval dict, the agent object)``."""
    settings = settings if settings is not None else get_settings()
    tracer = as_safe(tracer)
    planner_model = settings.agent_planner_model
    prefetch = {k: stream_kwargs[k] for k in _PREFETCH_ARGS if k in stream_kwargs}
    writer_kwargs = {k: v for k, v in stream_kwargs.items() if k not in _PREFETCH_ARGS}
    if timeout is not None:
        writer_kwargs["timeout"] = timeout
    try:
        with tracer.span("agent", strategy=strategy, planner_model=planner_model, question_chars=len(question)) as span:
            plan = yield from run_agent(question, driver, embedder, planner=planner or LiteLLMPlanner(planner_model),
                                        planner_model=planner_model, limits=Limits.from_settings(settings), tracer=tracer, **prefetch)
            planner_cost = _planner_cost(plan, planner_model)
            agent = _agent_info(plan, planner_model, planner_cost)
            span.set(tool_calls=len(plan.tool_calls), model_calls=plan.model_calls, fallback_reason=plan.fallback_reason,
                     stop_reason=plan.stop_reason, planner_cost_usd=planner_cost)
            try:
                for event in stream_answer_for_context(question, plan.r, strategy, llm_stream=llm_stream,
                                                       escalation_model=escalation_model, escalation_stream=escalation_stream,
                                                       max_tokens=max_tokens, **writer_kwargs):
                    event = _fold_spend(event, planner_cost, agent)
                    if event["event"] in ("done", "error"):
                        span.set(outcome=event["event"], cost_usd=event.get("cost_usd"))
                    yield event
            except Exception as e:  # noqa: BLE001 - see below: the planner's spend must not vanish with an unexpected failure
                if not planner_cost:
                    raise                                   # nothing was spent by the agent: exactly the fixed path's behaviour
                logger.exception("the answer phase failed after the planner had cost $%.6f", planner_cost)
                span.set(outcome="error", cost_usd=planner_cost)
                yield {"event": "error", "detail": f"{type(e).__name__}: {e}", "partial": "", "usage": None, "cost_usd": planner_cost,
                       "strategy": strategy}
            return plan.r, agent
    finally:
        tracer.flush()


def agent_answer_stream(question: str, driver, embedder, strategy: str = "agent", *, timeout=None, max_tokens: int = 1200,
                        escalation_model: str | None = None, settings=None, planner=None, tracer=None, llm_stream=None,
                        escalation_stream=None, **stream_kwargs) -> Iterator[dict]:
    """Streaming answer with a retrieval-planning agent in front of the shared writer: a generator of event dicts.

    Events: zero or more ``{"event": "step", "n", "tool", "args", "summary", "ok"}`` (one per planner tool call; the summary is
    counts / ids / fiscal years only), then exactly the ``answer_stream`` grammar (``retrieval``, ``delta``*, ``done`` | ``error``).
    ``done`` additionally carries ``agent``: ``tool_calls`` (one ``{"tool", "args", "ok"}`` per step event, the prefetch excluded),
    ``model_calls``, ``elapsed_s`` (the planning phase), ``fallback_reason`` (None unless the agent degraded: the answer is then the plain retrieval),
    ``planner_model``, ``planner_usage``, ``planner_cost_usd``, ``planner_prompt_version`` and ``stop_reason``. ``cost_usd`` of ``done``
    and of ``error`` includes the planner; ``usage`` is the writer's.

    ``planner`` is an injectable ``callable(messages, tools, *, timeout) -> PlannerTurn`` (default: :class:`LiteLLMPlanner` on
    ``settings.agent_planner_model``); ``settings`` supplies the limits; ``tracer`` follows :class:`semigraph.agent.trace.Tracer`.
    ``k_chunks`` / ``hops`` of ``stream_kwargs`` configure the prefetch; every other keyword (``model``, ...) goes to the writer."""
    yield from _run(question, driver, embedder, strategy, timeout=timeout, max_tokens=max_tokens, escalation_model=escalation_model,
                    settings=settings, planner=planner, tracer=tracer, llm_stream=llm_stream, escalation_stream=escalation_stream,
                    stream_kwargs=stream_kwargs)


def agent_answer(question: str, driver, embedder, strategy: str = "agent", **kwargs: Any) -> dict:
    """The non-streaming helper: drain :func:`agent_answer_stream` into the dict ``retrieval.answerer.answer`` returns (``answer``,
    ``citations``, ``cited``, ``valid_ids``, ``hallucinated``, ``context``, ``checks``, ``chunk_ids``, ``retrieval``), plus the
    ``agent`` object, ``usage`` / ``cost_usd`` (planner included), the ``steps`` and the routing keys of ``done``. ``valid_ids`` and
    ``context`` are rebuilt from the final retrieval dict, so they are exactly what the writer saw. Raises
    :class:`AgentAnswerError` on an ``error`` event, as ``answer()`` raises on a failed model call."""
    gen = _run(question, driver, embedder, strategy, **_defaults(kwargs))
    events: list[dict] = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            final_r, agent = stop.value
            break
    terminal = events[-1]
    if terminal["event"] != "done":
        raise AgentAnswerError(terminal)
    _, context, valid_ids = build_blocks(final_r)
    cited = set(terminal["citations"])
    return {"question": question, "strategy": strategy, "answer": terminal["answer"], "citations": terminal["citations"], "cited": cited,
            "valid_ids": valid_ids, "hallucinated": cited - valid_ids, "context": context, "checks": terminal["checks"],
            "chunk_ids": terminal["chunk_ids"], "retrieval": final_r, "agent": agent, "usage": terminal["usage"],
            "cost_usd": terminal["cost_usd"], "steps": [e for e in events if e["event"] == "step"],
            **{k: terminal[k] for k in _ROUTING_KEYS if k in terminal}}


def _defaults(kwargs: dict) -> dict:
    """``agent_answer_stream``'s keyword arguments with their defaults, in the shape :func:`_run` takes."""
    named = {"timeout": None, "max_tokens": 1200, "escalation_model": None, "settings": None, "planner": None, "tracer": None,
             "llm_stream": None, "escalation_stream": None}
    given = {k: kwargs[k] for k in named if k in kwargs}
    return {**named, **given, "stream_kwargs": {k: v for k, v in kwargs.items() if k not in named}}
