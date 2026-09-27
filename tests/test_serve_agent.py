"""M3 worker B: the serving seam of the opt-in agent (docs/v2/M3_AGENT_PLAN.md section 6).

The agent stream is a stub: what is under test is the route around it, that is, what a ``step`` event may and may not do, that
the ledger records the terminal event's spend unchanged, that the cache never crosses strategies, and that the per-request
tracer is created, handed to the agent only for ``strategy=agent`` and always closed.

The fixtures come from tests/test_serve_api.py (the app is built without its lifespan)."""

import json
import sys
import types
from types import SimpleNamespace

import pytest
from test_serve_api import CID, Q, Fakes, FakeSettings, client, fake_answer_stream, fakes, parse_sse  # noqa: F401 - fixtures

from semigraph.serve import routes, store

STEP_1 = {"event": "step", "n": 1, "tool": "lookup_company", "args": {"name": "Nvidia"}, "summary": "Nvidia", "ok": True}
STEP_2 = {"event": "step", "n": 2, "tool": "financial_metrics", "args": {"cik": 1045810, "metrics": ["revenue"]},
          "summary": "revenue for FY2024-2026", "ok": True}
AGENT_META = {"tool_calls": [{"tool": "lookup_company", "args": {"name": "Nvidia"}, "ok": True}], "model_calls": 2, "elapsed_s": 4.2,
              "fallback_reason": None, "planner_model": "openai/gpt-6-luna", "planner_usage": {"prompt_tokens": 900, "completion_tokens": 40},
              "planner_cost_usd": 0.0004}
DONE = {"event": "done", "answer": f"Nvidia depends on HBM suppliers [{CID}].", "citations": [CID], "hallucinated": [],
        "finish_reason": "stop", "usage": {"prompt_tokens": 1500, "completion_tokens": 120}, "cost_usd": 0.0123,   # writer + planner, already folded
        "chunk_ids": [CID], "context_chars": 100, "strategy": "agent", "question": Q, "agent": AGENT_META}


def install_agent(monkeypatch, stream):
    """Register a stub ``semigraph.agent.stream`` (langgraph is not needed to test the route)."""
    stub = types.ModuleType("semigraph.agent.stream")
    stub.agent_answer_stream = stream
    monkeypatch.setitem(sys.modules, "semigraph.agent", types.ModuleType("semigraph.agent"))
    monkeypatch.setitem(sys.modules, "semigraph.agent.stream", stub)


def scripted(*events, seen=None):
    def stream(question, driver, embedder, strategy="agent", **kw):
        if seen is not None:
            seen.update(kw)
        yield from events
    return stream


@pytest.fixture
def agent_client(client, monkeypatch):
    client.app.state.settings.agent_enabled = True
    yield client
    client.app.state.settings.agent_enabled = False


def ask(client, question=Q, strategy="agent"):
    return client.post("/api/ask", json={"question": question, "strategy": strategy})


# ---------------------------------------------------------------- step events pass through and never count as an answer

def test_step_events_reach_the_browser_unchanged_and_in_order(agent_client, monkeypatch):
    install_agent(monkeypatch, scripted(STEP_1, STEP_2, {"event": "retrieval", "anchors": {}, "counts": {}},
                                        {"event": "delta", "text": "Nvidia "}, DONE))
    events = parse_sse(ask(agent_client).text)
    assert [e["event"] for e in events] == ["step", "step", "retrieval", "delta", "done"]
    assert events[0] == STEP_1 and events[1] == STEP_2                       # the same JSON that the agent emitted
    assert events[-1]["agent"] == AGENT_META and events[-1]["strategy"] == "agent"


def test_a_step_event_is_never_logged_as_spend_and_never_cached_as_an_answer(agent_client, fakes, monkeypatch):
    """Even a step that (wrongly) carries an answer, a cost or a usage: only the terminal event is money and only ``done`` is an answer."""
    sneaky = {**STEP_1, "answer": "not an answer", "cost_usd": 99.0, "usage": {"prompt_tokens": 10 ** 6}, "finish_reason": "stop",
              "citations": []}
    install_agent(monkeypatch, scripted(sneaky, STEP_2, DONE))
    events = parse_sse(ask(agent_client).text)
    assert events[0] == sneaky                                               # passed through, not interpreted
    assert len(fakes.queries) == 1 and fakes.queries[0]["cost_usd"] == 0.0123
    assert fakes.queries[0]["usage"] == DONE["usage"] and fakes.queries[0]["strategy"] == "agent"
    assert list(fakes.answers.values()) == [{"answer": DONE["answer"], "source": "live", "citations": [CID], "hallucinated": []}]


