"""M5a I2: the async twin of the agent answer stream (``semigraph.agent.stream_async``, M5A_BUILD_PLAN.md section 4).

Parity is proved against the recording of the SYNC code (``tests/data/agent_events_pre_m5.json``, rebuilt from each
scenario's ``inputs`` by ``tests/data/record_events_pre_m5.py``): every scenario's ``events`` and ``final`` must come
out EQUAL, in the same key order, and so must the ``observed`` side channels (prompt hashes, planner message hashes,
WARNING logs and the tracer's call kinds). The disconnect scenarios close the async generator after
``close_after_events`` events; the abandonment is reported by a WARNING whose dollars the recorder PARSES from the
text, so a reworded message fails here.

The rest pins what only the async design can get wrong: the planning runs on ONE worker thread under ``limiters.db``
and never on the loop; a consumer that goes away (close, anyio scope, native ``task.cancel()``) stops further paid
planner calls, joins the thread and still reports the planner's dollars (also one that goes away while the prefetch
is still running: the planner is guarded, not only the thread's loop); nothing blocks the loop; the module never
reaches uploaded documents.

No network, no Neo4j, no model. Every async test runs under ``anyio.fail_after`` and a watchdog, so a handshake
deadlock fails instead of hanging CI. Plain pytest: ``asyncio.run`` inside ordinary test functions.
"""

import ast
import asyncio
import faulthandler
import gc
import hashlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import threading
import time
import types
from contextlib import aclosing, contextmanager
from pathlib import Path
from warnings import catch_warnings, simplefilter

import anyio
import pytest
import test_answerer_async as writer_tests
from agent_fakes import LUNA, FakeDriver, FakeEmbedder, ScriptedPlanner, make_settings, turn

from semigraph.agent import stream as sync_stream
from semigraph.agent import stream_async
from semigraph.agent.planner import PLANNER_MAX_TOKENS
from semigraph.agent.stream_async import aagent_answer_stream
from semigraph.retrieval import answerer_async
from semigraph.retrieval.answerer import usage_cost
from semigraph.serve.limiters import Limiters, LoopLagMonitor
from semigraph.serve.meter import PaidMeter

DATA = Path(__file__).parent / "data"
SRC = Path(__file__).resolve().parent.parent / "src"
RECORDER_PATH = DATA / "record_events_pre_m5.py"
TERMINAL = {"done", "error"}


