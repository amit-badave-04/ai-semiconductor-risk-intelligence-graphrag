"""The per-ask state of a paid answer stream (M5a I2 and I4, docs/v2/M5A_BUILD_PLAN.md sections 0, 3 and 4).

:class:`PaidStream` replaces the sync ``routes._paid_stream``. It changes HOW an answer is streamed, not the policy.

``events()`` is an async generator that holds no thread while it waits for the model (the twins in ``retrieval`` and
``agent`` put every blocking hop on a worker thread). Every call to the state backend that touches the store is a thread
hop under ``limiters.state`` followed by a checkpoint (:func:`state_call`; the settle waits for a slot as long as it
takes, :func:`settle_call`; the memory-only ``kill_level`` and ``mark_started`` are called directly); the tracer close is
a hop under ``limiters.db``.

* **The lease.** The route took it, before the stream existed: ``state.reserve`` checked the kill level, both daily
  caps, the per-address cap and the in-flight cap, counted the ask and wrote its durable ``reserved`` ledger row
  (docs/v2/ M5_DECISIONS.md 2.2). The stream is handed that :class:`~semigraph.serve.state.Lease` and owes the backend
  exactly one ``reconcile`` for it. A full in-flight cap is therefore a pre-stream HTTP 429 (``routes``), never an event
  of the stream. When ``events()`` starts, before the twin is awaited, the stream calls ``mark_started``: from then on
  the maintenance thread renews the lease, so the lease TTL has to cover only a stream that never started (not one whose
  first event is slow).
* **The paid-call meter** (``serve.meter``, council 4 option C). Each ask has ONE :class:`~semigraph.serve.meter.PaidMeter`,
  created with the stream (:attr:`PaidStream.meter`) and handed to the twin as the keyword-only argument ``meter`` (never
  through ``**extra``, so it cannot reach anything but the twin). The twin records every paid model call the moment it
  starts (with the most it could cost) and the usage the provider reports when it ends. The lease is settled at what
  those calls cost: never less than the cost the provider REPORTED, and the estimate caps only what is a bound (a call
  still running): see :meth:`PaidStream._read_meter` and ``PaidMeter.charge_micro``. A meter that cannot be READ is
  not an EMPTY one: the cost is then unknown and the estimate stays charged. DETECTION keys on a twin parameter literally
  named ``meter``: a twin that does not declare it (every test double that takes ``**kw``, a wrapper that forwards it
  blindly) is not given one, and since it cannot record anything an empty meter is no evidence of an unpaid ask: it is
  settled exactly as before the meter existed. A twin that was never CALLED (the stream never ran, or its function could
  not even be chosen) made no paid call whatever it is: it is settled at 0, still counted, as ``routes._abandon``
  settles a lease no stream took.
* **The money rule.** For the terminal event (``done`` or ``error``) the lease is reconciled BEFORE the event is
  yielded (``outcome`` ``done`` or ``error``, the twin's usage and its cost in micro-dollars: the metered charge when
  the meter recorded a call or a fault, else the cost the event reports, ``None`` when that is unknown: the backend
  then keeps the estimate), then (``done`` only) the answer-cache write under the sync rule, the info line
  and the failed-checks warning. That whole block runs in a shielded scope and takes ONE checkpoint, after it: a
  disconnect landing between the two writes must not drop the cache write or the settled usage. The flag that says "the
  lease is settled" is set the moment the reconcile returns, before that checkpoint, so a cancellation raised there can
  never lead to a second settle (the backend is idempotent anyway). The cache write is an optimisation, not part of the
  answer: when it fails after the reconcile, the failure is logged and the ``done`` event is still sent. A reconcile
  that raises does not block the terminal event either: the lease is reserved on the ledger, the backend queues a failed
  settle for a retry (``serve.state.settle_queue``), and the sweep or the next boot charges the estimate otherwise.
  The meter is closed just before the terminal settle reads it: a paid call that starts later is counted and logged
  (``paid call after settlement``) but the lease is settled and is not settled again.
* **Failures after the settle.** Once the lease is settled the ask is on the ledger once. An exception from then on (the
  twin raising after it yielded ``done``, a malformed terminal event) is logged and nothing else: no second settle, no
  ``error`` event after a ``done`` the client already has. A malformed event that never reached the client still gets
  the generic ``error`` event: the page waits for ``done`` or ``error``.
* **Cleanup is ``finalize()``, and only that.** It is idempotent and shielded and does not raise for an ordinary
  failure. In order: the twin's generator is closed (so the upstream model stream closes; for an agent ask that joins
  the planning thread, up to seconds: a paid call still running there is billed whatever the client did, so its cost is
  known only once it has stopped), the request tracer is closed (on a worker thread: it can flush), the meter is closed,
  the lease is settled as ``abandoned`` when the client left before a terminal event (its cost is the metered charge: 0
  when no paid call started, the call's bound when it started and never reported; the estimate when the twin could not
  meter) and the drain count is given back. The settle used to come first, so that a process killed during the slow close
  still had the ask counted. It cannot come first now, and nothing is lost to a kill in the close: the row stays
  reserved, and the next boot settles it as ``abandoned_restart`` at the whole estimate (fail closed). The settle and the
  drain count are still given whatever the close raised (a ``finally``). A stream that never iterated (an exception
  between the reserve and the first event, a request cancelled before the response started) is settled by the same
  ``finalize`` as ``abandoned``, at 0: its twin was never called, so no paid call was possible (the same as
  ``routes._abandon``). ``events()`` calls it as its last statement,
  so the lease does not wait for the response to be sent (a client that stops reading can stall the closing chunk as
  long as it likes). It is also called from :class:`PaidResponse`: as the response's background task AND in a
  ``finally`` around the whole ASGI call, for the paths on which ``events()`` never reaches its end: sse-starlette runs
  the background task only when the response ends cleanly (a send timeout or a send error raises past it, and a
  generator suspended at its ``yield`` is dropped, not closed). ``events()`` itself has no ``finally``.
* **The drain count.** The route counted this stream on ``DRAIN`` (``try_enter``) before it reserved, so a drain that
  begins while the reserve runs still waits for the stream; ``finalize`` gives that count back, exactly once and last
  (after the closes and the settle), so ``DRAIN.active == 0`` means the ledger row is settled and the twin is closed.
  ``PaidResponse`` takes no ``shutdown_grace_period``: the drain (``serve.drain``) tells sse-starlette to end the
  streams only when it is over, and a positive grace would stack on top of it.

A twin that does not return an async generator is a :class:`TwinContractError` (a ``TypeError``): there is no sync
fallback and no adapter, and it is not turned into an error event (it is a wiring mistake; the lease is settled as
abandoned and charged what its meter says it started, the estimate when the twin could not meter or the meter cannot be
read: the one rule for every lease that never reached a terminal event).
"""

