"""The async twin of the SEC-path answer writer (M5a I2, docs/v2/M5A_BUILD_PLAN.md section 4).

``answerer.py`` writes an answer as sync generators, so a served stream holds one worker thread from retrieval to
the last token. This module is the same writer as async generators: a stream that waits for the model holds no
thread, and every blocking hop (the question embedding, the graph reads, the deterministic checks, the ``postprocess``
hook of a buffered answer) runs on a worker thread under a named limiter of
:class:`semigraph.serve.limiters.Limiters`.

Parity with the sync writer is the point: the events are dicts EQUAL to what ``answerer`` yields for the same inputs
(same keys, same order, same values), :class:`AsyncTextStream` retries, fails and accounts exactly like
:class:`answerer.TextStream`, and every pure helper (``_done_event``, ``_totals``, ``_draft_kwargs``,
``build_blocks``, ``render_prompt``, ...) is imported from ``answerer``, never copied. Only three things differ, all
on purpose:

* nothing here may block the event loop (no ``time.sleep``, no sync ``completion``, no ``TextStream``): backoff is
  awaited and the usage estimate runs on a thread;
* the upstream model stream is closed on every exit that unwinds the generators (normal end, error, a consumer that
  closes the generator, a cancelled consumer). A bare ``async for`` over an async generator does not close it, so
  every layer that delegates to an inner generator wraps it in ``aclosing``, and the close of the provider connection
  runs in a shielded scope (an anyio cancellation is level-triggered: an unshielded ``await`` in a ``finally`` would be
  cancelled again before it could release the connection). What a cancellation guarantees depends on how it is
  delivered. Cancelling an anyio scope (what sse-starlette does on a disconnect): the close is shielded and always
  completes, and every thread hop (the question embedding, the graph reads, the checks, the ``postprocess`` hook, the
  usage estimate) is awaited to its end before the cancellation lands. A shielded wait ends WITHOUT a checkpoint, so the
  cancellation that landed during it would be raised only at the next real suspension, and the next statement can be a
  yield into a consumer that never suspends or a paid model call. The rule: every thread hop goes through ``_hop``,
  which checkpoints once the thread has returned (the inline path of ``_acheck`` has no shield and needs none), and a
  paid call is preceded by a checkpoint of its own: ``AsyncTextStream._attempt`` before ``acompletion`` (every default
  model call, the retry of an empty answer included: nothing there awaits a backoff) and ``_adraft_then_escalate``
  before it announces an escalation (a failed draft arrives there from a shielded close, with no hop in between).
  A bare native ``task.cancel()``, which anyio's shield does not honour,
  gives less: the close is best effort (a second ``cancel()`` that lands inside it can cut it short), and a worker
  thread already started by a hop is not stopped: it runs to its end after the task is gone, no longer counted by its
  limiter (the token is returned at the cancel), so a pool can briefly run more threads than its limit. A
  cancellation is never mistaken for a failed draft (that would buy a strong-model escalation for a visitor who left):
  the drains catch ``Exception`` only;
* a stream that is not async iterable is a ``TypeError`` (a wiring mistake), not a ``draft_error`` that would
  silently escalate.

The paid-call meter (Wave 2, ``serve/meter.py``): an optional :class:`~semigraph.serve.meter.PaidMeter` rides on the
``meter`` keyword of every writer function down to :class:`AsyncTextStream`, which records each provider call it makes
(``start`` right after the checkpoint above and before ``acompletion``; ``complete`` in a ``finally`` after the upstream
is closed) so that an ask whose visitor left is settled at the calls that were really started. Every strong-role stream (the escalation, a question routed straight
to the strong model, and the sole answer model of a deployment with no escalation model) makes NO provider retries
(``num_retries=0``, as the draft never did; see ``_strong_stream``): each of its own attempts is one metered call, the
estimate prices exactly those attempts (``estimate.STREAM_ATTEMPTS``), and a LiteLLM retry would be a billed call nobody
could bound. This is deliberately NOT the sync writer's behaviour: ``answerer.TextStream`` (the CLI and the evaluation
harness, never a served ask) keeps LiteLLM's two retries, and so does ``AsyncTextStream`` constructed directly with its
default ``num_retries``. An injected stream is never metered. Nothing in the events changes: ``usage`` and ``cost_usd``
keep the sync writer's meaning (the last attempt's), the meter is a separate record.

The deterministic checks (``_done_event`` -> ``answer_checks``, and ``verify_answer``) are CPU-bound regex work: about
6,300 to 7,500 ``re`` calls for the two checks of one released draft, over a context of up to about 85,000 characters
(the eight largest real chunks plus a graph block). With ``limiters`` they run on a thread under ``limiters.db``;
``limiters=None`` runs them inline (the unit tests). Every figure below was measured on 2026-10-04 on the development
machine (Intel Core Ultra 9 275HX, Windows 11, Python 3.13.13, the interpreter's default 5 ms switch interval, a 1 ms
system timer, garbage collector off). "Loop lag" is the longest gap between two wake-ups of a ticker task that sleeps
2 ms while one ask's checks run. With the loop idle that gap is 4.0 ms (median of 60 asks; range 3.0 to 4.5), so read
every lag against that floor. A run is 60 asks; where there are two runs, both medians are given, and "worst" is the
single worst ask of both.

What the thread buys, on realistic input. A released draft runs both checks, and the loop lags:

* the longest real benchmark answer, 6,109 characters: both checks take 14.9 ms (median; range 14.7 to 15.3) and make
  6,327 ``re`` calls. Inline the loop lags 15.6 and 16.4 ms (worst 19.1); on a thread, 7.7 ms in both runs (worst 8.8);
* an answer at the 2,400-token cap, 9,600 characters (the generator of the test, with real chunk ids): 21.1 ms and
  7,465 ``re`` calls. Inline the loop lags 23.0 and 22.1 ms (worst 25.7); on a thread, 8.9 and 10.6 ms (worst 12.0).

Inline, the loop is held for as long as the checks run. On a thread it is held for 4 to 7 ms more than when idle,
and that is all a thread can do here: the interpreter asks a running thread to hand over the GIL after 5 ms
(``sys.getswitchinterval``), and the thread does so at its next bytecode or when the C call it is in returns. The
checks are thousands of short calls with Python between them, so a hand-over is never more than one call away. A
thread does NOT interrupt a long single ``re`` call: ``re`` holds the GIL for a whole match, so a loop stall with the
hop is roughly the 5 ms plus the cost of the longest single regex call of the checks (measured above: 3.7 to 6.6 ms
more than idle, in medians). At the sizes above that call is one step of ``verify._SUFFIX_RE.finditer`` over the whole
context: 1.03 and 1.04 ms (median over 30 asks of the longest call of each ask; worst 1.12 ms). That the interval is
what sets the lag was checked by changing it (the real answer, 60 asks each, idle 3.5 to 4.0 ms): 5 ms, the default,
7.7 ms; 1 ms, 5.0 ms; 0.2 ms, 4.1 ms. This module leaves the interpreter's default alone. The hop is kept because it lowers the loop's lag by 8 to 14 ms at these sizes for under
1 ms of wall time (0.04 ms for an empty call, 0.3 ms for the 7.5 ms check, on an otherwise idle loop). It pays
only for checks that outlast the 5 ms interval, as these do; a check shorter than that gains nothing from a thread.
``tests/test_answerer_async.py`` pins a generous bound (under 100 ms, inline and on a thread) on a synthetic input of
the same size (both checks 23.7 ms inline; lag 25.6 ms inline and 9.2 ms on a thread, one run), and prints the measured
lags in its failure message.

A thread cannot shorten ONE long ``re`` call, so protection against a pattern that is slow on crafted text lies in the
pattern, never in the thread. The old ``verify._PERCENT_VALUE_RE`` over ``1,1,1,...`` (5,000 characters, what a
prompt-injected answer can be steered into) is one call of 156 ms: the loop lagged 154 ms inline (median of 30 asks;
range 152 to 173) and 157 ms on a thread (151 to 187). The patterns are linear now, and ``answer_checks`` is too on
each shape the 2026-10-04 audit found (``tests/test_verify_regex.py`` keeps every replaced pattern verbatim and
compares it with its replacement; times are one ``answer_checks`` call, old -> new, median of 5):

* a run of comma-separated digits (``1,1,1,...``, 5,000 characters), quadratic in ``verify._PERCENT_VALUE_RE`` and
  ``_PERCENT_RE``: 155 ms -> 4.2 ms (8.5 ms for 10,000 characters, 16.5 ms for 20,000);
* a run of digits (``111...``, 1,200 characters), cubic in ``_PERCENT_RE``: 2,004 ms -> 0.7 ms;
* 5,000 line breaks, the quadratic walk of ``removal_claims._bullets_under`` over blank lines: 466 ms -> 17 ms
  (32 ms for 10,000 characters, 65 ms for 20,000);
* runs of blanks, long words and the list-label pattern that backtracked exponentially: the same file.

The ``postprocess`` hook of a buffered answer (the workspace passes ``strip_links_images``) takes the same route, on the
draft's text and on a buffered release's text, whichever the path runs: it is regex work over model output, which a
prompt-injected document can make long. Its patterns are linear (``workspace.py``;
``tests/test_retrieval_workspace_regex.py`` pins the output as unchanged and the cost as linear): 20,000 letters take
0.77 ms, 20,000 characters made of forty groups of images, links, HTML tags, bare URLs and reference links 1.6 ms, and
15,000 characters of unclosed brackets (``[`` then ``](`` repeated) 0.77 ms (median of 60 calls); the loop lag during
them is the idle floor, inline or on a thread. The hook goes through ``_acheck`` so that the thread, the limiter and
the checkpoint after it are decided in one place for everything that is not model I/O.

Plain Python is not like one long ``re`` call: the interpreter takes the GIL back from a thread that runs it every
5 ms, so a thread DOES keep the loop turning. That case is left, KNOWN, TRACKED and not fixed:
``removal_claims._survives`` re-reads the whole sentence before each removal verb and ``_clause_claims`` joins and
re-scans the other clauses of a line for each one, both quadratic pure-Python loops. A crafted answer of a single
sentence, "no risk factor was removed " repeated, costs one ``answer_checks`` call 568 ms at 5,000 characters and
2,186 ms at 10,000 (twice the text, 3.9 times the time). The two checks of one ask take 1,135 ms at 5,000 characters
and 3,995 ms at the 9,600-character cap. The loop lag of an ask (both checks, 30 asks each):

* 5,000 characters: inline 1,129 ms (worst 1,345); on a thread 14.5 ms (worst 15.2);
* 9,600 characters: inline 3,973 ms (worst 4,173); on a thread 15.0 ms (worst 15.5).

(One check call alone, 5,000 characters, 30 asks: 558 ms inline, 14.2 ms on a thread; 10,000 characters, 5 asks: 2,140
ms inline, 15.1 ms on a thread.) So the offload is worth keeping: it is what keeps the loop turning. It protects the
loop only: each such call still costs CPU and holds one ``limiters.db`` thread to its end. An ask makes at most two
check calls, one ``verify_answer`` (the draft) and one ``_done_event`` (the answer that is released, the draft or the
escalation); a released draft, an escalated ask and a workspace ask each make two (counted with a spy on
``answer_checks``), an ask routed straight to the strong model or without an escalation model makes one. The cost is
bounded, not removed, by the per-IP window (``rate_limit_questions``: 5 paid asks per 600 s per address), the daily cap
(``max_queries_per_day``: 150) and ``max_concurrent_answers`` (2 on the live service, so at most two such threads at
a time).

Garbage-collector pauses are outside all of this: a full collection of a large heap pauses every thread (118 ms was
measured with ``gc.callbacks`` in a pytest process), whichever thread runs the checks.
"""

