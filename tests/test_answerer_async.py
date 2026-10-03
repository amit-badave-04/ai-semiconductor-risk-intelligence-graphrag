"""M5a I2: the async twin of the SEC-path answer writer (``retrieval/answerer_async.py``, docs/v2/M5A_BUILD_PLAN.md
section 4).

The twin exists so a stream holds no thread while it waits for the model. Its contract is PARITY with the sync
writer: for the same inputs it yields event dicts equal to what ``answerer.stream_answer_for_prompt`` /
``answer_stream`` yield (same keys, same order, same values), and ``AsyncTextStream`` behaves exactly like
``TextStream`` (same retries, same error texts, same usage handling). On top of that it must never block the event
loop, and it must close the upstream model stream on EVERY exit: a visitor who disconnects mid-answer must not leave
a paid provider connection open.

No network, no model, no Neo4j: every model call is a scripted fake and the graph is
``tests/agent_fakes.FakeDriver``. This file runs in the CI ``serve-shipped`` job (no pandas, sentence-transformers
or neo4j): ``test_the_async_answer_path_imports_nothing_heavy`` pins that. There is no pytest-asyncio; every
scenario is an ordinary sync test around ``asyncio.run`` with a hard timeout so a bug cannot hang the suite.
"""

import ast
import asyncio
import subprocess
import sys
import threading
import time
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import anyio
import litellm
import pytest
from agent_fakes import FakeDriver, FakeEmbedder

from semigraph.retrieval import answerer, answerer_async
from semigraph.retrieval.answerer import TextStream, answer_stream, stream_answer_for_prompt
from semigraph.retrieval.answerer_async import AsyncTextStream
from semigraph.retrieval.retriever import hybrid_retrieve, vector_retrieve
from semigraph.serve.limiters import LoopLagMonitor, make_limiters

CHEAP, STRONG = "openai/gpt-6-luna", "anthropic/claude-sonnet-5"
USAGE = {"prompt_tokens": 1200, "completion_tokens": 40}
HARD_TIMEOUT_S = 20


# --- plumbing -------------------------------------------------------------------------------------------------------

def run(scenario, timeout: float = HARD_TIMEOUT_S):
    """Run ``scenario`` (a zero-argument async function) on a fresh loop, failing instead of hanging."""
    async def guarded():
        with anyio.fail_after(timeout):
            return await scenario()
    return asyncio.run(guarded())


async def collect(agen) -> list[dict]:
    async with aclosing(agen) as events:
        return [event async for event in events]


async def tick(delay: float = 0.0) -> None:
    # deliberately not anyio.sleep: tests patch that to record the stream's backoff waits
    await asyncio.sleep(delay)


def ordered(events: list[dict]) -> list[list]:
    """Events with their key order kept (``dict ==`` ignores it, and the wire format does not)."""
    return [list(event.items()) for event in events]


def make_limiters_here(db: int = 4):
    return make_limiters(SimpleNamespace(embed_slots=1, db_thread_limit=db))


# --- provider-shaped fakes ------------------------------------------------------------------------------------------

def chunk(delta=None, finish=None, usage=None, *, choices: bool = True):
    """A litellm streaming chunk: ``choices[0].delta.content`` / ``finish_reason`` and an optional ``usage``
    (prompt tokens, completion tokens)."""
    choice = SimpleNamespace(delta=SimpleNamespace(content=delta), finish_reason=finish)
    return SimpleNamespace(choices=[choice] if choices else [],
                           usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None)


def plain():
    return [chunk("Hello "), chunk("world", "stop"), chunk(usage=(10, 2), choices=False)]


def conn_error():
    return litellm.APIConnectionError(message="reset", llm_provider="openai", model=CHEAP)


def rate_error():
    return litellm.RateLimitError(message="slow down", llm_provider="openai", model=CHEAP)


class FakeUpstream:
    """What ``litellm.acompletion(stream=True)`` returns: an async iterator with ``aclose``. ``closed`` counts closes
    that COMPLETED: ``aclose`` has a real checkpoint first, so a task whose cancellation is not shielded never gets
    past it."""

    def __init__(self, items, *, pause: float = 0.0, close_delay: float = 0.01, close_error: Exception | None = None):
        self.items, self.pause, self.close_delay, self.close_error = list(items), pause, close_delay, close_error
        self.aclose_calls = self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await tick(self.pause)
        if not self.items:
            raise StopAsyncIteration
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self):
        self.aclose_calls += 1
        await tick(self.close_delay)
        if self.close_error:
            raise self.close_error
        self.closed += 1