def _load_recorder():
    spec = importlib.util.spec_from_file_location("record_events_pre_m5_for_the_async_twin", RECORDER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recorder = _load_recorder()
COMMITTED = json.loads(recorder.AGENT_FILE.read_text(encoding="utf-8"))
SCENARIOS = {s["name"]: s for s in COMMITTED["scenarios"]}
NAMES = list(SCENARIOS)
FM_2026, FM_2023, COMPUTE = recorder.FM_2026, recorder.FM_2023, recorder.COMPUTE_23_24
ONE_TURN_USD = usage_cost({"prompt_tokens": 500, "completion_tokens": 40}, LUNA)      # what turn(...) costs by default
LONG_PLAN = {**recorder.AGENT_SETTINGS, "agent_max_tool_calls": 6, "agent_max_model_calls": 6}
WORKER_NAME = "AnyIO worker thread"


# --- harness ---------------------------------------------------------------------------------------------------

def run(main, *, timeout: float = 20):
    """``asyncio.run(main())`` under a deadline; the faulthandler watchdog exits the process if even that cannot fire
    (a shielded wait that never ends is exactly the failure this file exists to catch)."""
    async def guarded():
        with anyio.fail_after(timeout):
            return await main()

    faulthandler.dump_traceback_later(timeout + 30, exit=True)
    try:
        return asyncio.run(guarded())
    finally:
        faulthandler.cancel_dump_traceback_later()


def make_limiters(db: int = 8) -> Limiters:
    """Must be called inside the running loop (a limiter belongs to the loop that created it)."""
    return Limiters(embed=anyio.CapacityLimiter(1), db=anyio.CapacityLimiter(db), health=anyio.CapacityLimiter(1),
                    state=anyio.CapacityLimiter(4))


class AsyncStream:
    """The async twin of ``agent_fakes.FakeStream``: the parts one at a time, then ``boom``. ``closed`` is set when
    the async generator behind ``async for`` is finalised (the real stream's provider connection is released at the
    same point); ``stall`` makes it hang after the first part, like a model that went quiet."""

    def __init__(self, stream, *, stall: bool = False):
        self.parts, self.boom, self.stall = list(stream.parts), stream.boom, stall
        self.model, self.usage, self.finish_reason = stream.model, stream.usage, stream.finish_reason
        self.closed = False

    async def __aiter__(self):
        try:
            for index, part in enumerate(self.parts):
                await anyio.sleep(0)
                yield part
                if self.stall and index == 0:
                    await anyio.sleep(60)
            await anyio.sleep(0)
            if self.boom:
                raise self.boom
        finally:
            self.closed = True


def asyncified(factory, streams: list | None = None, *, stall: bool = False):
    """A sync ``callable(prompt) -> FakeStream`` as ``callable(prompt) -> AsyncStream`` (each stream made is appended
    to ``streams``)."""
    def make(prompt):
        stream = AsyncStream(factory(prompt), stall=stall)
        if streams is not None:
            streams.append(stream)
        return stream
    return make


def writer_kwargs(inputs: dict, prompts: list[str]) -> dict:
    """``llm_stream`` and the escalation arguments of a scenario, with async streams."""
    kwargs = recorder._escalation_kwargs(inputs, prompts)
    if "escalation_stream" in kwargs:
        kwargs["escalation_stream"] = asyncified(kwargs["escalation_stream"])
    return {"llm_stream": asyncified(recorder._writer_factory(inputs["writer"], prompts)), **kwargs}


def open_stream(limiters, planner, *, tracer=None, settings=None, spec=None, prompts=None, stall=False,
                streams=None, embedder=None, **kw):
    """One ``aagent_answer_stream`` over the recorder's question, ``FakeDriver.world()`` and a scripted writer."""
    spec = spec or recorder.writer(recorder.GOOD_PARTS, usage=(12000, 300))
    factory = recorder._writer_factory(spec, prompts if prompts is not None else [])
    kw.setdefault("llm_stream", asyncified(factory, streams, stall=stall))
    return aagent_answer_stream(recorder.QUESTION, FakeDriver.world(), embedder or FakeEmbedder(), limiters=limiters,
                                planner=planner, settings=settings or make_settings(**recorder.AGENT_SETTINGS),
                                tracer=tracer, **kw)


async def collect(agen) -> list[dict]:
    async with aclosing(agen):
        return [event async for event in agen]


async def pull(agen) -> dict:
    return await anext(agen)


async def wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the condition never became true"
        await anyio.sleep(0.005)


def wait_for_threads_to_end(before: set, *, timeout: float = 5.0) -> None:
    """Every thread that appeared since ``before`` has ended (anyio's pool stops its workers when the loop's main task
    is done: a planning thread parked on a handshake that never came would NOT end, and this fails)."""
    deadline = time.monotonic() + timeout
    while True:
        extra = set(threading.enumerate()) - before
        if not extra:
            return
        assert time.monotonic() < deadline, f"threads still alive: {sorted(t.name for t in extra)}"
        time.sleep(0.02)


class ProbePlanner(ScriptedPlanner):
    """A scripted planner that notes the thread it is called on and the borrowed ``limiters.db`` tokens; can be slow."""

    def __init__(self, *turns, delay: float = 0.0, **kw):
        super().__init__(*turns, **kw)
        self.delay, self.idents, self.borrowed, self.limiters = delay, [], [], None

    def __call__(self, messages, tools, *, timeout):
        self.idents.append(threading.get_ident())
        if self.limiters is not None:
            self.borrowed.append(anyio.from_thread.run_sync(lambda: self.limiters.db.borrowed_tokens))
        if self.delay:
            time.sleep(self.delay)
        return super().__call__(messages, tools, timeout=timeout)


class RunAgentSpy:
    """Wraps ``run_agent``: the thread of every resumption, the planner it was handed, and a flag set when the
    generator is closed (or ended)."""

    def __init__(self, monkeypatch):
        self.resumed, self.planners, self.finished = [], [], threading.Event()
        real = stream_async.run_agent
        spy = self

        def wrapped(*args, **kwargs):
            spy.planners.append(kwargs["planner"])
            inner = real(*args, **kwargs)
            try:
                while True:
                    spy.resumed.append(threading.get_ident())
                    try:
                        event = next(inner)
                    except StopIteration as end:
                        return end.value
                    yield event
            finally:
                inner.close()
                spy.finished.set()

        monkeypatch.setattr(stream_async, "run_agent", wrapped)


class LedgerSpy:
    """Keeps every ``Ledger`` the stream makes, so a test can read what the planner had cost at a given moment."""

    def __init__(self, monkeypatch):
        self.made = []
        spy, real = self, stream_async.Ledger

        class Spied(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                spy.made.append(self)

        monkeypatch.setattr(stream_async, "Ledger", Spied)


class PlanningSpy:
    """Keeps every ``_Planning`` the stream makes, so a test can see when its stop flag is set (the consumer left)."""

    def __init__(self, monkeypatch):
        self.made = []
        spy, real = self, stream_async._Planning

        class Spied(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                spy.made.append(self)

        monkeypatch.setattr(stream_async, "_Planning", Spied)

    def stopped(self) -> bool:
        return bool(self.made) and self.made[0]._stop.is_set()


class GatedEmbedder(FakeEmbedder):
    """An embedder that waits inside ``encode_query`` until ``gate`` is set, which pins the prefetch of ``run_agent``
    inside the planning thread (a wait for an embed slot would pin it the same way). The wait times out, so a bug
    fails instead of hanging."""

    def __init__(self):
        super().__init__()
        self.entered, self.gate = threading.Event(), threading.Event()

    def encode_query(self, text):
        self.entered.set()
        assert self.gate.wait(timeout=10), "the gate was never opened"
        return super().encode_query(text)


@contextmanager
def unclosed_resources():
    """The messages of the ResourceWarnings raised while the block runs and what it dropped is collected (anyio reports
    an unclosed stream when it is garbage collected). Read the yielded list AFTER the block."""
    found = []
    with catch_warnings(record=True) as caught:
        simplefilter("always")
        yield found
        gc.collect()
    found.extend(str(w.message) for w in caught if issubclass(w.category, ResourceWarning))


class Halt(BaseException):
    """A failure that is not an ``Exception`` (a custom one: KeyboardInterrupt and SystemExit would leave the loop)."""


def abandoned(warnings: list[str]) -> list[str]:
    return [w for w in warnings if "abandoned" in w]


def abandoned_line(spend: float) -> str:
    return ("semigraph.agent: the agent stream was abandoned before a terminal event; "
            f"the planner had already cost ${spend:.6f}")


# --- 1. the replay of the recorded sync behaviour -----------------------------------------------------------------

_REPLAYS: dict[str, dict] = {}


def replay(name: str) -> dict:
    """The recorder's own ``run_agent_scenario`` with the async twin in place of the sync stream (cached)."""
    if name in _REPLAYS:
        return _REPLAYS[name]
    inputs, prompts = SCENARIOS[name]["inputs"], []
    planner, tracer = recorder._planner(inputs["planner"]), recorder._CallLog()
    close_after = inputs.get("close_after_events")

    async def main():
        agen = aagent_answer_stream(inputs["question"], FakeDriver.world(), FakeEmbedder(), inputs["strategy"],
                                    limiters=make_limiters(), planner=planner,
                                    settings=make_settings(**inputs["settings"]), tracer=tracer,
                                    **writer_kwargs(inputs, prompts))
        if close_after is None:
            return await collect(agen)
        events = [await anext(agen) for _ in range(close_after)]
        await agen.aclose()                          # the client went away
        return events

    with recorder.captured_warnings() as warnings:
        events = run(main)
    hashes = [hashlib.sha256(json.dumps(c["messages"], sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
              for c in planner.calls]
    _REPLAYS[name] = recorder._plain({
        "events": [recorder.normalise_event(e) for e in events],
        "final": recorder._final(events, closed_early=close_after is not None, agent=True, warnings=warnings),
        "observed": {"prompt_hashes": prompts, "planner_message_hashes": hashes, "warnings": warnings,
                     "tracer_calls": tracer.calls}})
    return _REPLAYS[name]


def test_the_recording_has_the_seven_scenarios_this_file_replays():
    assert len(NAMES) == 7 and {"agent_disconnect_mid_plan", "agent_disconnect_mid_answer"} <= set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_the_async_twin_replays_every_recorded_event_and_total(name):
    recorded, got = SCENARIOS[name], replay(name)
    assert [e["event"] for e in got["events"]] == [e["event"] for e in recorded["events"]]
    for index, (actual, want) in enumerate(zip(got["events"], recorded["events"], strict=True)):
        assert actual == want, f"event {index} ({want['event']}) differs from the recording"
    assert json.dumps(got["events"]) == json.dumps(recorded["events"])    # dict == ignores key order; a client does not
    assert got["final"] == recorded["final"]


@pytest.mark.parametrize("name", NAMES)
def test_the_async_twin_also_reproduces_the_side_channels(name):
    """Prompt hashes (the writer saw the same prompt), planner message hashes (the planner saw the same
    conversation), the WARNING lines (the abandonment wording included) and the tracer's call kinds (one 'agent'
    span, then the node spans, then a flush)."""
    assert replay(name)["observed"] == SCENARIOS[name]["observed"]


@pytest.mark.parametrize("name,turns", [("agent_disconnect_mid_plan", 1), ("agent_disconnect_mid_answer", 2)])
def test_a_closed_stream_reports_only_the_planners_dollars_in_the_recorded_words(name, turns):
    spend = usage_cost({"prompt_tokens": 500 * turns, "completion_tokens": 40 * turns}, LUNA)
    got = replay(name)
    assert abandoned(got["observed"]["warnings"]) == [abandoned_line(spend)]
    assert got["final"]["abandoned"] is True and got["final"]["abandoned_spend_usd"] == pytest.approx(spend)
    assert got["final"]["usage"] is None and got["final"]["cost_usd"] is None
    assert got["observed"]["tracer_calls"][-1] == ["flush", None]


def test_the_signature_is_the_sync_one_plus_a_required_limiters_keyword_an_optional_meter_and_no_workspace():
    sync, twin = inspect.signature(sync_stream.agent_answer_stream), inspect.signature(aagent_answer_stream)
    assert list(twin.parameters)[:4] == ["question", "driver", "embedder", "strategy"]
    assert twin.parameters["strategy"].default == "agent"
    keyword_only = {n: p.default for n, p in twin.parameters.items() if p.kind is inspect.Parameter.KEYWORD_ONLY}
    assert keyword_only.pop("limiters") is inspect.Parameter.empty
    assert keyword_only.pop("meter") is None                 # the paid-call meter: off unless the route passes one
    assert keyword_only == {n: p.default for n, p in sync.parameters.items()
                            if p.kind is inspect.Parameter.KEYWORD_ONLY}
    assert not [n for n in twin.parameters if "workspace" in n or "upload" in n or "document" in n]
    assert inspect.isasyncgenfunction(aagent_answer_stream)


def test_limiters_is_required():
    async def main():
        with pytest.raises(TypeError):
            aagent_answer_stream(recorder.QUESTION, FakeDriver.world(), FakeEmbedder())
    run(main)


def test_the_writer_gets_the_timeout_and_the_other_keywords_the_sync_stream_gives_it(monkeypatch):
    """An injected ``llm_stream`` never sees ``timeout``, so the writer's own call is spied on: it must receive
    ``timeout`` (only when one was given), ``max_tokens`` and no prefetch argument, exactly as in the sync stream."""
    async_calls, sync_calls = [], []
    real_async, real_sync = stream_async.astream_answer_for_context, sync_stream.stream_answer_for_context

    def spy_async(*args, **kwargs):
        async_calls.append(kwargs)
        return real_async(*args, **kwargs)

    def spy_sync(*args, **kwargs):
        sync_calls.append(kwargs)
        return real_sync(*args, **kwargs)

    monkeypatch.setattr(stream_async, "astream_answer_for_context", spy_async)
    monkeypatch.setattr(sync_stream, "stream_answer_for_context", spy_sync)
    sync_writer = recorder._writer_factory(recorder.writer(recorder.GOOD_PARTS), [])
    for given in ({"timeout": 7.5, "k_chunks": 3, "hops": 1}, {}):
        run(lambda: collect(open_stream(make_limiters(), ProbePlanner(turn()), **given)))
        list(sync_stream.agent_answer_stream(
            recorder.QUESTION, FakeDriver.world(), FakeEmbedder(), planner=ScriptedPlanner(turn()),
            settings=make_settings(**recorder.AGENT_SETTINGS), llm_stream=sync_writer, **given))
    with_timeout, without = async_calls
    assert with_timeout["timeout"] == 7.5 and with_timeout["max_tokens"] == 1200
    assert "timeout" not in without
    assert not {"k_chunks", "hops"} & (set(with_timeout) | set(without))          # those go to the prefetch only
    assert [set(call) - {"limiters", "meter"} for call in async_calls] == [set(call) for call in sync_calls]
    assert all(call["meter"] is None for call in async_calls)           # nobody is metering these asks


# --- 2. the planning thread --------------------------------------------------------------------------------

def test_the_planner_is_only_ever_called_from_one_worker_thread_never_the_loop_thread(monkeypatch):
    spy, planner, loop_thread = RunAgentSpy(monkeypatch), ProbePlanner(turn(FM_2026), turn(FM_2023), turn()), []

    async def main():
        loop_thread.append(threading.get_ident())
        return await collect(open_stream(make_limiters(), planner))

    events = run(main)
    assert events[-1]["event"] == "done" and len(planner.idents) == 3
    assert len(set(planner.idents)) == 1 and set(planner.idents) == set(spy.resumed)
    assert loop_thread[0] not in planner.idents


def test_limiters_db_is_borrowed_for_the_planning_phase_and_given_back():
    planner, seen = ProbePlanner(turn(FM_2026), turn()), {}

    async def main():
        limiters = planner.limiters = make_limiters()
        events = await collect(open_stream(limiters, planner))
        seen.update(db=limiters.db.borrowed_tokens, embed=limiters.embed.borrowed_tokens)
        return events

    assert run(main)[-1]["event"] == "done"
    assert planner.borrowed and all(count >= 1 for count in planner.borrowed)       # during planning
    assert seen == {"db": 0, "embed": 0}                                              # after: nothing is held


def test_planning_advances_only_while_the_consumer_asks_for_events():
    """The thread is parked after handing over an event, exactly as the sync generator is suspended at its
    ``yield``: this is what makes 'close after event k' mean 'no planner call k+1', whatever the timing."""
    planner = ProbePlanner(turn(FM_2026), turn(FM_2023), turn(), repeat=True)

    async def main():
        agen = open_stream(make_limiters(), planner, settings=make_settings(**LONG_PLAN))
        first = await anext(agen)
        await anyio.sleep(0.15)                                   # the consumer is busy with the first event
        held = len(planner.calls)
        second = await anext(agen)
        await agen.aclose()
        return first, second, held, len(planner.calls)

    first, second, held, after_second = run(main)
    assert (first["event"], second["event"]) == ("step", "step") and held == 1 and after_second == 2


def test_a_stream_that_is_never_iterated_starts_no_planning():
    planner = ProbePlanner(turn(FM_2026), turn())

    async def main():
        agen = open_stream(make_limiters(), planner)
        await anyio.sleep(0.05)
        await agen.aclose()

    run(main)
    assert planner.calls == []


PLANNER_PARAMETERS = [("messages", inspect.Parameter.POSITIONAL_OR_KEYWORD),
                      ("tools", inspect.Parameter.POSITIONAL_OR_KEYWORD), ("timeout", inspect.Parameter.KEYWORD_ONLY)]


def test_the_planner_guard_forwards_the_call_unchanged_until_the_stop_flag_is_set():
    planner, stop = ScriptedPlanner(turn(FM_2026)), threading.Event()
    reply, messages, tools = planner.turns[0], [{"role": "user", "content": "q"}], [{"type": "function"}]
    guarded = stream_async._unless_stopped(planner, stop)
    assert [(n, p.kind) for n, p in inspect.signature(guarded).parameters.items()] == PLANNER_PARAMETERS
    assert guarded(messages, tools, timeout=1.5) is reply
    assert planner.calls == [{"messages": messages, "tools": tools, "timeout": 1.5}]
    stop.set()
    with pytest.raises(stream_async._PlanningStopped):
        guarded(messages, tools, timeout=1.5)
    assert len(planner.calls) == 1                              # the refused call never reached the planner
    # Not an Exception: run_agent turns a planner's Exception into a fallback and a WARNING, and this is neither.
    assert not issubclass(stream_async._PlanningStopped, Exception)


def test_run_agent_is_handed_the_injected_planner_behind_a_guard_with_the_planner_signature(monkeypatch):
    spy, planner = RunAgentSpy(monkeypatch), ProbePlanner(turn())
    assert run(lambda: collect(open_stream(make_limiters(), planner)))[-1]["event"] == "done"
    (handed,) = spy.planners
    assert handed is not planner and len(planner.calls) == 1 and planner.calls[0]["timeout"] > 0
    assert [(n, p.kind) for n, p in inspect.signature(handed).parameters.items()] == PLANNER_PARAMETERS


def test_the_default_planner_is_called_through_the_guard_with_the_model_of_the_settings(monkeypatch):
    live, made = ProbePlanner(turn(FM_2026), turn()), []

    def default_planner(model):
        made.append(model)
        return live

    monkeypatch.setattr(stream_async, "LiteLLMPlanner", default_planner)     # the real one would call the network
    spy = RunAgentSpy(monkeypatch)
    assert run(lambda: collect(open_stream(make_limiters(), None)))[-1]["event"] == "done"
    assert made == [LUNA] and len(live.calls) == 2 and spy.planners[0] is not live


# --- 3. disconnect: the stop flag --------------------------------------------------------------------------------

def test_closing_after_the_first_step_starts_no_further_planner_call_and_ends_the_thread(monkeypatch):
    spy = RunAgentSpy(monkeypatch)
    planner = ProbePlanner(turn(FM_2026), turn(FM_2023), turn(COMPUTE), turn(), repeat=True)
    before = set(threading.enumerate())
    seen = {}

    async def main():
        agen = open_stream(make_limiters(), planner, settings=make_settings(**LONG_PLAN))
        seen["first"] = await anext(agen)
        await agen.aclose()
        seen["calls_at_close"] = len(planner.calls)
        await anyio.sleep(0.2)                                   # a call that was going to start would have by now
        seen["calls_later"] = len(planner.calls)

    with recorder.captured_warnings() as warnings:
        run(main)
    assert seen["first"]["event"] == "step" and seen["calls_at_close"] == seen["calls_later"] == 1
    assert spy.finished.is_set() and len(spy.resumed) == 1                      # run_agent was closed, never resumed
    assert abandoned(warnings) == [abandoned_line(ONE_TURN_USD)]                # once, with the accrued dollars
    wait_for_threads_to_end(before)


def test_a_planner_call_in_flight_at_a_disconnect_finishes_its_one_call_and_starts_no_other():
    """The documented limit: an in-flight call cannot be interrupted, so ONE call's cost can still accrue; it is in
    the ledger and in the warning, and nothing after it runs."""
    planner = ProbePlanner(turn(FM_2026), turn(FM_2023), turn(), delay=0.3, repeat=True)
    before, seen = set(threading.enumerate()), {}

    async def main():
        agen = open_stream(make_limiters(), planner, settings=make_settings(**LONG_PLAN))
        task = asyncio.create_task(pull(agen))         # planning starts and blocks inside the first planner call
        await wait_until(lambda: len(planner.idents) == 1)
        task.cancel()                                              # the client went away while it is in flight
        with pytest.raises(asyncio.CancelledError):
            await task
        seen["calls"] = len(planner.idents)

    with recorder.captured_warnings() as warnings:
        run(main)
    assert seen["calls"] == 1 and len(planner.calls) == 1
    assert abandoned(warnings) == [abandoned_line(ONE_TURN_USD)]
    wait_for_threads_to_end(before)


# --- 4. exceptions --------------------------------------------------------------------------------

def test_a_planner_that_raises_gives_the_recorded_fallback_reason():
    done = replay("agent_planner_error_fallback")["events"][-1]
    assert done["agent"]["fallback_reason"] == "planner_error:RuntimeError" and done["agent"]["planner_cost_usd"] == 0.0
    assert [w for w in replay("agent_planner_error_fallback")["observed"]["warnings"] if "planner call failed" in w]


class Down(FakeDriver):
    def answer(self, query, params, *, timeout=None):
        raise ConnectionError("neo4j unreachable")


def test_a_failing_prefetch_raises_out_of_the_stream_with_its_own_type_and_no_warning():
    async def main():
        agen = aagent_answer_stream(recorder.QUESTION, Down(), FakeEmbedder(), limiters=make_limiters(),
                                    planner=ScriptedPlanner(turn()), settings=make_settings(**recorder.AGENT_SETTINGS))
        with pytest.raises(ConnectionError, match="neo4j unreachable"):
            await collect(agen)

    with recorder.captured_warnings() as warnings:
        run(main)
    assert abandoned(warnings) == []


def test_an_exception_inside_run_agent_surfaces_after_the_events_that_preceded_it(monkeypatch):
    step = {"event": "step", "n": 1, "tool": "lookup_company", "args": {}, "summary": "x", "ok": True}

    def exploding(*args, **kwargs):
        yield step
        yield {**step, "n": 2}
        raise ValueError("planning exploded")

    monkeypatch.setattr(stream_async, "run_agent", exploding)
    got, tracer = [], recorder._CallLog()

    async def main():
        agen = open_stream(make_limiters(), ScriptedPlanner(turn()), tracer=tracer)
        with pytest.raises(ValueError, match="planning exploded"):
            async for event in agen:
                got.append(event)

    run(main)
    assert [e["n"] for e in got] == [1, 2]                                 # no event was lost to the failure
    assert tracer.calls[-1] == ["flush", None]


def test_a_failing_answer_phase_with_planner_spend_is_an_error_event_with_the_planners_dollars(monkeypatch):
    async def boom(*args, **kwargs):
        raise ValueError("a bug in the answer phase")
        yield

    monkeypatch.setattr(stream_async, "astream_answer_for_context", boom)
    events = run(lambda: collect(open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn()))))
    assert [e["event"] for e in events] == ["step", "error"]
    error = events[-1]
    assert error == {"event": "error", "detail": "ValueError: a bug in the answer phase", "partial": "", "usage": None,
                     "cost_usd": pytest.approx(2 * ONE_TURN_USD), "strategy": "agent"}
    assert list(error) == ["event", "detail", "partial", "usage", "cost_usd", "strategy"]


def test_the_sync_stream_makes_the_same_error_event_for_the_same_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("a bug in the answer phase")
        yield

    monkeypatch.setattr(sync_stream, "stream_answer_for_context", boom)
    sync = list(sync_stream.agent_answer_stream(
        recorder.QUESTION, FakeDriver.world(), FakeEmbedder(), planner=ScriptedPlanner(turn(FM_2026), turn()),
        settings=make_settings(**recorder.AGENT_SETTINGS)))

    async def aboom(*args, **kwargs):
        raise ValueError("a bug in the answer phase")
        yield

    monkeypatch.setattr(stream_async, "astream_answer_for_context", aboom)
    twin = run(lambda: collect(open_stream(make_limiters(), ScriptedPlanner(turn(FM_2026), turn()))))
    assert json.dumps(twin) == json.dumps(sync)


def test_a_base_exception_of_the_planning_thread_is_raised_as_it_is_like_the_sync_stream():
    """Not an ``Exception``, so it is not a planner error that falls back; the sync stream raises it as it is, and so
    must the twin (it used to come out as ``RuntimeError('the planning run has not finished')``)."""
    tracer = recorder._CallLog()

    def planner():
        return ScriptedPlanner(Halt("the planner was halted"))

    with pytest.raises(Halt, match="the planner was halted"):
        list(sync_stream.agent_answer_stream(recorder.QUESTION, FakeDriver.world(), FakeEmbedder(), planner=planner(),
                                             settings=make_settings(**recorder.AGENT_SETTINGS)))

    async def main():
        agen = open_stream(make_limiters(), planner(), tracer=tracer)
        with pytest.raises(Halt, match="the planner was halted"):
            await collect(agen)

    with recorder.captured_warnings() as warnings:
        run(main)
    assert abandoned(warnings) == [] and tracer.calls[-1] == ["flush", None]


def test_a_failure_while_closing_the_planning_run_is_logged_and_does_not_mask_the_disconnect(monkeypatch):
    """A client that goes away is being handled; a cleanup that fails on top of that is a WARNING of its own and
    nothing else: ``aclose`` of the stream still completes."""
    step = {"event": "step", "n": 1, "tool": "lookup_company", "args": {}, "summary": "x", "ok": True}

    def failing_to_close(*args, **kwargs):
        try:
            yield step
            yield {**step, "n": 2}
        finally:
            raise ValueError("the cleanup failed")

    monkeypatch.setattr(stream_async, "run_agent", failing_to_close)

    async def main():
        agen = open_stream(make_limiters(), ScriptedPlanner(turn()))
        assert (await anext(agen))["n"] == 1
        await agen.aclose()                                   # does not raise the ValueError

    with recorder.captured_warnings() as warnings:
        run(main)
    assert warnings == ["semigraph.agent: closing the planning run failed: ValueError: the cleanup failed"]


def _warnings_of_a_failing_close(monkeypatch, error: Exception) -> list[str]:
    """The WARNINGs logged when a client leaves after the first step and the closing planning run raises ``error``."""
    step = {"event": "step", "n": 1, "tool": "lookup_company", "args": {}, "summary": "x", "ok": True}

    def failing_to_close(*args, **kwargs):
        try:
            yield step
            yield {**step, "n": 2}
        finally:
            raise error

    monkeypatch.setattr(stream_async, "run_agent", failing_to_close)

    async def main():
        agen = open_stream(make_limiters(), ScriptedPlanner(turn()))
        assert (await anext(agen))["n"] == 1
        await agen.aclose()

    with recorder.captured_warnings() as warnings:
        run(main)
    return warnings


def test_the_log_line_of_a_failed_close_of_the_planning_run_redacts_secret_shaped_text(monkeypatch):
    """The error of the closing run is provider- or tool-made text: it goes through the serve-side redaction before
    it is logged, whatever it quotes, and the exception class stays so the line is still useful."""
    canary = "sk-live-abcdef1234567890"    # a fake, deliberately secret-shaped canary, not a real key - gitleaks:allow
    warnings = _warnings_of_a_failing_close(monkeypatch, ValueError(f"the provider rejected the call, key {canary}"))
    assert warnings == ["semigraph.agent: closing the planning run failed: "
                        "ValueError: the provider rejected the call, key ***"]


def test_the_log_line_of_a_failed_close_of_the_planning_run_is_bounded_in_length(monkeypatch):
    """An exception message is unbounded; the log line keeps a bounded part of it (the redaction truncates)."""
    warnings = _warnings_of_a_failing_close(monkeypatch, ValueError("x" * 50_000))
    assert len(warnings) == 1 and len(warnings[0]) < 500


def test_a_failing_answer_phase_without_planner_spend_is_re_raised(monkeypatch):
    async def boom(*args, **kwargs):
        raise ValueError("a bug in the answer phase")
        yield

    monkeypatch.setattr(stream_async, "astream_answer_for_context", boom)

    async def main():
        with pytest.raises(ValueError, match="a bug in the answer phase"):
            await collect(open_stream(make_limiters(), ScriptedPlanner(RuntimeError("no planner spend"))))

    run(main)


# --- 5. cancellation --------------------------------------------------------------------------------

def test_an_anyio_scope_cancelled_during_planning_stops_the_thread_and_reports_the_spend(monkeypatch):
    spy = RunAgentSpy(monkeypatch)
    planner = ProbePlanner(turn(FM_2026), turn(FM_2023), turn(), delay=0.3, repeat=True)
    prompts, events, before, seen = [], [], set(threading.enumerate()), {}

    async def main():
        agen = open_stream(make_limiters(), planner, settings=make_settings(**LONG_PLAN), prompts=prompts)
        with anyio.move_on_after(0.1) as scope:
            async for event in agen:
                events.append(event)
        seen["cancelled"] = scope.cancelled_caught

    with recorder.captured_warnings() as warnings:
        run(main)
    assert seen["cancelled"] and events == [] and prompts == []             # no event, and the writer never started
    assert len(planner.calls) == 1 and spy.finished.is_set()
    assert abandoned(warnings) == [abandoned_line(ONE_TURN_USD)]
    wait_for_threads_to_end(before)


def test_a_native_task_cancel_during_planning_propagates_and_joins_the_thread(monkeypatch):
    spy = RunAgentSpy(monkeypatch)
    planner = ProbePlanner(turn(FM_2026), turn(), delay=0.2)

    async def main():
        events = []

        async def consume():
            async for event in open_stream(make_limiters(), planner):
                events.append(event)

        task = asyncio.create_task(consume())
        await wait_until(lambda: len(planner.idents) == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return events

    with recorder.captured_warnings() as warnings:
        assert run(main) == []
    assert spy.finished.is_set() and len(planner.calls) == 1
    assert abandoned(warnings) == [abandoned_line(ONE_TURN_USD)]


def test_a_native_cancel_during_the_join_of_a_closing_stream_is_not_swallowed(monkeypatch):
    """The wait is looped so that a cancellation cannot cut the join short, and that must not turn into swallowing it:
    here only a ``GeneratorExit`` is in flight (the client closed the stream), so the remembered cancellation is the
    one thing that tells the task it was cancelled. It is raised once, after the thread has finished closing the run."""
    closed, seen = threading.Event(), {}
    step = {"event": "step", "n": 1, "tool": "lookup_company", "args": {}, "summary": "x", "ok": True}

    def slow_to_close(*args, **kwargs):
        try:
            yield step
            yield {**step, "n": 2}
        finally:
            time.sleep(0.6)                                    # the thread is busy closing the run
            closed.set()

    monkeypatch.setattr(stream_async, "run_agent", slow_to_close)

    async def main():
        agen = open_stream(make_limiters(), ScriptedPlanner(turn()))
        await anext(agen)
        closing = asyncio.create_task(agen.aclose())
        await anyio.sleep(0.1)                                 # the join is waiting for the thread
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        seen["closed"] = closed.is_set()

    run(main)
    assert seen == {"closed": True}                            # propagated, and only after the thread was done


@pytest.mark.parametrize("how", ["anyio_scope", "native_cancel"])
def test_a_cancel_during_the_writer_closes_the_upstream_and_invents_no_terminal_event(how, monkeypatch):
    spy = RunAgentSpy(monkeypatch)
    planner, streams, events, before = ProbePlanner(turn(FM_2026), turn()), [], [], set(threading.enumerate())

    async def consume(agen):
        async for event in agen:
            events.append(event)

    async def main():
        agen = open_stream(make_limiters(), planner, stall=True, streams=streams)
        if how == "anyio_scope":
            with anyio.move_on_after(0.4) as scope:
                await consume(agen)
            assert scope.cancelled_caught
            return
        task = asyncio.create_task(consume(agen))
        await wait_until(lambda: [e["event"] for e in events][-1:] == ["delta"])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with recorder.captured_warnings() as warnings:
        run(main)
    kinds = [e["event"] for e in events]
    assert kinds == ["step", "retrieval", "delta"] and not TERMINAL & set(kinds)
    assert len(streams) == 1 and streams[0].closed                             # the writer's upstream was released
    assert spy.finished.is_set() and len(planner.calls) == 2
    assert abandoned(warnings) == [abandoned_line(2 * ONE_TURN_USD)]
    wait_for_threads_to_end(before)


def test_a_consumer_that_closes_during_the_writer_closes_the_upstream_too():
    streams, planner = [], ProbePlanner(turn(FM_2026), turn())

    async def main():
        agen = open_stream(make_limiters(), planner, stall=True, streams=streams)
        for _ in range(3):
            await anext(agen)
        await agen.aclose()

    with recorder.captured_warnings() as warnings:
        run(main)
    assert streams[0].closed and abandoned(warnings) == [abandoned_line(2 * ONE_TURN_USD)]


def test_a_stream_that_reached_its_terminal_event_is_not_reported_as_abandoned():
    async def main():
        agen = open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn()))
        got = [await anext(agen) for _ in range(5)]         # step, retrieval, delta, delta, done
        await agen.aclose()
        return got

    with recorder.captured_warnings() as warnings:
        got = run(main)
    assert got[-1]["event"] == "done" and abandoned(warnings) == []


def test_a_second_native_cancel_while_joining_the_planning_thread_does_not_abandon_it(monkeypatch):
    """A shield holds against an anyio scope but not against a native ``task.cancel()``: a second one, landing while
    the join waits for a planner call in flight, used to end the wait. The stream was then left with the call still
    running: no call recorded, no abandonment WARNING, and the thread went on writing spans after the flush. Everything
    below is read at the moment the cancelled task is gone, not later."""
    spy, ledgers, tracer = RunAgentSpy(monkeypatch), LedgerSpy(monkeypatch), ThreadTracer()
    planner = ProbePlanner(turn(FM_2026), turn(), delay=0.5)
    before, seen = set(threading.enumerate()), {}

    async def main():
        async def consume():
            async for _ in open_stream(make_limiters(), planner, tracer=tracer):
                pass

        task = asyncio.create_task(consume())
        await wait_until(lambda: len(planner.idents) == 1)         # the first planner call is in flight
        task.cancel()                                              # the client went away
        await anyio.sleep(0.05)
        task.cancel()                                              # and the task is cancelled AGAIN, mid-join
        with pytest.raises(asyncio.CancelledError):
            await task
        seen.update(finished=spy.finished.is_set(), calls=len(planner.calls), cancelled=task.cancelled(),
                    usage=dict(ledgers.made[0].usage), log=list(tracer.log))

    with recorder.captured_warnings() as warnings:
        run(main)
    assert seen["cancelled"] and seen["finished"] and seen["calls"] == 1     # the call ended before the stream was left
    assert seen["usage"] == {"prompt_tokens": 500, "completion_tokens": 40}   # the ledger was final
    assert abandoned(warnings) == [abandoned_line(ONE_TURN_USD)]
    assert seen["log"][-1][:2] == ("flush", None)                             # the flush came after the last span
    wait_for_threads_to_end(before)
    assert tracer.log == seen["log"]                                          # and nothing was written after it


def test_a_cancel_absorbed_by_the_shielded_join_does_not_start_the_writer(monkeypatch):
    """The disconnect lands while the join of the planning thread is shielded, so the shield absorbs it and the join
    returns normally: no exception is in flight. Nothing between the join and the writer is a checkpoint, so without
    one the writer's model call would be asked for a client that has gone."""
    prompts, streams, events, seen, holder = [], [], [], {}, {}
    real = stream_async._Planning.aclose

    async def aclose_with_the_disconnect_landing_in_it(self):
        holder["scope"].cancel()
        await real(self)

    monkeypatch.setattr(stream_async._Planning, "aclose", aclose_with_the_disconnect_landing_in_it)

    async def main():
        agen = open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn()), prompts=prompts, streams=streams)
        with anyio.CancelScope() as scope:
            holder["scope"] = scope
            async for event in agen:
                events.append(event)
        seen["cancelled"] = scope.cancelled_caught

    with recorder.captured_warnings() as warnings:
        run(main)
    assert prompts == [] and streams == []                       # the writer's model call was never asked for
    assert seen["cancelled"] and [e["event"] for e in events] == ["step"]
    assert abandoned(warnings) == [abandoned_line(2 * ONE_TURN_USD)]