import logging
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

import anyio
import anyio.lowlevel
import litellm
from litellm import acompletion

from ..config import get_settings
from ..llm_shape import completion_params, provider_kwargs
from .answerer import (
    CITE_RE,
    TRANSIENT,
    _done_event,
    _draft_kwargs,
    _identity,
    _totals,
    build_blocks,
    dropped_anchors_field,
    render_prompt,
    sources_from_context,
    usage_cost,
)
from .retriever import company_edges_query, hybrid_retrieve, vector_retrieve
from .router import needs_strong_model
from .verify import verify_answer

if TYPE_CHECKING:
    from ..serve.limiters import Limiters
    from ..serve.meter import PaidMeter

# The sync writer's logger on purpose: the same operator filters and the same lines ("draft model ... failed") apply.
logger = logging.getLogger("semigraph.answerer")


async def _hop(fn, *args, limiter):
    """Run ``fn(*args)`` on a worker thread under ``limiter``, then raise a cancellation that landed while it ran.

    The hop is shielded (``abandon_on_cancel=False``) and returns without a checkpoint, so a cancelled scope would
    otherwise be noticed only at the next real suspension, and the next statement may be a yield into a consumer that
    never suspends or a paid model call. The checkpoint puts that suspension right here. Every thread hop of this
    module goes through this function."""
    result = await anyio.to_thread.run_sync(fn, *args, limiter=limiter)
    await anyio.lowlevel.checkpoint()
    return result


