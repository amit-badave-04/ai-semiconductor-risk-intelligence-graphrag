"""``PaidStream`` and ``PaidResponse`` (serve/stream_runtime.py): the async paid-answer stream, with the store, the
twins and the tracer faked and no HTTP (M5a I2, docs/v2/M5A_BUILD_PLAN.md section 4).

What is pinned: the busy event is byte-identical and costs nothing; the ledger row is written BEFORE the terminal event
is delivered, the cache write follows under the sync rule; a client that leaves before the terminal event still costs
exactly one ledger row (and never two); every store call is a worker-thread hop holding one ``limiters.db`` token; the
slot is released on every path; cleanup is ``finalize()`` and only that, which ``PaidResponse`` runs even when
sse-starlette skips its background task (a send timeout)."""

import asyncio
import json
import logging
import sys
import threading
import time
import types
from types import SimpleNamespace

import anyio
import pytest
from sse_starlette.event import ensure_bytes

from semigraph.serve import store
from semigraph.serve.limiters import make_limiters
from semigraph.serve.stream_runtime import (
    MSG_BUSY,
    MSG_FAILED,
    PaidResponse,
    PaidStream,
    TwinContractError,
    select_twin,
    sse_event,
)

Q = "Which HBM suppliers does Nvidia depend on, and which export rules apply?"
CID = "0001045810-26-000021:I.1:0320"
RETRIEVAL = {"event": "retrieval", "anchors": {"Nvidia": 1045810}, "counts": {"edges": 2}}
DELTA_1 = {"event": "delta", "text": "Nvidia depends on "}
DELTA_2 = {"event": "delta", "text": f"HBM suppliers [{CID}]."}
DONE = {"event": "done", "answer": f"Nvidia depends on HBM suppliers [{CID}].", "citations": [CID], "hallucinated": [],
        "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost_usd": 0.00007,
        "strategy": "hybrid", "question": Q}
FAILED_CHECKS = {"citations_retrieved": True, "numbers_grounded": False, "unmatched_numbers": ["$190 billion"],
                 "pseudo_citations": []}
WORKSPACE = {"workspace_id": "0123456789abcdef", "as_of": None}
NO_SPEND_ROW = {"ip_hash": "iph", "strategy": "hybrid", "cached": False, "workspace": False}   # no usage, no cost
MAIN_THREAD = threading.get_ident()


def run(main, timeout: float = 20.0):
    async def guarded():
        with anyio.fail_after(timeout):
            return await main()
    return asyncio.run(guarded())


class Tracer:
    """A tracer factory (``tracing.LangfuseTracer.for_request``) that records where its per-request tracer closed."""

    def __init__(self, close_error: Exception | None = None):
        self.created, self.closed_on, self.close_error = 0, [], close_error

    def for_request(self, question, *, strategy=""):
        self.created += 1
        return SimpleNamespace(close=self._close)

    def _close(self):
        self.closed_on.append(threading.get_ident())
        if self.close_error:
            raise self.close_error


class Recorder:
    """The store's two write functions: one ordered call list, plus the thread and the ``limiters.db`` tokens held."""

    def __init__(self):
        self.calls: list[str] = []
        self.rows: list[dict] = []
        self.puts: list[dict] = []
        self.threads: list[tuple[str, int, int]] = []
        self.st = None
        self.log_query_hook = None

    def install(self, monkeypatch):
        monkeypatch.setattr(store, "log_query", self._log_query)
        monkeypatch.setattr(store, "put_answer", self._put_answer)

    def _note(self, name: str) -> None:
        self.calls.append(name)
        self.threads.append((name, threading.get_ident(), self.st.limiters.db.borrowed_tokens))

    def _log_query(self, driver, **kw):
        self._note("log_query")
        if self.log_query_hook is not None:
            self.log_query_hook()
        self.rows.append(kw)

    def _put_answer(self, driver, **kw):
        self._note("put_answer")
        self.puts.append(kw)

    def names(self) -> list[str]:
        return list(self.calls)


