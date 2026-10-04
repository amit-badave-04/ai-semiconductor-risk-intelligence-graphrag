"""The per-ask state of a paid answer stream (M5a I2, docs/v2/M5A_BUILD_PLAN.md sections 0 and 4).

:class:`PaidStream` replaces the sync ``routes._paid_stream``. It changes HOW an answer is streamed, not the policy: the
in-flight cap, the gate order that precedes it, the ledger rows, the caching rule and the user-visible messages are the
sync generator's. Four things differ on purpose, all of them in what happens after the money is spent: a failing
answer-cache write is logged and no longer turns ``done`` into an error; an exception after the terminal ledger row was
written is logged and costs no second row and no error event; the slot and the cleanup are released when ``events()``
ends, not when the response has been sent; and the response's closing chunk is bounded by the send timeout.

``events()`` is an async generator that holds no thread while it waits for the model (the twins in ``retrieval`` and
``agent`` put every blocking hop on a worker thread). Each store call is a thread hop under ``limiters.db``.

* **The slot.** The in-flight cap is ``st.answer_limiter`` (an ``anyio.CapacityLimiter``), taken first and without
  waiting, under a private token. A full cap yields the busy ``error`` event and nothing else: no ledger row, no tracer.
* **The money rule.** For the terminal event (``done`` or ``error``) the ledger row is written BEFORE the event is
  yielded, then (``done`` only) the answer-cache write under the sync rule, the info line and the failed-checks warning.
  That whole block runs in a shielded scope and takes ONE checkpoint, after it: the sync path always reached the cache
  write once the answer was produced (its thread could not be interrupted), so a disconnect landing between the two
  writes must not drop the cache write or the ledger row's usage. The flag that says "a row exists" is set the moment
  the write returns, before that checkpoint, so a cancellation raised there can never lead to a second row. The cache
  write is an optimisation, not part of the answer: when it fails after the ledger row, the failure is logged and the
  ``done`` event is still sent (a second row for the same ask, and the generic error instead of the answer, would be
  the alternative). The info line and the warning cannot raise for a ``done`` event of the twin's shape.
* **Failures after the terminal row.** Once the ledger row exists the ask is on the ledger once. An exception from then
  on (the twin raising after it yielded ``done``, a malformed terminal event) is logged and nothing else: no second row,
  no ``error`` event after a ``done`` the client already has. A malformed event that never reached the client simply
  ends the stream without a terminal event.
* **Cleanup is ``finalize()``, and only that.** It is idempotent and shielded and does not raise for an ordinary
  failure. In order: the one ledger row without usage when the client left before a terminal event (first: the ask must
  be counted even if the process is killed during the slower steps), the twin's generator is closed (so the upstream
  model stream closes; for an agent ask that joins a thread, up to seconds), the request tracer is closed (on a worker
  thread: it can flush) and the slot is released. ``events()`` calls it as its last statement, so the slot does not wait
  for the response to be sent (a client that stops reading can stall the closing chunk as long as it likes). It is also
  called from :class:`PaidResponse`: as the response's background task AND in a ``finally`` around the whole ASGI call,
  for the paths on which ``events()`` never reaches its end: sse-starlette runs the background task only when the
  response ends cleanly (a send timeout or a send error raises past it, and a generator suspended at its ``yield`` is
  dropped, not closed). ``events()`` itself has no ``finally``.

A twin that does not return an async generator is a :class:`TwinContractError` (a ``TypeError``): there is no sync
fallback and no adapter, and it is not turned into an error event (it is a wiring mistake, nothing was spent).
"""

import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from functools import partial

import anyio
import anyio.lowlevel
import anyio.to_thread
from sse_starlette import EventSourceResponse, ServerSentEvent
from sse_starlette.sse import SendTimeoutError
from starlette.background import BackgroundTask
from starlette.datastructures import State
from starlette.types import Message, Receive, Scope, Send

from ..retrieval.answerer_async import aanswer_stream
from ..retrieval.verify import checks_failed
from ..retrieval.workspace_async import astream_workspace_answer
from . import guard, store, tracing

logger = logging.getLogger("semigraph.serve")