class AsyncTextStream:
    """Async sibling of :class:`answerer.TextStream` — ``async for`` it for text deltas.

    After exhaustion ``finish_reason`` and ``usage`` are set exactly as the sync stream sets them (provider-reported
    when it has ``prompt_tokens``, else estimated by LiteLLM's chunk builder and flagged ``estimated``). Transient
    errors are retried only BEFORE the first delta; once text has reached the client a mid-stream failure raises
    RuntimeError.

    The provider stream (what ``acompletion`` returns) is closed however the iteration ends; :meth:`aclose` closes it
    on demand.

    ``usage`` keeps the sync stream's meaning: the usage of the LAST attempt that reported one (an attempt that reported
    none leaves the earlier value), which is what ``done`` carries. It therefore forgets a first attempt that was billed
    and returned no text. ``attempt_usages`` is the record that does not forget: one entry per provider call this stream
    started, in order, the usage that call reported or None (the call raised, was cut off, or the provider sent none).

    ``meter`` (a :class:`semigraph.serve.meter.PaidMeter`, or None) is told about each of those calls: ``start`` the
    moment before the provider is asked (``role`` is ``"draft"`` or ``"strong"``; ``attempts`` is ``1 + num_retries``,
    the provider calls one ``acompletion`` may make) and ``complete`` once the upstream is closed, with that attempt's
    usage. See ``_attempt`` for why the two sit exactly where they do.
    """

    def __init__(self, prompt: str, *, model: str | None = None, max_tokens: int = 1200,
                 attempts: int = 2, backoff: tuple[int, ...] = (5, 15),
                 timeout: float | None = None, num_retries: int = 2,
                 meter: "PaidMeter | None" = None, role: str = "strong"):
        self.prompt = prompt
        self.num_retries = num_retries
        self.model = model or get_settings().answer_model
        self.max_tokens, self.attempts = max_tokens, attempts
        self.backoff, self.timeout = backoff, timeout
        self.meter, self.role = meter, role
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self.attempt_usages: list[dict | None] = []
        self.text = ""
        self._chunks: list = []
        self._upstream = None

    async def aclose(self) -> None:
        """Close the provider stream that is open, if any. Idempotent; a failing close is logged, never raised (the
        connection is being released, there is nothing left to do about it); runs shielded so a cancelled task still
        completes it."""
        upstream, self._upstream = self._upstream, None
        close = getattr(upstream, "aclose", None)
        if close is None:
            return
        with anyio.CancelScope(shield=True):
            try:
                await close()
            except Exception as e:  # noqa: BLE001 - releasing a connection must never break the caller
                logger.debug("closing the model stream failed: %s", e)

    def _meter_start(self) -> int | None:
        """Record the provider call that is about to be made and return its id (None without a meter). ``attempts`` is
        ``1 + num_retries``: the provider calls one ``acompletion`` may make, each billed. Synchronous on purpose: see
        :meth:`_attempt`."""
        if self.meter is None:
            return None
        return self.meter.start(role=self.role, model=self.model, prompt_chars=len(self.prompt),
                                max_output_tokens=self.max_tokens, attempts=1 + self.num_retries)

    async def _attempt(self, messages: list[dict]) -> AsyncIterator[str]:
        extra = {"timeout": self.timeout} if self.timeout else {}
        chunks = []
        attempt_usage: dict | None = None       # what THIS attempt's provider call reported (``usage`` keeps the last)
        # Every paid default call starts here. A cancellation that landed during a shielded wait just before (a hop, the
        # close of the previous attempt's upstream) is raised now, so a visitor who left never buys the call.
        await anyio.lowlevel.checkpoint()
        # The meter is told AFTER that checkpoint (a call that is never made is not recorded) and with no await between
        # this line and the provider call (a record whose call is not made, or a call made after the record was settled,
        # needs a suspension in between).
        call_id = self._meter_start()
        try:
            resp = await acompletion(
                model=self.model, messages=messages, **completion_params(self.model, self.max_tokens),
                num_retries=self.num_retries, stream=True, stream_options={"include_usage": True}, **extra,
                **provider_kwargs(self.model, get_settings()),
            )
            self._upstream = resp
            try:
                async for chunk in resp:
                    chunks.append(chunk)
                    choice = chunk.choices[0] if chunk.choices else None
                    delta = getattr(getattr(choice, "delta", None), "content", None)
                    if delta:
                        self.text += delta
                        yield delta
                    if choice is not None and choice.finish_reason:
                        self.finish_reason = choice.finish_reason
                    usage = getattr(chunk, "usage", None)
                    if usage and getattr(usage, "prompt_tokens", None):
                        attempt_usage = {"prompt_tokens": usage.prompt_tokens,
                                         "completion_tokens": usage.completion_tokens}
                        self.usage = dict(attempt_usage)
            finally:
                await self.aclose()
        finally:
            # An outer ``finally`` of its own, not a line after the close: the close can be cut short (a second native
            # ``task.cancel()`` landing inside it), and a usage the provider had already reported must still be recorded.
            # The call is completed whatever ended it: a provider error, a cut-off stream, a cancelled visitor.
            self.attempt_usages.append(attempt_usage)
            if call_id is not None:
                self.meter.complete(call_id, attempt_usage)
        self._chunks = chunks

    async def _estimate_usage(self, messages: list[dict]) -> None:
        # The chunk builder tokenises the whole prompt: 3 to 15 ms measured on 2026-10-04 (600 to 2,400 chunks, a prompt
        # of 3,000 to 85,000 characters), so it runs on a thread, not on the loop.
        # It takes anyio's default thread limiter (40 tokens, shared with Starlette's sync endpoints), not a named one
        # of ``Limiters``: it runs once per answer, only when the provider reported no usage, and for a few ms, so
        # the worst a busy limiter does is delay this stream's ``done``; it can never block the loop.
        build = partial(litellm.stream_chunk_builder, self._chunks, messages=messages)
        try:
            built = await _hop(build, limiter=None)
            self.usage = {"prompt_tokens": built.usage.prompt_tokens,
                          "completion_tokens": built.usage.completion_tokens,
                          "estimated": True}
        except Exception as e:  # accounting must never break an answer
            logger.debug("usage estimate unavailable: %s", e)

    async def __aiter__(self) -> AsyncIterator[str]:
        messages = [{"role": "user", "content": self.prompt}]
        last_err = "unknown"
        for attempt in range(self.attempts):
            self._chunks = []
            try:
                async with aclosing(self._attempt(messages)) as deltas:
                    async for delta in deltas:
                        yield delta
            except TRANSIENT as e:
                if self.text:
                    raise RuntimeError(f"stream interrupted mid-answer: {type(e).__name__}") from e
                last_err = f"transient: {type(e).__name__}"
                if attempt < self.attempts - 1:      # nothing to wait for after the final attempt
                    wait = self.backoff[min(attempt, len(self.backoff) - 1)]
                    logger.warning("transient error (%s) before first token — waiting %ss",
                                   type(e).__name__, wait)
                    await anyio.sleep(wait)
                continue
            if not self.text:
                last_err = f"empty response (finish_reason={self.finish_reason})"
                continue
            if self.usage is None and self._chunks:
                await self._estimate_usage(messages)
            return
        raise RuntimeError(f"streaming answer failed after {self.attempts} attempts — last error: {last_err}")