class FakeAcompletion:
    """Stands in for ``litellm.acompletion``: one scripted outcome per call (an upstream, or an exception to raise)."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def fake_builder(monkeypatch, *, fail: bool = False):
    """``litellm.stream_chunk_builder`` without a tokenizer: a fixed estimate, or a failure."""
    def build(chunks, messages=None):
        if fail:
            raise ValueError("no tokenizer")
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3))
    monkeypatch.setattr(litellm, "stream_chunk_builder", build)


# --- AsyncTextStream against TextStream: the same script through both -----------------------------------------------

@dataclass(frozen=True)
class Seen:
    deltas: tuple
    text: str
    finish_reason: str | None
    usage: dict | None
    error: str | None
    cause: str | None
    calls: tuple
    waits: tuple


# name -> (outcomes factory, TextStream kwargs, expected error text). An outcome is a list of chunks (an exception in
# it is raised when the stream reaches it) or an exception raised by the model call itself.
STREAM_CASES = {
    "plain": (lambda: [plain()], {}, None),
    "transient_on_the_call_then_ok": (lambda: [conn_error(), plain()], {"backoff": (7, 9)}, None),
    "transient_before_any_text_then_ok": (lambda: [[rate_error()], plain()], {"backoff": (7, 9)}, None),
    "transient_exhausted": (lambda: [conn_error(), conn_error()], {"backoff": (7, 9)},
                            "RuntimeError: streaming answer failed after 2 attempts — last error: "
                            "transient: APIConnectionError"),
    "three_attempts_walk_the_backoff_tuple": (lambda: [conn_error()] * 3, {"attempts": 3, "backoff": (7,)},
                                              "RuntimeError: streaming answer failed after 3 attempts — last error: "
                                              "transient: APIConnectionError"),
    "empty_then_ok": (lambda: [[chunk(None, "stop")], plain()], {"backoff": (7,)}, None),
    "empty_exhausted": (lambda: [[chunk(None, "length")], [chunk(None, "length")]], {},
                        "RuntimeError: streaming answer failed after 2 attempts — last error: "
                        "empty response (finish_reason=length)"),
    "mid_stream_transient": (lambda: [[chunk("Hel"), rate_error()]], {},
                             "RuntimeError: stream interrupted mid-answer: RateLimitError"),
    "non_transient_on_the_call": (lambda: [ValueError("bad request")], {}, "ValueError: bad request"),
    "non_transient_mid_stream": (lambda: [[chunk("Hel"), KeyError("boom")]], {}, "KeyError: 'boom'"),
    "usage_estimated": (lambda: [[chunk("Hi "), chunk("there", "stop")]], {}, None),
    "usage_unavailable": (lambda: [[chunk("Hi "), chunk("there", "stop")]], {"fail_builder": True}, None),
    "truncated_is_returned_not_regenerated": (lambda: [[chunk("cut"), chunk(None, "length")]], {}, None),
    "options_reach_the_model_call": (lambda: [plain()], {"timeout": 12.5, "num_retries": 0, "max_tokens": 77}, None),
}


def _stream_kwargs(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if k != "fail_builder"}


class _SyncUpstream:
    def __init__(self, items):
        self.items = list(items)

    def __iter__(self):
        for item in self.items:
            if isinstance(item, BaseException):
                raise item
            yield item


def observe_sync(monkeypatch, outcomes, kwargs) -> Seen:
    fake_builder(monkeypatch, fail=kwargs.get("fail_builder", False))
    calls, waits, queue = [], [], list(outcomes)

    def completion(**kw):
        calls.append(kw)
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _SyncUpstream(outcome)

    monkeypatch.setattr(answerer, "completion", completion)
    monkeypatch.setattr(time, "sleep", waits.append)
    stream = TextStream("PROMPT", model=CHEAP, **_stream_kwargs(kwargs))
    deltas, error, cause = [], None, None
    try:
        deltas.extend(stream)
    except Exception as e:  # noqa: BLE001 - the error text IS the observation
        error, cause = f"{type(e).__name__}: {e}", type(e.__cause__).__name__
    return Seen(tuple(deltas), stream.text, stream.finish_reason, stream.usage, error, cause, tuple(map(repr, calls)),
                tuple(waits))


def observe_async(monkeypatch, outcomes, kwargs) -> Seen:
    fake_builder(monkeypatch, fail=kwargs.get("fail_builder", False))
    waits = []
    fake = FakeAcompletion(*[o if isinstance(o, BaseException) else FakeUpstream(o) for o in outcomes])

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(answerer_async, "acompletion", fake)
    monkeypatch.setattr(anyio, "sleep", sleep)

    async def go():
        stream = AsyncTextStream("PROMPT", model=CHEAP, **_stream_kwargs(kwargs))
        deltas, error, cause = [], None, None
        try:
            async with aclosing(aiter(stream)) as it:
                async for delta in it:
                    deltas.append(delta)
        except Exception as e:  # noqa: BLE001
            error, cause = f"{type(e).__name__}: {e}", type(e.__cause__).__name__
        return Seen(tuple(deltas), stream.text, stream.finish_reason, stream.usage, error, cause,
                    tuple(map(repr, fake.calls)), tuple(waits))
    return run(go)


@pytest.mark.parametrize("name", list(STREAM_CASES))
def test_async_text_stream_behaves_exactly_like_text_stream(monkeypatch, name):
    outcomes, kwargs, expected_error = STREAM_CASES[name]
    sync_seen = observe_sync(monkeypatch, outcomes(), kwargs)
    async_seen = observe_async(monkeypatch, outcomes(), kwargs)
    assert async_seen == sync_seen
    assert async_seen.error == expected_error          # the two agreeing on a wrong result would pass the line above


def test_the_stream_reports_text_finish_reason_and_provider_usage(monkeypatch):
    seen = observe_async(monkeypatch, [plain()], {})
    assert (seen.deltas, seen.text, seen.finish_reason) == (("Hello ", "world"), "Hello world", "stop")
    assert seen.usage == {"prompt_tokens": 10, "completion_tokens": 2}      # no ``estimated``: the provider reported it


def test_missing_provider_usage_is_estimated_and_flagged(monkeypatch):
    seen = observe_async(monkeypatch, [[chunk("Hi "), chunk("there", "stop")]], {})
    assert seen.usage == {"prompt_tokens": 11, "completion_tokens": 3, "estimated": True}
    assert observe_async(monkeypatch, [[chunk("Hi"), chunk(None, "stop")]], {"fail_builder": True}).usage is None


def test_the_real_chunk_builder_estimates_from_real_litellm_chunks(monkeypatch):
    """No fakes here: litellm's own chunk type and its own ``stream_chunk_builder``, offline, so a change in either
    shows up."""
    from litellm import ModelResponseStream
    from litellm.types.utils import Delta, StreamingChoices

    def real(text=None, finish=None):
        return ModelResponseStream(id="x", model=CHEAP, choices=[StreamingChoices(delta=Delta(content=text),
                                                                                     finish_reason=finish, index=0)])

    monkeypatch.setattr(answerer_async, "acompletion", FakeAcompletion(FakeUpstream([real("Hello "), real("world"),
                                                                                       real(None, "stop")])))

    async def go():
        stream = AsyncTextStream("say hello", model=CHEAP)
        return stream, [d async for d in stream]

    stream, deltas = run(go)
    assert deltas == ["Hello ", "world"] and stream.finish_reason == "stop"
    assert stream.usage["estimated"] is True
    assert stream.usage["completion_tokens"] > 0 and stream.usage["prompt_tokens"] > 0


def test_the_model_call_gets_the_sync_writers_exact_arguments(monkeypatch):
    """completion_params is the single source of the budget argument: the answer model REJECTS a bare ``max_tokens``
    (live probe 2026-10-03: "use max_completion_tokens"), so a gpt-6 model gets ``max_completion_tokens`` and no
    ``max_tokens``."""
    def kwargs_sent(model, **stream_kw):
        fake = FakeAcompletion(FakeUpstream(plain()))
        monkeypatch.setattr(answerer_async, "acompletion", fake)

        async def go():
            return [d async for d in AsyncTextStream("PROMPT", model=model, **stream_kw)]

        run(go)
        return fake.calls[0]

    luna = kwargs_sent(CHEAP)
    assert luna == {"model": CHEAP, "messages": [{"role": "user", "content": "PROMPT"}], "max_completion_tokens": 1200,
                    "num_retries": 2, "stream": True, "stream_options": {"include_usage": True}}
    assert "max_tokens" not in luna
    sonnet = kwargs_sent(STRONG, max_tokens=2400, timeout=90, num_retries=0)
    assert sonnet == {"model": STRONG, "messages": [{"role": "user", "content": "PROMPT"}], "max_tokens": 2400,
                      "thinking": {"type": "disabled"}, "allowed_openai_params": ["thinking"], "num_retries": 0,
                      "stream": True, "stream_options": {"include_usage": True}, "timeout": 90}


def test_the_constructor_keeps_the_sync_writers_defaults_and_attributes():
    s, a = TextStream("p", model=CHEAP), AsyncTextStream("p", model=CHEAP)
    names = ("prompt", "model", "max_tokens", "attempts", "backoff", "timeout", "num_retries", "finish_reason", "usage",
             "text")
    assert {n: getattr(a, n) for n in names} == {n: getattr(s, n) for n in names}


def test_backoff_waits_are_awaited_not_slept_and_skipped_after_the_last_attempt(monkeypatch):
    seen = observe_async(monkeypatch, [conn_error()] * 3, {"attempts": 3, "backoff": (7, 9)})
    assert seen.waits == (7, 9)                              # 3 attempts: two waits, none after the third
    assert observe_async(monkeypatch, [conn_error()], {"attempts": 1, "backoff": (7,)}).waits == ()


# --- the upstream is closed on EVERY exit ---------------------------------------------------------------------------

def make_stream(monkeypatch, *outcomes, **kwargs):
    fake = FakeAcompletion(*outcomes)
    monkeypatch.setattr(answerer_async, "acompletion", fake)
    return AsyncTextStream("PROMPT", model=CHEAP, **kwargs), fake


def test_the_upstream_is_closed_after_a_normal_end(monkeypatch):
    up = FakeUpstream(plain())
    stream, _ = make_stream(monkeypatch, up)

    async def go():
        return [d async for d in stream]

    assert run(go) == ["Hello ", "world"]
    assert (up.aclose_calls, up.closed) == (1, 1)


def test_the_upstream_is_closed_after_an_exception(monkeypatch):
    up = FakeUpstream([chunk("Hel"), ValueError("provider exploded")])
    stream, _ = make_stream(monkeypatch, up)

    async def go():
        return [d async for d in stream]

    with pytest.raises(ValueError, match="provider exploded"):
        run(go)
    assert (up.aclose_calls, up.closed) == (1, 1)


def test_every_retried_attempt_closes_its_own_upstream(monkeypatch):
    first, second = FakeUpstream([rate_error()]), FakeUpstream(plain())
    stream, _ = make_stream(monkeypatch, first, second, backoff=(0,))

    async def go():
        return [d async for d in stream]

    assert run(go) == ["Hello ", "world"]
    assert [(u.aclose_calls, u.closed) for u in (first, second)] == [(1, 1), (1, 1)]


def test_closing_the_generator_early_closes_the_upstream_at_once(monkeypatch):
    up = FakeUpstream(plain(), pause=0.01)
    stream, _ = make_stream(monkeypatch, up)

    async def go():
        agen = aiter(stream)
        assert await anext(agen) == "Hello "
        await agen.aclose()
        assert up.closed == 1                                                # at once: no loop tick in between
        await agen.aclose()                                                  # idempotent

    run(go)
    assert up.aclose_calls == 1


def test_breaking_out_of_an_aclosing_loop_closes_the_upstream_at_once(monkeypatch):
    up = FakeUpstream(plain(), pause=0.01)
    stream, _ = make_stream(monkeypatch, up)

    async def go():
        async with aclosing(aiter(stream)) as it:
            async for _ in it:
                break
        assert up.closed == 1

    run(go)


def test_a_bare_break_is_cleaned_up_by_the_loops_generator_finalizer(monkeypatch):
    """``break`` does not close an async generator synchronously: the loop's asyncgen hook schedules ``aclose()``. The
    upstream must still be released then (this is why the real consumers use ``aclosing`` and do not rely on it)."""
    up = FakeUpstream(plain(), pause=0.01)
    stream, _ = make_stream(monkeypatch, up)

    async def go():
        async for _ in stream:
            break
        for _ in range(100):
            if up.closed:
                break
            await tick(0.01)
        assert up.closed == 1

    run(go)


def test_aclose_closes_the_open_upstream_once_and_never_raises(monkeypatch):
    up = FakeUpstream(plain(), pause=0.01, close_error=RuntimeError("connection already gone"))
    stream, _ = make_stream(monkeypatch, up)

    async def go():
        await stream.aclose()                                                # nothing open yet: a no-op
        agen = aiter(stream)
        await anext(agen)
        await stream.aclose()                                                # the upstream raises on close: swallowed
        await stream.aclose()                                                # idempotent
        await agen.aclose()

    run(go)
    assert up.aclose_calls == 1


def test_the_upstream_is_closed_when_the_consuming_scope_is_cancelled_mid_stream(monkeypatch):
    """What Starlette / sse-starlette do on a disconnect: cancel the scope the consumer runs in. The fake's ``aclose``
    has a real checkpoint before it records the close, so this only passes because the close runs in a shielded scope: a
    level-triggered anyio cancellation would otherwise raise at that checkpoint."""
    up = FakeUpstream(plain(), pause=0.05)
    stream, _ = make_stream(monkeypatch, up)

    async def go():
        seen = anyio.Event()

        async def consume():
            async for _ in stream:
                seen.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await seen.wait()
            tg.cancel_scope.cancel()

    run(go)
    assert (up.aclose_calls, up.closed) == (1, 1)


def test_the_upstream_is_closed_after_a_single_native_cancel_of_the_consuming_task(monkeypatch):
    """A bare ``task.cancel()`` is delivered once (it is not level-triggered like an anyio scope), so one cancel that
    lands while the stream waits for its next chunk still leaves the close to run to its end. That is all a native
    cancel promises: the module docstring calls it best effort, and the next test shows where it stops holding."""
    up = FakeUpstream(plain(), pause=0.05)
    stream, _ = make_stream(monkeypatch, up)

    async def go():
        seen = asyncio.Event()

        async def consume():
            async for _ in stream:
                seen.set()

        task = asyncio.create_task(consume())
        await seen.wait()
        task.cancel()
        await asyncio.wait({task})
        return task

    assert run(go).cancelled()
    assert (up.aclose_calls, up.closed) == (1, 1)


def test_a_second_native_cancel_landing_inside_the_close_cuts_it_short(monkeypatch):
    """The documented limit of a bare ``task.cancel()``: the close runs in an anyio shield, which only anyio's own scope
    cancellation honours, so a second ``cancel()`` that lands while the close is waiting interrupts it and the provider
    connection stays open. Production cancels with an anyio scope and closes under a shield in the finalize, so it never
    gets here; if this test ever fails because the close became immune, update the module docstring with it."""
    up = FakeUpstream(plain(), pause=0.05, close_delay=0.3)
    stream, _ = make_stream(monkeypatch, up)

    async def go():
        seen = asyncio.Event()

        async def consume():
            async for _ in stream:
                seen.set()

        task = asyncio.create_task(consume())
        await seen.wait()
        task.cancel()
        while not up.aclose_calls:                                           # the close has begun and is waiting
            await tick(0.005)
        task.cancel()
        await asyncio.wait({task})
        return task

    assert run(go).cancelled()
    assert (up.aclose_calls, up.closed) == (1, 0)


# --- the whole chain closes from the OUTERMOST generator (what the route's finalize does) ---------------------------

Q_SEC = "Who supplies Nvidia's HBM?"
Q_CHANGE = "How have Nvidia's supply-chain risk factors changed over time?"


def test_closing_the_outermost_generator_closes_the_upstream_at_once(monkeypatch):
    """No injected stream: ``aanswer_stream`` -> context -> prompt -> live events -> ``AsyncTextStream`` -> upstream.
    Every layer has to close the one below it (a bare ``async for`` would leave it to the garbage collector)."""
    up = FakeUpstream(plain(), pause=0.01)
    fake = FakeAcompletion(up)
    monkeypatch.setattr(answerer_async, "acompletion", fake)

    async def go():
        outer = answerer_async.aanswer_stream(Q_SEC, FakeDriver.world(), FakeEmbedder(), limiters=make_limiters_here(),
                                              model=CHEAP)
        kinds = []
        async for event in outer:
            kinds.append(event["event"])
            if event["event"] == "delta":
                break
        await outer.aclose()
        assert up.closed == 1                                                # at once, no loop tick
        return kinds

    assert run(go) == ["retrieval", "delta"]
    assert up.aclose_calls == 1 and fake.calls[0]["model"] == CHEAP


def test_closing_the_outermost_generator_mid_escalated_answer_closes_both_upstreams(monkeypatch):
    draft = FakeUpstream([chunk("Nvidia revenue was $215.9 billion.", "stop"), chunk(usage=(900, 20), choices=False)])
    strong = FakeUpstream([chunk("one "), chunk("two "), chunk("three", "stop")], pause=0.01)
    fake = FakeAcompletion(draft, strong)
    monkeypatch.setattr(answerer_async, "acompletion", fake)

    async def go():
        outer = answerer_async.aanswer_stream(Q_SEC, FakeDriver.world(), FakeEmbedder(), limiters=make_limiters_here(),
                                              escalation_model=STRONG, model=CHEAP, timeout=90, max_tokens=2400)
        kinds = []
        async for event in outer:
            kinds.append(event["event"])
            if event["event"] == "delta":
                break
        await outer.aclose()
        return kinds

    assert run(go) == ["retrieval", "escalated", "delta"]
    assert (draft.closed, strong.closed) == (1, 1)
    # the default construction path: a short fuse for the draft, the full timeout and retries for the strong model
    draft_kw, strong_kw = fake.calls
    assert (draft_kw["model"], draft_kw["num_retries"], draft_kw["timeout"], draft_kw["max_completion_tokens"]) == (
        CHEAP, 0, answerer.DRAFT_TIMEOUT_S, 2400)
    assert (strong_kw["model"], strong_kw["num_retries"], strong_kw["timeout"], strong_kw["max_tokens"]) == (
        STRONG, 2, 90, 2400)


def test_cancelling_the_task_that_consumes_the_outermost_generator_closes_the_upstream(monkeypatch):
    up = FakeUpstream([chunk("a "), chunk("b "), chunk("c", "stop")], pause=0.05)
    monkeypatch.setattr(answerer_async, "acompletion", FakeAcompletion(up))

    async def go():
        seen = anyio.Event()

        async def consume():
            async for event in answerer_async.aanswer_stream(Q_SEC, FakeDriver.world(), FakeEmbedder(),
                                                              limiters=make_limiters_here(), model=CHEAP):
                if event["event"] == "delta":
                    seen.set()

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await seen.wait()
            tg.cancel_scope.cancel()

    run(go)
    assert (up.aclose_calls, up.closed) == (1, 1)


class ClosableIterator:
    """The iterator of an injected stream that releases something on ``aclose``: a real checkpoint, no shield.
    ``aclose_calls`` counts the closes that began, ``closed`` those that completed; ``close_error`` makes the close fail
    (after its checkpoint) and ``fail`` makes the iteration fail once the parts are delivered."""

    def __init__(self, parts, *, close_error: Exception | None = None, fail: Exception | None = None):
        self.parts, self.close_error, self.fail = list(parts), close_error, fail
        self.aclose_calls = self.closed = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        await tick()
        if not self.parts:
            if self.fail:
                raise self.fail
            raise StopAsyncIteration
        return self.parts.pop(0)

    async def aclose(self):
        self.aclose_calls += 1
        await tick(0.01)
        if self.close_error:
            raise self.close_error
        self.closed += 1


class ClosableStream:
    model, usage, finish_reason = CHEAP, None, "stop"

    def __init__(self, iterator):
        self.iterator = iterator

    def __aiter__(self):
        return self.iterator


def test_an_unshielded_aclose_of_the_writer_still_releases_an_injected_streams_iterator():
    """The consumer is cancelled while it waits on its OWN await (a slow client) and its cleanup calls ``aclose()`` on
    the writer in the cancelled scope without a shield of its own: the iterator of an injected stream, which has no
    shield either, must still be closed to the end."""
    iterator = ClosableIterator(["a ", "b ", "c"])

    async def go():
        seen = anyio.Event()

        async def consume():
            events = answerer_async.astream_answer_for_prompt(
                Q, PROMPT, CONTEXT, {DOC}, [DOC], "hybrid", sources=SOURCES,
                llm_stream=lambda p: ClosableStream(iterator))
            try:
                async for _ in events:
                    seen.set()
                    await anyio.sleep(10)
            finally:
                await events.aclose()

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await seen.wait()
            tg.cancel_scope.cancel()

    run(go)
    assert iterator.closed == 1


# --- the event helpers ----------------------------------------------------------------------------------------------

class SyncOnly:
    """A TextStream look-alike that is only iterable the sync way."""
    model, usage, finish_reason = CHEAP, None, "stop"

    def __iter__(self):
        yield "x"


@dataclass(frozen=True)
class Script:
    """One scripted model answer, turned into a sync or an async stream double so both writers get the SAME script."""

    parts: tuple[str, ...]
    model: str = CHEAP
    usage: dict | None = field(default_factory=lambda: dict(USAGE))
    finish_reason: str = "stop"
    fail: Exception | None = None

    def with_id(self, cid: str) -> "Script":
        return replace(self, parts=tuple(p.replace("<CID>", cid) for p in self.parts))


class SyncFake:
    def __init__(self, script: Script):
        self.parts, self.model, self.usage = script.parts, script.model, script.usage
        self.finish_reason, self.fail = script.finish_reason, script.fail

    def __iter__(self):
        yield from self.parts
        if self.fail:
            raise self.fail


class AsyncFake:
    def __init__(self, script: Script, *, pause: float = 0.0):
        self.parts, self.model, self.usage = script.parts, script.model, script.usage
        self.finish_reason, self.fail, self.pause = script.finish_reason, script.fail, pause

    async def __aiter__(self):
        for part in self.parts:
            await tick(self.pause)
            yield part
        await tick(self.pause)
        if self.fail:
            raise self.fail


def test_drain_returns_the_text_and_the_error_description():
    async def go():
        return (await answerer_async._adrain(AsyncFake(Script(("a", "b")))),
                await answerer_async._adrain(AsyncFake(Script(("a",), fail=RuntimeError("boom")))))

    assert run(go) == (("ab", None), ("a", "RuntimeError: boom"))


def test_a_sync_stream_behind_the_async_writer_is_a_loud_type_error_not_a_swallowed_draft_error():
    """The sync writer reports a failing draft as ``draft_error`` and escalates. A sync iterable handed to the async
    writer is a wiring mistake, not a provider failure: it must not turn into a silent escalation."""
    async def drain():
        return await answerer_async._adrain(SyncOnly())

    with pytest.raises(TypeError, match="async"):
        run(drain)

    async def live():
        return await collect(answerer_async.astream_answer_for_prompt(
            Q, PROMPT, CONTEXT, {DOC}, [DOC], "hybrid", sources=SOURCES, llm_stream=lambda p: SyncOnly()))

    with pytest.raises(TypeError, match="async"):
        run(live)


def closable_events(iterator: ClosableIterator, **kw):
    """The live writer over an injected stream whose iterator is ``iterator``."""
    return answerer_async.astream_answer_for_prompt(
        Q, PROMPT, CONTEXT, {DOC}, [DOC], "hybrid", sources=SOURCES, llm_stream=lambda p: ClosableStream(iterator),
        **kw)


def test_a_failing_close_of_an_injected_streams_iterator_does_not_escape_the_writer(caplog):
    """Releasing the iterator is cleanup: if it raises, the answer that was already written is still delivered, and the
    failure is only logged (the model stream's own ``aclose`` is held to the same rule)."""
    caplog.set_level("DEBUG", logger="semigraph.answerer")
    clean = ClosableIterator(GOOD_PARTS)
    broken = ClosableIterator(GOOD_PARTS, close_error=RuntimeError("connection already gone"))

    expected = run(lambda: collect(closable_events(clean)))
    assert ordered(run(lambda: collect(closable_events(broken)))) == ordered(expected)
    assert expected[-1]["event"] == "done" and (broken.aclose_calls, broken.closed) == (1, 0)
    assert any(r.levelname == "DEBUG" and "connection already gone" in r.message for r in caplog.records)


def test_a_failing_close_does_not_replace_the_streams_own_failure_or_break_a_drain():
    """The stream fails and its close fails too: the client still gets the ``error`` event for the stream's failure,
    and a silent drain still returns the text and the failure (not the close's)."""
    def failing() -> ClosableIterator:
        return ClosableIterator(GOOD_PARTS[:1], fail=TimeoutError("model stalled"),
                                close_error=RuntimeError("connection already gone"))

    events = run(lambda: collect(closable_events(failing())))
    assert [e["event"] for e in events] == ["delta", "error"]
    assert events[-1]["detail"] == "TimeoutError: model stalled" and events[-1]["partial"] == GOOD_PARTS[0]

    async def drain():
        return await answerer_async._adrain(ClosableStream(failing()))

    assert run(drain) == (GOOD_PARTS[0], "TimeoutError: model stalled")


def test_a_failed_draft_is_logged_with_its_error_so_a_revoked_key_is_not_silent(caplog):
    caplog.set_level("WARNING", logger="semigraph.answerer")
    draft = Script((), fail=RuntimeError("401 invalid api key"))

    async def go():
        return await collect(answerer_async.astream_answer_for_prompt(
            Q, PROMPT, CONTEXT, {DOC}, [DOC], "hybrid", sources=SOURCES, llm_stream=lambda p: AsyncFake(draft),
            escalation_stream=lambda p: AsyncFake(Script(GOOD_PARTS, model=STRONG)), escalation_model=STRONG,
            model=CHEAP))

    run(go)
    assert any(CHEAP in r.message and "401 invalid api key" in r.message for r in caplog.records)


def test_the_transient_retry_warning_uses_the_sync_writers_logger(monkeypatch, caplog):
    caplog.set_level("INFO", logger="semigraph.answerer")
    observe_async(monkeypatch, [conn_error(), plain()], {"backoff": (7,)})
    assert any("transient error (APIConnectionError) before first token" in r.message for r in caplog.records)


# --- the writer's tail: the same script through the sync and the async writer ---------------------------------------

DOC = "doc:0123456789ab:v1:0003"
PROMPT = "CUSTOM PROMPT"
Q = "What does my document say about margins?"
Q_CHANGED = "How has my document's margin changed over time?"
CONTEXT = f"EXCERPTS:\n[{DOC}]\nOur margin was 41.5% in Q2.\n"
SOURCES = {DOC: "Our margin was 41.5% in Q2."}
GOOD_PARTS = (f"Margin was 41.5% [{DOC}]", " in Q2.")
UNCITED = ("Margin was 41.5%.",)
BAD_NUMBER = (f"Margin was 99.9% [{DOC}].",)
LINKED = (f"Margin was 41.5% [{DOC}].", " See ![x](https://evil.test/p.png) and [here](https://evil.test).")


def strip_links(text: str) -> str:
    return text.replace(" See ![x](https://evil.test/p.png) and [here](https://evil.test).", "")


@dataclass(frozen=True)
class Scenario:
    name: str
    kinds: tuple[str, ...]
    question: str = Q
    # None = that stream must not be asked for: ``llm`` is the first one (live, or the cheap draft), ``strong`` the
    # escalation stream
    llm: Script | None = None
    strong: Script | None = None
    kw: dict = field(default_factory=dict)


ESC = {"escalation_model": STRONG, "model": CHEAP}
BUF = {"postprocess": strip_links, "force_buffered": True}

PROMPT_SCENARIOS = [
    Scenario("live_without_escalation", ("delta", "delta", "done"), llm=Script(GOOD_PARTS)),
    Scenario("clean_cheap_draft_released", ("delta", "done"), llm=Script(GOOD_PARTS), kw=ESC),
    Scenario("draft_rejected_uncited", ("escalated", "delta", "delta", "done"), llm=Script(UNCITED),
             strong=Script(GOOD_PARTS, model=STRONG), kw=ESC),
    Scenario("draft_rejected_bad_number", ("escalated", "delta", "delta", "done"), llm=Script(BAD_NUMBER),
             strong=Script(GOOD_PARTS, model=STRONG), kw=ESC),
    Scenario("routed_straight_to_the_strong_model", ("delta", "delta", "done"), question=Q_CHANGED,
             strong=Script(GOOD_PARTS, model=STRONG), kw=ESC),
    Scenario("draft_error_escalates", ("escalated", "delta", "delta", "done"),
             llm=Script((), fail=TimeoutError("draft timed out")), strong=Script(GOOD_PARTS, model=STRONG), kw=ESC),
    Scenario("error_mid_stream_live", ("delta", "error"),
             llm=Script(GOOD_PARTS[:1], fail=RuntimeError("provider reset"))),
    Scenario("error_mid_stream_after_escalation_carries_both_attempts", ("escalated", "delta", "error"),
             llm=Script(UNCITED),
             strong=Script(GOOD_PARTS[:1], model=STRONG, fail=RuntimeError("provider reset")), kw=ESC),
    Scenario("forced_buffered_strips_a_link", ("delta", "done"), llm=Script(LINKED), kw=BUF),
    Scenario("forced_buffered_after_escalation", ("escalated", "delta", "done"), llm=Script(UNCITED),
             strong=Script(LINKED, model=STRONG), kw={**ESC, **BUF}),
    Scenario("forced_buffered_routed_strong", ("delta", "done"), question=Q_CHANGED,
             strong=Script(LINKED, model=STRONG), kw={**ESC, **BUF}),
    Scenario("forced_buffered_error_reports_the_edited_partial", ("error",),
             llm=Script(LINKED, fail=RuntimeError("reset")), kw=BUF),
    Scenario("same_model_in_both_roles_collapses_to_live", ("delta", "delta", "done"), llm=Script(GOOD_PARTS),
             kw={"escalation_model": CHEAP, "model": CHEAP}),
    Scenario("estimated_usage_survives_summing_two_attempts", ("escalated", "delta", "delta", "done"),
             llm=Script(UNCITED), strong=Script(GOOD_PARTS, model=STRONG, usage={**USAGE, "estimated": True}), kw=ESC),
]


def factory(role: str, script: Script | None, calls: list, cls):
    def make(prompt):
        calls.append((role, prompt))
        if script is None:
            raise AssertionError(f"the {role} stream must not be requested in this scenario")
        return cls(script)
    return make


def prompt_args(sc: Scenario) -> tuple:
    return sc.question, PROMPT, CONTEXT, {DOC}, [DOC], "hybrid"


def sync_prompt_run(sc: Scenario) -> tuple[list[dict], list]:
    calls: list = []
    events = list(stream_answer_for_prompt(
        *prompt_args(sc), sources=SOURCES, llm_stream=factory("llm", sc.llm, calls, SyncFake),
        escalation_stream=factory("strong", sc.strong, calls, SyncFake), **sc.kw))
    return events, calls


def prompt_events(sc: Scenario, calls: list, **extra):
    return answerer_async.astream_answer_for_prompt(
        *prompt_args(sc), sources=SOURCES, llm_stream=factory("llm", sc.llm, calls, AsyncFake),
        escalation_stream=factory("strong", sc.strong, calls, AsyncFake), **sc.kw, **extra)


async def async_prompt_run(sc: Scenario, **extra) -> tuple[list[dict], list]:
    calls: list = []
    return await collect(prompt_events(sc, calls, **extra)), calls


@pytest.mark.parametrize("sc", [pytest.param(s, id=s.name) for s in PROMPT_SCENARIOS])
def test_the_async_writer_yields_exactly_the_sync_writers_events(sc):
    sync_events, sync_calls = sync_prompt_run(sc)
    async_events, async_calls = run(lambda: async_prompt_run(sc))
    assert ordered(async_events) == ordered(sync_events)              # same keys, same order, same values
    assert async_calls == sync_calls                                  # and the same models, asked in the same order
    assert tuple(e["event"] for e in async_events) == sc.kinds


@pytest.mark.parametrize("sc", [pytest.param(s, id=s.name) for s in PROMPT_SCENARIOS])
def test_parity_holds_with_the_checks_offloaded_to_worker_threads(sc):
    async def go():
        return await async_prompt_run(sc, limiters=make_limiters_here())

    inline, _ = run(lambda: async_prompt_run(sc))
    offloaded, _ = run(go)
    assert ordered(offloaded) == ordered(inline)


def test_escalated_done_events_carry_both_attempts_and_the_routing_fields():
    sc = next(s for s in PROMPT_SCENARIOS if s.name == "draft_rejected_uncited")
    events, _ = run(lambda: async_prompt_run(sc))
    escalated, done = events[0], events[-1]
    assert escalated == {"event": "escalated", "reasons": ["no_citation", "ungrounded_number"], "from": CHEAP,
                         "to": STRONG}
    assert done["escalated"] is True and done["answered_by"] == STRONG and done["routed"] == "cheap"
    assert done["usage"] == {"prompt_tokens": 2400, "completion_tokens": 80}         # both attempts are paid for


def test_the_estimated_flag_survives_summing_two_attempts():
    plain_run = next(s for s in PROMPT_SCENARIOS if s.name == "draft_rejected_uncited")
    estimated = next(s for s in PROMPT_SCENARIOS if s.name == "estimated_usage_survives_summing_two_attempts")
    assert "estimated" not in run(lambda: async_prompt_run(plain_run))[0][-1]["usage"]
    assert run(lambda: async_prompt_run(estimated))[0][-1]["usage"]["estimated"] is True


def test_postprocess_without_force_buffered_is_refused_on_the_first_iteration_like_the_sync_writer():
    sc = Scenario("refused", (), llm=Script(GOOD_PARTS), kw={"postprocess": strip_links})
    with pytest.raises(ValueError, match="postprocess needs force_buffered"):
        sync_prompt_run(sc)
    with pytest.raises(ValueError, match="postprocess needs force_buffered"):
        run(lambda: async_prompt_run(sc))


def test_limiters_is_a_keyword_of_the_writer_not_an_argument_of_the_model_stream(monkeypatch):
    """``limiters`` must not fall into ``**stream_kwargs`` and reach the stream constructor (the sync writer would
    reject it)."""
    seen = []

    class Recording(AsyncFake):
        def __init__(self, prompt, **kw):
            seen.append(kw)
            super().__init__(Script(GOOD_PARTS, model=kw.get("model", CHEAP)))

    monkeypatch.setattr(answerer_async, "AsyncTextStream", Recording)

    async def go():
        return await collect(answerer_async.astream_answer_for_prompt(
            *prompt_args(Scenario("x", ())), sources=SOURCES, limiters=make_limiters_here(), timeout=90,
            max_tokens=2400, **ESC))

    run(go)
    assert all("limiters" not in kw for kw in seen) and len(seen) == 1


def test_the_draft_is_given_a_short_fuse_so_an_outage_does_not_delay_the_strong_model(monkeypatch):
    seen = []

    class Recording(AsyncFake):
        def __init__(self, prompt, **kw):
            seen.append(kw)
            super().__init__(Script(GOOD_PARTS, model=kw.get("model", CHEAP)))

    monkeypatch.setattr(answerer_async, "AsyncTextStream", Recording)

    async def go():
        return await collect(answerer_async.astream_answer_for_prompt(
            *prompt_args(Scenario("x", ())), sources=SOURCES, timeout=90, max_tokens=2400, **ESC))

    run(go)
    draft_kw = seen[0]
    assert draft_kw["attempts"] == 1 and draft_kw["num_retries"] == 0
    assert draft_kw["timeout"] == answerer.DRAFT_TIMEOUT_S <= 30 and draft_kw["max_tokens"] == 2400


def test_the_strong_model_keeps_the_full_timeout_and_retries(monkeypatch):
    seen = []

    class Recording(AsyncFake):
        def __init__(self, prompt, **kw):
            seen.append(kw)
            super().__init__(Script(UNCITED if len(seen) == 1 else GOOD_PARTS, model=kw.get("model", CHEAP)))

    monkeypatch.setattr(answerer_async, "AsyncTextStream", Recording)

    async def go():
        return await collect(answerer_async.astream_answer_for_prompt(
            *prompt_args(Scenario("x", ())), sources=SOURCES, timeout=90, max_tokens=2400, **ESC))

    run(go)
    assert seen[1]["model"] == STRONG and seen[1]["timeout"] == 90
    assert "attempts" not in seen[1] and "num_retries" not in seen[1]


# --- a cancellation while the draft is drained stays a cancellation ---------------------------------------------------
# ``_adrain`` turns a failing draft into a ``draft_error`` (and so into a paid escalation) by catching ``Exception``. It
# must never catch ``BaseException``: a visitor who disconnects while the cheap draft is generated would otherwise
# trigger the strong model, and an ``error`` event, for nobody. (Changing it to ``except BaseException`` survived every
# test before these were written.)

DRAFT_WAIT_S = 1.0
DRAIN_PATHS = {
    "escalating": ESC,                                  # _adraft_then_escalate drains the cheap draft
    "force_buffered": BUF,                              # _abuffered_events drains the one stream
    "escalating_force_buffered": {**ESC, **BUF},        # the draft drain of the escalating path, buffered release
}


class WaitingIterator(ClosableIterator):
    """The iterator of a slow draft: it delivers one delta, then waits for the next. ``on_wait`` fires as the wait
    begins, so a test can cancel exactly while the draft is being drained. A cancellation that were swallowed would let
    the wait end, and the draft would be judged and rejected like any other (which the assertions catch)."""

    def __init__(self, parts, on_wait, **kw):
        super().__init__(parts, **kw)
        self.on_wait = on_wait

    async def __anext__(self):
        if self.parts:
            return self.parts.pop(0)
        self.on_wait()
        await tick(DRAFT_WAIT_S)
        raise StopAsyncIteration


async def until_set(flag: threading.Event) -> None:
    """Wait, without blocking the loop, for a flag that a fake sets (possibly from a worker thread)."""
    while not flag.is_set():
        await tick(0.002)


async def cancel_when_set(scope: anyio.CancelScope, flag: threading.Event) -> None:
    await until_set(flag)
    scope.cancel()


async def feed(events, sink: list[dict]) -> None:
    """Consume ``events`` the way a route does, keeping what was delivered even if the consumer is cancelled."""
    async with aclosing(events) as stream:
        async for event in stream:
            sink.append(event)


class StallingUpstream(FakeUpstream):
    """The provider stream of a slow draft: its first chunk is immediate, the next one takes ``DRAFT_WAIT_S``.
    ``on_wait`` fires as that wait begins."""

    def __init__(self, items, on_wait):
        super().__init__(items)
        self.on_wait, self.total = on_wait, len(self.items)

    async def __anext__(self):
        if len(self.items) < self.total:
            self.on_wait()
            await tick(DRAFT_WAIT_S)
        return await super().__anext__()


# A builder takes ``on_wait`` and returns ``(events, strong_asked, upstream)``: the writer's events, a check of whether
# the escalation stream was ever asked for, and the upstream that must end up closed.

def injected_draft(path: str, **iterator_kw):
    """The writer on ``path`` over an injected draft that waits."""
    def build(on_wait):
        calls: list = []
        iterator = WaitingIterator(["a "], on_wait, **iterator_kw)

        def draft(prompt):
            calls.append(("llm", prompt))
            return ClosableStream(iterator)

        events = answerer_async.astream_answer_for_prompt(
            Q, PROMPT, CONTEXT, {DOC}, [DOC], "hybrid", sources=SOURCES, llm_stream=draft,
            escalation_stream=factory("strong", None, calls, AsyncFake), **DRAIN_PATHS[path])
        return events, lambda: any(role == "strong" for role, _ in calls), iterator
    return build


def model_stream_draft(monkeypatch):
    """The default construction path, as production runs it: no injected stream, the cheap draft is an
    ``AsyncTextStream`` over a scripted ``acompletion``, and escalating would be a second model call."""
    def build(on_wait):
        upstream = StallingUpstream([chunk("a "), chunk("b", "stop")], on_wait)
        fake = FakeAcompletion(upstream)
        monkeypatch.setattr(answerer_async, "acompletion", fake)
        events = answerer_async.astream_answer_for_prompt(
            Q, PROMPT, CONTEXT, {DOC}, [DOC], "hybrid", sources=SOURCES, timeout=90, max_tokens=2400, **ESC)
        return events, lambda: len(fake.calls) > 1, upstream
    return build


DRAIN_KINDS = [*DRAIN_PATHS, "model_stream"]


def drain_builder(kind: str, monkeypatch):
    return model_stream_draft(monkeypatch) if kind == "model_stream" else injected_draft(kind)


async def cancel_scope_during_drain(build):
    waiting, sink = threading.Event(), []
    events, strong_asked, upstream = build(waiting.set)
    async with anyio.create_task_group() as tg:
        with anyio.CancelScope() as scope:
            tg.start_soon(cancel_when_set, scope, waiting)
            await feed(events, sink)
            sink.append({"event": "the writer finished instead of being cancelled"})
        tg.cancel_scope.cancel()                         # the code after the scope runs: the helper is stopped here
    return scope, sink, strong_asked, upstream


async def cancel_task_during_drain(build):
    waiting, sink = threading.Event(), []
    events, strong_asked, upstream = build(waiting.set)
    task = asyncio.ensure_future(feed(events, sink))
    await until_set(waiting)
    task.cancel()
    await asyncio.wait({task})
    return task, sink, strong_asked, upstream


def assert_not_mistaken_for_a_failed_draft(sink: list[dict], strong_asked, upstream) -> None:
    assert sink == []                                    # nothing reached the client: no 'escalated', no 'error'
    assert not strong_asked()                            # the escalation was never asked for: no paid strong call
    assert upstream.aclose_calls == 1                    # and the draft's upstream was released


@pytest.mark.parametrize("kind", DRAIN_KINDS)
def test_an_anyio_scope_cancelled_during_the_draft_drain_is_a_cancellation_not_a_failed_draft(monkeypatch, kind):
    scope, sink, strong_asked, upstream = run(lambda: cancel_scope_during_drain(drain_builder(kind, monkeypatch)))
    assert scope.cancelled_caught                        # the CancelledError unwound the writer up to the scope
    assert_not_mistaken_for_a_failed_draft(sink, strong_asked, upstream)
    assert upstream.closed == 1                          # the shielded close ran to its end


@pytest.mark.parametrize("kind", DRAIN_KINDS)
def test_a_native_task_cancel_during_the_draft_drain_is_a_cancellation_not_a_failed_draft(monkeypatch, kind):
    task, sink, strong_asked, upstream = run(lambda: cancel_task_during_drain(drain_builder(kind, monkeypatch)))
    assert task.cancelled()                              # not a task that returned or raised something else
    assert_not_mistaken_for_a_failed_draft(sink, strong_asked, upstream)
    assert upstream.closed == 1                          # one native cancel: the close still ran to its end


@pytest.mark.parametrize("kind", ["anyio_scope", "native_cancel"])
def test_a_failing_close_does_not_turn_a_cancellation_into_an_error(kind):
    """The close of the draft's iterator raises while a cancellation is unwinding the writer: the cancellation must
    still be what leaves it (a close error taking its place would end the stream with a RuntimeError)."""
    build = injected_draft("escalating", close_error=RuntimeError("connection already gone"))
    if kind == "anyio_scope":
        scope, sink, strong_asked, upstream = run(lambda: cancel_scope_during_drain(build))
        assert scope.cancelled_caught
    else:
        task, sink, strong_asked, upstream = run(lambda: cancel_task_during_drain(build))
        assert task.cancelled()
    assert_not_mistaken_for_a_failed_draft(sink, strong_asked, upstream)
    assert upstream.closed == 0                          # it did fail, and only that was swallowed


# --- the thread hops of a cancelled stream ----------------------------------------------------------------------------

class Hop:
    """A call made on a worker thread that a test can watch and hold open: ``started`` and ``finished`` flags, and
    ``hold_s`` seconds of work (or, with ``hold_s=None``, work that lasts until the test sets ``gate``)."""

    def __init__(self, hold_s: float | None = 0.3):
        self.hold_s = hold_s
        self.started, self.finished, self.gate = threading.Event(), threading.Event(), threading.Event()

    def run(self, real, *args, **kwargs):
        self.started.set()
        if self.hold_s is None:
            self.gate.wait(10)
        else:
            time.sleep(self.hold_s)
        result = real(*args, **kwargs)
        self.finished.set()
        return result


# The named limiter each hop runs under (None: anyio's default one, which the usage estimate takes on purpose).
HOP_LIMITER = {"embed": "embed", "retrieval": "db", "check": "db", "estimate": None}


def hop_limiter(name: str, limiters):
    named = HOP_LIMITER[name]
    return anyio.to_thread.current_default_thread_limiter() if named is None else getattr(limiters, named)


def hop_writer(name: str, hop: Hop, monkeypatch, limiters):
    """The writer's events, with the ``name`` hop (the question embedding, the graph reads, the deterministic checks or
    the usage estimate) made watchable."""
    if name == "estimate":                               # a provider that reports no usage: the chunk builder runs
        usage = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3))
        monkeypatch.setattr(litellm, "stream_chunk_builder", lambda chunks, messages=None: hop.run(lambda: usage))
        stream = FakeUpstream([chunk("Hi "), chunk("there", "stop")])
        monkeypatch.setattr(answerer_async, "acompletion", FakeAcompletion(stream))
        return aiter(AsyncTextStream("PROMPT", model=CHEAP))
    if name == "check":
        monkeypatch.setattr(answerer_async, "verify_answer", partial(hop.run, answerer_async.verify_answer))
        sc = next(s for s in PROMPT_SCENARIOS if s.name == "clean_cheap_draft_released")
        return prompt_events(sc, [], limiters=limiters)
    embedder = FakeEmbedder()
    if name == "retrieval":
        monkeypatch.setattr(answerer_async, "hybrid_retrieve", partial(hop.run, answerer_async.hybrid_retrieve))
    else:
        class SlowEmbedder(FakeEmbedder):
            def encode_query(self, text):
                return hop.run(super().encode_query, text)

        embedder = SlowEmbedder()
    return answerer_async.aanswer_stream(CTX_Q, FakeDriver.world(), embedder, limiters=limiters,
                                         llm_stream=lambda p: AsyncFake(GOOD_C.with_id(WORLD_IDS["hybrid"])))