@pytest.fixture
def rec(monkeypatch):
    recorder = Recorder()
    recorder.install(monkeypatch)
    return recorder


def make_state(rec: Recorder, *, max_answers: int = 1, db_threads: int = 4, tracer=None, escalation_model: str = ""):
    """The app state the stream reads; built INSIDE the running loop (the limiters are bound to it)."""
    settings = SimpleNamespace(llm_request_timeout_s=5, llm_answer_max_tokens=100, escalation_model=escalation_model,
                               embed_slots=1, db_thread_limit=db_threads, max_concurrent_answers=max_answers)
    st = SimpleNamespace(settings=settings, driver=object(), embedder=object(), limiters=make_limiters(settings),
                         answer_limiter=anyio.CapacityLimiter(max_answers), tracer=tracer)
    rec.st = st
    return st


def twin_of(*events, then=None, closed=None, seen=None):
    """An async-generator twin yielding ``events``; ``then`` is awaited after the last one (``anyio.sleep_forever`` to
    hold the stream open); ``closed`` gets an item when the generator is closed; ``seen`` gets the call's arguments."""
    async def twin(question, driver, embedder, **kw):
        if seen is not None:
            seen.update(kw, question=question, driver=driver, embedder=embedder)
        try:
            for event in events:
                yield event
            if then is not None:
                await then()
        finally:
            if closed is not None:
                closed.append(True)
    return twin


def failing_twin(*events, error: Exception):
    async def twin(question, driver, embedder, **kw):
        for event in events:
            yield event
        raise error
    return twin


def stream_of(st, twin, *, strategy="hybrid", workspace=None, iph="iph", snapshot_id="snap-1"):
    return PaidStream(st, Q, strategy, iph, snapshot_id, workspace, twin=twin)


async def drain(stream: PaidStream, rec: Recorder | None = None) -> list[dict]:
    out = []
    async for sse in stream.events():
        payload = json.loads(sse.data)
        if rec is not None:
            rec.calls.append(f"delivered:{payload['event']}")
        out.append(payload)
    return out


# ---------------------------------------------------------------- the busy path

def test_a_full_cap_gives_the_busy_event_byte_for_byte_and_touches_nothing(rec):
    expected = ensure_bytes(sse_event({"event": "error", "detail": MSG_BUSY}), "\n")
    assert expected == (b'event: error\ndata: {"event": "error", "detail": "The service is busy answering other '
                        b'questions \\u2014 try again in a moment."}\n\n')

    async def main():
        tracer = Tracer()
        st = make_state(rec, tracer=tracer)
        first = stream_of(st, twin_of(RETRIEVAL, then=anyio.sleep_forever))
        held = first.events()
        await held.__anext__()
        assert st.answer_limiter.borrowed_tokens == 1
        second = stream_of(st, twin_of(DONE), strategy="agent")
        events = [ensure_bytes(sse, "\n") async for sse in second.events()]
        assert events == [expected]
        await second.finalize()
        assert st.answer_limiter.borrowed_tokens == 1            # the busy stream took nothing and released nothing
        assert rec.rows == [] and tracer.created == 0            # no ledger row, no tracer
        await held.aclose()
        await first.finalize()
        assert st.answer_limiter.borrowed_tokens == 0 and len(rec.rows) == 1   # the first one's own abandoned row only

    run(main)


# ---------------------------------------------------------------- the normal path

def test_events_are_the_twins_events_through_the_sse_formatting(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1, DELTA_2, DONE))
        raw = [ensure_bytes(sse, "\n") async for sse in stream.events()]
        await stream.finalize()
        return raw

    raw = run(main)
    assert raw == [ensure_bytes(sse_event(e), "\n") for e in (RETRIEVAL, DELTA_1, DELTA_2, DONE)]


