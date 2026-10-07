"""``PaidStream`` and ``PaidResponse`` (serve/stream_runtime.py): the async paid-answer stream, with the state backend,
the twins and the tracer faked and no HTTP (M5a I2 and I4, docs/v2/M5A_BUILD_PLAN.md sections 3 and 4).

What is pinned: the lease the stream is handed is reconciled BEFORE the terminal event is delivered, then the cache
write follows under the sync rule; a client that leaves before the terminal event still costs exactly one settle
(``abandoned``, never two); every state call is a worker-thread hop holding one ``limiters.state`` token; the lease and
the drain count are released on every path; cleanup is ``finalize()`` and only that, which ``PaidResponse`` runs even
when sse-starlette skips its background task (a send timeout). A full in-flight cap is the ROUTE's 429
(tests/test_serve_api.py and tests/test_serve_state_wiring.py), not an event of the stream."""

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
from serve_state_fakes import FakeStateBackend, InMemoryLedger, fresh_drain  # noqa: F401 - fresh_drain: a fixture
from sse_starlette.event import ensure_bytes
from test_state_inprocess import FakeClock, state_settings

from semigraph.serve import drain, store
from semigraph.serve.limiters import make_limiters
from semigraph.serve.state import Denied, StateDrivers, StateUnavailable, make_backend
from semigraph.serve.stream_runtime import (
    MSG_BUSY,
    MSG_FAILED,
    PaidResponse,
    PaidStream,
    TwinContractError,
    admin_call,
    cost_micro_of,
    select_twin,
    slot_call,
    sse_event,
    state_call,
)

pytestmark = pytest.mark.usefixtures("fresh_drain")

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
# What the backend is told when a lease is settled: (outcome, usage, cost in micro-dollars; None keeps the estimate).
ABANDONED = ("abandoned", None, None)
ERROR_NO_SPEND = ("error", None, None)
SPEND = ("done", DONE["usage"], 70)
MAIN_THREAD = threading.get_ident()
PROSE = "the supplier quoted a confidential price of forty two dollars per wafer"   # prose, not key-shaped


def run(main, timeout: float = 20.0):
    async def guarded():
        with anyio.fail_after(timeout):
            return await main()
    return asyncio.run(guarded())


