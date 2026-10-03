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

The deterministic checks (``_done_event`` -> ``answer_checks``, and ``verify_answer``) are CPU-bound regex work.
Measured on 2026-10-03 over a 79,621-character context (the eight largest real chunks plus a realistic graph block),
200 repetitions each: p95 6.9 ms for the longest benchmark answer (5.7k characters) and 10.4 ms for an answer at the
2,400-token cap; a draft that is released runs both, so about 14 to 21 ms of loop time per ask. That is over the 10
ms bar, so with ``limiters`` they run on a thread under ``limiters.db``; ``limiters=None`` runs them inline (the
unit tests).

The ``postprocess`` hook of a buffered answer (the workspace passes ``strip_links_images``) takes the same route, on the
draft's text and on a buffered release's text, whichever the path runs: it is regex work over model output, which a
prompt-injected document can make long, and ``strip_links_images`` is quadratic on one long run of letters (222 ms for
10,000 characters, 886 ms for 20,000). A thread does not shorten that stall: ``re`` holds the GIL for a whole match, and
measured on 2026-10-03 (median of three) the loop lagged as long as the strip took, on a thread (916 ms for 20,000
characters) as inline (886 ms). The fix for that input is a linear-time pattern in ``workspace.py``. The hook goes
through ``_acheck`` so that the thread, the limiter and the checkpoint after it are decided in one place for everything
that is not model I/O.
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
from ..llm_shape import completion_params
from .answerer import (
    CITE_RE,
    TRANSIENT,
    _done_event,
    _draft_kwargs,
    _identity,
    _totals,
    build_blocks,
    render_prompt,
    sources_from_context,
    usage_cost,
)
from .retriever import company_edges_query, hybrid_retrieve, vector_retrieve
from .router import needs_strong_model
from .verify import verify_answer

if TYPE_CHECKING:
    from ..serve.limiters import Limiters

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
    """

    def __init__(self, prompt: str, *, model: str | None = None, max_tokens: int = 1200,
                 attempts: int = 2, backoff: tuple[int, ...] = (5, 15),
                 timeout: float | None = None, num_retries: int = 2):
        self.prompt = prompt
        self.num_retries = num_retries
        self.model = model or get_settings().answer_model
        self.max_tokens, self.attempts = max_tokens, attempts
        self.backoff, self.timeout = backoff, timeout
        self.finish_reason: str | None = None
        self.usage: dict | None = None
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

    async def _attempt(self, messages: list[dict]) -> AsyncIterator[str]:
        extra = {"timeout": self.timeout} if self.timeout else {}
        chunks = []
        # Every paid default call starts here. A cancellation that landed during a shielded wait just before (a hop, the
        # close of the previous attempt's upstream) is raised now, so a visitor who left never buys the call.
        await anyio.lowlevel.checkpoint()
        resp = await acompletion(
            model=self.model, messages=messages, **completion_params(self.model, self.max_tokens),
            num_retries=self.num_retries, stream=True, stream_options={"include_usage": True}, **extra,
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
                    self.usage = {"prompt_tokens": usage.prompt_tokens,
                                  "completion_tokens": usage.completion_tokens}
        finally:
            await self.aclose()
        self._chunks = chunks

    async def _estimate_usage(self, messages: list[dict]) -> None:
        # The chunk builder tokenises the whole prompt: 5 to 16 ms measured, so it runs on a thread, not on the loop.
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
    (unit tests), else on a worker thread under the graph pool (a thread hop is cheap next to the 7 to 21 ms the checks
    cost; see the module docstring), with a checkpoint once it has returned (see :func:`_hop`)."""
    if limiters is None:
        return fn(*args, **kwargs)
    return await _hop(partial(fn, *args, **kwargs), limiter=limiters.db)


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


async def _adraft_then_escalate(prompt, *, llm_stream, escalation_stream, escalation_model, stream_kwargs, context,
                                postprocess=None, release=None, limiters=None, **ctx):
    """Cheap draft -> deterministic verification -> release it, or escalate to the strong model (the twin of
    :func:`answerer._draft_then_escalate`: a rejected draft is never shown; both attempts' tokens and cost land in
    ``done``)."""
    draft = llm_stream(prompt) if llm_stream else AsyncTextStream(prompt, **_draft_kwargs(stream_kwargs))
    text, error = await _adrain(draft)
    if postprocess is not None:
        text = await _acheck(limiters, postprocess, text)
    draft_model = getattr(draft, "model", None)
    if error:
        logger.warning("draft model %s failed (%s) - escalating to %s", draft_model, error[:300], escalation_model)
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
    strong_kwargs = {k: v for k, v in stream_kwargs.items() if k != "model"}
    strong = (escalation_stream(prompt) if escalation_stream
              else AsyncTextStream(prompt, model=escalation_model, **strong_kwargs))
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
                         escalation_model: str | None = None, escalation_stream=None, **stream_kwargs):
    """Async twin of :func:`answerer.answer_stream`: the same event grammar, with retrieval off the event loop.

    The question is embedded ONCE, on a worker thread under ``limiters.embed``, and the retrieval receives that vector
    (``query_vec``) instead of embedding again; it runs on a thread under ``limiters.db``. ``llm_stream`` and
    ``escalation_stream`` are injectable: ``callable(prompt) -> async iterable[str]``. An unknown strategy raises the
    sync writer's ValueError on the first ``__anext__``, before anything is embedded; so does a bad ``hops`` of the
    hybrid strategy (the vector strategy never reads it, as in the sync writer)."""
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
        escalation_stream=escalation_stream, limiters=limiters, **stream_kwargs)
    async with aclosing(events) as inner:
        async for event in inner:
            yield event