def test_a_scope_cancelled_as_planning_ends_does_not_start_the_writer(monkeypatch):
    """The same, with the disconnect landing on the loop AFTER the join (here in ``_agent_info``, which runs between
    the join and the writer without a checkpoint): the checkpoint must sit immediately before the writer."""
    prompts, streams, events, seen, holder = [], [], [], {}, {}
    real = stream_async._agent_info

    def agent_info_after_which_the_client_is_gone(*args, **kwargs):
        holder["scope"].cancel()
        return real(*args, **kwargs)

    monkeypatch.setattr(stream_async, "_agent_info", agent_info_after_which_the_client_is_gone)

    async def main():
        agen = open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn()), prompts=prompts, streams=streams)
        with anyio.CancelScope() as scope:
            holder["scope"] = scope
            async for event in agen:
                events.append(event)
        seen["cancelled"] = scope.cancelled_caught

    with recorder.captured_warnings() as warnings:
        run(main)
    assert prompts == [] and streams == []                       # the writer's model call was never asked for
    assert seen["cancelled"] and [e["event"] for e in events] == ["step"]
    assert abandoned(warnings) == [abandoned_line(2 * ONE_TURN_USD)]


def test_a_scope_cancelled_between_two_events_does_not_wake_the_thread_for_another_planner_call(monkeypatch):
    """Waking the parked planning thread is what starts the next PAID planner call, and the thread checks the stop flag
    only after it wakes. A cancelled scope is delivered at the next checkpoint, so that checkpoint has to come BEFORE
    the wake-up. The loop thread is made to lose 50 ms right after each wake-up (a busy loop, a descheduled thread),
    which is all the parked thread needs to start the call."""
    real, parked = threading.Semaphore, threading.Event()

    class WatchedSemaphore(real):
        def acquire(self, *args, **kwargs):
            parked.set()                                   # the planning thread is about to park after an event
            return super().acquire(*args, **kwargs)

        def release(self, n=1):
            super().release(n)
            time.sleep(0.05)

    shim = types.SimpleNamespace(Semaphore=WatchedSemaphore, Event=threading.Event)     # only this module sees it
    monkeypatch.setattr(stream_async, "threading", shim)
    planner = ProbePlanner(turn(FM_2026), turn(FM_2023), turn(), repeat=True)
    before, seen = set(threading.enumerate()), {}

    async def main():
        agen = open_stream(make_limiters(), planner, settings=make_settings(**LONG_PLAN))
        with anyio.CancelScope() as scope:
            seen["first"] = await anext(agen)
            await wait_until(parked.is_set)                # the thread is parked after handing over its first event
            scope.cancel()                                 # the client has gone; delivered at the next checkpoint
            await anext(agen)                              # the consumer never suspended in between
        seen["cancelled"] = scope.cancelled_caught
        await anyio.sleep(0.2)                             # a call that was going to start would have by now

    with recorder.captured_warnings() as warnings:
        run(main)
    assert seen["first"]["event"] == "step" and seen["cancelled"]
    assert len(planner.idents) == 1                        # no second planner call was started for the gone client
    assert abandoned(warnings) == [abandoned_line(ONE_TURN_USD)]
    wait_for_threads_to_end(before)