@pytest.mark.parametrize("name", list(HOP_LIMITER))
def test_a_cancelled_anyio_scope_waits_for_the_thread_hop_to_finish(monkeypatch, name):
    """The module docstring's claim for anyio cancellation: every hop is shielded, so the cancelled consumer is released
    only after the worker thread has returned (nothing is left running unobserved)."""
    hop = Hop()

    async def go():
        sink: list[dict] = []
        async with anyio.create_task_group() as tg:
            with anyio.CancelScope() as scope:
                tg.start_soon(cancel_when_set, scope, hop.started)
                await feed(hop_writer(name, hop, monkeypatch, make_limiters_here()), sink)
            tg.cancel_scope.cancel()
        done = any(isinstance(e, dict) and e["event"] == "done" for e in sink)
        return scope.cancel_called, hop.finished.is_set(), done                        # read as the scope was left

    cancelled, hop_finished, done = run(go)
    assert cancelled and hop_finished                    # cancelled while the hop ran, released only after it returned
    assert not done                                      # and the answer stopped (the estimate is the last await)


@pytest.mark.parametrize("name", list(HOP_LIMITER))
def test_a_native_cancel_ends_the_task_but_not_a_thread_hop_already_started(monkeypatch, name):
    """The documented limit: ``task.cancel()`` ends the task at once, while the worker thread started by the hop runs on
    to its end (no thread can be stopped) and no longer holds its named limiter's token, so that limiter can be
    oversubscribed until the thread is done. The test holds the thread open until it has looked."""
    hop, limiters = Hop(hold_s=None), make_limiters_here()

    async def go():
        sink: list[dict] = []
        task = asyncio.ensure_future(feed(hop_writer(name, hop, monkeypatch, limiters), sink))
        await until_set(hop.started)
        task.cancel()
        await asyncio.wait({task})
        seen = (task.cancelled(), hop.finished.is_set(), hop_limiter(name, limiters).borrowed_tokens)
        hop.gate.set()
        with anyio.fail_after(5):
            await until_set(hop.finished)                # the thread completes on its own (and the test waits for it)
        return seen

    assert run(go) == (True, False, 0)                   # task gone, thread still working, token already returned


