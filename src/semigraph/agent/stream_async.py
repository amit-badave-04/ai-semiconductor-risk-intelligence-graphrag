"""The async twin of the agent answer stream (M5a I2, docs/v2/M5A_BUILD_PLAN.md section 4).

``stream.agent_answer_stream`` is one sync generator, so a served agent ask holds a worker thread from the first
planner call to the last token. :func:`aagent_answer_stream` is the same stream as an async generator. Its events and
its ``done`` / ``error`` grammar are EQUAL to the sync ones (replayed against ``tests/data/agent_events_pre_m5.json``:
same keys, same order, same values), because the pure helpers (``_agent_info``, ``_planner_cost``, ``_fold_spend``,
the prefetch/writer split) are imported from ``stream``, and the writer is the already reviewed async twin
``answerer_async.astream_answer_for_context``. The agent still takes no workspace argument: uploaded-document text
never reaches the planner, and this module imports nothing of the uploads.

Planning (``run_agent``, a sync LangGraph run whose planner call and tools block) runs on ONE worker thread under
``limiters.db``. One thread, not one hop per step, because a sync generator must be driven from a single thread and
context. The thread and the loop take turns (a lock-step handshake): the thread hands over one ``step`` event, then
PARKS until the loop is asked for the next event, exactly as the sync generator is suspended at its ``yield``. So a
consumer that has taken event k (k >= 1) and then goes away never causes planner call k+1, whatever the timing;
backpressure needs nothing else.

The stretch BEFORE the first event is not covered by that: ``run_agent`` yields only step events, so the prefetch (the
embed and the graph reads, including any wait for an embed slot) and the first plan node run inside ONE resumption,
and the thread's own check of the stop flag happens only between resumptions. So the planner handed to ``run_agent``
is wrapped (:func:`_unless_stopped`, for an injected planner and the default one alike): immediately before EVERY
planner call it checks the stop flag and, if it is set, raises :class:`_PlanningStopped` in place of the call. That
is a ``BaseException`` on purpose, so ``run_agent``'s own planner-error fallback (and its "planner call failed"
WARNING) does not run; the planning thread catches it and ends like on any other stop. So once the stream has been
told to stop (``aclose`` sets the flag, which happens when the loop has delivered the disconnect to the consumer),
no planner call that has not begun is made, call 1 after a disconnect during the prefetch included. The sync
stream has no such guard: a client that leaves during ITS prefetch still has planner call 1 paid. What cannot be
stopped, here or there, is a planner call ALREADY IN FLIGHT: one that passed its check, or one that began between
the disconnect and its delivery to the consumer (one or more loop iterations).

A consumer that leaves for ANY reason before planning has finished (``aclose``, a cancelled task or scope, an
exception) sets a stop flag, wakes the thread, closes the hand-over stream and joins the thread under a shield. The
thread checks the flag before every resumption of ``run_agent``, the planner wrapper checks it before every planner
call, and the thread calls ``gen.close()`` on the way out. The join makes the
:class:`~semigraph.agent.state.Ledger` final before the planner's dollars are reported.

An anyio shield holds against a cancelled anyio scope but NOT against a native ``task.cancel()``, so the wait is
looped: each cancellation that lands during the join (or during the thread hop of the final ``flush()``) is remembered,
the wait resumes until the thread (or the flush) has ended, and the first cancellation is re-raised ONCE afterwards. So
however the consumer leaves, a planner call in flight has finished and is in the ledger before the stream is gone.

A cancelled scope is delivered at the next checkpoint, and a shielded thread hop returns without one. So a checkpoint
(``anyio.lowlevel.checkpoint``) sits immediately before everything that can start a paid call: before the loop starts
the planning thread or wakes it for its next resumption (the stop flag is set only once the cancellation has been
delivered, and the thread starts the next planner call the moment it wakes: without the checkpoint the wake-up wins
that race), and before the writer is made. A cancelled scope raises there, instead of buying a call for a client that
has gone.

An error of ``run_agent`` of any kind, a ``BaseException`` that is not an ``Exception`` included (the sync generator
raises it as it is), is raised on the consumer's side by the ``__anext__`` that follows the last event handed over. An
error while CLOSING ``run_agent`` after the consumer has left is logged (WARNING of ``semigraph.agent``, "closing the
planning run failed") and swallowed on purpose: a failure while leaving must not mask the disconnect that is being
handled. The limits of this design:

* a planner call already in flight cannot be interrupted, so up to one planner call (``planner_call_cap_s``, 12 s)
  can still be paid after a disconnect, and the abandoned stream is not finished (nor its ledger row written) until
  it returns: the same as the sync code today. The tools that follow it are read-only graph queries and run before
  the thread notices the flag. A call that begins before the stream has been told to stop counts as in flight, the
  first one included; once it has been told, the planner wrapper refuses every call that has not begun, so a
  prefetch that outlasts the delivery of the disconnect leads to no planner call;
* one thread is held from the first resumption to the end of planning, including while it is parked waiting for the
  consumer to take an event (up to ``agent_time_budget_s``, 25 s). Concurrent agent streams in planning are therefore
  bounded by ``limiters.db``, and the in-flight answer cap bounds how many such threads a slow client can park. A
  thread takes blocking WAITS (the planner's network call, a graph query) off the loop. CPU-bound pure-Python work on
  it shares the GIL with the loop, which the interpreter takes back every 5 ms: the loop keeps turning, with a lag of
  about 15 ms (measured with the answer checks on a crafted answer, ``answerer_async`` module docstring). One long C
  call, a single regex match, holds the GIL for its whole length, whichever thread makes it;
* the prefetch embeds the question inside that thread (``hybrid_retrieve`` takes no ``query_vec`` here), so it waits
  for the embedder's own semaphore while holding a ``limiters.db`` token. A disconnect does not interrupt it: it
  runs to its end holding the thread and the token, and then leads to no planner call;
* the looped join answers a cancellation of the CONSUMER. If the event loop itself is shut down, its tasks are all
  cancelled, the hosting task of the thread included, and the join then ends with that task, not with the thread (which
  cannot be interrupted and is left to finish on its own).

What the sync ``stream._run`` does around ``run_agent`` is repeated here, because ``run_agent`` itself does none of
it: the ``agent`` span, the ``tracer.flush()`` in a ``finally`` and, when the stream is abandoned before a terminal
event (a close, and here also a cancellation, which the sync code cannot tell apart), the WARNING of logger
``semigraph.agent`` with the planner's accrued dollars. Its wording is byte-identical to the sync one: the fixtures
PARSE the dollars out of it.

Tracing keeps the sync structure: ONE ``agent`` span around planning and writing, with the node spans of ``run_agent``
as its children. The span handle is entered, set and exited on the loop thread while the planning thread opens the
children (an explicit parent, not a thread-local); the handshake guarantees the two never touch the tracer at once
(the loop only waits while the thread runs, and the thread is parked or joined whenever the loop acts).
``serve.tracing`` only builds observations in memory (the SDK exports on its own thread) and a request-level
``flush()`` is a no-op there, so spans stay on the loop. The one call that can block on the network is the
``flush()`` of an app-level tracer, so it runs on a thread under ``limiters.db`` unless the tracer says it is off
(``enabled`` is False). The tracer is wrapped with ``as_safe`` and is given lengths, counts and ids, never the
question or the answer.

No anyio task group or cancel scope stays open across a ``yield`` (a generator finalised from another task would fail
to exit it), so the thread is hosted by a plain asyncio task, the server's own event loop being asyncio.
"""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from contextlib import aclosing
from typing import TYPE_CHECKING