def test_the_ledger_row_is_written_before_the_terminal_event_is_delivered_then_the_cache(rec, caplog):
    caplog.set_level("INFO", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1, DELTA_2, DONE))
        events = await drain(stream, rec)
        await stream.finalize()
        assert st.answer_limiter.borrowed_tokens == 0
        return events

    events = run(main)
    assert [e["event"] for e in events] == ["retrieval", "delta", "delta", "done"]
    assert rec.names() == ["delivered:retrieval", "delivered:delta", "delivered:delta", "log_query", "put_answer",
                           "delivered:done"]
    assert rec.rows == [{"ip_hash": "iph", "strategy": "hybrid", "cached": False, "usage": DONE["usage"],
                         "cost_usd": 0.00007, "workspace": False}]
    assert rec.puts == [{"question": Q, "strategy": "hybrid", "answer": DONE["answer"], "citations": [CID],
                         "hallucinated": [], "usage": DONE["usage"], "cost_usd": 0.00007, "snapshot_id": "snap-1"}]
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("answered "))
    assert "strategy=hybrid citations=1 hallucinated=0 cost=7e-05" in line


@pytest.mark.parametrize("done,workspace", [
    ({**DONE, "answer": "  \n"}, None),
    ({**DONE, "finish_reason": "length"}, None),
    ({**DONE, "checks": FAILED_CHECKS}, None),
    (DONE, WORKSPACE),
], ids=["empty answer", "truncated", "failed checks", "workspace"])
def test_the_answer_is_cached_only_under_the_four_conditions(rec, done, workspace):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, done), workspace=workspace)
        await drain(stream)
        await stream.finalize()

    run(main)
    assert rec.puts == [] and len(rec.rows) == 1                 # still on the ledger


def test_a_clean_answer_with_clean_checks_is_cached(rec):
    clean = {**DONE, "checks": {"citations_retrieved": True, "numbers_grounded": True, "unmatched_numbers": [],
                                "pseudo_citations": []}}

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, clean))
        await drain(stream)
        await stream.finalize()

    run(main)
    assert len(rec.puts) == 1 and len(rec.rows) == 1


def test_an_answer_that_failed_a_check_is_released_with_a_warning_that_logs_workspace_lists_as_lengths(rec, caplog):
    checks = {**FAILED_CHECKS, "unmatched_numbers": ["$190 billion", "$5 million"]}
    done = {**DONE, "checks": checks, "routed": "strong"}
    caplog.set_level("INFO", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, done), workspace=WORKSPACE)
        events = await drain(stream)
        await stream.finalize()
        return events

    events = run(main)
    assert events[-1]["checks"] == done["checks"]                # the client still receives them
    warning = next(r.getMessage() for r in caplog.records
                   if r.levelname == "WARNING" and "failed checks" in r.getMessage())
    assert "$190 billion" not in warning and "'unmatched_numbers': 2" in warning and "routed=strong" in warning


def test_a_terminal_error_event_is_ledgered_with_its_spend_and_reaches_the_client_as_the_generic_text(rec, caplog):
    secret = "sk-live-abcdef1234567890"    # a fake, deliberately secret-shaped canary, not a real key - gitleaks:allow
    failed = {"event": "error", "detail": f"BadRequestError: upstream rejected key {secret}", "partial": "p",
              "usage": {"prompt_tokens": 5, "completion_tokens": 0}, "cost_usd": 0.002, "strategy": "hybrid"}
    caplog.set_level("WARNING", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, failed))
        events = await drain(stream, rec)
        await stream.finalize()
        return events

    events = run(main)
    assert events[-1] == {"event": "error", "detail": MSG_FAILED} and secret not in json.dumps(events)
    assert rec.names() == ["delivered:retrieval", "log_query", "delivered:error"]
    assert rec.rows[0]["cost_usd"] == 0.002 and rec.rows[0]["usage"] == failed["usage"] and rec.puts == []
    line = next(r.getMessage() for r in caplog.records if "mid-stream" in r.getMessage())
    assert secret not in line and "***" in line


# ---------------------------------------------------------------- the exception path