# --- the deterministic checks run on a worker thread (measured 2026-10-03: p95 10.4 ms for a 2400-token answer, 6.9 ms
# for the longest benchmark answer, over a 79,621-character context of the eight largest real chunks; the draft path
# runs two of them) -------------------------------------------------------------------------------------------------

def _spy(seen: dict, name: str, real, loop_thread: int, limiters):
    def wrapper(*args, **kwargs):
        seen.setdefault(name, []).append((threading.get_ident() != loop_thread, limiters.db.borrowed_tokens))
        return real(*args, **kwargs)
    return wrapper


def test_the_checks_run_on_a_worker_thread_under_the_db_limiter(monkeypatch):
    seen: dict = {}
    sc = next(s for s in PROMPT_SCENARIOS if s.name == "clean_cheap_draft_released")

    async def go():
        limiters, loop_thread = make_limiters_here(), threading.get_ident()
        for name in ("_done_event", "verify_answer"):
            real = getattr(answerer_async, name)
            monkeypatch.setattr(answerer_async, name, _spy(seen, name, real, loop_thread, limiters))
        return await async_prompt_run(sc, limiters=limiters)

    events, _ = run(go)
    assert events[-1]["event"] == "done"
    assert seen == {"verify_answer": [(True, 1)], "_done_event": [(True, 1)]}      # off the loop, holding one db token