def test_steps_followed_by_nothing_are_one_ledger_row_without_a_cost_and_no_cache_entry(agent_client, fakes, monkeypatch):
    """The client vanished (or the agent died) after some steps: the query still counts, its cost is unknown, nothing is cached."""
    install_agent(monkeypatch, scripted(STEP_1, STEP_2, DONE))
    gen = routes._paid_stream(agent_client.app.state, Q + " gone", "agent", "iph")
    assert json.loads(next(gen).data)["event"] == "step"
    gen.close()
    assert len(fakes.queries) == 1 and fakes.queries[0].get("cost_usd") is None and fakes.answers == {}


def test_steps_then_an_agent_crash_is_one_error_one_ledger_row_and_no_cache_entry(agent_client, fakes, monkeypatch):
    def crashing(question, driver, embedder, strategy="agent", **kw):
        yield STEP_1
        raise RuntimeError("planner exploded")
    install_agent(monkeypatch, crashing)
    events = parse_sse(ask(agent_client).text)
    assert [e["event"] for e in events] == ["step", "error"] and "RuntimeError" in events[-1]["detail"]
    assert len(fakes.queries) == 1 and fakes.queries[0].get("cost_usd") is None and fakes.answers == {}
    assert agent_client.app.state.answer_slots.acquire(blocking=False)         # the slot was released
    agent_client.app.state.answer_slots.release()


# ---------------------------------------------------------------- requirement 1: the ledger records the terminal event's cost as is

def test_the_ledger_records_the_agents_terminal_cost_unchanged_and_does_not_add_the_planner_cost_again(agent_client, fakes, monkeypatch):
    install_agent(monkeypatch, scripted(STEP_1, DONE))
    list(routes._paid_stream(agent_client.app.state, Q + " ledger", "agent", "iph"))
    (row,) = fakes.queries
    assert row["cost_usd"] == 0.0123 and row["usage"] == {"prompt_tokens": 1500, "completion_tokens": 120}
    assert row["strategy"] == "agent" and row["cached"] is False


def test_the_ledger_records_the_agents_terminal_cost_on_the_error_path_too(agent_client, fakes, monkeypatch):
    failed = {"event": "error", "detail": "ServiceUnavailableError: overloaded", "partial": "", "strategy": "agent",
              "usage": {"prompt_tokens": 900, "completion_tokens": 40}, "cost_usd": 0.0031, "agent": AGENT_META}
    install_agent(monkeypatch, scripted(STEP_1, STEP_2, failed))
    events = parse_sse(ask(agent_client).text)
    assert events[-1]["event"] == "error" and "overloaded" not in events[-1]["detail"]     # the provider text stays server-side
    (row,) = fakes.queries
    assert row["cost_usd"] == 0.0031 and row["usage"] == failed["usage"] and row["strategy"] == "agent"
    assert fakes.answers == {}


# ---------------------------------------------------------------- the cache never crosses strategies

def test_an_answer_cached_under_agent_is_not_served_for_hybrid_and_the_other_way_round(agent_client, fakes, monkeypatch):
    install_agent(monkeypatch, scripted(STEP_1, DONE))
    first = parse_sse(ask(agent_client, strategy="agent").text)
    assert first[-1]["event"] == "done" and not first[-1].get("cached")
    assert set(fakes.answers) == {store.cache_key(Q, "agent")}

    hybrid = parse_sse(ask(agent_client, strategy="hybrid").text)              # same question, other strategy: a paid miss
    assert hybrid[-1]["event"] == "done" and not hybrid[-1].get("cached") and hybrid[-1]["strategy"] == "hybrid"
    assert set(fakes.answers) == {store.cache_key(Q, "agent"), store.cache_key(Q, "hybrid")}

    assert parse_sse(ask(agent_client, strategy="agent").text)[0]["cached"] is True
    assert parse_sse(ask(agent_client, strategy="hybrid").text)[0]["cached"] is True
    assert [q["strategy"] for q in fakes.queries] == ["agent", "hybrid", "agent", "hybrid"]
    assert [q["cached"] for q in fakes.queries] == [False, False, True, True]


# ---------------------------------------------------------------- the tracer: per request, agent only, always closed

class RequestTracer:
    def __init__(self, question, strategy):
        self.question, self.strategy, self.closed = question, strategy, 0

    def close(self):
        self.closed += 1