def test_a_twin_that_raises_is_one_ledger_row_without_usage_and_the_generic_error_event(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, failing_twin(RETRIEVAL, error=RuntimeError("provider down")))
        events = await drain(stream, rec)
        assert st.answer_limiter.borrowed_tokens == 1            # released by finalize, not by the generator
        await stream.finalize()
        assert st.answer_limiter.borrowed_tokens == 0
        return events

    events = run(main)
    assert events[-1] == {"event": "error", "detail": "The answer could not be completed (RuntimeError)."}
    assert rec.names() == ["delivered:retrieval", "log_query", "delivered:error"]
    assert rec.rows == [NO_SPEND_ROW]
    assert rec.puts == []


def test_a_ledger_failure_at_the_terminal_event_behaves_as_in_the_sync_path(rec):
    """``log_query`` raising for the terminal event is an ordinary failure: logged, a second attempt without usage, and
    the client gets the generic error event instead of the answer (the sync generator's ``except`` block)."""
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("db down")

    rec.log_query_hook = flaky

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        events = await drain(stream)
        await stream.finalize()
        return events

    events = run(main)
    assert events[-1] == {"event": "error", "detail": "The answer could not be completed (RuntimeError)."}
    assert len(attempts) == 2 and rec.rows == [NO_SPEND_ROW]
    assert rec.puts == []


# ---------------------------------------------------------------- the client leaves

def test_an_abandoned_stream_is_one_ledger_row_without_usage_written_by_finalize_and_only_by_it(rec):
    tracer, closed = Tracer(), []

    async def main():
        st = make_state(rec, tracer=tracer)
        twin = twin_of(RETRIEVAL, DELTA_1, DELTA_2, DONE, closed=closed)
        stream = stream_of(st, twin, strategy="agent")
        gen = stream.events()
        await gen.__anext__()                                    # retrieval
        await gen.__anext__()                                    # the first delta: the browser goes away
        await gen.aclose()
        assert rec.rows == [] and st.answer_limiter.borrowed_tokens == 1    # the generator cleans up nothing itself
        assert closed == [] and tracer.closed_on == []
        await stream.finalize()
        assert st.answer_limiter.borrowed_tokens == 0
        assert closed == [True]                                  # the twin generator was closed (and its upstream)
        await stream.finalize()                                  # idempotent
        assert st.answer_limiter.borrowed_tokens == 0

    run(main)
    assert rec.rows == [{**NO_SPEND_ROW, "strategy": "agent"}]
    assert rec.puts == []
    assert tracer.created == 1 and len(tracer.closed_on) == 1 and tracer.closed_on[0] != MAIN_THREAD


def test_a_client_that_leaves_after_the_terminal_event_costs_no_second_row(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        gen = stream.events()
        await gen.__anext__()
        done = await gen.__anext__()                             # the terminal event is delivered; the client leaves
        assert json.loads(done.data)["event"] == "done"
        await gen.aclose()
        await stream.finalize()
        assert st.answer_limiter.borrowed_tokens == 0

    run(main)
    assert len(rec.rows) == 1 and rec.rows[0]["cost_usd"] == 0.00007 and len(rec.puts) == 1


def test_a_stream_that_ends_without_a_terminal_event_is_one_abandoned_row(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1))
        await drain(stream)
        await stream.finalize()

    run(main)
    assert rec.rows == [NO_SPEND_ROW]


def test_cancelling_the_consumer_mid_stream_still_gives_exactly_one_row(rec):
    closed = []

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1, then=anyio.sleep_forever, closed=closed))
        seen = []

        async def consume():
            async for sse in stream.events():
                seen.append(json.loads(sse.data)["event"])

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            while len(seen) < 2:
                await anyio.sleep(0.005)
            tg.cancel_scope.cancel()                             # what sse-starlette does on a disconnect
        assert rec.rows == [] and st.answer_limiter.borrowed_tokens == 1
        await stream.finalize()
        await stream.finalize()
        assert st.answer_limiter.borrowed_tokens == 0

    run(main)
    assert rec.rows == [NO_SPEND_ROW]
    assert closed == [True]