async def until(condition, timeout: float = 2.0) -> None:
    """Let the loop run until ``condition()`` is true; fail instead of hanging when it never is."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition was never met"
        await asyncio.sleep(0.005)


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
    """The state backend's calls as ONE ordered list (shared with the ``delivered:*`` marks of :func:`drain_events`),
    the thread each call ran on and the ``limiters.state`` tokens held while it ran."""

    def __init__(self):
        self.backend = FakeStateBackend()
        self.calls = self.backend.calls
        self.threads: list[tuple[str, int, int]] = []
        self.st = None
        self.backend.on_call = self._note

    def _note(self, name: str) -> None:
        self.threads.append((name, threading.get_ident(), self.st.limiters.state.borrowed_tokens))

    def names(self) -> list[str]:
        """The calls in order, without the lease bookkeeping (``reserve`` and ``mark_started`` have their own tests)."""
        return [call[0] for call in self.calls if call[0] not in ("reserve", "mark_started")]

    def charges(self) -> list[tuple]:
        return [(r["outcome"], r["usage"], r["cost_micro"]) for r in self.backend.settled]

    @property
    def settled(self) -> list[dict]:
        return self.backend.settled

    @property
    def puts(self) -> list[dict]:
        return self.backend.puts


@pytest.fixture
def rec():
    return Recorder()


def make_state(rec: Recorder, *, db_threads: int = 4, tracer=None, escalation_model: str = ""):
    """The app state the stream reads; built INSIDE the running loop (the limiters are bound to it)."""
    settings = SimpleNamespace(llm_request_timeout_s=5, llm_answer_max_tokens=100, escalation_model=escalation_model,
                               embed_slots=1, db_thread_limit=db_threads)
    st = SimpleNamespace(settings=settings, driver=object(), embedder=object(), limiters=make_limiters(settings),
                         state=rec.backend, tracer=tracer)
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
    """A stream as the route builds one: the backend granted a lease and the ask is counted on the drain."""
    lease = st.state.reserve(ip_hash=iph, strategy=strategy, workspace=workspace is not None, estimate_micro=60_000,
                             now_wall=0.0, now_mono=0.0)
    drain.DRAIN.enter()
    return PaidStream(st, Q, strategy, iph, snapshot_id, workspace, twin=twin, lease=lease)


async def drain_events(stream: PaidStream, rec: Recorder | None = None) -> list[dict]:
    out = []
    async for sse in stream.events():
        payload = json.loads(sse.data)
        if rec is not None:
            rec.calls.append((f"delivered:{payload['event']}",))
        out.append(payload)
    return out


# ---------------------------------------------------------------- the refusal that is no longer an event

def test_the_in_flight_refusal_is_not_an_event_of_the_stream_and_its_message_is_the_sync_paths():
    """The in-flight cap is the backend's (``state.reserve`` answers INFLIGHT): a pre-stream 429 of the route, so a
    stream is always built over a lease and the busy ``error`` event of the sync path is gone. The message did not
    change."""
    assert MSG_BUSY == "The service is busy answering other questions — try again in a moment."
    assert not hasattr(PaidStream, "_take_slot")


def test_each_stream_settles_only_the_lease_it_holds(rec):
    async def main():
        st = make_state(rec)
        first = stream_of(st, twin_of(RETRIEVAL, then=anyio.sleep_forever))
        held = first.events()
        await held.__anext__()
        second = stream_of(st, twin_of(RETRIEVAL, DONE), strategy="agent")
        assert [e["event"] for e in await drain_events(second)] == ["retrieval", "done"]
        # the first still holds its own
        assert rec.st.state.inflight == 1 and [r["strategy"] for r in rec.settled] == ["agent"]
        await held.aclose()
        await first.finalize()
        assert rec.st.state.inflight == 0 and [r["strategy"] for r in rec.settled] == ["agent", "hybrid"]

    run(main)


# ---------------------------------------------------------------- the lease is registered when the stream starts

def test_the_lease_is_marked_started_once_before_the_twin_yields_anything_with_no_thread_hop_and_no_state_token(rec):
    """``mark_started`` is a memory write (the protocol says so), made as soon as the stream runs: a twin that is slow to
    its first event (slow graph reads) must not leave a lease that the sweep may reclaim after the lease TTL."""
    async def main():
        st = make_state(rec)
        release = asyncio.Event()

        async def slow_twin(question, driver, embedder, **kw):
            await release.wait()                                  # nothing is yielded until the test says so
            yield RETRIEVAL
            yield DONE

        stream = stream_of(st, slow_twin)
        task = asyncio.ensure_future(drain_events(stream))
        await until(lambda: rec.backend.started)
        assert rec.backend.started == {stream._lease.lease_id}    # registered while the twin has yielded nothing
        release.set()
        await task
        await stream.finalize()
        return st

    run(main)
    assert rec.backend.names().count("mark_started") == 1
    (_, ident, tokens), = [t for t in rec.threads if t[0] == "mark_started"]
    assert ident == MAIN_THREAD and tokens == 0


def test_a_failing_mark_started_is_logged_and_never_stops_the_answer(rec, caplog):
    rec.backend.errors["mark_started"] = RuntimeError("registry gone")
    caplog.set_level("WARNING", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        return await drain_events(stream_of(st, twin_of(RETRIEVAL, DONE)))

    assert run(main) == [RETRIEVAL, DONE] and rec.charges() == [SPEND]
    assert any("marking the lease as started failed" in r.getMessage() for r in caplog.records)


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


def test_the_lease_is_reconciled_before_the_terminal_event_is_delivered_then_the_cache(rec, caplog):
    caplog.set_level("INFO", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1, DELTA_2, DONE))
        events = await drain_events(stream, rec)
        await stream.finalize()
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0
        return events

    events = run(main)
    assert [e["event"] for e in events] == ["retrieval", "delta", "delta", "done"]
    assert rec.names() == ["delivered:retrieval", "delivered:delta", "delivered:delta", "reconcile", "cache_put",
                           "delivered:done"]
    assert rec.charges() == [SPEND]
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
        await drain_events(stream)
        await stream.finalize()

    run(main)
    assert rec.puts == [] and len(rec.settled) == 1                 # still on the ledger


def test_a_clean_answer_with_clean_checks_is_cached(rec):
    clean = {**DONE, "checks": {"citations_retrieved": True, "numbers_grounded": True, "unmatched_numbers": [],
                                "pseudo_citations": []}}

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, clean))
        await drain_events(stream)
        await stream.finalize()

    run(main)
    assert len(rec.puts) == 1 and len(rec.settled) == 1


def test_an_answer_that_failed_a_check_is_released_with_a_warning_that_logs_workspace_lists_as_lengths(rec, caplog):
    checks = {**FAILED_CHECKS, "unmatched_numbers": ["$190 billion", "$5 million"]}
    done = {**DONE, "checks": checks, "routed": "strong"}
    caplog.set_level("INFO", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, done), workspace=WORKSPACE)
        events = await drain_events(stream)
        await stream.finalize()
        return events

    events = run(main)
    assert events[-1]["checks"] == done["checks"]                # the client still receives them
    warning = next(r.getMessage() for r in caplog.records
                   if r.levelname == "WARNING" and "failed checks" in r.getMessage())
    assert "$190 billion" not in warning and "'unmatched_numbers': 2" in warning and "routed=strong" in warning


def test_a_terminal_error_event_is_settled_with_its_spend_and_reaches_the_client_as_the_generic_text(rec, caplog):
    secret = "sk-live-abcdef1234567890"    # a fake, deliberately secret-shaped canary, not a real key - gitleaks:allow
    failed = {"event": "error", "detail": f"BadRequestError: upstream rejected key {secret}", "partial": "p",
              "usage": {"prompt_tokens": 5, "completion_tokens": 0}, "cost_usd": 0.002, "strategy": "hybrid"}
    caplog.set_level("WARNING", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, failed))
        events = await drain_events(stream, rec)
        await stream.finalize()
        return events

    events = run(main)
    assert events[-1] == {"event": "error", "detail": MSG_FAILED} and secret not in json.dumps(events)
    assert rec.names() == ["delivered:retrieval", "reconcile", "delivered:error"]
    assert rec.charges() == [("error", failed["usage"], 2000)] and rec.puts == []
    line = next(r.getMessage() for r in caplog.records if "mid-stream" in r.getMessage())
    assert secret not in line and "***" in line


@pytest.mark.parametrize("cost,micro", [(0.00007, 70), (0.0, 0), (0.031, 31_000), (None, None), (-1.0, None),
                                        (float("nan"), None), (float("inf"), None), ("0.5", None), (True, None)])
def test_the_cost_becomes_micro_dollars_and_an_unusable_one_keeps_the_estimate(cost, micro):
    assert cost_micro_of(cost) == micro


# ---------------------------------------------------------------- the exception path

def test_a_twin_that_raises_is_one_settle_without_usage_and_the_generic_error_event(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, failing_twin(RETRIEVAL, error=RuntimeError("provider down")))
        events = await drain_events(stream, rec)
        # released when events() ended, not by the response
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0
        await stream.finalize()                                  # idempotent
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0
        return events

    events = run(main)
    assert events[-1] == {"event": "error", "detail": "The answer could not be completed (RuntimeError)."}
    assert rec.names() == ["delivered:retrieval", "reconcile", "delivered:error"]
    assert rec.charges() == [ERROR_NO_SPEND]
    assert rec.puts == []


def test_a_reconcile_that_raises_at_the_terminal_event_never_blocks_it_and_finalize_settles_the_lease_as_abandoned(
        rec, caplog):
    """The lease is reserved on the ledger, so the ask is counted whatever the settle does. The answer the visitor paid
    for is delivered (the sync path turned it into the generic error): a failing settle is logged and finalize settles
    the lease once more, as abandoned, which charges the estimate rather than the unknown cost."""
    attempts = []

    def flaky(name):
        if name == "reconcile":
            attempts.append(1)
            if len(attempts) == 1:
                rec.backend.errors["reconcile"] = StateUnavailable("db down")
            else:
                rec.backend.errors.pop("reconcile", None)

    rec.backend.on_call = flaky
    caplog.set_level("INFO", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        events = await drain_events(stream)
        await stream.finalize()
        return events

    events = run(main)
    assert events == [RETRIEVAL, DONE]                           # the answer, not an error event
    assert len(attempts) == 2 and rec.charges() == [ABANDONED]
    assert any("settling the ask failed (StateUnavailable)" in r.getMessage() for r in caplog.records)
    assert len(rec.puts) == 1                                    # the cache write was not skipped either


def test_a_failing_cache_write_after_the_ledger_row_costs_no_second_row_and_the_visitor_gets_the_answer(rec, caplog):
    """The cache write is an optimisation, not part of the answer: the lease is already settled when ``cache_put``
    raises, so the failure is logged once (never with the answer text) and the stream still ends with ``done``."""
    rec.backend.errors["cache_put"] = RuntimeError("db down")
    caplog.set_level("INFO", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        events = await drain_events(stream, rec)
        await stream.finalize()                                  # must not settle a second time for an answered ask
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0
        return events

    events = run(main)
    assert rec.charges() == [SPEND]                              # the money rule: one settle, with the spend
    assert events == [RETRIEVAL, DONE]
    assert rec.names() == ["delivered:retrieval", "reconcile", "cache_put", "delivered:done"]
    errors =[r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and errors[0].name == "semigraph.serve", [r.getMessage() for r in errors]
    assert errors[0].getMessage() == "caching the answer failed" and errors[0].exc_info[0] is RuntimeError
    assert not any(DONE["answer"] in r.getMessage() for r in caplog.records)
    assert any(r.getMessage().startswith("answered ") for r in caplog.records)    # the info line still follows


def test_a_failing_cache_write_still_delivers_done_through_the_response(rec):
    messages = []

    async def send(message):
        messages.append(message)

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        await call_response(stream, send=send)
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0

    rec.backend.errors["cache_put"] = RuntimeError("db down")
    run(main)
    body = b"".join(m.get("body", b"") for m in messages)
    assert b"event: done" in body and b"event: error" not in body
    assert rec.charges() == [SPEND]


def test_a_workspace_ask_never_reaches_the_cache_write_so_a_failing_one_changes_nothing(rec, caplog):
    """A guard, not a regression test: a workspace answer is private and never cached, so ``cache_put`` never runs."""
    rec.backend.errors["cache_put"] = RuntimeError("must never be called")
    caplog.set_level("INFO", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE), workspace=WORKSPACE)
        events = await drain_events(stream)
        await stream.finalize()
        return events

    events = run(main)
    assert events[-1] == DONE and "cache_put" not in rec.names()
    assert rec.charges() == [SPEND] and rec.settled[0]["workspace"] is True
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


LATE = RuntimeError("the twin failed after its terminal event")
ERROR_NO_DETAIL = {"event": "error", "usage": {"prompt_tokens": 5, "completion_tokens": 0}, "cost_usd": 0.002}


GENERIC_KEYERROR = {"event": "error", "detail": "The answer could not be completed (KeyError)."}


@pytest.mark.parametrize("twin,delivered,charge", [
    (failing_twin(RETRIEVAL, DONE, error=LATE), [RETRIEVAL, DONE], SPEND),
    (twin_of(RETRIEVAL, {**DONE, "answer": None}), [RETRIEVAL, {**GENERIC_KEYERROR, "detail":
     "The answer could not be completed (AttributeError)."}], SPEND),
    (twin_of(RETRIEVAL, ERROR_NO_DETAIL), [RETRIEVAL, GENERIC_KEYERROR], ("error", ERROR_NO_DETAIL["usage"], 2000)),
], ids=["twin raises after done", "done without an answer", "error event without a detail"])
def test_a_failure_after_the_terminal_settle_costs_no_second_settle_and_the_client_always_gets_a_terminal_event(
        rec, caplog, twin, delivered, charge):
    """Once the lease is settled, an exception (the twin raising after it yielded ``done``; a ``done`` whose answer is
    ``None``; an ``error`` with no ``detail``) is logged and costs NO second settle: the ask is on the ledger once,
    which is what the daily ceilings count. If the client already holds its terminal event (the first case) nothing more
    is sent; if the event never reached it (the other two) it gets the generic error, because the page waits for
    ``done`` or ``error`` and would otherwise sit on 'generating...'."""
    caplog.set_level("INFO", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        events = await drain_events(stream_of(st, twin))
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0
        return events

    events = run(main)
    assert events == delivered
    assert rec.charges() == [charge]
    assert [c for c in rec.backend.reconcile_calls if c["outcome"] == "abandoned"] == []
    failures = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(failures) == 1 and failures[0].exc_info is None, [r.getMessage() for r in failures]
    assert "after its ledger row" in failures[0].getMessage()


def test_a_failure_after_the_terminal_row_logs_the_class_name_only_for_a_workspace_ask(rec, caplog):
    caplog.set_level("INFO", logger="semigraph.serve")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, failing_twin(RETRIEVAL, DONE, error=RuntimeError(PROSE)), workspace=WORKSPACE)
        return await drain_events(stream)

    assert run(main) == [RETRIEVAL, DONE]
    assert PROSE not in caplog.text and "RuntimeError" in caplog.text
    assert rec.charges() == [SPEND] and rec.settled[0]["workspace"] is True


# ---------------------------------------------------------------- the twin cannot even be called

def test_a_twin_that_raises_when_called_still_gets_its_one_settle_when_the_first_one_fails(rec):
    """An async-generator function called with arguments it cannot bind raises when CALLED, not when iterated. The lease
    is the stream's from the start, so if the failure settle cannot be written either, ``finalize`` settles it (as
    abandoned: the estimate stays charged), exactly once."""
    attempts = []

    def flaky(name):
        if name == "reconcile":
            attempts.append(1)
            if len(attempts) == 1:
                rec.backend.errors["reconcile"] = RuntimeError("db down")
            else:
                rec.backend.errors.pop("reconcile", None)

    rec.backend.on_call = flaky

    async def narrow_twin(question):
        yield RETRIEVAL

    async def main():
        st = make_state(rec)
        events = await drain_events(stream_of(st, narrow_twin))
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0
        return events

    events = run(main)
    assert events == [{"event": "error", "detail": "The answer could not be completed (TypeError)."}]
    assert len(attempts) == 2 and rec.charges() == [ABANDONED]


# ---------------------------------------------------------------- no uploaded text in the logs of a workspace ask

class QuotingClose:
    """An upstream stream whose ``aclose`` raises an exception that quotes uploaded text."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        return RETRIEVAL

    async def aclose(self):
        raise RuntimeError(PROSE)