import anyio
import anyio.from_thread
import anyio.lowlevel
import anyio.to_thread

from ..config import get_settings
from ..retrieval.answerer import usage_cost
from ..retrieval.answerer_async import astream_answer_for_context
from ..serve.tracing import redact_secret_shaped
from .graph import AgentResult, run_agent
from .planner import LiteLLMPlanner
from .state import Ledger, Limits, PlannerTurn
from .stream import _PREFETCH_ARGS, _agent_info, _fold_spend, _planner_cost
from .trace import Tracer, as_safe

if TYPE_CHECKING:
    from ..serve.limiters import Limiters

logger = logging.getLogger("semigraph.agent")


async def _wait_through_cancellation(wait: Callable[[], Awaitable], done: Callable[[], bool]) -> BaseException | None:
    """Wait, shielded, until ``done()``, whatever happens to the waiting task. Returns the first cancellation that was
    caught on the way (None when there was none) for the caller to re-raise AFTER its cleanup.

    The shield stops a cancelled anyio scope. A native ``task.cancel()`` goes through it: it ends the ``await`` at once
    and whatever was being waited for (a thread, which cannot be interrupted) goes on unobserved. So the wait is
    looped: a cancellation is remembered, never acted on, and the wait resumes until ``done()``."""
    cancelled = None
    while not done():
        try:
            with anyio.CancelScope(shield=True):
                await wait()
        except anyio.get_cancelled_exc_class() as exc:
            if cancelled is None:
                cancelled = exc
    return cancelled