def test_a_scope_that_is_already_cancelled_when_the_first_event_is_asked_for_starts_no_planning():
    """Pins the invariant for the START of planning (the thread calls the planner at once). It does not isolate the
    checkpoint of ``__anext__``: starting the thread goes through several loop iterations of the thread hop itself, so
    the cancellation is delivered before the thread exists whether or not that checkpoint is there. The wake-up of the
    parked thread has no such cover, and the test above is the one that isolates it."""
    planner = ProbePlanner(turn(FM_2026), turn())
    before, events, seen = set(threading.enumerate()), [], {}

    async def main():
        agen = open_stream(make_limiters(), planner)
        with anyio.CancelScope() as scope:
            scope.cancel()                                 # the client had gone before the first event was asked for
            async for event in agen:
                events.append(event)
        seen["cancelled"] = scope.cancelled_caught
        await anyio.sleep(0.1)                             # a call that was going to start would have by now

    with recorder.captured_warnings() as warnings, unclosed_resources() as unclosed:
        run(main)
    assert seen["cancelled"] and events == []
    assert planner.idents == [] and planner.calls == []    # no planner call, hence no spend and no abandonment report
    assert abandoned(warnings) == []
    assert unclosed == []                                  # no host task ever ran, yet nothing was left unclosed
    wait_for_threads_to_end(before)