@pytest.mark.parametrize("private", [True, False], ids=["workspace ask", "public ask"])
@pytest.mark.parametrize("trigger", ["error event", "twin raises", "close raises"])
def test_a_workspace_ask_logs_only_the_exception_class_name_and_a_public_ask_keeps_its_redacted_detail(
        rec, caplog, trigger, private):
    """A provider exception can quote the text it was given. For a workspace ask the text may be an upload: the log gets
    the class name only, on the error-event line, on the exception line and on the close line (``caplog.text`` includes
    any traceback). A public ask keeps the redacted detail. The client never sees the text either way."""
    caplog.set_level("DEBUG")
    twins = {"error event": twin_of(RETRIEVAL, {"event": "error", "detail": f"BadRequestError: rejected {PROSE}",
                                                "usage": None, "cost_usd": None}),
             "twin raises": failing_twin(RETRIEVAL, error=RuntimeError(PROSE)),
             "close raises": lambda *a, **kw: QuotingClose()}
    cls = {"error event": "BadRequestError", "twin raises": "RuntimeError", "close raises": "RuntimeError"}[trigger]

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twins[trigger], workspace=WORKSPACE if private else None)
        gen = stream.events()
        if trigger == "close raises":
            events = [json.loads((await gen.__anext__()).data)]
            await gen.aclose()                                   # the browser leaves; finalize closes the upstream
        else:
            events = [json.loads(sse.data) async for sse in gen]
        await stream.finalize()
        return events

    events = run(main)
    assert PROSE not in json.dumps(events)
    assert cls in caplog.text
    assert (PROSE in caplog.text) is (not private), caplog.text


