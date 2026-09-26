"""Grounded answer generation with verified citations.

Ported from notebook 14 (the authoritative final version: ``build_blocks`` +
``answer`` returning the FULL context string), which finalized notebook 11's
cited answering.

Battle scars preserved:

- answer() returns the FULL context string the answering model saw (graph
  blocks + excerpts), so the faithfulness judge judges against exactly that.
  Judging hybrid answers against excerpts alone was a real metric bug in the
  first eval run: 0.95 correct yet "0.36 faithful" — graph-block claims were
  invisible to the judge.
- The answering call is plain completion (unstructured, citations
  post-parsed), routed through a LOCAL hardened helper with the same
  battle-scar handling as ``semigraph.llm.llm_json``: never pass sampling
  params (400), thinking disabled on Sonnet (content=None on capped calls
  otherwise), transient-only backoff, empty-response retry,
  finish_reason=="length" -> regenerate fresh with a doubled budget.

Importable without a Neo4j driver; ``answer`` receives one.
"""

import logging
import re
import time

import litellm
from litellm import completion

from ..artifacts import read_prompt
from ..config import get_settings
from ..llm import BACKOFF_S, MAX_BUDGET, TRANSIENT
from ..llm_shape import KNOWN_PRICES_PER_MTOK, completion_params
from . import ids as _ids
from .retriever import hybrid_retrieve, vector_retrieve
from .router import needs_strong_model
from .verify import verify_answer

logger = logging.getLogger("semigraph.answerer")

# Verbatim notebook 14 answering prompt (packaged as a template file).
ANSWER_PROMPT = read_prompt("answer")

# The citation grammar lives in retrieval/ids.py (chunk, XBRL and Federal Register ids); re-exported here.
CITE_RE = _ids.CITE_RE


# Currency label for metrics that carry no unit (v1 graphs never stored/selected one; every
# us-gaap metric is USD). Keeps USD lines byte-identical to the benchmarked v1 wording.
DEFAULT_UNIT = "USD"


def format_metric_line(m: dict) -> str:
    """One METRICS line: ``- <company> <metric> for period <start>..<end>: <value> <unit>``.

    The unit is the metric's own (``TWD``/``EUR`` for IFRS filers); a missing/empty unit
    renders as USD, exactly as v1 did, so existing USD lines are unchanged.
    """
    unit = m.get("unit") or DEFAULT_UNIT
    return (f"- {m['company']} {m['metric']} for period {m['period_start']}..{m['period_end']}: "
            f"{m['value']:,.0f} {unit}")


def build_blocks(r: dict) -> tuple[tuple[str, str, str, str, str], str, set[str]]:
    """Assemble prompt blocks + the full context string + the set of valid
    (citable) chunk ids from a retrieval result. Ported from notebook 14.

    Output for existing data is byte-identical to v1 (pinned by a golden test); the only
    permitted change is the unit label of non-USD metrics (:func:`format_metric_line`)."""
    valid_ids = set()
    e_lines = []
    for e in r["edges"]:
        ids = e.get("chunk_ids") or []
        valid_ids.update(ids)
        e_lines.append(f"- {e['source']} {e['relation']} {e['target']} (status={e.get('status')}) "
                       f"{' '.join('[' + i + ']' for i in ids[:3])}")
    m_lines = [format_metric_line(m) for m in r["metrics"]]
    k_lines = []
    for k in r["risks"]:
        valid_ids.add(k["chunk_id"])
        k_lines.append(f"- {k['company']} ({k['category']}): {k['summary']} [{k['chunk_id']}]")
    t_lines = [f"- {t['company']}: disclosed {t['first_seen']} through {t['last_seen']}, then dropped — "
               f"e.g. {t['example'][:120]}" for t in r["temporal"]]
    c_lines = []
    for c in r["chunks"]:
        valid_ids.add(c["chunk_id"])
        c_lines.append(f"[{c['chunk_id']}]\n{c['text']}\n")
    blocks = ("\n".join(e_lines) or "(none)", "\n".join(m_lines) or "(none)",
              "\n".join(k_lines) or "(none)", "\n".join(t_lines) or "(none)",
              "\n".join(c_lines) or "(none)")
    full_context = ("RELATIONSHIPS:\n{}\n\nMETRICS:\n{}\n\nACTIVE RISKS:\n{}\n\n"
                    "DROPPED RISK LINEAGES:\n{}\n\nEXCERPTS:\n{}").format(*blocks)
    return blocks, full_context, valid_ids


