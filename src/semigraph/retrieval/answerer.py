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

from litellm import completion

from ..artifacts import read_prompt
from ..config import get_settings
from ..llm import BACKOFF_S, MAX_BUDGET, TRANSIENT
from .retriever import hybrid_retrieve, vector_retrieve

logger = logging.getLogger("semigraph.answerer")

# Verbatim notebook 14 answering prompt (packaged as a template file).
ANSWER_PROMPT = read_prompt("answer")

# Verbatim notebook 14 citation grammar: accession_no:section_id:chunk_seq.
CITE_RE = re.compile(r"\[([0-9\-]+:[IVX]+\.[0-9A-Z]+:[0-9]{4})\]")


def build_blocks(r: dict) -> tuple[tuple[str, str, str, str, str], str, set[str]]:
    """Assemble prompt blocks + the full context string + the set of valid
    (citable) chunk ids from a retrieval result. Ported from notebook 14."""
    valid_ids = set()
    e_lines = []
    for e in r["edges"]:
        ids = e.get("chunk_ids") or []
        valid_ids.update(ids)
        e_lines.append(f"- {e['source']} {e['relation']} {e['target']} (status={e.get('status')}) "
                       f"{' '.join('[' + i + ']' for i in ids[:3])}")
    m_lines = [f"- {m['company']} {m['metric']} for period {m['period_start']}..{m['period_end']}: "
               f"{m['value']:,.0f} USD" for m in r["metrics"]]
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
    model = model or get_settings().llm_model
    budget = max_tokens
    last_err = "unknown"
    extra = {"timeout": timeout} if timeout else {}
    for attempt in range(attempts):
        try:
            resp = completion(
                model=model, messages=[{"role": "user", "content": prompt}],
                max_tokens=budget, thinking={"type": "disabled"}, num_retries=2, **extra,
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


def usage_cost(usage: dict | None) -> float | None:
    """USD estimate for a usage dict (prompt_tokens / completion_tokens) at the
    configured list prices. None when usage is unknown."""
    if not usage:
        return None
    s = get_settings()
    return round(usage.get("prompt_tokens", 0) * s.llm_input_price_per_mtok / 1e6
                 + usage.get("completion_tokens", 0) * s.llm_output_price_per_mtok / 1e6, 6)


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
                 timeout: float | None = None):
        self.prompt = prompt
        self.model = model or get_settings().llm_model
        self.max_tokens, self.attempts = max_tokens, attempts
        self.backoff, self.timeout = backoff, timeout
        self.finish_reason: str | None = None
        self.usage: dict | None = None
        self.text = ""

    def _run_once(self, messages: list[dict]) -> list:
        extra = {"timeout": self.timeout} if self.timeout else {}
        chunks = []
        resp = completion(
            model=self.model, messages=messages, max_tokens=self.max_tokens,
            thinking={"type": "disabled"}, num_retries=2, stream=True,
            stream_options={"include_usage": True}, **extra,
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
                wait = self.backoff[min(attempt, len(self.backoff) - 1)]
                logger.warning("transient error (%s) before first token — waiting %ss",
                               type(e).__name__, wait)
                time.sleep(wait)
                last_err = f"transient: {type(e).__name__}"
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


def answer_stream(question: str, driver, embedder, strategy: str = "hybrid",
                  llm_stream=None, k_chunks: int = 8, hops: int = 2, **stream_kwargs):
    """Streaming variant of :func:`answer` — a generator of event dicts.

    Events, in order: ``{"event": "retrieval", "anchors", "counts"}``, then
    ``{"event": "delta", "text"}`` per token batch, finally ``{"event": "done",
    "answer", "citations", "hallucinated", "finish_reason", "usage", "cost_usd",
    "chunk_ids", "context_chars"}``. Citations are post-verified exactly like
    ``answer``. ``llm_stream`` is injectable: ``callable(prompt) ->
    iterable[str]`` (defaults to :class:`TextStream` with ``stream_kwargs``).
    """
    if strategy == "hybrid":
        r = hybrid_retrieve(question, driver, embedder, k_chunks=k_chunks, hops=hops)
    elif strategy == "vector":
        r = vector_retrieve(question, driver, embedder, k=k_chunks)
    else:
        raise ValueError(f"unknown strategy {strategy!r} — use 'hybrid' or 'vector'")
    (e_b, m_b, k_b, t_b, c_b), full_context, valid_ids = build_blocks(r)
    yield {"event": "retrieval", "anchors": r["anchors"],
           "counts": {k: len(r[k]) for k in ("edges", "metrics", "risks", "temporal", "chunks")}}
    prompt = ANSWER_PROMPT.format(question=question, edges_block=e_b, metrics_block=m_b,
                                  risks_block=k_b, temporal_block=t_b, chunks_block=c_b)
    stream = llm_stream(prompt) if llm_stream else TextStream(prompt, **stream_kwargs)
    parts = []
    try:
        for delta in stream:
            parts.append(delta)
            yield {"event": "delta", "text": delta}
    except Exception as e:  # noqa: BLE001 — surface, with whatever spend is known
        usage = getattr(stream, "usage", None)
        yield {"event": "error", "detail": f"{type(e).__name__}: {e}", "partial": "".join(parts),
               "usage": usage, "cost_usd": usage_cost(usage), "strategy": strategy}
        return
    text = "".join(parts)
    cited = set(CITE_RE.findall(text))
    usage = getattr(stream, "usage", None)
    yield {"event": "done", "question": question, "strategy": strategy, "answer": text,
           "citations": sorted(cited), "hallucinated": sorted(cited - valid_ids),
           "finish_reason": getattr(stream, "finish_reason", None), "usage": usage,
           "cost_usd": usage_cost(usage), "chunk_ids": [c["chunk_id"] for c in r["chunks"]],
           "context_chars": len(full_context)}