class _PlanningStopped(BaseException):
    """Raised in place of a planner call once the consumer has gone. A ``BaseException`` on purpose: ``run_agent`` turns
    an ``Exception`` of the planner into a fallback answer and a "planner call failed" WARNING, and this is neither."""


def _unless_stopped(planner: Callable[..., PlannerTurn], stop: threading.Event) -> Callable[..., PlannerTurn]:
    """``planner`` with the stop flag checked immediately before every call: once it is set, a call that has not begun
    is not made. A call that has begun cannot be taken back. The wrapper has the signature of a planner, so an injected
    ``callable(messages, tools, *, timeout)`` and the default one are guarded alike."""

    def guarded(messages: list[dict], tools: list[dict], *, timeout: float) -> PlannerTurn:
        if stop.is_set():
            raise _PlanningStopped
        return planner(messages, tools, timeout=timeout)

    return guarded


class _Planning:
    """Drive the sync ``run_agent`` generator on one worker thread and iterate its events from the loop, in lock-step.

    ``async with`` it, ``async for`` over it, then read :attr:`result`. Leaving the block, however it is left (a normal
    end, an exception, ``aclose``, an anyio scope or a native ``task.cancel()``, once or repeatedly), stops the thread
    and waits until it has ended; a cancellation that lands during that wait is re-raised once, afterwards (see the
    module docstring). An exception of the run, a ``BaseException`` included, is raised by the ``__anext__`` that
    follows the last event handed over, which is where the sync generator would have raised it: no event is lost.

    ``stop`` is the flag the planner guard of ``events`` was made with (:func:`_unless_stopped`), so that stopping the
    planning also refuses the planner calls the thread's own check cannot reach."""

    def __init__(self, events: Generator[dict, None, AgentResult], limiter: anyio.CapacityLimiter,
                 stop: threading.Event):
        self._events, self._limiter, self._stop = events, limiter, stop
        self._send, self._receive = anyio.create_memory_object_stream[dict](0)   # unbuffered: a hand-over, not a queue
        self._resume = threading.Semaphore(0)   # the loop's "next event, please" (and the wake-up of a parked thread)
        self._finished = anyio.Event()          # the host task is done: the thread has ended and the stream is closed
        self._task: asyncio.Task | None = None
        self._result: AgentResult | None = None
        self._error: BaseException | None = None
        self._exhausted = False

    def _hand_over(self, event: dict) -> bool:
        """Give ``event`` to the loop (blocks until it has taken it); False when the consumer is gone."""
        try:
            anyio.from_thread.run(self._send.send, event)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            return False
        return True

    def _drive(self) -> AgentResult | None:
        """The worker thread: resume ``run_agent`` only while the consumer is asking for events. Returns its result,
        or None when stopped (also when the planner guard refused a call: that is a stop like any other);
        ``run_agent`` is closed (its own ``GeneratorExit`` handling runs) on every way out."""
        try:
            while not self._stop.is_set():
                try:
                    event = next(self._events)
                except StopIteration as end:
                    return end.value
                except _PlanningStopped:
                    return None
                if self._stop.is_set() or not self._hand_over(event):
                    break
                self._resume.acquire()
            return None
        finally:
            self._events.close()

    async def _host(self) -> None:
        try:
            self._result = await anyio.to_thread.run_sync(self._drive, limiter=self._limiter, abandon_on_cancel=False)
        except anyio.get_cancelled_exc_class():
            raise                               # the host task's own cancellation is not the run's failure
        except BaseException as e:  # noqa: BLE001 - __anext__ raises it on the consumer's side (as the sync code does)
            self._error = e
        finally:
            self._send.close()                  # on the loop: anyio streams are not thread-safe
            self._finished.set()

    def __aiter__(self) -> "_Planning":
        return self

    async def __anext__(self) -> dict:
        if self._exhausted:
            raise StopAsyncIteration
        # Waking the thread (or starting it) is what starts the next PAID planner call: a cancelled scope must raise
        # here, before that, not at the ``receive`` below, which the woken thread can outrun.
        await anyio.lowlevel.checkpoint()
        if self._task is None:                  # the thread starts on the first request for an event
            self._task = asyncio.create_task(self._host())
        else:
            self._resume.release()              # the previous event is consumed: let the thread go on
        try:
            return await self._receive.receive()
        except anyio.EndOfStream:
            self._exhausted = True
        error, self._error = self._error, None
        if error is not None:
            raise error
        raise StopAsyncIteration

    @property
    def result(self) -> AgentResult:
        if self._result is None:
            raise RuntimeError("the planning run has not finished")
        return self._result

    async def aclose(self) -> None:
        """Stop the thread (it checks before resuming ``run_agent``, and the planner guard before every planner call),
        wake it, fail a hand-over in flight, and wait for it under a shield that a native ``task.cancel()`` cannot cut
        short: the planner's ledger is final when this returns. A cancellation that landed during the wait is
        re-raised once, after the failure of the run's own close (if any) has been logged. Safe to call after a normal
        end. When the scope was cancelled before the first event was asked for, no host task exists to close the
        sending end, so it is closed here."""
        self._stop.set()
        self._resume.release()
        self._receive.close()
        cancelled = None
        if self._task is None:
            self._send.close()
        else:
            cancelled = await _wait_through_cancellation(self._finished.wait, self._finished.is_set)
        if self._error is not None:             # a failure nobody asked for (the run was already being abandoned)
            logger.warning("closing the planning run failed: %s: %s", type(self._error).__name__,
                           redact_secret_shaped(str(self._error)))
        if cancelled is not None:
            raise cancelled

    async def __aenter__(self) -> "_Planning":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