import inspect
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from functools import partial
from typing import Any, NamedTuple

import anyio
import anyio.lowlevel
import anyio.to_thread
from sse_starlette import EventSourceResponse, ServerSentEvent
from sse_starlette.sse import SendTimeoutError
from starlette.background import BackgroundTask
from starlette.datastructures import State
from starlette.types import Message, Receive, Scope, Send

from ..graph.client import STATE_OP_TIMEOUT_DEFAULT_S
from ..retrieval.answerer_async import aanswer_stream
from ..retrieval.verify import checks_failed
from ..retrieval.workspace_async import astream_workspace_answer
from . import drain, guard, tracing
from .meter import PaidMeter
from .state import Lease, StateUnavailable, usd_to_micro

logger = logging.getLogger("semigraph.serve")

# the route's 429 on a full in-flight cap
MSG_BUSY = "The service is busy answering other questions — try again in a moment."
MSG_FAILED = "The answer could not be completed — please try again."
TERMINAL_EVENTS = ("done", "error")
PING_SECONDS = 15
UNKNOWN_ERROR = "unknown error"
METER_PARAMETER = "meter"      # the keyword-only parameter a twin declares to be handed the ask's PaidMeter
METER_CHARGED, METER_EMPTY, METER_UNREADABLE = "charged", "empty", "unreadable"    # the states of a MeterReading
_CLASS_NAME_RE = re.compile(r"[A-Za-z_][\w.]{0,99}")


class MeterReading(NamedTuple):
    """What an ask's meter says. ``state`` is ``charged`` (it recorded a call or a fault: ``charge_micro`` is what they
    cost), ``empty`` (it can be read and holds nothing) or ``unreadable`` (reading it raised: nothing is known, which is
    not the same as nothing being spent)."""

    state: str
    charge_micro: int | None = None


class TwinContractError(TypeError):
    """The answer stream function did not return an async generator (a wiring mistake)."""


class NoStateSlot(StateUnavailable):
    """No state slot was free in time: the call did NOT run. Any other StateUnavailable comes from a call that ran and
    failed, which may have changed state (a kill-level tightening held in memory, a lease granted); this one never did,
    so a caller that must know the difference (the admin flip) can tell."""