def test_a_stream_cancelled_before_its_first_event_closes_both_ends_of_its_hand_over_stream(monkeypatch):
    """No event was handed over and no host task ever ran, so only ``aclose`` can close the sending end. A closed
    receiving end alone would make a later ``send`` fail as BROKEN rather than as closed, and leave the object to the
    collector (a ResourceWarning, an error under ``python -X dev -W error::ResourceWarning``)."""
    plannings = PlanningSpy(monkeypatch)

    async def main():
        agen = open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn()))
        with anyio.CancelScope() as scope:
            scope.cancel()
            async for _ in agen:
                pass

    run(main)
    (planning,) = plannings.made
    with pytest.raises(anyio.ClosedResourceError):
        planning._send.send_nowait({})


def leave_during_the_prefetch(how: str, monkeypatch, planner, **open_kw) -> types.SimpleNamespace:
    """Pin the prefetch of ``run_agent`` inside the planning thread (the embedder waits on a gate), make the consumer
    leave in the way ``how`` says (an anyio scope, a native ``task.cancel()``, or two of them with the second landing
    during the join), and open the gate only once the stream has been told to stop: the thread finds the consumer gone
    at the first moment it could start a planner call. ``planner`` is whatever ``open_stream`` takes."""
    spy, plannings, embedder = RunAgentSpy(monkeypatch), PlanningSpy(monkeypatch), GatedEmbedder()
    prompts, streams, events, holder = [], [], [], {}
    before = set(threading.enumerate())

    async def consume(agen):
        with anyio.CancelScope() as scope:
            holder["scope"] = scope
            async for event in agen:
                events.append(event)

    async def open_the_gate_once_the_stream_was_told_to_stop():
        await wait_until(plannings.stopped)
        await anyio.sleep(0.15)                            # a second cancellation can land while the join waits
        embedder.gate.set()

    async def main():
        agen = open_stream(make_limiters(), planner, embedder=embedder, prompts=prompts, streams=streams, **open_kw)
        gate = asyncio.create_task(open_the_gate_once_the_stream_was_told_to_stop())
        task = asyncio.create_task(consume(agen))
        await wait_until(embedder.entered.is_set)          # the prefetch is running on the planning thread
        if how == "anyio_scope":
            holder["scope"].cancel()
        else:
            task.cancel()
            if how == "double_native_cancel":
                await anyio.sleep(0.05)
                task.cancel()
        (outcome,) = await asyncio.gather(task, return_exceptions=True)
        await gate
        return outcome

    with recorder.captured_warnings() as warnings:
        outcome = run(main)
    return types.SimpleNamespace(outcome=outcome, events=events, prompts=prompts, streams=streams, spy=spy,
                                 embedder=embedder, warnings=warnings, before=before)