def llm_text(prompt: str, *, model: str | None = None, max_tokens: int = 1200,
             attempts: int = 4, backoff: tuple[int, ...] = tuple(BACKOFF_S),
             timeout: float | None = None) -> str:
    """Hardened plain-completion call (the unstructured sibling of
    ``semigraph.llm.llm_json`` — notebooks 11/14 answered unstructured).

    Same battle-scar handling: no sampling params, thinking disabled,
    transient-only backoff (15/60/180/300s by default), empty-response retry,
    and truncation -> regenerate from scratch with a doubled budget. On the
    last attempt a truncated answer is returned rather than raised (the
    notebooks accepted truncated answers; the citation post-check still
    applies).

    ``attempts`` / ``backoff`` / ``timeout`` make the retry budget
    request-scoped for the web service (a browser cannot wait out a 300 s
    backoff); the pipeline keeps the long-tailed defaults.
    """
    model = model or get_settings().answer_model
    budget = max_tokens
    last_err = "unknown"
    extra = {"timeout": timeout} if timeout else {}
    for attempt in range(attempts):
        try:
            resp = completion(
                model=model, messages=[{"role": "user", "content": prompt}],
                **completion_params(model, budget), num_retries=2, **extra,
            )
        except TRANSIENT as e:
            wait = backoff[min(attempt, len(backoff) - 1)]
            logger.warning("transient error (%s) — waiting %ss then retrying",
                           type(e).__name__, wait)
            time.sleep(wait)
            last_err = f"transient: {type(e).__name__}"
            continue
        choice = resp.choices[0]
        text = choice.message.content
        if not text:
            last_err = f"empty response (finish_reason={choice.finish_reason})"
            continue
        if choice.finish_reason == "length" and attempt < attempts - 1:
            budget = min(budget * 2, MAX_BUDGET)
            last_err = "output truncated"
            logger.warning("answer truncated — regenerating with budget %d", budget)
            continue
        return text
    raise RuntimeError(f"llm_text failed after {attempts} attempts — last error: {last_err}")


def usage_cost(usage: dict | None, model: str | None = None) -> float | None:
    """USD estimate for a usage dict (prompt_tokens / completion_tokens). None when usage is unknown.

    Without ``model`` the configured list prices apply (the benchmarked Sonnet default). With a
    ``model`` its own price comes from LiteLLM's cost map, falling back to the configured prices
    (with a warning) when the map does not know it — a wrong-but-loud cost beats a missing one."""
    if not usage:
        return None
    prompt, completion_toks = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    if model and model in KNOWN_PRICES_PER_MTOK:
        per_in, per_out = KNOWN_PRICES_PER_MTOK[model]
        return round(prompt * per_in / 1e6 + completion_toks * per_out / 1e6, 6)
    if model:
        try:
            p_cost, c_cost = litellm.cost_per_token(model=model, prompt_tokens=prompt,
                                                    completion_tokens=completion_toks)
            return round(p_cost + c_cost, 6)
        except Exception as e:  # noqa: BLE001 — accounting must never break an answer
            logger.warning("no price for %s (%s) — using the configured list prices", model, type(e).__name__)
    s = get_settings()
    return round(prompt * s.llm_input_price_per_mtok / 1e6 + completion_toks * s.llm_output_price_per_mtok / 1e6, 6)