def test_without_limiters_the_checks_run_inline_for_tests(monkeypatch):
    seen: dict = {}
    sc = next(s for s in PROMPT_SCENARIOS if s.name == "clean_cheap_draft_released")

    async def go():
        limiters, loop_thread = make_limiters_here(), threading.get_ident()
        for name in ("_done_event", "verify_answer"):
            real = getattr(answerer_async, name)
            monkeypatch.setattr(answerer_async, name, _spy(seen, name, real, loop_thread, limiters))
        return await async_prompt_run(sc)

    run(go)
    assert seen == {"verify_answer": [(False, 0)], "_done_event": [(False, 0)]}


# --- a cancellation that lands during a shielded wait is raised when the wait ends ----------------------------------
# A hop (``anyio.to_thread.run_sync``, shielded by default) and the shielded close of a stream both end WITHOUT a
# checkpoint, so the cancellation of the consuming scope is raised only at the next real suspension. What follows is
# often not one: a yield into a consumer that never suspends between events, or a model call that starts at once.
# Without a checkpoint of its own the writer would then deliver one more event, or start a PAID model call, for a
# visitor who is already gone. Each test below holds ONE wait open, cancels the consuming scope meanwhile, lets the
# wait end, and looks at what the writer did next.

async def cancel_while_held(events, started: threading.Event, release: threading.Event | None = None):
    """Consume ``events`` the way a route does (``feed``: nothing suspends between two events). Once ``started`` is
    set, cancel the consuming scope and then set ``release``. Returns the scope, everything that was delivered, and how
    much of it had been delivered when the cancel was issued."""
    sink: list[dict] = []
    at_cancel: list[int] = []

    async def cancel_then_release(scope: anyio.CancelScope) -> None:
        await until_set(started)
        at_cancel.append(len(sink))
        scope.cancel()
        if release is not None:
            release.set()

    async with anyio.create_task_group() as tg:
        with anyio.CancelScope() as scope:
            tg.start_soon(cancel_then_release, scope)
            await feed(events, sink)
        tg.cancel_scope.cancel()
    return scope, sink, (at_cancel[0] if at_cancel else None)