# ---------------------------------------------------------------- events() cleans up at its own end

def test_exhausting_events_releases_the_lease_and_runs_the_whole_cleanup_without_any_finalize_call(rec):
    """``finalize`` is the last statement of ``events()``: the lease, the drain count and the cleanup do not wait for
    the response to finish being sent (a client that stops reading can stall the closing chunk for as long as it
    likes)."""
    tracer = Tracer()

    async def main():
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE), strategy="agent")
        events = await drain_events(stream, rec)
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0
        assert len(tracer.closed_on) == 1 and tracer.closed_on[0] != MAIN_THREAD
        return events

    run(main)
    assert rec.names() == ["delivered:retrieval", "reconcile", "cache_put", "delivered:done"]
    # the one settle: no abandoned one on top
    assert rec.charges() == [SPEND] and rec.settled[0]["strategy"] == "agent"


def test_the_terminal_event_is_yielded_before_the_cleanup_runs(rec):
    """The cleanup follows the last yield (control returns to ``events()`` only when the consumer asks for more), so the
    terminal event is never held back by it. The lease itself is already settled: that came BEFORE the event."""
    tracer = Tracer()

    async def main():
        st = make_state(rec, tracer=tracer)
        gen = stream_of(st, twin_of(RETRIEVAL, DONE), strategy="agent").events()
        await gen.__anext__()
        await gen.__anext__()                                    # the terminal event is in the consumer's hands ...
        assert rec.backend.inflight == 0                         # ... its lease was settled before it was yielded ...
        assert drain.DRAIN.active == 1 and tracer.closed_on == []      # ... and the cleanup has not run yet
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        assert drain.DRAIN.active == 0 and len(tracer.closed_on) == 1

    run(main)


def test_the_stream_is_released_when_the_closing_chunk_reaches_send(rec):
    """Through the response: by the time the empty closing chunk is handed to the server, the drain count is back and
    the lease is settled (the BackgroundTask runs only after that chunk has been sent)."""
    seen = {}

    async def main():
        st = make_state(rec)

        async def send(message):
            if is_closing_chunk(message):
                seen.update(active=drain.DRAIN.active, settled=len(rec.settled))

        await call_response(stream_of(st, twin_of(RETRIEVAL, DONE)), send=send)

    run(main)
    assert seen == {"active": 0, "settled": 1}


# ---------------------------------------------------------------- the client leaves

def test_an_abandoned_stream_is_one_abandoned_settle_made_by_finalize_and_only_by_it(rec):
    tracer, closed = Tracer(), []

    async def main():
        st = make_state(rec, tracer=tracer)
        twin = twin_of(RETRIEVAL, DELTA_1, DELTA_2, DONE, closed=closed)
        stream = stream_of(st, twin, strategy="agent")
        gen = stream.events()
        await gen.__anext__()                                    # retrieval
        await gen.__anext__()                                    # the first delta: the browser goes away
        await gen.aclose()
        # the generator cleans up nothing itself
        assert rec.settled == [] and rec.backend.inflight == 1 and drain.DRAIN.active == 1
        assert closed == [] and tracer.closed_on == []
        await stream.finalize()
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0
        assert closed == [True]                                  # the twin generator was closed (and its upstream)
        await stream.finalize()                                  # idempotent
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0

    run(main)
    assert rec.charges() == [ABANDONED] and rec.settled[0]["strategy"] == "agent"
    assert rec.puts == []
    assert tracer.created == 1 and len(tracer.closed_on) == 1 and tracer.closed_on[0] != MAIN_THREAD


def test_a_client_that_leaves_after_the_terminal_event_costs_no_second_settle(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        gen = stream.events()
        await gen.__anext__()
        done = await gen.__anext__()                             # the terminal event is delivered; the client leaves
        assert json.loads(done.data)["event"] == "done"
        await gen.aclose()
        await stream.finalize()
        assert drain.DRAIN.active == 0

    run(main)
    assert rec.charges() == [SPEND] and len(rec.puts) == 1


def test_a_stream_that_ends_without_a_terminal_event_is_one_abandoned_settle(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1))
        await drain_events(stream)
        await stream.finalize()

    run(main)
    assert rec.charges() == [ABANDONED]


def test_cancelling_the_consumer_mid_stream_still_gives_exactly_one_settle(rec):
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
        assert rec.settled == [] and rec.backend.inflight == 1 and drain.DRAIN.active == 1
        await stream.finalize()
        await stream.finalize()
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0

    run(main)
    assert rec.charges() == [ABANDONED]
    assert closed == [True]


def test_a_disconnect_during_the_terminal_settle_does_not_lose_the_usage_or_add_a_second_settle(rec):
    """The settle is shielded and the 'the lease is settled' flag is set before the checkpoint that raises the
    cancellation."""
    rec.backend.delays["reconcile"] = 0.2

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        delivered = []

        async def consume():
            async for sse in stream.events():
                delivered.append(json.loads(sse.data)["event"])

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            while "reconcile" not in rec.backend.names():
                await anyio.sleep(0.005)
            tg.cancel_scope.cancel()
        await stream.finalize()
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0
        return delivered

    delivered = run(main)
    assert delivered == ["retrieval"]                            # the client never saw the terminal event ...
    assert rec.charges() == [SPEND]                              # ... but its usage and cost were settled, once ...
    assert len(rec.puts) == 1                                    # ... and the answer was paid for and is cached