@asynccontextmanager
async def _closing_iter(stream: AsyncIterable[str]) -> AsyncIterator[AsyncIterator[str]]:
    """Iterate ``stream`` and close its async iterator on every exit: a consumer that stops early (or is cancelled)
    would otherwise leave an async generator, and with it an open provider connection, to the garbage collector.

    ``aiter`` runs OUTSIDE the callers' ``try`` on purpose: a sync stream behind the async writer is a TypeError (a
    wiring mistake) that must fail loudly, not be reported as a failed draft and escalated. A close that raises an
    ``Exception`` is logged and dropped, like :meth:`AsyncTextStream.aclose`: the answer is already written, and the
    failure must neither replace the writer's own ``error`` event nor stand in for a cancellation that is unwinding the
    stack. Only ``Exception``: a cancellation is never swallowed here."""
    deltas = aiter(stream)
    try:
        yield deltas
    finally:
        close = getattr(deltas, "aclose", None)
        if close is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await close()
                except Exception as e:  # noqa: BLE001 - releasing a connection must never break the writer
                    logger.debug("closing the model stream's iterator failed: %s", e)


async def _acheck(limiters: "Limiters | None", fn, *args, **kwargs):
    """Run a deterministic, CPU-bound call (the checks, the ``postprocess`` hook): inline when ``limiters`` is None
    (unit tests), else on a worker thread under the graph pool (the hop costs under 1 ms and takes 8 to 14 ms off the
    loop lag of the 15 to 21 ms checks; what it cannot do is in the module docstring), with a checkpoint once it has
    returned (see :func:`_hop`)."""
    if limiters is None:
        return fn(*args, **kwargs)
    return await _hop(partial(fn, *args, **kwargs), limiter=limiters.db)