@pytest.mark.parametrize("how", ["anyio_scope", "native_cancel", "double_native_cancel"])
def test_a_disconnect_during_the_prefetch_starts_no_planner_call(how, monkeypatch):
    """The prefetch and the first plan node run inside ONE resumption of ``run_agent`` (it yields only step events), so
    the thread's own stop check never sees a client that left meanwhile: the planner has to be guarded, or a paid call
    is made for a client that has gone. Nothing was spent, so nothing is reported as abandoned either, and the run's
    own planner-error fallback (a WARNING) must not fire."""
    planner = ProbePlanner(turn(FM_2026), turn())
    left = leave_during_the_prefetch(how, monkeypatch, planner)
    assert (left.outcome is None) == (how == "anyio_scope")        # a native cancel is re-raised once, after the join
    assert planner.calls == [] and planner.idents == []            # the paid call was never started
    assert left.events == [] and left.prompts == [] and left.streams == []
    assert left.spy.finished.is_set() and len(left.embedder.queries) == 1      # the prefetch ended; the run was closed
    assert left.warnings == []
    wait_for_threads_to_end(left.before)


def test_a_disconnect_during_the_prefetch_stops_the_default_planner_too(monkeypatch):
    live, made = ProbePlanner(turn(FM_2026), turn()), []

    def default_planner(model):
        made.append(model)
        return live

    monkeypatch.setattr(stream_async, "LiteLLMPlanner", default_planner)     # the real one would call the network
    left = leave_during_the_prefetch("anyio_scope", monkeypatch, None)
    assert made == [LUNA] and live.calls == [] and live.idents == []
    assert left.events == [] and left.warnings == []
    wait_for_threads_to_end(left.before)


# --- 6. nothing blocks the loop --------------------------------------------------------------------------------

def test_ten_concurrent_agent_asks_with_slow_planners_do_not_lag_the_event_loop():
    """A blocking call on the loop shows up as lag. One ask is run first so first-use costs (cost tables, imports)
    are not measured, and the collector is paused for the measured window: a full collection of this process's large
    heap (litellm, pydantic) can pause EVERY thread for about 100 ms, which says nothing about the code under test
    (measured over 15 runs of ten asks: worst lag 24 ms with the collector paused; with it running, an occasional
    84 to 132 ms)."""
    outcomes, monitor = [], LoopLagMonitor(warn_ms=100)

    async def ask(limiters):
        planner = ProbePlanner(turn(FM_2026), turn(), delay=0.05)
        events = await collect(open_stream(limiters, planner))
        outcomes.append([e["event"] for e in events])

    async def main():
        limiters = make_limiters(db=16)
        await ask(limiters)
        outcomes.clear()
        gc.collect()
        gc.disable()
        try:
            async with anyio.create_task_group() as outer:
                outer.start_soon(monitor.run)
                async with anyio.create_task_group() as asks:
                    for _ in range(10):
                        asks.start_soon(ask, limiters)
                outer.cancel_scope.cancel()
        finally:
            gc.enable()

    run(main, timeout=30)
    assert outcomes == [["step", "retrieval", "delta", "delta", "done"]] * 10
    assert monitor.warnings == 0, f"the loop lagged: max {monitor.max_lag_ms:.0f} ms"


# --- 7. the tracer --------------------------------------------------------------------------------

class ThreadTracer:
    """Records ``(what, name, thread)`` for every call in ``log`` and ``(what, name, kwargs)`` of every call that takes
    keywords (``span``, a span's ``set``, ``event``, ``generation``) in ``attrs``; ``enabled`` is the repo's
    convention for a tracer that does something."""

    def __init__(self, enabled: bool | None = None):
        self.log, self.attrs = [], []
        if enabled is not None:
            self.enabled = enabled

    def _note(self, what, name, attrs=None):
        self.log.append((what, name, threading.get_ident()))
        if attrs is not None:
            self.attrs.append((what, name, dict(attrs)))

    def span(self, name, **attrs):
        tracer = self
        self.attrs.append(("span", name, dict(attrs)))

        class Span:
            def __enter__(self_):
                tracer._note("enter", name)
                return self_

            def __exit__(self_, *exc):
                tracer._note("exit", name)
                return False

            def set(self_, **more):
                tracer._note("set", name, more)

        return Span()

    def event(self, name, **attrs):
        self._note("event", name, attrs)

    def generation(self, **kw):
        self._note("generation", kw.get("name"), kw)

    def flush(self):
        self._note("flush", None)


def test_the_agent_span_stays_on_the_loop_and_the_node_spans_on_the_worker_thread_one_after_the_other():
    tracer, loop_thread = ThreadTracer(), []

    async def main():
        loop_thread.append(threading.get_ident())
        return await collect(open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn()), tracer=tracer))

    assert run(main)[-1]["event"] == "done"
    loop = loop_thread[0]
    agent = [(what, thread) for what, name, thread in tracer.log if name == "agent"]
    assert [what for what, _ in agent] == ["enter", "set", "set", "exit"] and {t for _, t in agent} == {loop}
    nodes = [(what, name, thread) for what, name, thread in tracer.log if name not in ("agent", None)]
    assert {t for _, _, t in nodes} and loop not in {t for _, _, t in nodes} and len({t for _, _, t in nodes}) == 1
    order = [(what, name) for what, name, _ in tracer.log]
    children = [i for i, entry in enumerate(order) if entry[1] in ("prefetch", "plan", "tool")]
    assert order[0] == ("enter", "agent") and order.index(("exit", "agent")) > max(children)    # inside the agent span
    assert order[-1] == ("flush", None)