# ---------------------------------------------------------------- threads and tokens

def test_every_state_call_that_touches_the_store_runs_on_a_worker_thread_holding_one_state_token(rec):
    async def main():
        st = make_state(rec, db_threads=4)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        await drain_events(stream)
        await stream.finalize()
        assert st.limiters.state.borrowed_tokens == 0 and st.limiters.db.borrowed_tokens == 0

    run(main)
    hops = [(name, ident, tokens) for name, ident, tokens in rec.threads if name not in ("reserve", "mark_started")]
    assert [name for name, _, _ in hops] == ["reconcile", "cache_put"]
    assert all(ident != MAIN_THREAD and tokens == 1 for _, ident, tokens in hops)


def test_the_abandoned_settle_and_the_tracer_close_also_run_on_a_worker_thread_under_their_limiters(rec):
    tracer = Tracer()
    holds = []

    def note(name):
        if name == "reconcile":
            holds.append((name, rec.st.limiters.state.borrowed_tokens))

    rec.backend.on_call = note

    async def main():
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, twin_of(RETRIEVAL, then=anyio.sleep_forever), strategy="agent")
        gen = stream.events()
        await gen.__anext__()
        await gen.aclose()
        await stream.finalize()

    run(main)
    assert holds == [("reconcile", 1)] and tracer.closed_on[0] != MAIN_THREAD
    assert rec.backend.threads["reconcile"] and MAIN_THREAD not in rec.backend.threads["reconcile"]


@pytest.mark.parametrize("path,held_after_events", [
    ("done", 0), ("error event", 0), ("raises", 0), ("no terminal event", 0),   # events() ran to its end: it released
    ("abandoned", 1), ("contract error", 1),                                    # it did not: ``PaidResponse`` does
])
def test_the_lease_and_the_drain_count_are_released_on_every_path(rec, path, held_after_events):
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
        assert (rec.backend.inflight, drain.DRAIN.active) == (held_after_events, held_after_events)
        await stream.finalize()
        assert (rec.backend.inflight, drain.DRAIN.active) == (0, 0)

    run(main)
    assert len(rec.settled) == 1                                 # whatever the path: the lease was settled exactly once


def test_finalize_leaves_the_drain_exactly_once_however_often_it_runs(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        drain.DRAIN.enter()                                      # a second, unrelated count that must survive
        assert drain.DRAIN.active == 2
        await drain_events(stream)
        for _ in range(3):
            await stream.finalize()
        assert drain.DRAIN.active == 1
        drain.DRAIN.leave()

    run(main)


# ---------------------------------------------------------------- workspace asks and the twin's arguments

def test_a_workspace_ask_is_ledgered_as_one_and_never_cached_and_reaches_the_workspace_twin_with_its_arguments(rec):
    seen, tracer = {}, Tracer()

    async def main():
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE, seen=seen), workspace=WORKSPACE)
        await drain_events(stream)
        await stream.finalize()

    run(main)
    assert rec.settled[0]["workspace"] is True and rec.puts == [] and tracer.created == 0
    assert seen["workspace_id"] == WORKSPACE["workspace_id"] and seen["as_of"] is None and "tracer" not in seen


def test_the_abandoned_settle_of_a_workspace_ask_carries_the_workspace_flag(rec):
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, then=anyio.sleep_forever), workspace=WORKSPACE)
        gen = stream.events()
        await gen.__anext__()
        await gen.aclose()
        await stream.finalize()

    run(main)
    assert rec.charges() == [ABANDONED] and rec.settled[0]["workspace"] is True


@pytest.mark.parametrize("escalation,expected", [("", None), ("m/strong", "m/strong")])
def test_the_twin_gets_the_limits_the_limiters_and_the_escalation_model(rec, escalation, expected):
    seen = {}

    async def main():
        st = make_state(rec, escalation_model=escalation)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE, seen=seen))
        await drain_events(stream)
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
        await drain_events(stream)
        assert len(tracer.closed_on) == 1                        # closed by finalize, which events() ran at its end
        await stream.finalize()
        return st

    st = run(main)
    assert seen["settings"] is st.settings and seen["tracer"] is not None and tracer.created == 1
    assert len(tracer.closed_on) == 1 and tracer.closed_on[0] != MAIN_THREAD


# ---------------------------------------------------------------- the twin contract

def test_a_sync_generator_twin_is_a_type_error_not_an_error_event_and_its_lease_is_settled_as_abandoned(rec):
    """A wiring mistake: no error event, nothing cached. The lease it held is settled by ``finalize`` like every lease
    that never reached a terminal event (abandoned: the estimate stays charged), so nothing leaks."""
    def sync_twin(question, driver, embedder, **kw):
        yield RETRIEVAL

    async def main():
        st = make_state(rec)
        stream = stream_of(st, sync_twin)
        with pytest.raises(TypeError, match="not an async generator"):
            await drain_events(stream)
        assert rec.backend.inflight == 1                         # events() raised: ``PaidResponse`` settles
        await stream.finalize()
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0

    run(main)
    assert issubclass(TwinContractError, TypeError) and rec.charges() == [ABANDONED] and rec.puts == []


# ---------------------------------------------------------------- finalize never raises

def test_finalize_logs_instead_of_raising_and_still_leaves_the_drain(rec, caplog):
    tracer = Tracer(close_error=RuntimeError("flush failed"))
    rec.backend.errors["reconcile"] = RuntimeError("db down")

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
        assert drain.DRAIN.active == 0

    caplog.set_level("ERROR", logger="semigraph.serve")
    run(main)
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "closing the answer stream failed" in messages and "settling the ask failed (RuntimeError)" in messages
    assert len(tracer.closed_on) == 1                            # the tracer close was still attempted