def _loggable_failure(error: str, *, over_uploaded_text: bool) -> str:
    """``error`` (``"ClassName: message"``, as :func:`_adrain` describes it) as it may appear in a log line. A
    provider's error can quote what it rejected, and over uploaded text that is a chunk of the visitor's document: the
    workspace writer (the one with a ``postprocess``) logs the class name only. A public ask keeps the sync writer's
    wording."""
    return error.partition(":")[0] if over_uploaded_text else error[:300]


async def _adrain(stream: AsyncIterable[str]) -> tuple[str, str | None]:
    """Consume a stream silently; returns (text, error description or None)."""
    parts = []
    async with _closing_iter(stream) as deltas:
        try:
            async for delta in deltas:
                parts.append(delta)
        except Exception as e:  # noqa: BLE001
            return "".join(parts), f"{type(e).__name__}: {e}"
    return "".join(parts), None


async def _alive_events(stream, *, question, strategy, valid_ids, chunk_ids, context_chars, carry=None,
                        context=None, sources=None, limiters=None):
    """Stream ``stream`` to the client as deltas, then the terminal ``done`` (or ``error``) event.

    ``carry`` folds an earlier, rejected attempt into the totals: ``{"usage", "cost_usd", "extra"}``
    (``extra`` keys such as ``escalated`` are merged into ``done``)."""
    carry = carry or {}
    parts, failure = [], None
    async with _closing_iter(stream) as deltas:
        try:
            async for delta in deltas:
                parts.append(delta)
                yield {"event": "delta", "text": delta}
        except Exception as e:  # noqa: BLE001 — surface, with whatever spend is known
            failure = f"{type(e).__name__}: {e}"
    usage, cost = _totals(stream, carry)
    if failure is not None:
        yield {"event": "error", "detail": failure, "partial": "".join(parts),
               "usage": usage, "cost_usd": cost, "strategy": strategy}
        return
    yield await _acheck(limiters, _done_event, "".join(parts), stream, question=question, strategy=strategy,
                        valid_ids=valid_ids, chunk_ids=chunk_ids, context_chars=context_chars, usage=usage,
                        cost_usd=cost, extra=carry.get("extra"), context=context, sources=sources)