def model_chunks(parts: tuple[str, ...]) -> list:
    """The provider chunks of a scripted answer, usage chunk included (so no estimate hop runs)."""
    return [*(chunk(p) for p in parts[:-1]), chunk(parts[-1], "stop"), chunk(usage=(10, 2), choices=False)]


def without_entry_checkpoint(monkeypatch) -> None:
    """anyio's ``run_sync`` starts with a checkpoint, so a hop that directly follows another one is protected by anyio
    itself. This stand-in takes that away (the real call runs inside a shield, where its first checkpoint cannot
    raise) and leaves only what the writer does between two hops."""
    real = anyio.to_thread.run_sync

    async def run_sync(func, *args, abandon_on_cancel=False, limiter=None):
        with anyio.CancelScope(shield=True):
            return await real(func, *args, abandon_on_cancel=abandon_on_cancel, limiter=limiter)

    monkeypatch.setattr(anyio.to_thread, "run_sync", run_sync)


# case: (the check that is held, the scenario of PROMPT_SCENARIOS it runs in; None = the default model streams)
CHECK_HOLDS = {
    "rejected_draft": ("verify_answer", "draft_rejected_uncited"),
    "rejected_draft_default_models": ("verify_answer", None),
    "released_draft": ("verify_answer", "clean_cheap_draft_released"),
    "final_event_of_a_live_stream": ("_done_event", "live_without_escalation"),
}


def held_check_writer(case: str, monkeypatch):
    """``(hop, the writer's events, a function counting the escalation model's requests)`` with one check held."""
    target, scenario = CHECK_HOLDS[case]
    hop, limiters = Hop(hold_s=None), make_limiters_here()
    monkeypatch.setattr(answerer_async, target, partial(hop.run, getattr(answerer_async, target)))
    if scenario is None:                        # no injected stream: the draft and the escalation are acompletion calls
        fake = FakeAcompletion(FakeUpstream(model_chunks(UNCITED)), FakeUpstream(model_chunks(GOOD_PARTS)))
        monkeypatch.setattr(answerer_async, "acompletion", fake)
        events = answerer_async.astream_answer_for_prompt(
            Q, PROMPT, CONTEXT, {DOC}, [DOC], "hybrid", sources=SOURCES, timeout=90, max_tokens=2400,
            limiters=limiters, **ESC)
        return hop, events, lambda: len(fake.calls) - 1             # every call beyond the draft's
    calls: list = []
    sc = next(s for s in PROMPT_SCENARIOS if s.name == scenario)
    return hop, prompt_events(sc, calls, limiters=limiters), lambda: sum(role == "strong" for role, _ in calls)


@pytest.mark.parametrize("case", list(CHECK_HOLDS))
def test_a_cancel_during_a_check_hop_is_raised_before_anything_is_delivered_or_bought(monkeypatch, case):
    hop, events, strong_requests = held_check_writer(case, monkeypatch)
    scope, sink, at_cancel = run(lambda: cancel_while_held(events, hop.started, hop.gate))
    assert strong_requests() == 0                        # the escalation model was never asked for
    assert len(sink) == at_cancel                        # and no event (escalated, delta, done) followed the cancel
    assert scope.cancelled_caught                        # the cancellation was raised, not lost


@pytest.mark.parametrize("entry_checkpoint", [True, False], ids=["as_shipped", "run_sync_without_entry_checkpoint"])
def test_a_cancel_during_the_embedding_hop_is_raised_before_the_retrieval_hop(monkeypatch, entry_checkpoint):
    """As shipped, anyio's own checkpoint at the start of the retrieval hop already raises, so this passes without the
    writer's checkpoint; the second case removes anyio's and leaves the writer's own."""
    hop, retrieved, calls = Hop(hold_s=None), [], []
    real_retrieve = answerer_async.hybrid_retrieve

    def retrieve(*args, **kwargs):
        retrieved.append(threading.get_ident())
        return real_retrieve(*args, **kwargs)

    class SlowEmbedder(FakeEmbedder):
        def encode_query(self, text):
            return hop.run(super().encode_query, text)

    monkeypatch.setattr(answerer_async, "hybrid_retrieve", retrieve)
    if not entry_checkpoint:
        without_entry_checkpoint(monkeypatch)
    events = answerer_async.aanswer_stream(
        CTX_Q, FakeDriver.world(), SlowEmbedder(), limiters=make_limiters_here(),
        llm_stream=factory("llm", GOOD_C.with_id(WORLD_IDS["hybrid"]), calls, AsyncFake))
    scope, sink, at_cancel = run(lambda: cancel_while_held(events, hop.started, hop.gate))
    assert retrieved == [] and calls == []               # no graph read, no model request
    assert len(sink) == at_cancel and scope.cancelled_caught


@pytest.mark.parametrize("models", ["injected", "default"])
def test_a_cancel_during_the_retrieval_hop_never_reaches_the_model(monkeypatch, models):
    hop, calls, script = Hop(hold_s=None), [], GOOD_C.with_id(WORLD_IDS["hybrid"])
    monkeypatch.setattr(answerer_async, "hybrid_retrieve", partial(hop.run, answerer_async.hybrid_retrieve))
    if models == "injected":
        extra, model_calls = {"llm_stream": factory("llm", script, calls, AsyncFake)}, lambda: len(calls)
    else:                                                # the draft would be an ``acompletion`` call
        fake = FakeAcompletion(FakeUpstream(model_chunks(script.parts)))
        monkeypatch.setattr(answerer_async, "acompletion", fake)
        extra, model_calls = {}, lambda: len(fake.calls)
    events = answerer_async.aanswer_stream(CTX_Q, FakeDriver.world(), FakeEmbedder(), limiters=make_limiters_here(),
                                           **extra)
    scope, sink, at_cancel = run(lambda: cancel_while_held(events, hop.started, hop.gate))
    assert model_calls() == 0                            # the draft model was never called
    assert len(sink) == at_cancel                        # not even the ``retrieval`` event was delivered
    assert scope.cancelled_caught


async def delta_events(stream):
    """The deltas of an ``AsyncTextStream`` as event dicts, with its iterator closed on every exit."""
    async with aclosing(aiter(stream)) as deltas:
        async for delta in deltas:
            yield {"event": "delta", "text": delta}


async def after_a_held_hop(hop: Hop, events):
    """``events`` after a shielded hop that returns without a checkpoint (the state a hop leaves the writer in)."""
    await anyio.to_thread.run_sync(partial(hop.run, int))
    async for event in events:
        yield event