def test_the_abandoned_settle_is_made_before_the_upstream_is_closed_and_before_the_tracer(rec):
    """Closing an agent twin joins its thread (up to 12 s) and the platform may kill the process long before that: the
    settle that the daily ceilings count is the FIRST thing ``finalize`` does. The order is: settle, twin, tracer,
    drain."""
    async def main():
        entered, gate = anyio.Event(), anyio.Event()

        class SlowClose:
            def __aiter__(self):
                return self

            async def __anext__(self):
                return RETRIEVAL

            async def aclose(self):
                entered.set()
                await gate.wait()

        tracer = Tracer()
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, lambda *a, **kw: SlowClose(), strategy="agent")
        gen = stream.events()
        await gen.__anext__()
        await gen.aclose()
        async with anyio.create_task_group() as tg:
            tg.start_soon(stream.finalize)
            await entered.wait()                                 # the upstream close is under way and blocked
            try:
                assert rec.charges() == [ABANDONED] and rec.settled[0]["strategy"] == "agent"   # ... the settle is done
                assert rec.backend.inflight == 0                 # ... the lease is free ...
                # ... and the drain count is not yet given back
                assert drain.DRAIN.active == 1 and tracer.closed_on == []
            finally:
                gate.set()                       # finalize is shielded: a failed assertion must not leave it blocked
        assert drain.DRAIN.active == 0 and len(tracer.closed_on) == 1

    run(main)
    assert len(rec.settled) == 1


def test_a_stream_that_never_ran_finalizes_to_one_abandoned_settle(rec):
    """Reserved, then the request went away before the first event (or the response could not be built): the lease is
    settled as abandoned by ``finalize``, and the drain count is given back."""
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL))
        assert rec.backend.inflight == 1 and drain.DRAIN.active == 1
        await stream.finalize()
        assert rec.backend.inflight == 0 and drain.DRAIN.active == 0

    run(main)
    assert rec.charges() == [ABANDONED]


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


def test_the_response_takes_no_shutdown_grace_period_because_the_drain_ends_the_streams_itself(rec):
    """``serve.drain`` tells sse-starlette to end the streams only when the drain is over; a positive grace would stack
    on top of it and the streams still running at the drain timeout would be cut past Fly's kill timeout, their lease
    unsettled. So the response keeps sse-starlette's default of 0 and never passes the argument."""
    import inspect

    async def main():
        stream = stream_of(make_state(rec), twin_of(RETRIEVAL))
        response = PaidResponse(stream)
        await stream.finalize()                                  # settle the lease the stream holds
        return response

    response = run(main)
    assert getattr(response, "_shutdown_grace_period", 0) == 0
    assert "shutdown_grace_period" not in inspect.getsource(PaidResponse)


def test_a_normal_response_settles_through_the_background_task(rec):
    messages = []

    async def send(message):
        messages.append(message)

    async def main():
        st = make_state(rec, tracer=Tracer())
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1, DONE), strategy="agent")
        await call_response(stream, send=send)
        assert drain.DRAIN.active == 0

    run(main)
    assert messages[0]["type"] == "http.response.start" and messages[-1] == {
        "type": "http.response.body", "body": b"", "more_body": False}
    assert len(rec.settled) == 1 and rec.settled[0]["cost_micro"] == 70


def test_a_disconnect_mid_stream_is_one_abandoned_settle_through_the_response(rec):
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
        assert drain.DRAIN.active == 0

    run(main)
    assert rec.charges() == [ABANDONED] and closed == [True]


async def blocking_send(message):
    """A client that stopped reading: the response start goes out, every body write then blocks."""
    if message["type"] != "http.response.start":
        await anyio.sleep_forever()


def is_closing_chunk(message) -> bool:
    """The empty ``more_body: False`` body message that ends a response (sse-starlette sends it last)."""
    return message["type"] == "http.response.body" and not message.get("more_body", False)


async def stalling_on_the_closing_chunk(message):
    """A server whose transport stopped draining just as the last frame went out: everything is accepted but the end."""
    if is_closing_chunk(message):
        await anyio.sleep_forever()


def spy_on_finalize(stream: PaidStream, caplog) -> list[int]:
    """Count the ``finalize`` calls of ``stream`` that did the cleanup (install it BEFORE the response takes
    ``finalize`` as its background task; ``events()`` calls it at its end, the response again, and only the first one
    works). Each such call that returns appends how many log records existed by then."""
    returned: list[int] = []
    real = stream.finalize

    async def finalize() -> None:
        first = not stream._finalized
        await real()
        if first:
            returned.append(len(caplog.records))
    stream.finalize = finalize
    return returned


def assert_dropped_with_one_warning_line(caplog, finalized: list[int]) -> None:
    """The send-timeout drop of a client: ``finalize`` ran once, and AFTERWARDS one warning line (the exception's class
    name, no traceback, nothing about the client) was logged, with no error record at all."""
    assert len(finalized) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    warnings = [r for r in caplog.records if r.name == "semigraph.serve" and r.levelno == logging.WARNING]
    assert len(warnings) == 1 and warnings[0].exc_info is None, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    assert "SendTimeoutError" in message and "iph" not in message and Q not in message
    assert caplog.records.index(warnings[0]) >= finalized[0]


def test_a_send_timeout_on_an_event_settles_the_stream_and_is_one_warning_line_not_a_traceback(rec, caplog):
    """sse-starlette raises past its background task here: ``PaidResponse`` finalizes in its own ``finally``, then logs
    the drop as one warning and returns normally (uvicorn would otherwise log an ERROR traceback per slow client)."""
    closed, tracer = [], Tracer()
    caplog.set_level("WARNING")

    async def main():
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1, then=anyio.sleep_forever, closed=closed), strategy="agent")
        finalized = spy_on_finalize(stream, caplog)
        await call_response(stream, send=blocking_send, send_timeout=0.05)
        assert drain.DRAIN.active == 0 and closed == [True]
        return finalized

    finalized = run(main)
    assert rec.charges() == [ABANDONED] and rec.settled[0]["strategy"] == "agent"
    assert len(tracer.closed_on) == 1
    assert_dropped_with_one_warning_line(caplog, finalized)


def test_a_send_timeout_on_a_ping_while_the_model_is_silent_is_one_warning_line_and_settles_the_stream(rec, caplog):
    closed = []
    caplog.set_level("WARNING")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(then=anyio.sleep_forever, closed=closed))
        finalized = spy_on_finalize(stream, caplog)
        await call_response(stream, send=blocking_send, send_timeout=0.05, ping=0.02)
        assert drain.DRAIN.active == 0
        return finalized

    finalized = run(main)
    assert rec.charges() == [ABANDONED] and closed == [True]
    assert_dropped_with_one_warning_line(caplog, finalized)