def test_a_disconnect_during_the_terminal_ledger_write_does_not_lose_the_usage_or_add_a_second_row(rec):
    """The write is shielded and the 'a row exists' flag is set before the checkpoint that raises the cancellation."""
    started = threading.Event()
    rec.log_query_hook = lambda: (started.set(), time.sleep(0.2))

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        delivered = []

        async def consume():
            async for sse in stream.events():
                delivered.append(json.loads(sse.data)["event"])

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            while not started.is_set():
                await anyio.sleep(0.005)
            tg.cancel_scope.cancel()
        await stream.finalize()
        assert st.answer_limiter.borrowed_tokens == 0
        return delivered

    delivered = run(main)
    assert delivered == ["retrieval"]                            # the client never saw the terminal event ...
    assert len(rec.rows) == 1 and rec.rows[0]["usage"] == DONE["usage"] and rec.rows[0]["cost_usd"] == 0.00007
    assert len(rec.puts) == 1                                    # ... but the answer was paid for and is cached


# ---------------------------------------------------------------- threads and tokens

def test_every_store_call_runs_on_a_worker_thread_holding_one_db_token(rec):
    async def main():
        st = make_state(rec, db_threads=4)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        await drain(stream)
        await stream.finalize()
        assert st.limiters.db.borrowed_tokens == 0

    run(main)
    assert [name for name, _, _ in rec.threads] == ["log_query", "put_answer"]
    assert all(ident != MAIN_THREAD and tokens == 1 for _, ident, tokens in rec.threads)


def test_the_abandoned_row_and_the_tracer_close_also_run_on_a_worker_thread_under_the_db_limiter(rec):
    tracer = Tracer()
    holds = []
    rec.log_query_hook = lambda: holds.append(rec.st.limiters.db.borrowed_tokens)

    async def main():
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, twin_of(RETRIEVAL, then=anyio.sleep_forever), strategy="agent")
        gen = stream.events()
        await gen.__anext__()
        await gen.aclose()
        await stream.finalize()

    run(main)
    assert holds == [1] and rec.threads[0][1] != MAIN_THREAD and tracer.closed_on[0] != MAIN_THREAD


@pytest.mark.parametrize("path", ["done", "error event", "raises", "abandoned", "no terminal event", "contract error"])
def test_the_slot_is_released_on_every_path(rec, path):
    async def main():
        st = make_state(rec)
        twins = {"done": twin_of(RETRIEVAL, DONE),
                 "error event": twin_of(RETRIEVAL, {"event": "error", "detail": "x", "usage": None, "cost_usd": None}),
                 "raises": failing_twin(error=ValueError("boom")),
                 "abandoned": twin_of(RETRIEVAL, then=anyio.sleep_forever),
                 "no terminal event": twin_of(RETRIEVAL),
                 "contract error": lambda *a, **kw: iter([RETRIEVAL])}
        stream = stream_of(st, twins[path])
        gen = stream.events()
        try:
            if path in ("abandoned",):
                await gen.__anext__()
                await gen.aclose()
            else:
                async for _ in gen:
                    pass
        except TwinContractError:
            assert path == "contract error"
        assert st.answer_limiter.borrowed_tokens == 1            # held until finalize
        await stream.finalize()
        assert st.answer_limiter.borrowed_tokens == 0

    run(main)


# ---------------------------------------------------------------- workspace asks and the twin's arguments

def test_a_workspace_ask_is_ledgered_as_one_and_never_cached_and_reaches_the_workspace_twin_with_its_arguments(rec):
    seen, tracer = {}, Tracer()

    async def main():
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE, seen=seen), workspace=WORKSPACE)
        await drain(stream)
        await stream.finalize()

    run(main)
    assert rec.rows[0]["workspace"] is True and rec.puts == [] and tracer.created == 0
    assert seen["workspace_id"] == WORKSPACE["workspace_id"] and seen["as_of"] is None and "tracer" not in seen