async def slot_call(limiter: anyio.CapacityLimiter, wait_s: float | None, fn: Callable[..., Any], *args: Any,
                    **kwargs: Any) -> Any:
    """``fn(*args, **kwargs)`` on a worker thread, holding one token of ``limiter``, then a checkpoint (a shielded thread
    hop does not deliver a cancellation on its own).

    Only the WAIT for the token is bounded: ``wait_s`` seconds (``None``: as long as it takes), then NoStateSlot (a
    StateUnavailable), and ``fn`` never ran. Once the token is held the call is never abandoned: abandoning a thread
    that a shielded reserve runs in would let the reserve finish after its caller gave up. Its own bound is in the work
    (the state driver's attempt timeouts and the server-side transaction timeout, ``serve.state.backend``).

    Why the token is taken here and not by ``run_sync(limiter=limiter)`` under a ``move_on_after``: ``run_sync`` is
    shielded once it holds the token, so a deadline that passes during the work would still fire at the next checkpoint
    and throw away the result of a call that completed (a granted lease would be lost). Taking the token ourselves
    puts the deadline on the wait alone. ``run_sync`` is then given a limiter of its own that nobody else uses: it
    takes its token at once, and handing it ``limiter`` too would be a second acquire by the same task (a
    RuntimeError). A cancellation (or the deadline) that arrives while waiting leaves no token behind, and the token
    that was granted is released in the ``finally``, whatever the work did."""
    with anyio.move_on_after(wait_s) as waiting:
        await limiter.acquire()
    if waiting.cancelled_caught:
        raise NoStateSlot(f"no state slot was free within {wait_s:g} s")
    try:
        result = await anyio.to_thread.run_sync(partial(fn, *args, **kwargs), limiter=anyio.CapacityLimiter(1))
    finally:
        limiter.release()
    await anyio.lowlevel.checkpoint()
    return result


def _slot_wait_s(st: State) -> float:
    return getattr(st.settings, "state_op_timeout_s", STATE_OP_TIMEOUT_DEFAULT_S)