def test_the_default_model_call_is_not_started_for_a_scope_cancelled_during_a_hop(monkeypatch):
    """Every paid default call goes through ``AsyncTextStream._attempt``: with ``acompletion`` the first await of the
    stream and the hop before it ending without a checkpoint, nothing else would stop it."""
    fake = FakeAcompletion(FakeUpstream(plain()))
    monkeypatch.setattr(answerer_async, "acompletion", fake)
    hop = Hop(hold_s=None)
    events = after_a_held_hop(hop, delta_events(AsyncTextStream("PROMPT", model=CHEAP)))
    scope, sink, _ = run(lambda: cancel_while_held(events, hop.started, hop.gate))
    assert fake.calls == []
    assert sink == [] and scope.cancelled_caught


class ClosingUpstream(FakeUpstream):
    """An upstream whose (shielded) close announces itself, so a test can cancel while it is closing."""

    def __init__(self, items, **kw):
        super().__init__(items, **kw)
        self.closing = threading.Event()

    async def aclose(self):
        self.closing.set()
        await super().aclose()


def test_the_retry_of_an_empty_answer_is_not_a_second_paid_call_for_a_visitor_who_left(monkeypatch):
    """No backoff separates an empty answer from its retry (``anyio.sleep`` would be a checkpoint), and the first
    attempt ends with the shielded close of its upstream: a cancel that lands during that close must be raised before
    the second ``acompletion``."""
    first = ClosingUpstream([chunk(None, "stop")], close_delay=0.05)
    fake = FakeAcompletion(first, FakeUpstream(plain()))
    monkeypatch.setattr(answerer_async, "acompletion", fake)
    events = delta_events(AsyncTextStream("PROMPT", model=CHEAP))
    scope, sink, _ = run(lambda: cancel_while_held(events, first.closing))
    assert len(fake.calls) == 1                          # the empty attempt only
    assert sink == [] and scope.cancelled_caught and first.closed == 1


class SlowCloseIterator(ClosableIterator):
    """A draft iterator whose (shielded) close announces itself and takes a while."""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.closing = threading.Event()

    async def aclose(self):
        self.closing.set()
        await tick(0.05)
        await super().aclose()


def test_a_failed_drafts_escalation_is_not_requested_for_a_scope_cancelled_while_the_draft_closes():
    """A draft that failed goes straight to the strong model: no check hop sits between its close and the paid call."""
    iterator, calls = SlowCloseIterator([], fail=TimeoutError("draft timed out")), []
    events = answerer_async.astream_answer_for_prompt(
        Q, PROMPT, CONTEXT, {DOC}, [DOC], "hybrid", sources=SOURCES, llm_stream=lambda p: ClosableStream(iterator),
        escalation_stream=factory("strong", Script(GOOD_PARTS, model=STRONG), calls, AsyncFake), **ESC)
    scope, sink, _ = run(lambda: cancel_while_held(events, iterator.closing))
    assert calls == []                                   # the escalation stream was never requested
    assert sink == [] and scope.cancelled_caught and iterator.closed == 1


def test_every_thread_hop_of_the_module_goes_through_the_checkpointing_helper():
    """A hop that does not checkpoint after it returns is the bug the tests above pin: keep new hops from bypassing
    ``_hop``."""
    tree = ast.parse(Path(answerer_async.__file__).read_text(encoding="utf-8"))

    def owners(node, owner=None):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
            if isinstance(child, ast.Attribute) and child.attr == "run_sync":
                yield name
            yield from owners(child, name)

    assert set(owners(tree)) == {"_hop"}


# --- the ``postprocess`` hook of a buffered answer is regex work over model output: it runs where the checks do ------
# ``strip_links_images`` is quadratic on one long unbroken run of letters (222 ms for 10,000 characters, 886 ms for
# 20,000), and an uploaded document that injects a prompt can make the model's output that long.

def _postprocess_case(name: str, calls: int):
    return pytest.param(next(s for s in PROMPT_SCENARIOS if s.name == name), calls, id=name)


# (scenario, how many times it calls ``postprocess``)
POSTPROCESS_CASES = [
    _postprocess_case("forced_buffered_strips_a_link", 1),                      # _abuffered_events: the one stream
    _postprocess_case("forced_buffered_error_reports_the_edited_partial", 1),   # ... and the partial of an error
    _postprocess_case("forced_buffered_after_escalation", 2),                   # the draft, then the buffered release
    _postprocess_case("forced_buffered_routed_strong", 1),
    pytest.param(Scenario("buffered_clean_draft", ("delta", "done"), llm=Script(GOOD_PARTS), kw={**ESC, **BUF}), 1,
                 id="buffered_clean_draft"),                                    # the draft is edited, then released
]


def _spied_postprocess(sc: Scenario, seen: dict, limiters) -> Scenario:
    spy = _spy(seen, "postprocess", strip_links, threading.get_ident(), limiters)
    return replace(sc, kw={**sc.kw, "postprocess": spy})


@pytest.mark.parametrize("sc, calls", POSTPROCESS_CASES)
def test_postprocess_runs_on_a_worker_thread_under_the_db_limiter(sc, calls):
    seen: dict = {}

    async def go():
        limiters = make_limiters_here()
        return await async_prompt_run(_spied_postprocess(sc, seen, limiters), limiters=limiters)

    events, _ = run(go)
    assert seen == {"postprocess": [(True, 1)] * calls}  # off the loop, holding one db token, once per call
    assert ordered(events) == ordered(run(lambda: async_prompt_run(sc))[0])            # the events are unchanged


@pytest.mark.parametrize("sc, calls", POSTPROCESS_CASES)
def test_without_limiters_postprocess_runs_inline_for_tests(sc, calls):
    seen: dict = {}

    async def go():
        return await async_prompt_run(_spied_postprocess(sc, seen, make_limiters_here()))

    events, _ = run(go)
    assert seen == {"postprocess": [(False, 0)] * calls}
    assert ordered(events) == ordered(run(lambda: async_prompt_run(sc))[0])


# --- the whole path: aanswer_stream against answer_stream, with a fake graph and a fake embedder --------------------

CTX_Q = Q_SEC


def _world_ids() -> dict[str, str]:
    """A chunk id the fake world's retrieval really returns, per strategy: the scripted answers cite it."""
    return {"hybrid": hybrid_retrieve(CTX_Q, FakeDriver.world(), FakeEmbedder())["chunks"][0]["chunk_id"],
            "vector": vector_retrieve(CTX_Q, FakeDriver.world(), FakeEmbedder())["chunks"][0]["chunk_id"]}


WORLD_IDS = _world_ids()
GOOD_C = Script(("Nvidia depends on TSMC [<CID>]", " for advanced wafer supply."))
UNCITED_C = Script(("Nvidia revenue was $215.9 billion.",))


@dataclass(frozen=True)
class CtxScenario:
    name: str
    kinds: tuple[str, ...]
    strategy: str = "hybrid"
    question: str = CTX_Q
    llm: Script | None = None
    strong: Script | None = None
    kw: dict = field(default_factory=dict)


CTX_SCENARIOS = [
    CtxScenario("hybrid_live", ("retrieval", "delta", "delta", "done"), llm=GOOD_C),
    CtxScenario("hybrid_clean_draft", ("retrieval", "delta", "done"), llm=GOOD_C, kw=ESC),
    CtxScenario("hybrid_draft_rejected", ("retrieval", "escalated", "delta", "delta", "done"), llm=UNCITED_C,
                strong=replace(GOOD_C, model=STRONG), kw=ESC),
    CtxScenario("hybrid_routed_strong", ("retrieval", "delta", "delta", "done"), question=Q_CHANGE,
                strong=replace(GOOD_C, model=STRONG), kw=ESC),
    CtxScenario("hybrid_draft_error", ("retrieval", "escalated", "delta", "delta", "done"),
                llm=Script((), fail=TimeoutError("draft timed out")), strong=replace(GOOD_C, model=STRONG), kw=ESC),
    CtxScenario("hybrid_error_mid_stream", ("retrieval", "delta", "error"),
                llm=Script(GOOD_C.parts[:1], fail=RuntimeError("provider reset"))),
    CtxScenario("hybrid_four_chunks", ("retrieval", "delta", "delta", "done"), llm=GOOD_C, kw={"k_chunks": 4}),
    CtxScenario("vector_live", ("retrieval", "delta", "delta", "done"), strategy="vector", llm=GOOD_C),
    CtxScenario("vector_draft_rejected", ("retrieval", "escalated", "delta", "delta", "done"), strategy="vector",
                llm=UNCITED_C, strong=replace(GOOD_C, model=STRONG), kw=ESC),
    # the vector strategy never reads ``hops``: the sync writer does not validate it there either
    CtxScenario("vector_ignores_hops", ("retrieval", "delta", "delta", "done"), strategy="vector", llm=GOOD_C,
                kw={"hops": 0}),
]


class CountingEmbedder(FakeEmbedder):
    """FakeEmbedder that also records which thread embedded and which limiter token that thread held."""

    def __init__(self, limiters=None):
        super().__init__()
        self.threads: list[int] = []
        self.embed_tokens: list[int] = []
        self.limiters = limiters

    def encode_query(self, text):
        self.threads.append(threading.get_ident())
        if self.limiters is not None:
            self.embed_tokens.append(self.limiters.embed.borrowed_tokens)
        return super().encode_query(text)


def sync_context_run(sc: CtxScenario):
    calls, driver, embedder = [], FakeDriver.world(), FakeEmbedder()
    cid = WORLD_IDS[sc.strategy]
    events = list(answer_stream(
        sc.question, driver, embedder, sc.strategy,
        llm_stream=factory("llm", sc.llm and sc.llm.with_id(cid), calls, SyncFake),
        escalation_stream=factory("strong", sc.strong and sc.strong.with_id(cid), calls, SyncFake), **sc.kw))
    return events, calls, driver.names(), embedder.queries


async def async_context_run(sc: CtxScenario):
    calls, driver, embedder = [], FakeDriver.world(), FakeEmbedder()
    cid = WORLD_IDS[sc.strategy]
    events = await collect(answerer_async.aanswer_stream(
        sc.question, driver, embedder, sc.strategy,
        llm_stream=factory("llm", sc.llm and sc.llm.with_id(cid), calls, AsyncFake),
        escalation_stream=factory("strong", sc.strong and sc.strong.with_id(cid), calls, AsyncFake),
        limiters=make_limiters_here(), **sc.kw))
    return events, calls, driver.names(), embedder.queries