def test_a_tracer_that_may_block_is_flushed_off_the_loop_and_one_that_is_off_is_not_hopped():
    on, off, loop_thread = ThreadTracer(), ThreadTracer(enabled=False), []

    async def main():
        loop_thread.append(threading.get_ident())
        await collect(open_stream(make_limiters(), ProbePlanner(turn()), tracer=on))
        await collect(open_stream(make_limiters(), ProbePlanner(turn()), tracer=off))

    run(main)
    flush_threads = lambda tracer: [t for what, _, t in tracer.log if what == "flush"]    # noqa: E731
    assert len(flush_threads(on)) == 1 and flush_threads(on)[0] != loop_thread[0]
    assert flush_threads(off) == [loop_thread[0]]                      # called (parity), never on a thread


def test_the_tracer_sees_lengths_counts_and_ids_never_the_question_or_the_answer():
    """Every keyword of every tracer call is recorded (``span``, a span's ``set``, ``event``, ``generation``; the loop
    thread's and the planning thread's alike), and neither the whole question nor the whole answer is in any of them."""
    tracer = ThreadTracer()
    events = run(lambda: collect(open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn()), tracer=tracer)))
    answer = events[-1]["answer"]
    assert answer and answer.strip() == "".join(recorder.GOOD_PARTS).strip()
    kinds = {what for what, _, _ in tracer.attrs}
    assert {"span", "set", "generation"} <= kinds, kinds        # the recording is not empty of the calls that matter
    text = json.dumps(tracer.attrs, default=str)
    assert recorder.QUESTION not in text and answer not in text
    assert not [part for part in recorder.GOOD_PARTS if part in text]
    what, name, attrs = tracer.attrs[0]
    assert (what, name) == ("span", "agent")
    assert attrs["question_chars"] == len(recorder.QUESTION) and "question" not in attrs


def test_a_tracer_that_raises_from_every_method_changes_nothing():
    class Boom:
        def span(self, *a, **k):
            raise RuntimeError("span")

        def event(self, *a, **k):
            raise RuntimeError("event")

        def generation(self, **k):
            raise RuntimeError("generation")

        def flush(self):
            raise RuntimeError("flush")

    quiet = run(lambda: collect(open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn()))))
    loud = run(lambda: collect(open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn()), tracer=Boom())))
    assert [e["event"] for e in loud] == [e["event"] for e in quiet]
    assert loud[-1]["answer"] == quiet[-1]["answer"] and loud[-1]["cost_usd"] == quiet[-1]["cost_usd"]


class SlowFlush(ThreadTracer):
    """A tracer whose ``flush`` takes 0.5 s, like an app-level tracer waiting for the network."""

    def __init__(self):
        super().__init__()
        self.started, self.done, self.flushes = threading.Event(), threading.Event(), 0

    def flush(self):
        self.flushes += 1
        self.started.set()
        time.sleep(0.5)
        super().flush()
        self.done.set()


def test_native_cancels_during_the_tracer_flush_wait_for_the_flush_and_do_not_repeat_it():
    tracer, seen = SlowFlush(), {}

    async def main():
        async def consume():
            async for _ in open_stream(make_limiters(), ProbePlanner(turn()), tracer=tracer):
                pass

        task = asyncio.create_task(consume())
        await wait_until(tracer.started.is_set)                  # the answer is complete; the flush is under way
        task.cancel()
        await anyio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        seen.update(done=tracer.done.is_set(), flushes=tracer.flushes)

    with recorder.captured_warnings() as warnings:
        run(main)
    assert seen == {"done": True, "flushes": 1}               # waited for the flush, and made no second one
    assert abandoned(warnings) == []                          # the terminal event had been sent


def test_an_anyio_scope_cancelled_stream_still_flushes_off_the_loop_before_it_is_left():
    """The flush hop is its own task now: a cancelled anyio scope must still be held off it (and the flush is not
    repeated), and the stream must report the abandonment as before."""
    tracer, seen = SlowFlush(), {}

    async def main():
        with anyio.move_on_after(0.1) as scope:
            async for _ in open_stream(make_limiters(), ProbePlanner(turn(FM_2026), turn(), delay=0.3), tracer=tracer):
                pass
        seen.update(cancelled=scope.cancelled_caught, done=tracer.done.is_set(), flushes=tracer.flushes)

    with recorder.captured_warnings() as warnings:
        run(main)
    assert seen == {"cancelled": True, "done": True, "flushes": 1}
    assert abandoned(warnings) == [abandoned_line(ONE_TURN_USD)]


# --- 7b. the paid-call meter -----------------------------------------------------------------------------------------
# Every planner call is a paid call. The planner runs on the planning thread, so the meter (thread-safe, synchronous) is
# told from that thread: ``start`` immediately before the call, ``complete`` when it ends, whatever it ended with. The
# wrapper sits INSIDE the stop guard (``_unless_stopped(_metered(planner))``): a call the guard refuses never happens,
# so it must not be recorded; a call that gets past the guard is made and is recorded, whatever the consumer does next.

class ThreadNotingMeter(PaidMeter):
    """A real ``PaidMeter`` that notes the thread each of its methods is called from."""

    def __init__(self, settings):
        super().__init__(settings)
        self.start_threads, self.complete_threads = [], []

    def start(self, **kwargs):
        self.start_threads.append(threading.get_ident())
        return super().start(**kwargs)

    def complete(self, call_id, usage):
        self.complete_threads.append(threading.get_ident())
        super().complete(call_id, usage)


def planner_prompt_chars(call: dict) -> int:
    """What the meter is told a planner prompt weighs: the characters of the messages and of the tool schemas, as JSON."""
    return (len(json.dumps(call["messages"], ensure_ascii=False, default=str))
            + len(json.dumps(call["tools"], ensure_ascii=False, default=str)))


def test_every_planner_call_is_metered_from_the_planning_thread():
    planner, meter, loop_thread = ProbePlanner(turn(FM_2026), turn(FM_2023), turn()), ThreadNotingMeter(
        make_settings()), []

    async def main():
        loop_thread.append(threading.get_ident())
        return await collect(open_stream(make_limiters(), planner, meter=meter))

    events = run(main)
    assert events[-1]["event"] == "done" and len(planner.calls) == 3
    assert [(c.role, c.model) for c in meter.calls] == [("planner", LUNA)] * 3
    assert [c.prompt_chars for c in meter.calls] == [planner_prompt_chars(call) for call in planner.calls]
    assert meter.calls[0].prompt_chars < meter.calls[2].prompt_chars        # the prompt grows with the tool results
    assert [c.bound_micro for c in meter.calls] == [writer_tests.bound_micro(LUNA, "planner", c.prompt_chars,
                                                                              PLANNER_MAX_TOKENS) for c in meter.calls]
    one_turn = writer_tests.reported_micro(LUNA, "planner", (500, 40))        # what ``turn()`` reports by default
    assert [c.reported_micro for c in meter.calls] == [one_turn] * 3
    assert meter.charge_micro(10**9) == 3 * one_turn and meter.faults == () and not any(c.late for c in meter.calls)
    # from the planning thread, both ends of every call, and never the loop's: the meter has a lock for exactly this
    assert set(meter.start_threads) == set(meter.complete_threads) == set(planner.idents)
    assert len(meter.start_threads) == len(meter.complete_threads) == 3 and loop_thread[0] not in planner.idents


def test_without_a_meter_the_planner_is_handed_to_run_agent_exactly_as_before(monkeypatch):
    spy, planner = RunAgentSpy(monkeypatch), ProbePlanner(turn())
    assert run(lambda: collect(open_stream(make_limiters(), planner)))[-1]["event"] == "done"
    (handed,) = spy.planners
    assert stream_async._metered(planner, None, LUNA) is planner         # no wrapper at all when nobody is metering
    assert [(n, p.kind) for n, p in inspect.signature(handed).parameters.items()] == PLANNER_PARAMETERS


def test_a_metered_stream_hands_run_agent_a_planner_signature_and_records_one_call_per_planner_call(monkeypatch):
    spy, planner, meter = RunAgentSpy(monkeypatch), ProbePlanner(turn()), writer_tests.new_meter()
    assert run(lambda: collect(open_stream(make_limiters(), planner, meter=meter)))[-1]["event"] == "done"
    (handed,) = spy.planners
    metered = stream_async._metered(planner, meter, LUNA)
    for wrapper in (handed, metered):
        assert [(n, p.kind) for n, p in inspect.signature(wrapper).parameters.items()] == PLANNER_PARAMETERS
    assert len(meter.calls) == 1 and len(planner.calls) == 1