def test_a_closing_chunk_that_is_never_accepted_is_bounded_by_the_send_timeout_and_is_one_warning_line(rec, caplog):
    """sse-starlette's closing send has no timeout of its own, and its ping waits on the same lock: a client that stops
    reading in the last frame would hold the response (and, without ``events()`` releasing early, the lease) forever.
    ``PaidResponse`` bounds that one send with ``send_timeout`` and drops the client like any other send timeout."""
    closed, tracer = [], Tracer()
    caplog.set_level("WARNING")
    send_timeout = 0.1

    async def main():
        st = make_state(rec, tracer=tracer)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE, closed=closed), strategy="agent")
        finalized = spy_on_finalize(stream, caplog)
        started = time.perf_counter()
        with anyio.fail_after(send_timeout + 2):                 # fails in about a second without the bound
            await call_response(stream, send=stalling_on_the_closing_chunk, send_timeout=send_timeout)
        assert time.perf_counter() - started >= send_timeout
        assert drain.DRAIN.active == 0
        return finalized

    finalized = run(main)
    # the answer's settle; the drop adds none
    assert rec.charges() == [SPEND] and rec.settled[0]["strategy"] == "agent"
    assert closed == [True] and len(tracer.closed_on) == 1       # the cleanup ran once
    assert_dropped_with_one_warning_line(caplog, finalized)


def test_a_closing_chunk_accepted_within_the_send_timeout_is_delivered_untouched(rec, caplog):
    messages = []
    caplog.set_level("WARNING")

    async def send(message):
        messages.append(message)

    async def main():
        st = make_state(rec)
        await call_response(stream_of(st, twin_of(RETRIEVAL, DONE)), send=send, send_timeout=5)
        assert drain.DRAIN.active == 0

    run(main)
    assert messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert [m["type"] for m in messages] == ["http.response.start", "http.response.body", "http.response.body",
                                             "http.response.body"]
    assert not caplog.records


def test_with_no_send_timeout_the_closing_chunk_is_passed_through_unbounded(rec):
    """``send_timeout=None`` (sse-starlette's default) means no bound at all: the wrapper does not invent one."""
    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        task = asyncio.ensure_future(call_response(stream, send=stalling_on_the_closing_chunk))
        await asyncio.sleep(0.3)
        # still waiting, lease already free
        assert not task.done() and rec.backend.inflight == 0 and drain.DRAIN.active == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(main)
    assert len(rec.settled) == 1


@pytest.mark.parametrize("error", [RuntimeError("socket gone"), TimeoutError("not the send timeout")],
                         ids=["runtime error", "plain TimeoutError"])
def test_any_other_send_failure_still_propagates_after_finalize(rec, caplog, error):
    """Only sse-starlette's ``SendTimeoutError`` is swallowed: any other exception (a plain ``TimeoutError`` included)
    is the ASGI server's to log, after ``finalize`` has run."""
    closed = []
    caplog.set_level("WARNING")

    async def failing_send(message):
        if message["type"] != "http.response.start":
            raise error

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DELTA_1, then=anyio.sleep_forever, closed=closed))
        finalized = spy_on_finalize(stream, caplog)
        with pytest.raises(type(error)) as raised:
            await call_response(stream, send=failing_send)
        assert raised.value is error and drain.DRAIN.active == 0 and closed == [True]
        return finalized

    finalized = run(main)
    assert len(finalized) == 1 and rec.charges() == [ABANDONED]
    assert not [r for r in caplog.records if "SendTimeoutError" in r.getMessage()]


def test_the_logger_name_is_the_one_the_operator_filters_on(rec, caplog):
    caplog.set_level("INFO")

    async def main():
        st = make_state(rec)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        await drain_events(stream)
        await stream.finalize()

    run(main)
    assert any(r.name == "semigraph.serve" and r.getMessage().startswith("answered ") for r in caplog.records)
    assert logging.getLogger("semigraph.serve").propagate


# ----------------------------------------------------------- M5a closeout: a slow first event does not cost the lease

def test_a_lease_whose_twin_is_slow_to_its_first_event_is_not_swept_holds_its_slot_and_settles_done_once(
        rec, monkeypatch):
    """The real backend, a lease TTL of 5 s and a twin that yields nothing for 20 s: the sweep must leave the lease alone
    (before the fix it reclaimed it, a second stream was admitted next to it and the first was ledgered abandoned)."""
    monkeypatch.setattr(store, "get_policy", lambda driver, key: None)               # the kill level reads off
    clock, ledger = FakeClock(), InMemoryLedger()
    settings = state_settings(max_concurrent_answers=1, lease_ttl_s=5.0, lease_renew_s=1.0)
    real = make_backend(settings, StateDrivers(state=object()), ledger=ledger, wall=clock.wall, clock=clock.mono)
    real.refresh_kill_level()

    def reserve(ip):
        return real.reserve(ip_hash=ip, strategy="hybrid", workspace=False, estimate_micro=60_000,
                            now_wall=clock.wall(), now_mono=clock.mono())

    async def main():
        st = make_state(rec)
        st.state = real
        entered, first_event = asyncio.Event(), asyncio.Event()

        async def slow_twin(question, driver, embedder, **kw):
            entered.set()
            await first_event.wait()
            yield RETRIEVAL
            yield DONE

        lease = reserve("a")
        drain.DRAIN.enter()
        stream = PaidStream(st, Q, "hybrid", "a", "snap-1", None, twin=slow_twin, lease=lease)
        task = asyncio.ensure_future(drain_events(stream))
        await until(entered.is_set)                                         # the twin is running, yielding nothing yet
        clock.advance(20)                                                   # four lease TTLs, and still no event
        assert real.sweep(clock.wall()) == 0
        assert reserve("b") is Denied.INFLIGHT
        first_event.set()
        assert [e["event"] for e in await task] == ["retrieval", "done"]
        await stream.finalize()

    run(main)
    (row,) = ledger.rows.values()
    assert (row["status"], row["outcome"]) == ("settled", "done") and real.snapshot()["inflight"] == 0


# ---------------------------------------------------------------- M5a closeout: the wait for a state slot

def holding(limiter, *names):
    async def take():
        for name in names:
            await limiter.acquire_on_behalf_of(name)
    return take()


# The slot-wait tests are the ones a regression turns into a hang (an unbounded wait for a token: a reverted run sat for
# 240 s, and the unit job has no timeout of its own), so each runs under a deadline of its own, a few times what the
# slowest of them needs and far below the 20 s of the other tests here. A regression fails them in seconds.
SLOT_TEST_BOUND_S = 5.0


def run_bounded(main):
    return run(main, timeout=SLOT_TEST_BOUND_S)