@pytest.mark.parametrize("sc", [pytest.param(s, id=s.name) for s in CTX_SCENARIOS])
def test_aanswer_stream_yields_exactly_answer_streams_events(sc):
    sync_events, sync_calls, sync_queries, sync_embedded = sync_context_run(sc)
    async_events, async_calls, async_queries, async_embedded = run(lambda: async_context_run(sc))
    assert ordered(async_events) == ordered(sync_events)
    assert async_calls == sync_calls
    assert async_queries == sync_queries                                    # the same graph queries, in the same order
    assert async_embedded == sync_embedded == [sc.question]                 # and the question is embedded exactly once
    assert tuple(e["event"] for e in async_events) == sc.kinds


def test_astream_answer_for_context_matches_the_sync_function():
    r = answerer.hybrid_retrieve(CTX_Q, FakeDriver.world(), FakeEmbedder())
    cid = WORLD_IDS["hybrid"]
    script = GOOD_C.with_id(cid)
    sync_events = list(answerer.stream_answer_for_context(CTX_Q, r, "hybrid", llm_stream=lambda p: SyncFake(script)))

    async def go():
        return await collect(answerer_async.astream_answer_for_context(
            CTX_Q, r, "hybrid", llm_stream=lambda p: AsyncFake(script)))

    assert ordered(run(go)) == ordered(sync_events) and sync_events[0]["event"] == "retrieval"


def test_an_unknown_strategy_is_the_same_value_error_before_anything_is_embedded_or_queried():
    driver, embedder = FakeDriver.world(), FakeEmbedder()
    with pytest.raises(ValueError) as sync_error:
        list(answer_stream(CTX_Q, driver, embedder, "graph", llm_stream=lambda p: SyncFake(GOOD_C)))
    a_driver, a_embedder = FakeDriver.world(), CountingEmbedder()

    async def go():
        return await collect(answerer_async.aanswer_stream(
            CTX_Q, a_driver, a_embedder, "graph", limiters=make_limiters_here(),
            llm_stream=lambda p: AsyncFake(GOOD_C)))

    with pytest.raises(ValueError) as async_error:
        run(go)
    assert str(async_error.value) == str(sync_error.value) and "unknown strategy 'graph'" in str(async_error.value)
    assert not a_embedder.threads and not a_driver.calls and not embedder.queries and not driver.calls


@pytest.mark.parametrize("hops", [0, -3, True, 2.5, "2"])
def test_a_bad_hops_is_the_same_value_error_before_anything_is_embedded_or_queried(hops):
    """The sync path rejects it inside ``hybrid_retrieve`` before any work; the async path must not spend an embed slot
    and 0.3 to 1.2 s of CPU on a question it is about to refuse."""
    driver, embedder = FakeDriver.world(), FakeEmbedder()
    with pytest.raises(ValueError) as sync_error:
        list(answer_stream(CTX_Q, driver, embedder, hops=hops, llm_stream=lambda p: SyncFake(GOOD_C)))
    a_driver, a_embedder = FakeDriver.world(), CountingEmbedder()

    async def go():
        return await collect(answerer_async.aanswer_stream(
            CTX_Q, a_driver, a_embedder, hops=hops, limiters=make_limiters_here(),
            llm_stream=lambda p: AsyncFake(GOOD_C)))

    with pytest.raises(ValueError) as async_error:
        run(go)
    assert str(async_error.value) == str(sync_error.value) and "hops must be an integer >= 1" in str(async_error.value)
    assert not a_embedder.threads and not a_embedder.queries and not a_driver.calls
    assert not embedder.queries and not driver.calls


@pytest.mark.parametrize("strategy", ["hybrid", "vector"])
def test_the_question_is_embedded_once_on_a_worker_thread_and_retrieval_gets_the_vector(monkeypatch, strategy):
    seen = {}

    async def go():
        limiters, loop_thread = make_limiters_here(), threading.get_ident()
        embedder = CountingEmbedder(limiters)
        name = "hybrid_retrieve" if strategy == "hybrid" else "vector_retrieve"
        real = getattr(answerer_async, name)

        def spy(*args, **kwargs):
            seen["retrieval"] = (threading.get_ident() != loop_thread, kwargs.pop("query_vec", None),
                                 limiters.db.borrowed_tokens, limiters.embed.borrowed_tokens)
            seen["options"] = kwargs
            return real(*args, query_vec=seen["retrieval"][1], **kwargs)

        monkeypatch.setattr(answerer_async, name, spy)
        events = await collect(answerer_async.aanswer_stream(
            CTX_Q, FakeDriver.world(), embedder, strategy, limiters=limiters, k_chunks=6,
            llm_stream=lambda p: AsyncFake(GOOD_C.with_id(WORLD_IDS[strategy]))))
        return events, embedder, loop_thread

    events, embedder, loop_thread = run(go)
    assert events[-1]["event"] == "done"
    assert embedder.queries == [CTX_Q]                                      # once: retrieval did not embed it again
    assert embedder.threads and embedder.threads[0] != loop_thread          # on a worker thread
    assert embedder.embed_tokens == [1]                                     # holding the embed limiter
    # retrieval: on a worker thread, with the vector, holding the db limiter (not the embed one)
    assert seen["retrieval"] == (True, [0.1, 0.2], 1, 0)
    assert seen["options"] == ({"k_chunks": 6, "hops": 2} if strategy == "hybrid" else {"k": 6})
    assert len(events[-1]["chunk_ids"]) == 6


def test_a_failing_embedder_surfaces_as_the_embedders_error():
    class Broken(FakeEmbedder):
        def encode_query(self, text):
            raise RuntimeError("model not loaded")

    async def go():
        return await collect(answerer_async.aanswer_stream(
            CTX_Q, FakeDriver.world(), Broken(), limiters=make_limiters_here(), llm_stream=lambda p: AsyncFake(GOOD_C)))

    with pytest.raises(RuntimeError, match="model not loaded"):
        run(go)


# --- nothing blocks the event loop ----------------------------------------------------------------------------------

def test_no_scenario_calls_a_blocking_function_or_the_sync_writer(monkeypatch):
    """``time.sleep`` raises (anywhere: the loop or a worker), and so does the sync model call and the sync
    ``TextStream``: a backoff, a draft, an escalation, a buffered release or a retry that fell back to them fails loudly
    instead of blocking."""
    def blocked(*args, **kwargs):
        raise AssertionError("a blocking call was made")

    monkeypatch.setattr(time, "sleep", blocked)
    monkeypatch.setattr(answerer, "completion", blocked)
    monkeypatch.setattr(answerer, "TextStream", blocked)
    monkeypatch.setattr(litellm, "completion", blocked)

    for sc in PROMPT_SCENARIOS:
        for offloaded in (False, True):
            async def go(sc=sc, offloaded=offloaded):
                return await async_prompt_run(sc, **({"limiters": make_limiters_here()} if offloaded else {}))

            assert run(go)[0][-1]["event"] in ("done", "error")
    for sc in CTX_SCENARIOS:
        assert run(lambda sc=sc: async_context_run(sc))[0][-1]["event"] in ("done", "error")
    for outcomes, kwargs, _ in STREAM_CASES.values():    # retries, their awaited backoff, the estimate, the errors
        observe_async(monkeypatch, outcomes(), kwargs)


def test_the_module_source_names_no_blocking_call():
    tree = ast.parse(Path(answerer_async.__file__).read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "sleep" and getattr(node.value, "id", None) == "time":
            offenders.append("time.sleep")
        if isinstance(node, ast.Name) and node.id in {"completion", "TextStream", "time"}:
            offenders.append(node.id)
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            if "time" in names or "TextStream" in names or "completion" in names:
                offenders.append("import of a blocking name")
    assert offenders == []


def _concurrent_streams(*, blocking: bool):
    """20 ``aanswer_stream`` calls at once (half with a buffered, verified draft) against fakes that await between
    deltas, with the lag monitor running beside them. ``blocking``: ONE stream whose fake calls ``time.sleep`` on the
    loop (the control)."""
    monitor = LoopLagMonitor(warn_ms=100, interval_s=0.02) if blocking else LoopLagMonitor(warn_ms=100)
    results: list[list[dict]] = []

    class Pausing(AsyncFake):
        async def __aiter__(self):
            async for part in AsyncFake.__aiter__(self):
                if blocking:
                    time.sleep(0.25)
                yield part

    async def one(n: int, limiters):
        script = GOOD_C.with_id(WORLD_IDS["hybrid"])
        results.append(await collect(answerer_async.aanswer_stream(
            f"{CTX_Q} ({n})", FakeDriver.world(), FakeEmbedder(), llm_stream=lambda p: Pausing(script, pause=0.01),
            escalation_stream=lambda p: Pausing(replace(script, model=STRONG), pause=0.01), limiters=limiters,
            **(ESC if n % 2 else {}))))

    async def go():
        limiters = make_limiters_here(db=32)
        async with anyio.create_task_group() as tg:
            tg.start_soon(monitor.run)
            async with anyio.create_task_group() as streams:
                for n in range(1 if blocking else 20):
                    streams.start_soon(one, n, limiters)
            tg.cancel_scope.cancel()

    run(go)
    return monitor, results


def test_twenty_concurrent_streams_never_lag_the_event_loop():
    monitor, results = _concurrent_streams(blocking=False)
    assert len(results) == 20 and all(r[-1]["event"] == "done" for r in results)
    assert all(sum(e["event"] == "delta" for e in r) >= 1 for r in results)
    assert monitor.warnings == 0, f"the loop lagged up to {monitor.max_lag_ms:.0f} ms"


def test_the_lag_monitor_would_have_caught_a_blocking_call_in_the_stream():
    """The control for the test above: without it ``warnings == 0`` proves nothing."""
    monitor, results = _concurrent_streams(blocking=True)
    assert len(results) == 1 and monitor.warnings >= 1


# --- CI: the serve-shipped environment has no pandas, sentence-transformers or neo4j --------------------------------

def test_the_async_answer_path_imports_nothing_heavy():
    """Importing the module AND this file's helpers (``agent_fakes``) in a clean interpreter must not pull in pandas,
    torch, sentence-transformers or neo4j: the ``serve-shipped`` CI job installs none of them (neo4j is shipped, but
    only the serving layer's own driver import may load it, never the answer path)."""
    tests_dir, src_dir = Path(__file__).resolve().parent, Path(answerer_async.__file__).resolve().parents[2]
    code = (f"import sys; sys.path[:0] = [{str(tests_dir)!r}, {str(src_dir)!r}]\n"
            "import agent_fakes\n"
            "from semigraph.retrieval import answerer_async\n"
            "from semigraph.serve import limiters\n"
            "heavy = ('pandas', 'torch', 'sentence_transformers', 'neo4j')\n"
            "print('HEAVY', sorted(m for m in heavy if m in sys.modules))\n")
    run_ = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True)
    out = run_.stdout
    assert out.strip().splitlines()[-1] == "HEAVY []", out