async def _abuffered_events(stream, *, postprocess, question, strategy, valid_ids, chunk_ids, context_chars,
                            carry=None, context=None, sources=None, limiters=None):
    """:func:`_alive_events` for text that must be edited before anyone sees it: drain ``stream``, apply
    ``postprocess``, then release ONE delta and the ``done`` event built on the edited text (or an ``error`` whose
    ``partial`` is edited too)."""
    carry = carry or {}
    raw, error = await _adrain(stream)
    text = await _acheck(limiters, postprocess, raw)
    usage, cost = _totals(stream, carry)
    if error:
        yield {"event": "error", "detail": error, "partial": text, "usage": usage, "cost_usd": cost,
               "strategy": strategy}
        return
    yield {"event": "delta", "text": text}
    yield await _acheck(limiters, _done_event, text, stream, question=question, strategy=strategy,
                        valid_ids=valid_ids, chunk_ids=chunk_ids, context_chars=context_chars, usage=usage,
                        cost_usd=cost, extra=carry.get("extra"), context=context, sources=sources)


def _strong_stream(prompt: str, *, model: str | None, stream_kwargs: dict,
                   meter: "PaidMeter | None") -> AsyncTextStream:
    """A strong-role model's stream: the caller's arguments without the draft's ``model``, and NO provider retries. Every
    stream of the strong role is built here: the escalation model's, the one a question routed straight to the strong
    model gets, and the sole answer model's when there is no escalation model (``model`` None: the configured answer
    model).

    The stream makes its own attempts (``attempts``, 2 by default: a first answer that comes back empty is asked for
    again) and each of them is metered as one provider call. A LiteLLM retry (``num_retries``, 2 by default) would be a
    billed call inside an attempt that nothing could see or bound, so it is switched off here, as ``_draft_kwargs``
    switches it off for the draft, whatever the caller passed; a transient error before the first token is already
    retried by the stream, after its backoff. (Merged as a dict, so a ``num_retries`` in ``stream_kwargs`` cannot
    collide with the one set here.) The estimate prices exactly those attempts (``estimate.STREAM_ATTEMPTS``). The model
    is passed only when there is one, so a sole answer model the caller did not name is resolved by the stream itself."""
    kwargs = {**{k: v for k, v in stream_kwargs.items() if k != "model"}, "num_retries": 0}
    if model is not None:
        kwargs["model"] = model
    return AsyncTextStream(prompt, meter=meter, role="strong", **kwargs)


