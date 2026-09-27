"""Synthetic agent-run fixtures for the agent-evaluation harness tests (M3-C): items, ``step`` / ``done`` / ``error`` events, rows.

Everything is built from the CONTRACT (docs/v2/M3_AGENT_PLAN.md section 6), never from a live agent, so the harness is gated with no LLM,
no Neo4j and no network. Costs are priced with ``usage_cost`` on models listed in ``KNOWN_PRICES_PER_MTOK`` only (so the price never
falls through to LiteLLM's network price map or the settings).
"""

from semigraph.eval import agent_eval as ae
from semigraph.retrieval.answerer import usage_cost

LUNA = "openai/gpt-6-luna"
SONNET = "anthropic/claude-sonnet-5"
LIMITS = ae.AgentLimits(max_tool_calls=4, max_model_calls=3, time_budget_s=25.0)
XBRL = "xbrl:1045810:revenue:2026-01-25"
GOOD_ANSWER = f"Nvidia's total revenue was $215.9 billion for the fiscal year ended January 25, 2026 [{XBRL}]."
CLEAN_CHECKS = {"citations_retrieved": True, "numbers_grounded": True, "numbers_checked": 1, "unmatched_numbers": [],
                "echoed_numbers": [], "pseudo_citations": [], "has_citation": True, "is_refusal": False,
                "unsupported_removal_claim": False, "unsupported_removal_sentences": []}


def item(id="A01", **over) -> dict:
    """An agent-benchmark item that a clean run of ``events()`` satisfies."""
    base = {"id": id, "type": "numeric", "category": "named_years", "split": "agent",
            "q": "What was Nvidia's total revenue for the fiscal year ended January 25, 2026?",
            "expect": {"value": 215938000000}, "expected_tools": ["financial_metrics"], "forbidden_tools": ["risk_changes"],
            "max_steps": 3, "source": "test fixture"}
    return {**base, **over}


def step_events(tools, ok=True) -> list[dict]:
    flags = ok if isinstance(ok, (list, tuple)) else [ok] * len(tools)
    return [{"event": "step", "n": n, "tool": tool, "args": {"ticker": "NVDA"}, "summary": f"{tool}: 3 rows", "ok": flag}
            for n, (tool, flag) in enumerate(zip(tools, flags, strict=True), 1)]


def events(tools=("financial_metrics",), *, ok=True, model_calls=2, elapsed_s=4.0, fallback_reason=None, answer=GOOD_ANSWER,
           cited=(XBRL,), hallucinated=(), checks=None, escalated=False, answered_by=LUNA, planner_model=LUNA,
           planner_usage=None, writer_usage=None, cost_delta=0.0, planner_cost_delta=0.0, with_agent=True, step_tools=None) -> list[dict]:
    """A whole run: ``retrieval``, the ``step`` events, ``delta``, and a ``done`` whose spend is consistent unless a delta is given."""
    planner_usage = planner_usage or {"prompt_tokens": 2500, "completion_tokens": 150}
    writer_usage = writer_usage or {"prompt_tokens": 12000, "completion_tokens": 300}
    planner_cost = usage_cost(planner_usage, planner_model)
    writer_cost = usage_cost(writer_usage, answered_by)
    agent = {"tool_calls": [{"tool": t, "args": {"ticker": "NVDA"}, "ok": f}
                            for t, f in zip(tools, ok if isinstance(ok, (list, tuple)) else [ok] * len(tools), strict=True)],
             "model_calls": model_calls, "elapsed_s": elapsed_s, "fallback_reason": fallback_reason,
             "planner_model": planner_model, "planner_usage": planner_usage,
             "planner_cost_usd": round(planner_cost + planner_cost_delta, 6)}
    done = {"event": "done", "question": "q", "strategy": "agent", "answer": answer, "citations": sorted(cited),
            "hallucinated": sorted(hallucinated), "checks": CLEAN_CHECKS if checks is None else checks,
            "finish_reason": "stop", "usage": writer_usage, "cost_usd": round(planner_cost + writer_cost + cost_delta, 6),
            "chunk_ids": [], "context_chars": 40000, "escalated": escalated, "answered_by": answered_by, "routed": "cheap"}
    if with_agent:
        done["agent"] = agent
    shown = list(tools) if step_tools is None else list(step_tools)
    return [{"event": "retrieval", "anchors": {}, "counts": {}, "anchor_defaulted": False},
            *step_events(shown, ok if step_tools is None else True), {"event": "delta", "text": answer}, done]


def error_events(tools=(), cost_usd=0.0021) -> list[dict]:
    return [*step_events(tools), {"event": "error", "detail": "RuntimeError: boom", "partial": "", "usage": None,
                                  "cost_usd": cost_usd, "strategy": "agent"}]


def row(it=None, evs=None, latency_s=5.0) -> dict:
    return ae.agent_row(it or item(), evs if evs is not None else events(), latency_s)