class TracerFactory:
    """Stands for ``app.state.tracer``: ``for_request`` hands out one tracer per question."""

    def __init__(self):
        self.made: list[RequestTracer] = []

    def for_request(self, question="", *, strategy=""):
        tracer = RequestTracer(question, strategy)
        self.made.append(tracer)
        return tracer


def test_the_agent_gets_a_per_request_tracer_that_is_closed_when_the_answer_ends(agent_client, monkeypatch):
    seen, factory = {}, TracerFactory()
    agent_client.app.state.tracer = factory
    install_agent(monkeypatch, scripted(STEP_1, DONE, seen=seen))
    ask(agent_client)
    (tracer,) = factory.made
    assert seen["tracer"] is tracer and (tracer.question, tracer.strategy) == (Q, "agent") and tracer.closed == 1
    assert seen["settings"] is agent_client.app.state.settings


def test_the_request_tracer_is_closed_on_the_error_and_the_disconnect_paths_too(agent_client, monkeypatch):
    factory = TracerFactory()
    agent_client.app.state.tracer = factory

    def crashing(question, driver, embedder, strategy="agent", **kw):
        yield STEP_1
        raise RuntimeError("boom")
    install_agent(monkeypatch, crashing)
    ask(agent_client, Q + " crash")
    install_agent(monkeypatch, scripted(STEP_1, STEP_2, DONE))
    gen = routes._paid_stream(agent_client.app.state, Q + " gone", "agent", "iph")
    next(gen)
    gen.close()
    assert [t.closed for t in factory.made] == [1, 1]


def test_a_tracer_without_for_request_is_passed_through_as_is_and_left_open(agent_client, monkeypatch):
    seen, shared = {}, SimpleNamespace(closed=0, close=lambda: pytest.fail("a shared tracer must not be closed per request"))
    agent_client.app.state.tracer = shared
    install_agent(monkeypatch, scripted(DONE, seen=seen))
    ask(agent_client)
    assert seen["tracer"] is shared


def test_no_tracer_on_the_app_means_the_agent_is_told_none(agent_client, monkeypatch):
    seen = {}
    install_agent(monkeypatch, scripted(DONE, seen=seen))
    ask(agent_client)
    assert "tracer" in seen and seen["tracer"] is None


def test_the_fixed_path_never_gets_a_tracer_and_never_asks_for_one(agent_client, monkeypatch):
    seen, factory = {}, TracerFactory()
    agent_client.app.state.tracer = factory

    def capture(question, driver, embedder, strategy="hybrid", **kw):
        seen.update(kw)
        yield from fake_answer_stream(question, driver, embedder, strategy)
    monkeypatch.setattr(routes, "answer_stream", capture)
    ask(agent_client, strategy="hybrid")
    assert "tracer" not in seen and "settings" not in seen and factory.made == []


def test_a_tracer_that_raises_never_breaks_the_answer(agent_client, fakes, monkeypatch, caplog):
    class Broken:
        def for_request(self, *a, **k):
            raise RuntimeError("langfuse down")
    agent_client.app.state.tracer = Broken()
    install_agent(monkeypatch, scripted(STEP_1, DONE))
    events = parse_sse(ask(agent_client).text)
    assert events[-1]["event"] == "done" and len(fakes.queries) == 1

    class BrokenClose(TracerFactory):
        def for_request(self, question="", *, strategy=""):
            tracer = super().for_request(question, strategy=strategy)
            tracer.close = lambda: (_ for _ in ()).throw(RuntimeError("close failed"))
            return tracer
    agent_client.app.state.tracer = BrokenClose()
    events = parse_sse(ask(agent_client, Q + " again").text)
    assert events[-1]["event"] == "done"
    assert agent_client.app.state.answer_slots.acquire(blocking=False)
    agent_client.app.state.answer_slots.release()


# ---------------------------------------------------------------- /api/stats says whether the trace sample is running

def test_stats_report_tracing_only_when_the_agent_is_on_and_a_real_tracer_is_running(client):
    assert client.get("/api/stats").json()["tracing"] is False                # no tracer on the app
    client.app.state.tracer = SimpleNamespace(enabled=True)
    assert client.get("/api/stats").json()["tracing"] is False                # the agent is off: nothing is traced
    client.app.state.settings.agent_enabled = True
    try:
        assert client.get("/api/stats").json()["tracing"] is True
        client.app.state.tracer = SimpleNamespace(enabled=False)
        assert client.get("/api/stats").json()["tracing"] is False            # the no-op tracer
    finally:
        client.app.state.settings.agent_enabled = False