def _may_block(tracer: Tracer | None) -> bool:
    """False for a tracer that says it does nothing (``enabled`` is False: an unsampled request): its flush is cheap."""
    return tracer is not None and getattr(tracer, "enabled", True) is not False


async def _aflush(tracer: Tracer, *, off_loop: bool, limiters: "Limiters") -> None:
    """``tracer.flush()``. An app-level tracer's flush waits for the network, so it runs on a thread (shielded: it must
    also run for a cancelled stream, and a native ``task.cancel()`` must not end the wait for it: the hop is its own
    task, awaited through :func:`_wait_through_cancellation`, and a cancellation is re-raised once the flush ended)."""
    if not off_loop:
        tracer.flush()
        return
    hop = asyncio.ensure_future(anyio.to_thread.run_sync(tracer.flush, limiter=limiters.db))
    cancelled = await _wait_through_cancellation(lambda: asyncio.wait({hop}), hop.done)
    if cancelled is not None:
        if not hop.cancelled():
            hop.exception()                     # retrieved: a failure the cancellation outranks is not reported twice
        raise cancelled
    hop.result()


def _report_abandoned(ledger: Ledger, planner_model: str, terminal_emitted: bool) -> None:
    """The sync ``_run``'s report of an abandoned stream: best effort, never raises (a broken ``usage_cost`` must not
    replace the exit that is under way). The wording is the sync one, byte for byte."""
    try:
        spend = usage_cost(ledger.usage, planner_model) or 0.0
    except Exception:  # noqa: BLE001 - see above
        spend = 0.0
    if spend and not terminal_emitted:
        logger.warning("the agent stream was abandoned before a terminal event; the planner had already cost $%.6f",
                       spend)


def _split_kwargs(stream_kwargs: dict, timeout) -> tuple[dict, dict]:
    """``(the prefetch's arguments, the writer's)`` exactly as the sync ``_run`` splits them."""
    prefetch = {k: stream_kwargs[k] for k in _PREFETCH_ARGS if k in stream_kwargs}
    writer = {k: v for k, v in stream_kwargs.items() if k not in _PREFETCH_ARGS}
    if timeout is not None:
        writer["timeout"] = timeout
    return prefetch, writer