async def state_call(st: State, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """One call to the state backend, the one idiom for every caller that has a refusal to fall back on (the routes'
    503 paths, the answer-cache write): a worker thread under ``limiters.state``, and a wait for a free slot of at most
    ``state_op_timeout_s``. With every slot held by calls stuck on a silent database the call is refused with
    StateUnavailable instead of queueing behind them for as long as they take. See :func:`slot_call`."""
    return await slot_call(st.limiters.state, _slot_wait_s(st), fn, *args, **kwargs)


async def settle_call(st: State, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """A state call that must not be dropped: the settle of a lease. Waits for a slot as long as it takes. A reconcile
    that gave up would leave its lease registered (the maintenance thread renews exactly those) and its in-flight slot
    taken for the life of the process; there is no refusal to answer here, only an ask that has been paid for."""
    return await slot_call(st.limiters.state, None, fn, *args, **kwargs)


async def admin_call(st: State, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """A state call of an admin route, on the admin limiter (one token): a kill-level flip or the state report is never
    queued behind the public traffic of ``limiters.state``. The wait for the token is bounded like a public call's."""
    return await slot_call(st.limiters.admin, _slot_wait_s(st), fn, *args, **kwargs)


def cost_micro_of(cost_usd: object) -> int | None:
    """A twin's ``cost_usd`` as whole micro-dollars; ``None`` (the backend then keeps the estimate) when it is unknown,
    negative, not finite or not a number."""
    if cost_usd is None:
        return None
    try:
        return usd_to_micro(cost_usd)
    except (TypeError, ValueError):
        return None


def sse_event(event: dict) -> ServerSentEvent:
    return ServerSentEvent(data=json.dumps(event, default=str), event=event["event"], sep="\n")


def _checks_failed(done: dict) -> bool:
    """True when the answer's ``checks`` report a problem (an event without ``checks`` reports none). The predicate is
    ``verify.checks_failed``: the one definition example seeding and the page share (ungrounded or question-echoed
    figures, an unretrieved citation, a pseudo-citation, an unsupported removal claim, an uncited non-refusal)."""
    return checks_failed(done.get("checks"))


def _loggable_checks(checks: dict | None, private: bool) -> dict | None:
    """The ``checks`` block as it may be logged. For a workspace answer every list (answer sentences, bracketed text,
    numbers) becomes its LENGTH: those strings paraphrase a private upload (docs/v2/M4_PLAN.md 5: no uploaded text in
    logs)."""
    if not private or not isinstance(checks, dict):
        return checks
    return {k: (len(v) if isinstance(v, (list, tuple, set)) else v) for k, v in checks.items()}


def _warn_on_failed_checks(done: dict, private: bool = False) -> None:
    """An answer that cannot escalate (routed straight to the strong model, or streamed live) is released whatever the
    deterministic checks find: say so in the log instead of letting it pass silently."""
    if _checks_failed(done):
        logger.warning("answer released with failed checks (routed=%s escalated=%s by=%s): %s", done.get("routed"),
                       done.get("escalated"), done.get("answered_by"), _loggable_checks(done.get("checks"), private))


def _class_name_of(detail: object) -> str:
    """The exception class name an error event's ``detail`` (``"ClassName: message"``) starts with: the part before the
    first colon, and only when it looks like a class name. Anything else may be text a provider quoted, so it is not
    returned."""
    head = str(detail).split(":", 1)[0].strip()
    return head if _CLASS_NAME_RE.fullmatch(head) else UNKNOWN_ERROR


def _nothing() -> None:
    return None


def accepts_meter(twin: Callable) -> bool:
    """True when ``twin`` declares a parameter literally named ``meter`` that can be passed by keyword. ``**kwargs`` does
    not count: nearly every test double takes it and records nothing, and a twin that cannot record must not be taken for
    one that did (its empty meter would settle an abandoned ask at zero). A signature that cannot be read is "no"."""
    try:
        parameter = inspect.signature(twin).parameters.get(METER_PARAMETER)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                        inspect.Parameter.KEYWORD_ONLY)


def select_twin(strategy: str, workspace: bool = False, *, sec: Callable | None = None,
                workspace_twin: Callable | None = None) -> Callable:
    """The async event-stream function for a strategy. A workspace ask always goes to the workspace writer (``strategy``
    is already restricted to ``hybrid`` for it). The agent package (langgraph) is imported only when an ``agent``
    question is actually served, so a deployment with ``AGENT_ENABLED`` off never needs it installed. ``routes`` passes
    its own module-level names as ``sec`` and ``workspace_twin`` so that a test can replace them there; without them
    this module's own names are used (looked up when called)."""
    if workspace:
        return workspace_twin or astream_workspace_answer
    if strategy != guard.AGENT_STRATEGY:
        return sec or aanswer_stream
    from ..agent.stream_async import aagent_answer_stream
    return aagent_answer_stream


def stream_extras(st: State, question: str, strategy: str) -> tuple[dict, Callable[[], None]]:
    """The extra keyword arguments of the agent stream and the cleanup to run when the request ends (nothing for the
    fixed path).

    ``st.tracer`` is a FACTORY (``tracing.LangfuseTracer.for_request``): the per-request tracer draws the sampling
    decision once and keeps its own span stack, which one shared object could not do for concurrent requests. The
    cleanup closes it (it can flush, so :class:`PaidStream` runs it on a worker thread) and runs on every path. A
    tracer without ``for_request`` is passed through as is and is never closed here. A failing tracer never breaks an
    answer."""
    if strategy != guard.AGENT_STRATEGY:
        return {}, _nothing
    tracer = getattr(st, "tracer", None)
    factory = getattr(tracer, "for_request", None)
    if not callable(factory):
        return {"settings": st.settings, "tracer": tracer}, _nothing
    try:
        request_tracer = factory(question, strategy=strategy)
    except Exception:  # noqa: BLE001 - tracing must never break an answer
        logger.exception("creating the request tracer failed; answering untraced")
        return {"settings": st.settings, "tracer": None}, _nothing

    def close() -> None:
        try:
            request_tracer.close()
        except Exception:  # noqa: BLE001
            logger.exception("closing the request tracer failed")
    return {"settings": st.settings, "tracer": request_tracer}, close


class PaidStream:
    """One paid ask holding ``lease``: retrieval -> LLM deltas -> done. ``workspace`` (``{"workspace_id", "as_of"}``)
    routes to the workspace writer and disables the answer cache for this answer. ``twin`` is the answer stream function
    (an async generator function; ``routes`` resolves it per request, the default is :func:`select_twin`). A twin that
    declares a keyword parameter named ``meter`` is handed this ask's :attr:`meter` (:func:`accepts_meter`) and the lease
    is settled at what the calls it records cost. The caller has counted the stream on the drain (``DRAIN.try_enter()``)
    and the stream gives that count back in ``finalize``."""

    def __init__(self, st: State, question: str, strategy: str, iph: str, snapshot_id: str = "",
                 workspace: dict | None = None, *, lease: Lease, twin: Callable | None = None):
        self._st, self._question, self._strategy, self._iph = st, question, strategy, iph
        self._snapshot_id, self._workspace, self._twin = snapshot_id, workspace, twin
        self._lease, self._backend, self._drain = lease, st.state, drain.DRAIN
        self._in_workspace = workspace is not None
        self._meter = PaidMeter(st.settings)   # this ask's paid calls; handed to the twin only if it declares ``meter``
        self._metered = False                  # the twin was handed the meter, so an empty meter means "nothing was spent"
        self._twin_called = False              # the twin was invoked; until then no paid call was possible at all
        self._failed_settle = False            # a done / error settle raised: the cost the twin knew is not on the ledger
        self._stream = None                    # the twin's async generator
        self._close_tracer: Callable[[], None] = _nothing
        self._marked = False                   # mark_started was called (the maintenance thread renews the lease)
        self._settled = False                  # the backend reconciled the lease (or a retry of it is queued there)
        self._finalized = False

    @property
    def meter(self) -> PaidMeter:
        """The ask's paid-call meter (read-only: the twin records into it, :meth:`finalize` closes it)."""
        return self._meter

    # ---- the stream

    async def events(self) -> AsyncIterator[ServerSentEvent]:
        """The SSE events of this ask (what ``sse_event`` makes of each event dict). :meth:`finalize` gives the lease
        and the drain count back, and this generator calls it as its last statement: after the last event has been taken
        by the consumer, not in a ``finally`` (a consumer that goes away, a contract error and a cancellation are
        :class:`PaidResponse`'s to clean up)."""
        terminal_sent = False                  # the client has been handed a done or error event
        self._mark_started()                   # before the twin has yielded anything (see _mark_started)
        try:
            async for ev in self._open_stream():
                terminal = ev["event"] in TERMINAL_EVENTS
                out = await self._settle(ev) if terminal else ev
                yield sse_event(out)
                terminal_sent = terminal_sent or terminal
        except TwinContractError:
            raise
        except Exception as e:  # noqa: BLE001 — report, never hang the stream
            if self._settled:
                # No second settle (the ask is already counted). If the client never got its terminal event (the writes
                # after it raised, or it was malformed) it still gets the generic error: the page waits for done or error.
                logger.error("answer failed after its ledger row was settled (%s)", self._failure_text(e))
                if not terminal_sent:
                    yield sse_event(self._generic_failure(e))
            else:
                self._log_failure("answer failed", e)
                yield sse_event(await self._record_failure(e))
        await self.finalize()

    def _failure_text(self, e: Exception) -> str:
        """An exception as it may be logged WITHOUT a traceback. A provider's exception can quote the text it was given,
        which for a workspace ask may be an upload (docs/v2/M4_PLAN.md 5: no uploaded text in logs): only the class name
        then. A public ask logs the message too, with secret-shaped substrings redacted."""
        name = type(e).__name__
        return name if self._in_workspace else tracing.redact_secret_shaped(f"{name}: {e}")

    def _log_failure(self, message: str, e: Exception) -> None:
        """``logger.exception`` for a public ask; for a workspace ask the class name only, as its traceback carries
        ``str(e)``."""
        if self._in_workspace:
            logger.error("%s (%s)", message, type(e).__name__)
        else:
            logger.error(message, exc_info=e)

    def _open_stream(self):
        st, s = self._st, self._st.settings
        extra, self._close_tracer = ((dict(self._workspace), _nothing) if self._in_workspace
                                     else stream_extras(st, self._question, self._strategy))
        twin = self._twin or select_twin(self._strategy, self._in_workspace)
        metering = {METER_PARAMETER: self._meter} if accepts_meter(twin) else {}
        self._metered = bool(metering)         # set before the call: a twin may start a paid call while it is being called
        self._twin_called = True               # likewise: from here on a paid call may have started, whatever the call does
        stream = twin(self._question, st.driver, st.embedder, strategy=self._strategy, timeout=s.llm_request_timeout_s,
                      max_tokens=s.llm_answer_max_tokens, escalation_model=s.escalation_model or None,
                      limiters=st.limiters, **extra, **metering)
        if not (hasattr(stream, "__aiter__") and hasattr(stream, "aclose")):
            getattr(stream, "close", _nothing)()
            raise TwinContractError(
                f"the answer stream function returned {type(stream).__name__}, not an async generator")
        self._stream = stream
        return stream

    def _mark_started(self) -> None:
        """The stream runs: the lease is registered for renewal. This happens before the twin is awaited, not on its
        first event: retrieval can take longer than the lease TTL before it yields anything, and the sweep reclaims a
        lease that is not registered (a second stream would be admitted next to this one, which would then be ledgered
        abandoned). ``mark_started`` is a memory write on every backend (the protocol requires it), so it is called
        directly: no thread hop, no state slot to wait for. A failure is logged and changes nothing else (the lease then
        expires after its TTL like any other that nobody renews; the reconcile still settles it)."""
        if self._marked:
            return
        self._marked = True
        try:
            self._backend.mark_started(self._lease.lease_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("marking the lease as started failed (%s)", type(e).__name__)

    # ---- the terminal event: the money rule

    async def _settle(self, ev: dict) -> dict:
        """The reconcile, then (done) the cache write, the info line and the warning; returns the event to send. The twin
        has yielded its terminal event, so the strong stream is closed and the planning thread joined: the meter is closed
        here, then read."""
        with anyio.CancelScope(shield=True):
            self._close_meter()
            await self._reconcile(ev["event"], usage=ev.get("usage"),
                                  cost_micro=self._terminal_cost_micro(ev.get("cost_usd")))
            out = await self._after_done(ev) if ev["event"] == "done" else self._after_error(ev)
        await anyio.lowlevel.checkpoint()
        return out

    async def _reconcile(self, outcome: str, *, usage: dict | None = None, cost_micro: int | None = None) -> None:
        """Settle the lease on a worker thread at ``cost_micro`` micro-dollars; ``None`` means the cost is unknown and the
        backend keeps the estimate (an ``abandoned`` ask is charged exactly what it is given: the caller decides, see
        :meth:`_abandoned_cost_micro`). A raise is logged, never propagated: the lease stays reserved on the ledger, which
        is what the daily ceilings count, until a retry, the sweep or the next boot charges it."""
        try:
            await settle_call(self._st, self._backend.reconcile, self._lease.lease_id, outcome=outcome, usage=usage,
                              cost_micro=cost_micro)
        except Exception as e:  # noqa: BLE001
            logger.error("settling the ask failed (%s): its estimate stays charged", type(e).__name__)
            self._failed_settle = self._failed_settle or outcome != "abandoned"
            return
        self._settled = True

    # ---- what an ask is charged

    def _read_meter(self) -> MeterReading:
        """What the ask's meter says: ``charged`` (it recorded a call or a fault; the charge is what the calls cost, never
        less than what the provider reported and otherwise not more than the estimate: see ``PaidMeter.charge_micro``),
        ``empty`` (it can be read and holds nothing) or ``unreadable`` (reading it raised, so nothing is known). The last
        two are NOT the same: an empty meter on a metered twin is a call that was never made, an unreadable one is a meter
        that may be hiding calls that were, and the estimate stays charged."""
        try:
            if not (self._meter.calls or self._meter.faults):
                return MeterReading(METER_EMPTY)
            return MeterReading(METER_CHARGED, self._meter.charge_micro(self._lease.estimate_micro))
        except Exception:  # noqa: BLE001 - the meter never decides whether an ask is settled
            logger.exception("reading the paid-call meter failed; the estimate stays charged")
            return MeterReading(METER_UNREADABLE)

    def _terminal_cost_micro(self, cost_usd: object) -> int | None:
        """The cost a done / error settle carries: the metered charge when the meter recorded anything; the estimate
        (``None``) when it cannot be read; else the cost the event reports (``None`` when that is unknown or unusable)."""
        reading = self._read_meter()
        if reading.state == METER_CHARGED:
            return reading.charge_micro
        return None if reading.state == METER_UNREADABLE else cost_micro_of(cost_usd)

    def _failure_cost_micro(self) -> int | None:
        """The cost of an ask whose twin raised (or could not be chosen): the metered charge when the meter recorded
        anything. With nothing recorded the cost is unknown (the twin broke before it said what it spent) and the estimate
        stays charged, unless the twin was never called: no paid call was possible then, so it is ``0``."""
        reading = self._read_meter()
        if reading.state == METER_CHARGED:
            return reading.charge_micro
        return 0 if reading.state == METER_EMPTY and not self._twin_called else None

    def _abandoned_cost_micro(self) -> int | None:
        """The cost an ask pays that never reached a terminal event: the metered charge when the meter recorded anything;
        the estimate (``None``) when the meter cannot be read. An empty meter means the ask spent nothing (``0``) when the
        twin was never called (no paid call was possible: the route's own ``_abandon`` settles the same case at 0) or when
        it was handed the meter, and its settle was not a done / error one that failed (the twin knew a cost the ledger
        never got); otherwise the cost is unknown and the backend charges the estimate."""
        reading = self._read_meter()
        if reading.state == METER_CHARGED:
            return reading.charge_micro
        if reading.state == METER_UNREADABLE:
            return None
        return 0 if not self._twin_called or (self._metered and not self._failed_settle) else None

    def _close_meter(self) -> None:
        try:
            self._meter.close()
        except Exception:  # noqa: BLE001
            logger.exception("closing the paid-call meter failed")

    async def _on_db_thread(self, fn: Callable[[], object]) -> None:
        await anyio.to_thread.run_sync(fn, limiter=self._st.limiters.db)

    def _cacheable(self, done: dict) -> bool:
        """A cached replay carries no ``checks`` (the store does not persist them), so an answer that failed any
        (including one that cites nothing without being a refusal) is not cached: it would otherwise look clean for the
        whole TTL. A workspace answer is never cached (it is private)."""
        return bool(not self._in_workspace and done["answer"].strip() and done["finish_reason"] != "length"
                    and not _checks_failed(done))

    async def _after_done(self, ev: dict) -> dict:
        if self._cacheable(ev):
            await self._cache_answer(ev)
        logger.info("answered strategy=%s citations=%d hallucinated=%d cost=%s routed=%s escalated=%s by=%s checks=%s",
                    self._strategy, len(ev["citations"]), len(ev["hallucinated"]), ev["cost_usd"], ev.get("routed"),
                    ev.get("escalated"), ev.get("answered_by"), _loggable_checks(ev.get("checks"), self._in_workspace))
        _warn_on_failed_checks(ev, self._in_workspace)
        return ev

    async def _cache_answer(self, ev: dict) -> None:
        """The answer cache is an optimisation, not part of the answer: the lease is already settled, so a failing write
        is logged (the exception, never the answer text) and the visitor still gets the answer they paid for."""
        try:
            await state_call(self._st, self._backend.cache_put, question=self._question, strategy=self._strategy,
                             answer=ev["answer"], citations=ev["citations"], hallucinated=ev["hallucinated"],
                             usage=ev["usage"], cost_usd=ev["cost_usd"], snapshot_id=self._snapshot_id)
        except Exception:  # noqa: BLE001
            logger.exception("caching the answer failed")

    def _after_error(self, ev: dict) -> dict:
        # The client-facing message below is already generic; ``ev["detail"]`` is not — it can be an f-string of a
        # provider exception's type and text (retrieval/answerer.py), which can itself quote a secret-shaped
        # substring (a key embedded in a provider's own error message) or, for a workspace ask, the uploaded text it
        # was given. Redact before it ever reaches the log; a workspace ask logs the class name only.
        detail = ev["detail"]
        logged = _class_name_of(detail) if self._in_workspace else tracing.redact_secret_shaped(detail)
        logger.warning("answer failed mid-stream: %s (cost=%s)", logged, ev.get("cost_usd"))
        return {"event": "error", "detail": MSG_FAILED}

    async def _record_failure(self, e: Exception) -> dict:
        """The twin (or the writes after its terminal event) raised: the lease is settled as ``error`` with no usage and
        the metered charge when the meter recorded a paid call; with none the cost is unknown (the twin broke before it
        said what it spent) and the estimate is charged, as the sync path wrote a row without usage, unless the twin was
        never called (it could not even be chosen): no paid call was possible, so 0. Called only while the lease is not
        settled."""
        with anyio.CancelScope(shield=True):
            self._close_meter()
            await self._reconcile("error", cost_micro=self._failure_cost_micro())
        await anyio.lowlevel.checkpoint()
        return self._generic_failure(e)

    @staticmethod
    def _generic_failure(e: Exception) -> dict:
        return {"event": "error", "detail": f"The answer could not be completed ({type(e).__name__})."}

    # ---- cleanup

    async def finalize(self) -> None:
        """Release everything this stream holds. Idempotent, shielded, and it does not raise for an ordinary failure:
        each step logs its own. The order: the twin (closing it joins an agent's planning thread, and a paid call still
        running there is billed whatever the client did: what an abandoned ask cost is known only once the twin has
        stopped), the tracer, the meter (a paid call that starts after this is counted and logged, not charged), the
        abandoned ask's settle, and the drain count last, whatever happened before. The settle and the drain count are in
        ``finally`` clauses: whatever closing the twin or the tracer raised, they still happen. A process killed in the
        slow close leaves the row reserved; the next boot charges its estimate (``abandoned_restart``)."""
        if self._finalized:
            return
        self._finalized = True
        with anyio.CancelScope(shield=True):
            try:
                try:
                    await self._close_upstream()
                finally:
                    await self._close_request_tracer()
            finally:
                try:
                    self._close_meter()
                    await self._settle_if_abandoned()
                finally:
                    self._leave_drain()

    async def _close_upstream(self) -> None:
        if self._stream is None:
            return
        try:
            await self._stream.aclose()
        except Exception as e:  # noqa: BLE001
            self._log_failure("closing the answer stream failed", e)

    async def _settle_if_abandoned(self) -> None:
        """The client went away before the terminal event, or the stream never ran (a buffered draft widens the window
        to the whole generation). The ask still counts against both daily ceilings (the backend counts every settled
        ask), and it is charged what its paid calls cost: see :meth:`_abandoned_cost_micro` (0 for a metered ask that
        started no call and for a stream whose twin was never called, the estimate when the twin could not meter or the
        meter cannot be read)."""
        if not self._settled:
            await self._reconcile("abandoned", cost_micro=self._abandoned_cost_micro())

    async def _close_request_tracer(self) -> None:
        if self._close_tracer is _nothing:
            return
        try:
            await self._on_db_thread(self._close_tracer)
        except Exception:  # noqa: BLE001
            logger.exception("closing the request tracer failed")

    def _leave_drain(self) -> None:
        try:
            self._drain.leave()
        except Exception:  # noqa: BLE001
            logger.exception("leaving the drain count failed")


class PaidResponse(EventSourceResponse):
    """The SSE response of a :class:`PaidStream`: ``finalize`` runs as the background task AND in a ``finally`` around
    the whole ASGI call (and ``events()`` runs it at its own end, which is what normally settles the lease).
    sse-starlette awaits the background task only when its task group exits cleanly, so a send timeout, a send error or
    a cancelled request would otherwise skip it and leak the lease (and the drain count). A middleware that re-wraps the
    body (Starlette's ``BaseHTTPMiddleware``) would bypass ``__call__`` altogether; the app uses none, and a pure-ASGI
    middleware leaves it alone.

    A client that stops reading is dropped by sse-starlette with ``SendTimeoutError``. That is an expected event, not a
    server fault: once ``finalize`` has run it is logged as one warning line (the exception's class name only: no
    traceback, nothing about the client) and swallowed, instead of reaching the ASGI server as an ``Exception in ASGI
    application`` traceback for every slow or dead client. Any other exception still propagates, after ``finalize``.

    sse-starlette puts no timeout on the closing empty chunk of the body (a bare send under the lock its ping also
    waits on): ``__call__`` bounds that one send by ``send_timeout`` too and raises the same ``SendTimeoutError``."""

    def __init__(self, stream: PaidStream, *, send_timeout: float | None = None):
        super().__init__(stream.events(), ping=PING_SECONDS, sep="\n", send_timeout=send_timeout,
                         background=BackgroundTask(stream.finalize))
        self._paid_stream = stream

    def _closing_send_bounded(self, send: Send) -> Send:
        """``send`` with the closing chunk of the body (``more_body`` false) under ``send_timeout``. sse-starlette
        bounds every event and every ping but sends this one bare, under the lock the ping also waits on: a transport
        that stops draining in the last frame would hold the response until the client goes away. Raising its own
        ``SendTimeoutError`` lets the existing handler treat it like any other dropped client. No timeout, no wrapper.
        """
        timeout = self.send_timeout
        if timeout is None:
            return send

        async def bounded(message: Message) -> None:
            if message["type"] != "http.response.body" or message.get("more_body", False):
                await send(message)
                return
            with anyio.move_on_after(timeout) as scope:
                await send(message)
            if scope.cancelled_caught:
                raise SendTimeoutError()
        return bounded

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        dropped: SendTimeoutError | None = None
        try:
            await super().__call__(scope, receive, self._closing_send_bounded(send))
        except SendTimeoutError as error:
            dropped = error
        finally:
            await self._paid_stream.finalize()
        if dropped is not None:
            logger.warning("client dropped on the send timeout (%s)", type(dropped).__name__)