MSG_BUSY = "The service is busy answering other questions — try again in a moment."
MSG_FAILED = "The answer could not be completed — please try again."
TERMINAL_EVENTS = ("done", "error")
PING_SECONDS = 15
UNKNOWN_ERROR = "unknown error"
_CLASS_NAME_RE = re.compile(r"[A-Za-z_][\w.]{0,99}")


class TwinContractError(TypeError):
    """The answer stream function did not return an async generator (a wiring mistake)."""


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
    """One paid ask: slot -> retrieval -> LLM deltas -> done. ``workspace`` (``{"workspace_id", "as_of"}``) routes to
    the workspace writer and disables the answer cache for this answer. ``twin`` is the answer stream function (an
    async generator function; ``routes`` resolves it per request, the default is :func:`select_twin`)."""

    def __init__(self, st: State, question: str, strategy: str, iph: str, snapshot_id: str = "",
                 workspace: dict | None = None, *, twin: Callable | None = None):
        self._st, self._question, self._strategy, self._iph = st, question, strategy, iph
        self._snapshot_id, self._workspace, self._twin = snapshot_id, workspace, twin
        self._in_workspace = workspace is not None
        self._token: object | None = None      # the in-flight slot, while this stream holds it
        self._stream = None                    # the twin's async generator
        self._close_tracer: Callable[[], None] = _nothing
        self._started = False                  # paid work may have begun (the twin is being called or iterated)
        self._ledgered = False                 # a ledger row exists for this ask
        self._finalized = False

    # ---- the stream

    async def events(self) -> AsyncIterator[ServerSentEvent]:
        """The SSE events of this ask (what ``sse_event`` makes of each event dict). Iterating it takes the slot; only
        :meth:`finalize` gives it back, and this generator calls it as its last statement: after the last event has been
        taken by the consumer, not in a ``finally`` (a consumer that goes away, a contract error and a cancellation are
        :class:`PaidResponse`'s to clean up)."""
        if not self._take_slot():
            yield sse_event({"event": "error", "detail": MSG_BUSY})
            return
        terminal_sent = False                  # the client has been handed a done or error event
        try:
            async for ev in self._open_stream():
                terminal = ev["event"] in TERMINAL_EVENTS
                out = await self._settle(ev) if terminal else ev
                yield sse_event(out)
                terminal_sent = terminal_sent or terminal
        except TwinContractError:
            raise
        except Exception as e:  # noqa: BLE001 — report, never hang the stream
            if self._ledgered:
                # No second row (the ask is already counted). If the client never got its terminal event (the writes
                # after it raised, or it was malformed) it still gets the generic error: the page waits for done or error.
                logger.error("answer failed after its ledger row was written (%s)", self._failure_text(e))
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

    def _take_slot(self) -> bool:
        token = object()
        try:
            self._st.answer_limiter.acquire_on_behalf_of_nowait(token)
        except anyio.WouldBlock:
            return False
        self._token = token
        return True

    def _open_stream(self):
        st, s = self._st, self._st.settings
        extra, self._close_tracer = ((dict(self._workspace), _nothing) if self._in_workspace
                                     else stream_extras(st, self._question, self._strategy))
        twin = self._twin or select_twin(self._strategy, self._in_workspace)
        self._started = True            # from the call on: a twin that raises when CALLED is still an ask to count
        stream = twin(self._question, st.driver, st.embedder, strategy=self._strategy, timeout=s.llm_request_timeout_s,
                      max_tokens=s.llm_answer_max_tokens, escalation_model=s.escalation_model or None,
                      limiters=st.limiters, **extra)
        if not (hasattr(stream, "__aiter__") and hasattr(stream, "aclose")):
            getattr(stream, "close", _nothing)()
            self._started = False       # a wiring mistake: nothing was spent, so nothing is owed on the ledger
            raise TwinContractError(
                f"the answer stream function returned {type(stream).__name__}, not an async generator")
        self._stream = stream
        return stream

    # ---- the terminal event: the money rule

    async def _settle(self, ev: dict) -> dict:
        """The ledger row, then (done) the cache write, the info line and the warning; returns the event to send."""
        with anyio.CancelScope(shield=True):
            await self._write_ledger(usage=ev.get("usage"), cost_usd=ev.get("cost_usd"))
            out = await self._after_done(ev) if ev["event"] == "done" else self._after_error(ev)
        await anyio.lowlevel.checkpoint()
        return out

    async def _write_ledger(self, **spend) -> None:
        """One ledger row, on a worker thread. ``spend`` (``usage``, ``cost_usd``) is passed through only when known."""
        await self._on_db_thread(partial(store.log_query, self._st.driver, ip_hash=self._iph, strategy=self._strategy,
                                         cached=False, **spend, workspace=self._in_workspace))
        self._ledgered = True

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
        """The answer cache is an optimisation, not part of the answer: the ledger row already exists, so a failing
        write is logged (the exception, never the answer text) and the visitor still gets the answer they paid for. If
        it raised into ``events()`` instead, ``_record_failure`` would write a second row for the same ask."""
        try:
            await self._on_db_thread(partial(
                store.put_answer, self._st.driver, question=self._question, strategy=self._strategy,
                answer=ev["answer"], citations=ev["citations"], hallucinated=ev["hallucinated"], usage=ev["usage"],
                cost_usd=ev["cost_usd"], snapshot_id=self._snapshot_id))
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
        """The twin (or the writes after its terminal event) raised: a ledger row without usage, as the sync path did.
        Called only while no row exists for the ask."""
        try:
            with anyio.CancelScope(shield=True):
                await self._write_ledger()
        except Exception:  # noqa: BLE001
            logger.exception("ledger write failed after an answer failure")
        await anyio.lowlevel.checkpoint()
        return self._generic_failure(e)

    @staticmethod
    def _generic_failure(e: Exception) -> dict:
        return {"event": "error", "detail": f"The answer could not be completed ({type(e).__name__})."}

    # ---- cleanup

    async def finalize(self) -> None:
        """Release everything this stream holds. Idempotent, shielded, and it does not raise for an ordinary failure:
        each step logs its own. Nothing happens when the slot was never taken (the busy stream). The order is the order
        of urgency: the abandoned ask's ledger row first (closing the twin can take seconds, joining an agent thread,
        and the platform kills a stopping process after 5 s by default; the row decision cannot change while the twin
        closes), then the twin, then the tracer, and the slot last, whatever happened before."""
        if self._finalized:
            return
        self._finalized = True
        token = self._token
        if token is None:
            return
        with anyio.CancelScope(shield=True):
            try:
                await self._ledger_if_abandoned()
                await self._close_upstream()
                await self._close_request_tracer()
            finally:
                self._release(token)

    async def _close_upstream(self) -> None:
        if self._stream is None:
            return
        try:
            await self._stream.aclose()
        except Exception as e:  # noqa: BLE001
            self._log_failure("closing the answer stream failed", e)

    async def _ledger_if_abandoned(self) -> None:
        """The client went away before the terminal event (a buffered draft widens that window to the whole generation).
        The query still counts against the daily ceiling; its cost is unknown here."""
        if not self._started or self._ledgered:
            return
        try:
            await self._write_ledger()
        except Exception:  # noqa: BLE001
            logger.exception("ledger write failed for an abandoned answer")

    async def _close_request_tracer(self) -> None:
        if self._close_tracer is _nothing:
            return
        try:
            await self._on_db_thread(self._close_tracer)
        except Exception:  # noqa: BLE001
            logger.exception("closing the request tracer failed")

    def _release(self, token: object) -> None:
        self._token = None
        try:
            self._st.answer_limiter.release_on_behalf_of(token)
        except Exception:  # noqa: BLE001
            logger.exception("releasing the answer slot failed")


class PaidResponse(EventSourceResponse):
    """The SSE response of a :class:`PaidStream`: ``finalize`` runs as the background task AND in a ``finally`` around
    the whole ASGI call (and ``events()`` runs it at its own end, which is what normally frees the slot). sse-starlette
    awaits the background task only when its task group exits cleanly, so a send timeout, a send error or a cancelled
    request would otherwise skip it and leak the slot (and the abandoned ask's ledger row). A middleware that re-wraps
    the body (Starlette's ``BaseHTTPMiddleware``) would bypass ``__call__`` altogether; the app uses none, and a
    pure-ASGI middleware leaves it alone.

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