async def aagent_answer_stream(question: str, driver, embedder, strategy: str = "agent", *, limiters: "Limiters",
                               timeout=None, max_tokens: int = 1200, escalation_model: str | None = None,
                               settings=None, planner=None, tracer=None, llm_stream=None, escalation_stream=None,
                               **stream_kwargs) -> AsyncIterator[dict]:
    """Async twin of :func:`semigraph.agent.stream.agent_answer_stream`: an async generator of the same event dicts
    (``step`` events, then ``retrieval``, ``delta``*, ``done`` | ``error``; ``done`` carries the ``agent`` object, and
    the planner's dollars are folded into the ``cost_usd`` of both terminal events).

    ``limiters`` (:class:`semigraph.serve.limiters.Limiters`) bounds the planning thread (``db``) and the writer's
    checks. ``llm_stream`` / ``escalation_stream`` are ``callable(prompt) -> async iterable[str]``; ``k_chunks`` /
    ``hops`` configure the prefetch and every other keyword goes to the writer, as in the sync stream. See the module
    docstring for the planning thread, the disconnect behaviour and its limits."""
    settings = settings if settings is not None else get_settings()
    safe, planner_model = as_safe(tracer), settings.agent_planner_model
    prefetch, writer_kwargs = _split_kwargs(stream_kwargs, timeout)
    # Kept here, not inside ``run_agent``, so it survives an abandoned stream (the sync code's reason too).
    ledger, terminal_emitted = Ledger(), False
    stop = threading.Event()                    # set when the consumer leaves; read by the thread and the planner guard
    planning_events = run_agent(question, driver, embedder,
                                planner=_unless_stopped(planner or LiteLLMPlanner(planner_model), stop),
                                planner_model=planner_model, limits=Limits.from_settings(settings), tracer=safe,
                                ledger=ledger, **prefetch)
    try:
        try:
            with safe.span("agent", strategy=strategy, planner_model=planner_model,
                           question_chars=len(question)) as span:
                async with _Planning(planning_events, limiters.db, stop) as planning:
                    async for event in planning:
                        yield event
                plan = planning.result
                planner_cost = _planner_cost(plan, planner_model)
                agent = _agent_info(plan, planner_model, planner_cost)
                span.set(tool_calls=len(plan.tool_calls), model_calls=plan.model_calls,
                         fallback_reason=plan.fallback_reason, stop_reason=plan.stop_reason,
                         planner_cost_usd=planner_cost)
                # A cancelled scope that the shielded join absorbed (or that landed since) must stop the stream here:
                # past this point the first thing the writer does is to ask for a paid model call.
                await anyio.lowlevel.checkpoint()
                writer = astream_answer_for_context(
                    question, plan.r, strategy, llm_stream=llm_stream, escalation_model=escalation_model,
                    escalation_stream=escalation_stream, max_tokens=max_tokens, limiters=limiters, **writer_kwargs)
                try:
                    async with aclosing(writer) as events:
                        async for event in events:
                            event = _fold_spend(event, planner_cost, agent)
                            if event["event"] in ("done", "error"):
                                span.set(outcome=event["event"], cost_usd=event.get("cost_usd"))
                                terminal_emitted = True
                            yield event
                except Exception as e:  # noqa: BLE001 - the planner's spend must not vanish with an unexpected failure
                    if not planner_cost:
                        raise                       # nothing was spent by the agent: exactly the fixed path's behaviour
                    logger.exception("the answer phase failed after the planner had cost $%.6f", planner_cost)
                    span.set(outcome="error", cost_usd=planner_cost)
                    terminal_emitted = True
                    yield {"event": "error", "detail": f"{type(e).__name__}: {e}", "partial": "", "usage": None,
                           "cost_usd": planner_cost, "strategy": strategy}
        finally:
            await _aflush(safe, off_loop=_may_block(tracer), limiters=limiters)
    except (GeneratorExit, asyncio.CancelledError):
        _report_abandoned(ledger, planner_model, terminal_emitted)
        raise