async def _adraft_then_escalate(prompt, *, llm_stream, escalation_stream, escalation_model, stream_kwargs, context,
                                postprocess=None, release=None, limiters=None, meter=None, **ctx):
    """Cheap draft -> deterministic verification -> release it, or escalate to the strong model (the twin of
    :func:`answerer._draft_then_escalate`: a rejected draft is never shown; both attempts' tokens and cost land in
    ``done``)."""
    draft = (llm_stream(prompt) if llm_stream
             else AsyncTextStream(prompt, meter=meter, role="draft", **_draft_kwargs(stream_kwargs)))
    text, error = await _adrain(draft)
    if postprocess is not None:
        text = await _acheck(limiters, postprocess, text)
    draft_model = getattr(draft, "model", None)
    if error:
        logger.warning("draft model %s failed (%s) - escalating to %s", draft_model,
                       _loggable_failure(error, over_uploaded_text=postprocess is not None), escalation_model)
    reasons = ["draft_error"] if error else await _acheck(
        limiters, verify_answer, text, set(CITE_RE.findall(text)), ctx["valid_ids"],
        getattr(draft, "finish_reason", None), context=context, sources=ctx.get("sources"), question=ctx["question"])
    if not reasons:
        yield {"event": "delta", "text": text}
        yield await _acheck(limiters, _done_event, text, draft, context=context, **ctx,
                            extra={"escalated": False, "answered_by": draft_model, "routed": "cheap"})
        return
    # The strong model is a paid call. A draft that failed reaches this line from a shielded close, with no check hop
    # in between, so the cancellation of a visitor who left is raised here, before the escalation is announced.
    await anyio.lowlevel.checkpoint()
    logger.info("draft rejected (%s) - escalating to %s", ",".join(reasons), escalation_model)
    yield {"event": "escalated", "reasons": reasons, "from": draft_model, "to": escalation_model}
    strong = (escalation_stream(prompt) if escalation_stream
              else _strong_stream(prompt, model=escalation_model, stream_kwargs=stream_kwargs, meter=meter))
    draft_usage = getattr(draft, "usage", None)
    carry = {"usage": draft_usage, "cost_usd": usage_cost(draft_usage, draft_model) if draft_usage else None,
             "extra": {"escalated": True, "escalation_reasons": reasons, "routed": "cheap",
                       "answered_by": getattr(strong, "model", None) or escalation_model}}
    events = (release or _alive_events)(strong, carry=carry, context=context, limiters=limiters, **ctx)
    async with aclosing(events) as inner:
        async for event in inner:
            yield event


async def aanswer_stream(question: str, driver, embedder, strategy: str = "hybrid",
                         llm_stream=None, k_chunks: int = 8, hops: int = 2, *, limiters: "Limiters",
                         escalation_model: str | None = None, escalation_stream=None,
                         meter: "PaidMeter | None" = None, **stream_kwargs):
    """Async twin of :func:`answerer.answer_stream`: the same event grammar, with retrieval off the event loop.

    The question is embedded ONCE, on a worker thread under ``limiters.embed``, and the retrieval receives that vector
    (``query_vec``) instead of embedding again; it runs on a thread under ``limiters.db``. ``llm_stream`` and
    ``escalation_stream`` are injectable: ``callable(prompt) -> async iterable[str]``. An unknown strategy raises the
    sync writer's ValueError on the first ``__anext__``, before anything is embedded; so does a bad ``hops`` of the
    hybrid strategy (the vector strategy never reads it, as in the sync writer).

    ``meter`` (a :class:`semigraph.serve.meter.PaidMeter`) is a keyword of its own, like ``limiters``: it never reaches
    ``**stream_kwargs``, so it cannot reach an injected stream either, and an injected stream is not metered."""
    if strategy == "hybrid":
        company_edges_query(hops)       # the validator ``hybrid_retrieve`` runs first: refuse before the embed
        retrieve = partial(hybrid_retrieve, question, driver, embedder, k_chunks=k_chunks, hops=hops)
    elif strategy == "vector":
        retrieve = partial(vector_retrieve, question, driver, embedder, k=k_chunks)
    else:
        raise ValueError(f"unknown strategy {strategy!r} — use 'hybrid' or 'vector'")
    vec = await _hop(embedder.encode_query, question, limiter=limiters.embed)
    r = await _hop(partial(retrieve, query_vec=vec), limiter=limiters.db)
    events = astream_answer_for_context(
        question, r, strategy, llm_stream=llm_stream, escalation_model=escalation_model,
        escalation_stream=escalation_stream, limiters=limiters, meter=meter, **stream_kwargs)
    async with aclosing(events) as inner:
        async for event in inner:
            yield event