def test_the_abandoned_row_of_a_workspace_ask_carries_the_workspace_flag(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, then=anyio.sleep_forever), workspace=WORKSPACE)
        gen = stream.events()
        await gen.__anext__()
        await gen.aclose()
        await stream.finalize()

    run(main)
    assert rec.rows == [{**NO_SPEND_ROW, "workspace": True}]


@pytest.mark.parametrize("escalation,expected", [("", None), ("m/strong", "m/strong")])
def test_the_twin_gets_the_limits_the_limiters_and_the_escalation_model(rec, escalation, expected):
    seen = {}

    async def main():
        st = make_state(rec, escalation_model=escalation)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE, seen=seen))
        await drain(stream)
        await stream.finalize()
        return st

    st = run(main)
    assert seen["strategy"] == "hybrid" and seen["timeout"] == 5 and seen["max_tokens"] == 100
    assert seen["escalation_model"] == expected and seen["limiters"] is st.limiters
    assert seen["question"] == Q and seen["driver"] is st.driver and seen["embedder"] is st.embedder


def test_an_agent_ask_gets_a_request_tracer_that_is_closed_on_a_worker_thread(rec):
    seen, tracer = {}, Tracer()

    async def main():
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE, seen=seen), strategy="agent")
        await drain(stream)
        assert tracer.closed_on == []                            # closed by finalize
        await stream.finalize()
        return st

    st = run(main)
    assert seen["settings"] is st.settings and seen["tracer"] is not None and tracer.created == 1
    assert len(tracer.closed_on) == 1 and tracer.closed_on[0] != MAIN_THREAD


# ---------------------------------------------------------------- the twin contract

def test_a_sync_generator_twin_is_a_type_error_not_an_error_event_and_costs_nothing(rec):
    def sync_twin(question, driver, embedder, **kw):
        yield RETRIEVAL

    async def main():
        st = make_state(rec)
        stream = stream_of(st, sync_twin)
        with pytest.raises(TypeError, match="not an async generator"):
            await drain(stream)
        await stream.finalize()
        assert st.answer_limiter.borrowed_tokens == 0

    run(main)
    assert issubclass(TwinContractError, TypeError) and rec.rows == [] and rec.puts == []


# ---------------------------------------------------------------- finalize never raises

def test_finalize_logs_instead_of_raising_and_still_releases_the_slot(rec, caplog):
    tracer = Tracer(close_error=RuntimeError("flush failed"))
    rec.log_query_hook = lambda: (_ for _ in ()).throw(RuntimeError("db down"))

    class BrokenClose:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return RETRIEVAL

        async def aclose(self):
            raise RuntimeError("upstream will not close")

    async def main():
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, lambda *a, **kw: BrokenClose(), strategy="agent")
        gen = stream.events()
        await gen.__anext__()
        await gen.aclose()
        await stream.finalize()                                  # must not raise
        assert st.answer_limiter.borrowed_tokens == 0

    caplog.set_level("ERROR", logger="semigraph.serve")
    run(main)
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "closing the answer stream failed" in messages and "ledger write failed for an abandoned answer" in messages
    assert len(tracer.closed_on) == 1                            # the tracer close was still attempted


def test_a_stream_that_never_started_finalizes_to_nothing(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL))
        await stream.finalize()
        assert st.answer_limiter.borrowed_tokens == 0

    run(main)
    assert rec.rows == []


# ---------------------------------------------------------------- the twin selection