class TextStream:
    """Streaming sibling of :func:`llm_text` — iterate it for text deltas.

    After exhaustion ``finish_reason`` and ``usage`` (``prompt_tokens`` /
    ``completion_tokens``; provider-reported when available, else estimated
    by LiteLLM's chunk builder and flagged ``estimated``) are set. Transient
    errors are retried only BEFORE the first delta has been yielded — once
    text has reached the client, a mid-stream failure raises RuntimeError so
    the caller can report a partial answer honestly instead of silently
    regenerating.
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

    def _run_once(self, messages: list[dict]) -> list:
        extra = {"timeout": self.timeout} if self.timeout else {}
        chunks = []
        resp = completion(
            model=self.model, messages=messages, **completion_params(self.model, self.max_tokens),
            num_retries=self.num_retries, stream=True, stream_options={"include_usage": True}, **extra,
        )
        for chunk in resp:
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
        self._chunks = chunks

    def __iter__(self):
        import litellm

        messages = [{"role": "user", "content": self.prompt}]
        last_err = "unknown"
        for attempt in range(self.attempts):
            self._chunks = []
            try:
                yield from self._run_once(messages)
            except TRANSIENT as e:
                if self.text:
                    raise RuntimeError(f"stream interrupted mid-answer: {type(e).__name__}") from e
                last_err = f"transient: {type(e).__name__}"
                if attempt < self.attempts - 1:      # nothing to wait for after the final attempt
                    wait = self.backoff[min(attempt, len(self.backoff) - 1)]
                    logger.warning("transient error (%s) before first token — waiting %ss",
                                   type(e).__name__, wait)
                    time.sleep(wait)
                continue
            if not self.text:
                last_err = f"empty response (finish_reason={self.finish_reason})"
                continue
            if self.usage is None and self._chunks:
                try:
                    built = litellm.stream_chunk_builder(self._chunks, messages=messages)
                    self.usage = {"prompt_tokens": built.usage.prompt_tokens,
                                  "completion_tokens": built.usage.completion_tokens,
                                  "estimated": True}
                except Exception as e:  # accounting must never break an answer
                    logger.debug("usage estimate unavailable: %s", e)
            return
        raise RuntimeError(f"streaming answer failed after {self.attempts} attempts — last error: {last_err}")


def answer(question: str, driver, embedder, strategy: str = "hybrid",
           llm=None, k_chunks: int = 8, hops: int = 2) -> dict:
    """Retrieve, answer with citations, and post-verify them.

    Ported from notebook 14 ``answer()``. ``llm`` is an injectable
    ``callable(prompt) -> str`` (defaults to the hardened :func:`llm_text`).

    Returns a dict with (at least): ``answer``, ``citations`` (sorted cited
    ids), ``context`` (the FULL string the model saw), ``chunk_ids``
    (retrieved evidence-chunk ids), plus the notebook keys ``cited``,
    ``valid_ids``, ``hallucinated`` and the raw ``retrieval`` result.
    """
    if strategy == "hybrid":
        r = hybrid_retrieve(question, driver, embedder, k_chunks=k_chunks, hops=hops)
    elif strategy == "vector":
        r = vector_retrieve(question, driver, embedder, k=k_chunks)
    else:
        raise ValueError(f"unknown strategy {strategy!r} — use 'hybrid' or 'vector'")
    (e_b, m_b, k_b, t_b, c_b), full_context, valid_ids = build_blocks(r)
    prompt = ANSWER_PROMPT.format(question=question, edges_block=e_b, metrics_block=m_b,
                                  risks_block=k_b, temporal_block=t_b, chunks_block=c_b)
    text = (llm or llm_text)(prompt)
    cited = set(CITE_RE.findall(text))
    return {"question": question, "strategy": strategy, "answer": text,
            "citations": sorted(cited), "cited": cited, "valid_ids": valid_ids,
            "hallucinated": cited - valid_ids, "context": full_context,
            "chunk_ids": [c["chunk_id"] for c in r["chunks"]], "retrieval": r}


def _sum_usage(a: dict | None, b: dict | None) -> dict | None:
    """Token usage of two attempts added together (None when neither is known)."""
    if a is None or b is None:
        return a or b
    total = {"prompt_tokens": a.get("prompt_tokens", 0) + b.get("prompt_tokens", 0),
             "completion_tokens": a.get("completion_tokens", 0) + b.get("completion_tokens", 0)}
    if a.get("estimated") or b.get("estimated"):
        total["estimated"] = True
    return total


def _done_event(text: str, stream, *, question, strategy, valid_ids, chunk_ids, context_chars,
                usage=None, cost_usd=None, extra=None) -> dict:
    """The terminal ``done`` event for a finished answer (usage/cost default to the stream's own)."""
    cited = set(CITE_RE.findall(text))
    own_usage = getattr(stream, "usage", None)
    return {"event": "done", "question": question, "strategy": strategy, "answer": text,
            "citations": sorted(cited), "hallucinated": sorted(cited - valid_ids),
            "finish_reason": getattr(stream, "finish_reason", None),
            "usage": usage if usage is not None else own_usage,
            "cost_usd": cost_usd if cost_usd is not None else usage_cost(own_usage, getattr(stream, "model", None)),
            "chunk_ids": chunk_ids, "context_chars": context_chars, **(extra or {})}


def _live_events(stream, *, question, strategy, valid_ids, chunk_ids, context_chars, carry=None):
    """Stream ``stream`` to the client as deltas, then the terminal ``done`` (or ``error``) event.

    ``carry`` folds an earlier, rejected attempt into the totals: ``{"usage", "cost_usd", "extra"}``
    (``extra`` keys such as ``escalated`` are merged into ``done``)."""
    carry = carry or {}
    parts = []

    def totals():
        usage = getattr(stream, "usage", None)
        cost = usage_cost(usage, getattr(stream, "model", None))
        prior = carry.get("cost_usd")
        return _sum_usage(carry.get("usage"), usage), (cost if prior is None else round((cost or 0.0) + prior, 6))

    try:
        for delta in stream:
            parts.append(delta)
            yield {"event": "delta", "text": delta}
    except Exception as e:  # noqa: BLE001 — surface, with whatever spend is known
        usage, cost = totals()
        yield {"event": "error", "detail": f"{type(e).__name__}: {e}", "partial": "".join(parts),
               "usage": usage, "cost_usd": cost, "strategy": strategy}
        return
    usage, cost = totals()
    yield _done_event("".join(parts), stream, question=question, strategy=strategy, valid_ids=valid_ids,
                      chunk_ids=chunk_ids, context_chars=context_chars, usage=usage, cost_usd=cost,
                      extra=carry.get("extra"))


def _drain(stream) -> tuple[str, str | None]:
    """Consume a stream silently; returns (text, error description or None)."""
    parts = []
    try:
        for delta in stream:
            parts.append(delta)
    except Exception as e:  # noqa: BLE001
        return "".join(parts), f"{type(e).__name__}: {e}"
    return "".join(parts), None


DRAFT_TIMEOUT_S = 30   # a hung cheap model must not hold the request (and an answer slot) for minutes


def _draft_kwargs(stream_kwargs: dict) -> dict:
    """The cheap draft fails fast (one attempt, no provider retries, a short timeout) because the strong model
    is the fallback: waiting out retries on a struggling provider would only delay the answer it cannot give."""
    timeout = min(stream_kwargs.get("timeout") or DRAFT_TIMEOUT_S, DRAFT_TIMEOUT_S)
    return {**stream_kwargs, "attempts": 1, "num_retries": 0, "timeout": timeout}


def _draft_then_escalate(prompt, *, llm_stream, escalation_stream, escalation_model, stream_kwargs, context, **ctx):
    """Cheap draft -> deterministic verification -> release it, or escalate to the strong model.

    The draft is buffered, so a draft the verifier rejects is never shown. A clean draft is released in one
    delta; a rejected one is announced with an ``escalated`` event (reasons included) and the strong model
    then streams live. Both attempts' tokens and cost land in the terminal event."""
    draft = llm_stream(prompt) if llm_stream else TextStream(prompt, **_draft_kwargs(stream_kwargs))
    text, error = _drain(draft)
    draft_model = getattr(draft, "model", None)
    if error:
        logger.warning("draft model %s failed (%s) - escalating to %s", draft_model, error[:300], escalation_model)
    reasons = ["draft_error"] if error else verify_answer(
        text, set(CITE_RE.findall(text)), ctx["valid_ids"], getattr(draft, "finish_reason", None), context=context)
    if not reasons:
        yield {"event": "delta", "text": text}
        yield _done_event(text, draft, extra={"escalated": False, "answered_by": draft_model, "routed": "cheap"}, **ctx)
        return
    logger.info("draft rejected (%s) - escalating to %s", ",".join(reasons), escalation_model)
    yield {"event": "escalated", "reasons": reasons, "from": draft_model, "to": escalation_model}
    strong_kwargs = {k: v for k, v in stream_kwargs.items() if k != "model"}
    strong = escalation_stream(prompt) if escalation_stream else TextStream(prompt, model=escalation_model, **strong_kwargs)
    draft_usage = getattr(draft, "usage", None)
    carry = {"usage": draft_usage, "cost_usd": usage_cost(draft_usage, draft_model) if draft_usage else None,
             "extra": {"escalated": True, "escalation_reasons": reasons, "routed": "cheap",
                       "answered_by": getattr(strong, "model", None) or escalation_model}}
    yield from _live_events(strong, carry=carry, **ctx)


def answer_stream(question: str, driver, embedder, strategy: str = "hybrid",
                  llm_stream=None, k_chunks: int = 8, hops: int = 2, *,
                  escalation_model: str | None = None, escalation_stream=None, **stream_kwargs):
    """Streaming variant of :func:`answer` — a generator of event dicts.

    Events, in order: ``{"event": "retrieval", "anchors", "counts", "anchor_defaulted"}``
    (``anchor_defaulted`` is True when no company was detected and retrieval fell back to
    the default anchor — additive; False when the retriever does not report it), then
    ``{"event": "delta", "text"}`` per token batch, finally ``{"event": "done",
    "answer", "citations", "hallucinated", "finish_reason", "usage", "cost_usd",
    "chunk_ids", "context_chars"}``. Citations are post-verified exactly like
    ``answer``. ``llm_stream`` is injectable: ``callable(prompt) ->
    iterable[str]`` (defaults to :class:`TextStream` with ``stream_kwargs``).

    With ``escalation_model`` set, questions about change over time (:func:`needs_strong_model`) stream the
    strong model live (``routed: "strong"``); every other question's draft is buffered and verified before
    anything is shown (see :func:`_draft_then_escalate`): the deltas then arrive after generation, an
    ``escalated`` event precedes the strong model's live stream when the draft is rejected, and ``done``
    additionally carries ``escalated`` / ``answered_by`` / ``routed`` (and ``escalation_reasons``). Without an
    escalation model the behaviour is unchanged: the model streams live.
    """
    if strategy == "hybrid":
        r = hybrid_retrieve(question, driver, embedder, k_chunks=k_chunks, hops=hops)
    elif strategy == "vector":
        r = vector_retrieve(question, driver, embedder, k=k_chunks)
    else:
        raise ValueError(f"unknown strategy {strategy!r} — use 'hybrid' or 'vector'")
    (e_b, m_b, k_b, t_b, c_b), full_context, valid_ids = build_blocks(r)
    yield {"event": "retrieval", "anchors": r["anchors"],
           "counts": {k: len(r[k]) for k in ("edges", "metrics", "risks", "temporal", "chunks")},
           "anchor_defaulted": bool(r.get("anchor_defaulted", False))}
    prompt = ANSWER_PROMPT.format(question=question, edges_block=e_b, metrics_block=m_b,
                                  risks_block=k_b, temporal_block=t_b, chunks_block=c_b)
    ctx = {"question": question, "strategy": strategy, "valid_ids": valid_ids,
           "chunk_ids": [c["chunk_id"] for c in r["chunks"]], "context_chars": len(full_context)}
    if escalation_model and escalation_model == (stream_kwargs.get("model") or get_settings().answer_model):
        escalation_model = None    # one model in both roles is plain live streaming (the documented rollback)
    if escalation_model and needs_strong_model(question):
        strong = (escalation_stream(prompt) if escalation_stream else
                  TextStream(prompt, model=escalation_model, **{k: v for k, v in stream_kwargs.items() if k != "model"}))
        extra = {"escalated": False, "routed": "strong", "answered_by": getattr(strong, "model", None) or escalation_model}
        yield from _live_events(strong, carry={"extra": extra}, **ctx)
        return
    if escalation_model:
        yield from _draft_then_escalate(prompt, llm_stream=llm_stream, escalation_stream=escalation_stream,
                                        escalation_model=escalation_model, stream_kwargs=stream_kwargs,
                                        context=full_context, **ctx)
        return
    stream = llm_stream(prompt) if llm_stream else TextStream(prompt, **stream_kwargs)
    yield from _live_events(stream, **ctx)