async def astream_answer_for_context(question: str, r: dict, strategy: str, *, llm_stream=None,
                                     escalation_model: str | None = None, escalation_stream=None,
                                     limiters: "Limiters | None" = None, meter: "PaidMeter | None" = None,
                                     **stream_kwargs):
    """Async twin of :func:`answerer.stream_answer_for_context`: everything AFTER retrieval (the six blocks, the
    ``retrieval`` event, then :func:`astream_answer_for_prompt`). ``limiters`` and ``meter`` are keywords of their own,
    so they never reach the model stream's arguments."""
    blocks, full_context, valid_ids = build_blocks(r)
    yield {"event": "retrieval", "anchors": r["anchors"],
           "counts": {k: len(r[k]) for k in ("edges", "metrics", "risks", "temporal", "chunks")},
           "anchor_defaulted": bool(r.get("anchor_defaulted", False)), **dropped_anchors_field(r)}
    events = astream_answer_for_prompt(
        question, render_prompt(question, blocks), full_context, valid_ids, [c["chunk_id"] for c in r["chunks"]],
        strategy, sources=sources_from_context(full_context), llm_stream=llm_stream,
        escalation_model=escalation_model, escalation_stream=escalation_stream, limiters=limiters, meter=meter,
        **stream_kwargs)
    async with aclosing(events) as inner:
        async for event in inner:
            yield event


async def astream_answer_for_prompt(question: str, prompt: str, full_context: str, valid_ids: set[str],
                                    chunk_ids: list[str], strategy: str, *, sources: dict[str, str] | None = None,
                                    llm_stream=None, escalation_model: str | None = None, escalation_stream=None,
                                    postprocess=None, force_buffered: bool = False,
                                    limiters: "Limiters | None" = None, meter: "PaidMeter | None" = None,
                                    **stream_kwargs):
    """Async twin of :func:`answerer.stream_answer_for_prompt`: route, draft / verify / escalate or stream live,
    ending in ``done``, for an ALREADY RENDERED prompt. Same arguments and behaviour (including the ``postprocess`` /
    ``force_buffered`` ValueError and the same-model collapse), plus ``limiters`` (see the module docstring) and
    ``meter``: every model stream THIS function builds is metered (the draft as ``"draft"``; the escalation, the
    question routed straight to the strong model and the sole answer model as ``"strong"``, the role the estimate
    prices a sole answer model in); an injected ``llm_stream`` / ``escalation_stream`` is not."""
    if postprocess is not None and not force_buffered:
        raise ValueError("postprocess needs force_buffered=True: a live stream cannot be edited after it is shown")
    ctx = {"question": question, "strategy": strategy, "valid_ids": valid_ids, "chunk_ids": list(chunk_ids),
           "context_chars": len(full_context),
           "sources": sources if sources is not None else sources_from_context(full_context)}
    release = partial(_abuffered_events, postprocess=postprocess or _identity) if force_buffered else _alive_events
    if escalation_model and escalation_model == (stream_kwargs.get("model") or get_settings().answer_model):
        escalation_model = None    # one model in both roles is plain live streaming (the documented rollback)
    if escalation_model and needs_strong_model(question):
        strong = (escalation_stream(prompt) if escalation_stream else
                  _strong_stream(prompt, model=escalation_model, stream_kwargs=stream_kwargs, meter=meter))
        extra = {"escalated": False, "routed": "strong",
                 "answered_by": getattr(strong, "model", None) or escalation_model}
        events = release(strong, carry={"extra": extra}, context=full_context, limiters=limiters, **ctx)
    elif escalation_model:
        events = _adraft_then_escalate(prompt, llm_stream=llm_stream, escalation_stream=escalation_stream,
                                       escalation_model=escalation_model, stream_kwargs=stream_kwargs,
                                       context=full_context, postprocess=postprocess,
                                       release=release if force_buffered else None, limiters=limiters, meter=meter,
                                       **ctx)
    else:
        stream = (llm_stream(prompt) if llm_stream
                  else _strong_stream(prompt, model=stream_kwargs.get("model"), stream_kwargs=stream_kwargs, meter=meter))
        events = release(stream, context=full_context, limiters=limiters, **ctx)
    async with aclosing(events) as inner:
        async for event in inner:
            yield event