def test_slot_call_runs_the_work_on_a_worker_thread_under_the_token_and_gives_it_back():
    async def main():
        limiter, seen = anyio.CapacityLimiter(1), []
        result = await slot_call(limiter, 1.0, lambda a, b=0: (seen.append((threading.get_ident(),
                                                                          limiter.borrowed_tokens)), a + b)[1], 1, b=2)
        assert result == 3 and limiter.borrowed_tokens == 0
        ((ident, borrowed),) = seen
        assert ident != MAIN_THREAD and borrowed == 1

    run_bounded(main)


def test_slot_call_a_deadline_only_bounds_the_wait_for_the_slot_never_the_work():
    async def main():
        limiter = anyio.CapacityLimiter(1)
        assert await slot_call(limiter, 0.05, lambda: (time.sleep(0.3), "finished")[1]) == "finished"
        assert limiter.borrowed_tokens == 0

    run_bounded(main)


def test_slot_call_gives_up_with_state_unavailable_when_no_slot_frees_in_time_and_never_runs_the_work():
    async def main():
        limiter, ran = anyio.CapacityLimiter(1), []
        await limiter.acquire_on_behalf_of("holder")
        started = time.perf_counter()
        with pytest.raises(StateUnavailable, match="slot"):
            await slot_call(limiter, 0.1, ran.append, 1)
        assert 0.09 <= time.perf_counter() - started < 0.5
        assert ran == [] and limiter.borrowed_tokens == 1 and limiter.statistics().tasks_waiting == 0
        limiter.release_on_behalf_of("holder")
        assert await slot_call(limiter, 0.1, lambda: "free again") == "free again"

    run_bounded(main)


def test_slot_call_a_cancellation_while_waiting_leaves_the_limiter_exactly_as_it_was():
    async def main():
        limiter, ran = anyio.CapacityLimiter(1), []
        await limiter.acquire_on_behalf_of("holder")
        task = asyncio.ensure_future(slot_call(limiter, None, ran.append, 1))
        await until(lambda: limiter.statistics().tasks_waiting == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ran == [] and limiter.borrowed_tokens == 1 and limiter.statistics().tasks_waiting == 0
        limiter.release_on_behalf_of("holder")
        assert limiter.borrowed_tokens == 0

    run_bounded(main)


def test_slot_call_hands_the_slot_on_when_the_waiter_that_was_next_is_cancelled_in_the_instant_it_was_granted():
    """The release wakes the first waiter; if that waiter is cancelled before it runs, the slot must go to the next."""
    async def main():
        limiter, ran = anyio.CapacityLimiter(1), []
        await limiter.acquire_on_behalf_of("holder")
        first = asyncio.ensure_future(slot_call(limiter, None, ran.append, "first"))
        await until(lambda: limiter.statistics().tasks_waiting == 1)
        second = asyncio.ensure_future(slot_call(limiter, None, ran.append, "second"))
        await until(lambda: limiter.statistics().tasks_waiting == 2)
        limiter.release_on_behalf_of("holder")                                # wakes ``first``
        first.cancel()                                                        # ... which never gets to run
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        assert ran == ["second"] and limiter.borrowed_tokens == 0

    run_bounded(main)


@pytest.mark.parametrize("path", ["returns", "raises", "scope cancelled while the work runs"])
def test_slot_call_releases_the_slot_it_was_granted_on_every_path(path):
    async def main():
        limiter, started, finish = anyio.CapacityLimiter(1), threading.Event(), threading.Event()
        scope = anyio.CancelScope()                                         # what a disconnect or a deadline cancels

        def work():
            started.set()
            finish.wait(5)
            if path == "raises":
                raise ValueError("the work failed")
            return "done"

        async def call():
            with scope:
                return await slot_call(limiter, 0.5, work)

        task = asyncio.ensure_future(call())
        await until(started.is_set)
        assert limiter.borrowed_tokens == 1
        if path.startswith("scope cancelled"):
            scope.cancel()
            await asyncio.sleep(0.05)
            assert limiter.borrowed_tokens == 1 and not task.done()         # the thread is not abandoned: it keeps it
        finish.set()
        if path == "raises":
            with pytest.raises(ValueError, match="the work failed"):
                await task
        else:
            # a cancelled call returns nothing: the cancellation is delivered once the work has finished
            assert await task == ("done" if path == "returns" else None)
            assert scope.cancelled_caught == path.startswith("scope cancelled")
        assert limiter.borrowed_tokens == 0

    run_bounded(main)


def test_a_state_call_gives_up_when_every_slot_is_held_for_the_state_op_timeout_and_a_settle_does_not(rec):
    """``state_call`` is for the callers that have a refusal to fall back on; a settle has none, and a lease whose
    reconcile gave up would stay registered, be renewed for ever and hold its in-flight slot."""
    async def main():
        st = make_state(rec)
        st.settings.state_op_timeout_s = 0.05
        state = st.limiters.state
        names = [f"held-{n}" for n in range(int(state.total_tokens))]
        await holding(state, *names)
        with pytest.raises(StateUnavailable):
            await state_call(st, rec.backend.cache_get, "key", 24)
        stream = stream_of(st, twin_of(RETRIEVAL, DONE))
        task = asyncio.ensure_future(drain_events(stream))
        await until(lambda: state.statistics().tasks_waiting == 1)             # at its reconcile
        await asyncio.sleep(0.25)                                              # five times the bound of a state call
        assert not task.done() and rec.charges() == []
        for name in names:
            state.release_on_behalf_of(name)
        assert [e["event"] for e in await task] == ["retrieval", "done"]
        await stream.finalize()
        assert state.borrowed_tokens == 0

    run_bounded(main)
    assert rec.charges() == [SPEND] and len(rec.puts) == 1


def test_an_admin_call_has_its_own_limiter_and_gives_up_at_the_same_bound_on_it():
    async def main():
        st = SimpleNamespace(settings=SimpleNamespace(state_op_timeout_s=0.1),
                             limiters=SimpleNamespace(state=anyio.CapacityLimiter(1), admin=anyio.CapacityLimiter(1)))
        await holding(st.limiters.state, "busy")                                # the public pool is full ...
        assert await admin_call(st, lambda: "flipped") == "flipped"             # ... and the admin does not notice
        await holding(st.limiters.admin, "another admin call")
        with pytest.raises(StateUnavailable):
            await admin_call(st, lambda: "never")

    run_bounded(main)