async def astream_answer_for_context(question: str, r: dict, strategy: str, *, llm_stream=None,
                                     escalation_model: str | None = None, escalation_stream=None,
                                     limiters: "Limiters | None" = None, **stream_kwargs):
    """Async twin of :func:`answerer.stream_answer_for_context`: everything AFTER retrieval (the six blocks, the
    ``retrieval`` event, then :func:`astream_answer_for_prompt`). ``limiters`` is a keyword of its own, so it never
    reaches the model stream."""
    blocks, full_context, valid_ids = build_blocks(r)
    yield {"event": "retrieval", "anchors": r["anchors"],
           "counts": {k: len(r[k]) for k in ("edges", "metrics", "risks", "temporal", "chunks")},
           "anchor_defaulted": bool(r.get("anchor_defaulted", False))}
    events = astream_answer_for_prompt(
        question, render_prompt(question, blocks), full_context, valid_ids, [c["chunk_id"] for c in r["chunks"]],
        strategy, sources=sources_from_context(full_context), llm_stream=llm_stream,
        escalation_model=escalation_model, escalation_stream=escalation_stream, limiters=limiters, **stream_kwargs)
    async with aclosing(events) as inner:
        async for event in inner:
            yield event


async def astream_answer_for_prompt(question: str, prompt: str, full_context: str, valid_ids: set[str],
                                    chunk_ids: list[str], strategy: str, *, sources: dict[str, str] | None = None,
                                    llm_stream=None, escalation_model: str | None = None, escalation_stream=None,
                                    postprocess=None, force_buffered: bool = False,
                                    limiters: "Limiters | None" = None, **stream_kwargs):
    """Async twin of :func:`answerer.stream_answer_for_prompt`: route, draft / verify / escalate or stream live,
    ending in ``done``, for an ALREADY RENDERED prompt. Same arguments and behaviour (including the ``postprocess`` /
    ``force_buffered`` ValueError and the same-model collapse), plus ``limiters`` (see the module docstring)."""
    if postprocess is not None and not force_buffered:
        raise ValueError("postprocess needs force_buffered=True: a live stream cannot be edited after it is shown")
    ctx = {"question": question, "strategy": strategy, "valid_ids": valid_ids, "chunk_ids": list(chunk_ids),
           "context_chars": len(full_context),
           "sources": sources if sources is not None else sources_from_context(full_context)}
    release = partial(_abuffered_events, postprocess=postprocess or _identity) if force_buffered else _alive_events
    if escalation_model and escalation_model == (stream_kwargs.get("model") or get_settings().answer_model):
        escalation_model = None    # one model in both roles is plain live streaming (the documented rollback)
    if escalation_model and needs_strong_model(question):
        strong_kwargs = {k: v for k, v in stream_kwargs.items() if k != "model"}
        strong = (escalation_stream(prompt) if escalation_stream else
                  AsyncTextStream(prompt, model=escalation_model, **strong_kwargs))
        extra = {"escalated": False, "routed": "strong",
                 "answered_by": getattr(strong, "model", None) or escalation_model}
        events = release(strong, carry={"extra": extra}, context=full_context, limiters=limiters, **ctx)
    elif escalation_model:
        events = _adraft_then_escalate(prompt, llm_stream=llm_stream, escalation_stream=escalation_stream,
                                       escalation_model=escalation_model, stream_kwargs=stream_kwargs,
                                       context=full_context, postprocess=postprocess,
                                       release=release if force_buffered else None, limiters=limiters, **ctx)
    else:
        stream = llm_stream(prompt) if llm_stream else AsyncTextStream(prompt, **stream_kwargs)
        events = release(stream, context=full_context, limiters=limiters, **ctx)
    async with aclosing(events) as inner:
        async for event in inner:
            yield event