def test_a_planner_call_refused_by_the_stop_guard_is_not_metered():
    """The guard is outermost: once the stop flag is set a call is refused BEFORE the metered wrapper sees it, so a call
    that never happened leaves no record (and the ask is not charged a bound for it)."""
    planner, meter, stop = ScriptedPlanner(turn(FM_2026), turn()), writer_tests.new_meter(), threading.Event()
    guarded = stream_async._unless_stopped(stream_async._metered(planner, meter, LUNA), stop)
    messages, tools = [{"role": "user", "content": "q"}], [{"type": "function"}]
    guarded(messages, tools, timeout=1.5)
    assert len(meter.calls) == 1 and meter.calls[0].reported_micro == writer_tests.reported_micro(LUNA, "planner", (500, 40))
    stop.set()
    with pytest.raises(stream_async._PlanningStopped):
        guarded(messages, tools, timeout=1.5)
    assert len(planner.calls) == 1 and len(meter.calls) == 1               # the refused call: no planner call, no record


@pytest.mark.parametrize("how", ["anyio_scope", "native_cancel"])
def test_a_disconnect_during_the_prefetch_meters_nothing(how, monkeypatch):
    """The whole stream, not the wrapper: the consumer leaves while the prefetch runs on the planning thread, so the
    guard refuses planner call 1. Nothing was started, so the ask is charged nothing for the planner."""
    planner, meter = ProbePlanner(turn(FM_2026), turn()), writer_tests.new_meter()
    left = leave_during_the_prefetch(how, monkeypatch, planner, meter=meter)
    assert planner.calls == [] and meter.calls == () and meter.charge_micro(10**9) == 0
    wait_for_threads_to_end(left.before)


def test_a_planner_call_in_flight_at_a_disconnect_is_metered_with_the_usage_it_reports():
    """The documented limit: a call that began cannot be taken back. It is recorded when it starts and completed when it
    returns, and the planning thread is joined before the stream is gone, so the record is final by then."""
    planner, meter = ProbePlanner(turn(FM_2026), turn(FM_2023), turn(), delay=0.3, repeat=True), writer_tests.new_meter()
    before = set(threading.enumerate())

    async def main():
        agen = open_stream(make_limiters(), planner, settings=make_settings(**LONG_PLAN), meter=meter)
        task = asyncio.create_task(pull(agen))
        await wait_until(lambda: len(planner.idents) == 1)            # planning is blocked inside the first planner call
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with recorder.captured_warnings():
        run(main)
    (call,) = meter.calls
    assert call.role == "planner" and call.reported_micro == writer_tests.reported_micro(LUNA, "planner", (500, 40))
    assert len(planner.calls) == 1 and not call.late
    wait_for_threads_to_end(before)


def test_a_planner_call_that_starts_after_a_disconnect_is_counted():
    """The settlement (``meter.close()``) follows the disconnect; a planner call that STARTS after it (the join of the
    planning thread was cut short) is not refused here, only counted: recorded, marked late, logged at ERROR. The lease
    is already settled and is not settled again (the runtime's rule), so what the meter can do is not hide it."""
    meter = writer_tests.new_meter()
    planner = ScriptedPlanner(turn(FM_2026), turn())
    metered = stream_async._metered(planner, meter, LUNA)
    metered([{"role": "user", "content": "q"}], [], timeout=1.0)
    meter.close()
    metered([{"role": "user", "content": "q"}], [], timeout=1.0)
    first, second = meter.calls
    assert (first.late, second.late) == (False, True) and second.reported_micro is not None


def test_a_planner_call_that_starts_after_the_settlement_is_logged_at_error_through_the_whole_stream(caplog):
    meter = writer_tests.new_meter()

    class SettlingPlanner(ProbePlanner):
        """Its first call settles the ask while it is in flight (as if the lease had been closed while the join of the
        planning thread was cut short): the calls that START afterwards are late."""

        def __call__(self, messages, tools, *, timeout):
            if not self.calls:
                meter.close()
            return super().__call__(messages, tools, timeout=timeout)

    planner = SettlingPlanner(turn(FM_2026), turn(FM_2023), turn())
    caplog.set_level("ERROR", logger="semigraph.serve.meter")
    assert run(lambda: collect(open_stream(make_limiters(), planner, meter=meter)))[-1]["event"] == "done"
    assert [c.late for c in meter.calls] == [False, True, True]              # calls 2 and 3 started after the close
    assert all(c.reported_micro is not None for c in meter.calls)            # and all three are counted in the charge
    late_logs = [r.getMessage() for r in caplog.records if "paid call after settlement" in r.getMessage()]
    assert len(late_logs) == 2


def test_a_planner_that_raises_is_still_completed_with_its_bound_and_the_error_goes_on():
    meter = writer_tests.new_meter()
    planner = ScriptedPlanner(RuntimeError("provider down"))
    metered = stream_async._metered(planner, meter, LUNA)
    with pytest.raises(RuntimeError, match="provider down"):
        metered([{"role": "user", "content": "q"}], [], timeout=1.0)
    (call,) = meter.calls
    assert call.reported_micro is None and call.bound_micro > 0 and meter.faults == ()    # started: the bound is kept
    assert meter.charge_micro(10**9) == call.bound_micro


def test_a_prompt_that_cannot_be_sized_is_a_fault_not_a_failed_planner_call():
    """``json.dumps`` failing must not raise into ``run_agent`` (that would be silently turned into a ``planner_error``
    fallback for a call that was never made): the planner is still called, the meter records a fault, and a faulty meter
    charges the whole estimate."""
    meter = writer_tests.new_meter()
    planner = ScriptedPlanner(turn())
    loop: list = []
    loop.append(loop)                                                      # a list that contains itself
    reply = stream_async._metered(planner, meter, LUNA)([{"role": "user", "content": loop}], [], timeout=1.0)
    assert reply is planner.turns[0] and len(planner.calls) == 1
    assert meter.faults == ("bad_prompt_chars",) and meter.charge_micro(123_456) == 123_456


def test_the_meter_reaches_the_writer_as_its_own_keyword(monkeypatch):
    seen, real = [], stream_async.astream_answer_for_context

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(stream_async, "astream_answer_for_context", spy)
    meter = writer_tests.new_meter()
    run(lambda: collect(open_stream(make_limiters(), ProbePlanner(turn()), meter=meter)))
    (kwargs,) = seen
    assert kwargs["meter"] is meter


def test_the_default_writer_of_an_agent_ask_is_metered_after_the_planner(monkeypatch):
    """No injected writer: the real ``AsyncTextStream`` against a scripted provider. The meter holds the planner calls,
    then the draft, in the order they were started."""
    wt = writer_tests
    fake = wt.FakeAcompletion(wt.FakeUpstream(wt.answer_chunks(recorder.GOOD_PARTS, (12000, 300))))
    monkeypatch.setattr(answerer_async, "acompletion", fake)
    planner, meter = ProbePlanner(turn(FM_2026), turn()), wt.new_meter()

    async def main():
        agen = aagent_answer_stream(recorder.QUESTION, FakeDriver.world(), FakeEmbedder(), limiters=make_limiters(),
                                    planner=planner, settings=make_settings(**recorder.AGENT_SETTINGS), meter=meter,
                                    model=LUNA, max_tokens=2400)
        return await collect(agen)

    events = run(main)
    assert events[-1]["event"] == "done" and len(fake.calls) == 1
    assert [c.role for c in meter.calls] == ["planner", "planner", "strong"]      # sole answer model: the strong role
    assert meter.calls[2].model == LUNA and meter.calls[2].reported_micro == wt.reported_micro(LUNA, "strong", (12000, 300))


# --- 8. what the module may import --------------------------------------------------------------------------------

def _imported(path: Path) -> list[str]:
    """Every module (and every name imported from one) the file imports; a relative import keeps its leading dots."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found += [base, *(f"{base}.{alias.name}" for alias in node.names)]
    return found


def test_the_module_imports_nothing_of_the_uploads_the_workspace_or_the_heavy_packages():
    found = _imported(Path(stream_async.__file__))
    assert found, "the AST walk found no imports"
    assert not [m for m in found if "uploads" in m or "workspace" in m], found
    assert not [m for m in found if m.split(".")[0] in {"pandas", "sentence_transformers", "neo4j"}], found


def test_importing_the_module_does_not_load_the_uploads_or_the_workspace_writer():
    code = ("import sys; import semigraph.agent.stream_async; "
            "bad = [m for m in sys.modules if m.startswith(('semigraph.uploads', 'semigraph.retrieval.workspace'))]; "
            "sys.exit(1 if bad else 0)")
    env = {**os.environ, "PYTHONPATH": str(SRC), "LITELLM_LOCAL_MODEL_COST_MAP": "True"}   # no cost-map fetch at import
    done = subprocess.run([sys.executable, "-c", code], env=env, timeout=180, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-500:]