def test_the_twin_is_chosen_by_workspace_first_then_strategy_and_the_agent_is_imported_lazily(monkeypatch):
    sec, ws, agent = object(), object(), object()
    guard_stub = types.ModuleType("semigraph.agent.stream_async")
    touched = []

    def probe(name):
        if name.startswith("__"):
            raise AttributeError(name)
        touched.append(name)
        return agent
    guard_stub.__getattr__ = probe
    monkeypatch.setitem(sys.modules, "semigraph.agent", types.ModuleType("semigraph.agent"))
    monkeypatch.setitem(sys.modules, "semigraph.agent.stream_async", guard_stub)
    assert select_twin("hybrid", True, sec=sec, workspace_twin=ws) is ws
    assert select_twin("hybrid", False, sec=sec, workspace_twin=ws) is sec
    assert select_twin("vector", False, sec=sec, workspace_twin=ws) is sec
    assert touched == []                                         # nothing imported from the agent module so far
    assert select_twin("agent", False, sec=sec, workspace_twin=ws) is agent
    assert touched == ["aagent_answer_stream"]


# ------------------------------------------------------- PaidResponse: finalize survives what skips the background task

async def call_response(stream: PaidStream, *, send, receive=None, send_timeout=None, ping=None):
    response = PaidResponse(stream, send_timeout=send_timeout)
    if ping is not None:
        response.ping_interval = ping
    forever = receive or (lambda: anyio.sleep_forever())
    await response({"type": "http"}, forever, send)


def test_a_normal_response_settles_through_the_background_task(rec):
    messages = []

    async def send(message):
        messages.append(message)

    async def main():
        st = make_state(rec, tracer=Tracer())
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1, DONE), strategy="agent")
        await call_response(stream, send=send)
        assert st.answer_limiter.borrowed_tokens == 0

    run(main)
    assert messages[0]["type"] == "http.response.start" and messages[-1] == {
        "type": "http.response.body", "body": b"", "more_body": False}
    assert len(rec.rows) == 1 and rec.rows[0]["cost_usd"] == 0.00007


def test_a_disconnect_mid_stream_is_one_abandoned_row_through_the_response(rec):
    gone = anyio.Event()
    closed = []

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message.get("body", b"").startswith(b"event: retrieval"):
            gone.set()

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, then=anyio.sleep_forever, closed=closed))
        await call_response(stream, send=send, receive=receive)
        assert st.answer_limiter.borrowed_tokens == 0

    run(main)
    assert rec.rows == [NO_SPEND_ROW] and closed == [True]


async def blocking_send(message):
    """A client that stopped reading: the response start goes out, every body write then blocks."""
    if message["type"] != "http.response.start":
        await anyio.sleep_forever()


def test_a_send_timeout_on_an_event_still_settles_the_stream(rec):
    """sse-starlette raises past its background task here: ``PaidResponse`` finalizes in its own ``finally``."""
    from sse_starlette.sse import SendTimeoutError
    closed = []

    async def main():
        st = make_state(rec, tracer=Tracer())
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1, then=anyio.sleep_forever, closed=closed), strategy="agent")
        with pytest.raises(SendTimeoutError):
            await call_response(stream, send=blocking_send, send_timeout=0.05)
        assert st.answer_limiter.borrowed_tokens == 0 and closed == [True]

    run(main)
    assert rec.rows == [{**NO_SPEND_ROW, "strategy": "agent"}]


def test_a_send_timeout_on_a_ping_while_the_model_is_silent_still_settles_the_stream(rec):
    from sse_starlette.sse import SendTimeoutError
    closed = []

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(then=anyio.sleep_forever, closed=closed))
        with pytest.raises(SendTimeoutError):
            await call_response(stream, send=blocking_send, send_timeout=0.05, ping=0.02)
        assert st.answer_limiter.borrowed_tokens == 0

    run(main)
    assert len(rec.rows) == 1 and "usage" not in rec.rows[0] and closed == [True]


def test_the_logger_name_is_the_one_the_operator_filters_on(rec, caplog):
    caplog.set_level("INFO")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        await drain(stream)
        await stream.finalize()

    run(main)
    assert any(r.name == "semigraph.serve" and r.getMessage().startswith("answered ") for r in caplog.records)
    assert logging.getLogger("semigraph.serve").propagate
