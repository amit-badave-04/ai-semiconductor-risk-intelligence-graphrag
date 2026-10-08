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

M1b (truthful temporal layer and numbers): the full-context template lives in ``context_layout`` (re-exported here as
:data:`CONTEXT_HEADERS`, one definition for the writer and the verifier); metrics are grouped by fiscal year with a
citable ``xbrl:`` id per line and year-over-year computed in code (plus the periods the question names); Federal Register
rules get their own block (they are external events, never the company's disclosure); the temporal block lists
text-verified removed / added / reworded risk items and the changed passages inside surviving ones, with their totals,
or says a pair could not be compared; every answer carries a ``checks`` object.

Importable without a Neo4j driver; ``answer`` receives one.
"""

import hashlib
import logging
import re
import time
from collections.abc import Iterable
from datetime import date
from functools import partial
from typing import NamedTuple

import litellm
from litellm import completion

from ..artifacts import read_prompt
from ..config import get_settings
from ..llm import BACKOFF_S, MAX_BUDGET, TRANSIENT
from ..llm_shape import KNOWN_PRICES_PER_MTOK, MOCK_ALIASES, completion_params, provider_kwargs
from . import ids as _ids
from .context_layout import (  # noqa: F401 - re-exported: eval/bakeoff and the tests import these from here
    CONTEXT_HEADERS,
    LEGACY_CONTEXT_HEADERS,
    MAX_CHUNK_IDS_PER_ITEM,
    NONE_BLOCK,
    temporal_block,
)
from .retriever import METRIC_PERIODS_SHOWN, hybrid_retrieve, vector_retrieve
from .router import needs_strong_model
from .verify import answer_checks, verify_answer

logger = logging.getLogger("semigraph.answerer")

# The answering prompt (packaged as a template file). Placeholders: ``question`` and the six :class:`ContextBlocks` fields.
ANSWER_PROMPT = read_prompt("answer")

# The citation grammar lives in retrieval/ids.py (chunk, XBRL and Federal Register ids); re-exported here.
CITE_RE = _ids.CITE_RE


# Currency label for metrics that carry no unit (v1 graphs never stored/selected one; every
# us-gaap metric is USD). Keeps USD lines byte-identical to the benchmarked v1 wording.
DEFAULT_UNIT = "USD"

# The template headers (CONTEXT_HEADERS, LEGACY_CONTEXT_HEADERS) and the temporal block live in ``context_layout``: the
# verifier reads that block back, so writer and reader share one definition. Imported above.
RULE_RELATION = "AFFECTED_BY"       # a Federal Register rule linked to a company by keyword heuristic
YOY_GAP_DAYS = (350, 380)           # the prior fiscal year ends this many days earlier (52/53-week years, leap years)


def template_fingerprint() -> str:
    """A short hash of everything that shapes what the model is asked and shown: the answer prompt and the context
    headers. It is part of the answer-cache key and of the seeded examples, so a prompt or template change can never
    replay an answer that was written under the old one."""
    return hashlib.sha256("\x00".join([ANSWER_PROMPT, *CONTEXT_HEADERS]).encode("utf-8")).hexdigest()[:10]


class ContextBlocks(NamedTuple):
    """The six blocks of the context, in template order; the field names are the prompt's placeholders."""

    edges_block: str
    external_block: str
    metrics_block: str
    risks_block: str
    temporal_block: str
    chunks_block: str


def format_full_context(blocks: "ContextBlocks | tuple[str, ...]") -> str:
    """The full context string: every header of :data:`CONTEXT_HEADERS` followed by its block."""
    return "".join(header + block for header, block in zip(CONTEXT_HEADERS, blocks, strict=True))


def render_prompt(question: str, blocks: ContextBlocks) -> str:
    """The answering prompt for ``question`` over ``blocks`` (the one place the placeholders are filled)."""
    return ANSWER_PROMPT.format(question=question, **blocks._asdict())


def _metric_amount(m: dict) -> str:
    """``215,938,000,000 USD``: the metric's own unit (``TWD``/``EUR`` for IFRS filers); none renders as USD."""
    return f"{m['value']:,.0f} {m.get('unit') or DEFAULT_UNIT}"


def yoy_note(current: dict, prior: dict | None) -> str | None:
    """The year-over-year sentence for ``current`` against the fiscal year before it, computed in code (the model never
    does arithmetic): ``computed: +65.5% vs fiscal year ended 2025-01-26 (change +85,441,000,000 USD)``.

    None (no computed line) unless ``prior`` is the immediately preceding fiscal year (period ends 350-380 days apart)
    in the same unit. A zero or negative prior has no meaningful percentage: ``computed: n/m ...`` states the change only."""
    if prior is None:
        return None
    unit = current.get("unit") or DEFAULT_UNIT
    if unit != (prior.get("unit") or DEFAULT_UNIT):
        return None
    try:
        gap = (date.fromisoformat(current["period_end"]) - date.fromisoformat(prior["period_end"])).days
    except (KeyError, TypeError, ValueError):
        return None
    if not YOY_GAP_DAYS[0] <= gap <= YOY_GAP_DAYS[1]:
        return None
    now, before = float(current["value"]), float(prior["value"])
    change = f"{now - before:+,.0f} {unit}"
    if before <= 0:
        return (f"computed: n/m vs fiscal year ended {prior['period_end']} "
                f"(change {change}; percentage not meaningful, prior value not positive)")
    return f"computed: {(now - before) / before * 100:+.1f}% vs fiscal year ended {prior['period_end']} (change {change})"


def _metric_citation(m: dict) -> str | None:
    """The ``xbrl:<cik>:<metric>:<period_end>`` id of a metric row, or None when it cannot form a well-formed one."""
    try:
        citation = _ids.xbrl_id(m["cik"], m["metric"], m["period_end"])
    except (KeyError, TypeError, ValueError):
        return None
    return citation if _ids.XBRL_ID_RE.match(citation) else None


def _named_period(row: dict, years: Iterable[int], dates: Iterable[str]) -> bool:
    end = str(row.get("period_end") or "")
    return end in set(dates) or (end[:4].isdigit() and int(end[:4]) in set(years))


def _shown_rows(group: list[dict], years: Iterable[int], dates: Iterable[str]) -> list[int]:
    """Indexes (newest first) of the rows of one series that are shown: the latest :data:`METRIC_PERIODS_SHOWN`, and every
    period the question names together with the fiscal year before it (the base of its computed change)."""
    shown = set(range(min(METRIC_PERIODS_SHOWN, len(group))))
    for i, row in enumerate(group):
        if _named_period(row, years, dates):
            shown.add(i)
            if i + 1 < len(group) and yoy_note(row, group[i + 1]):
                shown.add(i + 1)
    return sorted(shown)


def metrics_lines(metrics: list[dict], *, years: Iterable[int] = (),
                  dates: Iterable[str] = ()) -> tuple[list[str], set[str]]:
    """The METRICS block lines and the ids they make citable.

    Rows are grouped per (company, metric) and the last :data:`METRIC_PERIODS_SHOWN` fiscal periods shown (the next older
    row is only the base of the oldest shown year-over-year), plus any fiscal ``years`` / period-end ``dates`` the
    question named and the fiscal year before each; the output is grouped by company, then ``fiscal year ended
    <period_end>`` newest first, so a value can only ever appear under its own fiscal year."""
    series: dict[tuple[str, str], list[dict]] = {}
    for m in metrics:
        series.setdefault((m["company"], m["metric"]), []).append(m)
    rank: dict[str, int] = {}
    entries: list[tuple[str, str, str, str, str | None]] = []       # (company, period_end, metric, line, citation)
    for (company, name), group in series.items():
        rank.setdefault(company, len(rank))
        group.sort(key=lambda m: m["period_end"], reverse=True)
        for i in _shown_rows(group, years, dates):
            m = group[i]
            note = yoy_note(m, group[i + 1] if i + 1 < len(group) else None)
            citation = _metric_citation(m)
            line = f"- {name} for period {m['period_start']}..{m['period_end']}: {_metric_amount(m)}"
            line += f" [{citation}]" if citation else ""
            line += f" | {note}" if note else ""
            entries.append((company, m["period_end"], name, line, citation))
    entries.sort(key=lambda e: e[2])
    entries.sort(key=lambda e: e[1], reverse=True)
    entries.sort(key=lambda e: rank[e[0]])
    lines, group_key = [], None
    for company, period_end, _, line, _ in entries:
        if (company, period_end) != group_key:
            group_key = (company, period_end)
            lines.append(f"{company}: fiscal year ended {period_end}")
        lines.append(line)
    return lines, {citation for *_, citation in entries if citation}


def _external_line(edge: dict, valid_ids: set[str]) -> str:
    """One dated line for a Federal Register rule linked to a company; the rule's own ``fr:`` id is its citation. The
    company chunk ids on the edge are NOT printed: they are the company's own filing text that matched keywords, and
    beside a rule they read as its source."""
    citation = _ids.fr_id(edge["rule_id"]) if edge.get("rule_id") else None
    if citation and not _ids.FR_ID_RE.match(citation):
        citation = None
    if citation:
        valid_ids.add(citation)
    label = " ".join([edge.get("date") or "undated", *([f"[{citation}]"] if citation else []), edge["target"]])
    return f"- {label} (linked to {edge['source']} by {edge.get('link_method') or 'keyword'} match)"


def build_blocks(r: dict) -> tuple[ContextBlocks, str, set[str]]:
    """Assemble prompt blocks + the full context string + the set of valid (citable) ids from a retrieval result.

    Ported from notebook 14, changed in M1b (the goldens in ``tests/test_answerer_context.py``): AFFECTED_BY rows leave
    RELATIONSHIPS for their own EXTERNAL REGULATORY EVENTS block; METRICS is grouped by fiscal year with ids and computed
    year-over-year; the temporal block is the text-verified removed / added / reworded item list. The valid ids are
    chunk ids (relations, risks, excerpts, temporal items), ``xbrl:`` ids and ``fr:`` ids."""
    valid_ids: set[str] = set()
    e_lines = []
    for e in (e for e in r["edges"] if e["relation"] != RULE_RELATION):
        ids = e.get("chunk_ids") or []
        valid_ids.update(ids)
        e_lines.append(f"- {e['source']} {e['relation']} {e['target']} (status={e.get('status')}) "
                       f"{' '.join('[' + i + ']' for i in ids[:MAX_CHUNK_IDS_PER_ITEM])}")
    x_lines = [_external_line(e, valid_ids) for e in r["edges"] if e["relation"] == RULE_RELATION]
    periods = r.get("metric_periods") or {}
    m_lines, metric_ids = metrics_lines(r["metrics"], years=periods.get("years") or (), dates=periods.get("dates") or ())
    valid_ids |= metric_ids
    # Lines a planner computed in code from cited facts (``semigraph.agent``) ride in METRICS as plain data: no template change.
    # They carry the ``[xbrl:...]`` ids of the facts they derive from, so the verifier reads their figures from those sources.
    m_lines.extend(r.get("computed") or [])
    k_lines = []
    for k in r["risks"]:
        valid_ids.add(k["chunk_id"])
        k_lines.append(f"- {k['company']} ({k['category']}): {k['summary']} [{k['chunk_id']}]")
    t_block, temporal_ids = temporal_block(r.get("temporal") or [], r.get("temporal_pairs") or [],
                                           r.get("temporal_passages") or [], r.get("temporal_notices") or [])
    valid_ids |= temporal_ids
    c_lines = []
    for c in r["chunks"]:
        valid_ids.add(c["chunk_id"])
        c_lines.append(f"[{c['chunk_id']}]\n{c['text']}\n")
    blocks = ContextBlocks("\n".join(e_lines) or NONE_BLOCK, "\n".join(x_lines) or NONE_BLOCK,
                           "\n".join(m_lines) or NONE_BLOCK, "\n".join(k_lines) or NONE_BLOCK, t_block,
                           "\n".join(c_lines) or NONE_BLOCK)
    return blocks, format_full_context(blocks), valid_ids


_EXCERPTS_MARKER = "\n\nEXCERPTS:\n"        # the last header of the current AND the legacy template
# A filing chunk or (M4) an uploaded-document chunk heads an excerpt; not part of the template fingerprint.
_EXCERPT_ID_LINE = re.compile(rf"^\[({_ids.CHUNK_ID_PATTERN}|{_ids.DOC_ID_PATTERN})\]$")


def sources_from_context(context: str) -> dict[str, str]:
    """citation id -> the text behind it, read back from the full context string: every line of the graph blocks that
    carries ``[id]`` (a risk summary, an item headline, a rule title, a metric line) and, under EXCERPTS, the chunk text
    that follows its ``[chunk id]`` line. The verifier uses it to tell whether a percentage in an answer appears in text
    THAT ANSWER CITES. Production and the bake-off both derive it from the context, so they cannot disagree; it works
    for the current and the legacy template alike."""
    head, marker, excerpts = context.partition(_EXCERPTS_MARKER)
    if not marker:
        head, excerpts = context, ""
    parts: dict[str, list[str]] = {}
    for line in head.splitlines():
        for citation in CITE_RE.findall(line):
            parts.setdefault(citation, []).append(line)
    current = None
    for line in excerpts.splitlines():
        header = _EXCERPT_ID_LINE.match(line)
        if header:
            current = header.group(1)
            parts.setdefault(current, [])
        elif current is not None:
            parts[current].append(line)
    return {citation: "\n".join(lines) for citation, lines in parts.items()}


def llm_text(prompt: str, *, model: str | None = None, max_tokens: int = 1200,
             attempts: int = 4, backoff: tuple[int, ...] = tuple(BACKOFF_S),
             timeout: float | None = None, reasoning_effort: str | None = None) -> str:
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
                **completion_params(model, budget, reasoning_effort=reasoning_effort), num_retries=2, **extra,
                **provider_kwargs(model, get_settings()),
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
    (with a warning) when the map does not know it — a wrong-but-loud cost beats a missing one.

    A staging mock (``openai/mock-luna`` ...) costs what the real model it stands for costs (``llm_shape.MOCK_ALIASES``),
    so a staging run is billed against the caps as live would be."""
    if not usage:
        return None
    prompt, completion_toks = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    model = MOCK_ALIASES.get(model, model) if model else model
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
            **provider_kwargs(self.model, get_settings()),
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
    ``valid_ids``, ``hallucinated``, the raw ``retrieval`` result and (M1b) ``checks``: the deterministic
    citation / numeric-grounding report of :func:`semigraph.retrieval.verify.answer_checks`.
    """
    if strategy == "hybrid":
        r = hybrid_retrieve(question, driver, embedder, k_chunks=k_chunks, hops=hops)
    elif strategy == "vector":
        r = vector_retrieve(question, driver, embedder, k=k_chunks)
    else:
        raise ValueError(f"unknown strategy {strategy!r} — use 'hybrid' or 'vector'")
    blocks, full_context, valid_ids = build_blocks(r)
    text = (llm or llm_text)(render_prompt(question, blocks))
    cited = set(CITE_RE.findall(text))
    checks = answer_checks(text, cited, valid_ids, full_context, sources=sources_from_context(full_context),
                           question=question)
    return {"question": question, "strategy": strategy, "answer": text,
            "citations": sorted(cited), "cited": cited, "valid_ids": valid_ids,
            "hallucinated": cited - valid_ids, "context": full_context, "checks": checks.as_dict(),
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
                usage=None, cost_usd=None, extra=None, context=None, sources=None) -> dict:
    """The terminal ``done`` event for a finished answer (usage/cost default to the stream's own).

    ``checks`` is present for EVERY answer, whether it was released as a draft, escalated, routed straight to the strong
    model or streamed live: an answer that cannot escalate reports what it fails instead of passing silently."""
    cited = set(CITE_RE.findall(text))
    own_usage = getattr(stream, "usage", None)
    checks = answer_checks(text, cited, valid_ids, context, sources=sources, question=question)
    return {"event": "done", "question": question, "strategy": strategy, "answer": text,
            "citations": sorted(cited), "hallucinated": sorted(cited - valid_ids), "checks": checks.as_dict(),
            "finish_reason": getattr(stream, "finish_reason", None),
            "usage": usage if usage is not None else own_usage,
            "cost_usd": cost_usd if cost_usd is not None else usage_cost(own_usage, getattr(stream, "model", None)),
            "chunk_ids": chunk_ids, "context_chars": context_chars, **(extra or {})}


def _live_events(stream, *, question, strategy, valid_ids, chunk_ids, context_chars, carry=None,
                 context=None, sources=None):
    """Stream ``stream`` to the client as deltas, then the terminal ``done`` (or ``error``) event.

    ``carry`` folds an earlier, rejected attempt into the totals: ``{"usage", "cost_usd", "extra"}``
    (``extra`` keys such as ``escalated`` are merged into ``done``)."""
    carry = carry or {}
    parts = []
    try:
        for delta in stream:
            parts.append(delta)
            yield {"event": "delta", "text": delta}
    except Exception as e:  # noqa: BLE001 — surface, with whatever spend is known
        usage, cost = _totals(stream, carry)
        yield {"event": "error", "detail": f"{type(e).__name__}: {e}", "partial": "".join(parts),
               "usage": usage, "cost_usd": cost, "strategy": strategy}
        return
    usage, cost = _totals(stream, carry)
    yield _done_event("".join(parts), stream, question=question, strategy=strategy, valid_ids=valid_ids,
                      chunk_ids=chunk_ids, context_chars=context_chars, usage=usage, cost_usd=cost,
                      extra=carry.get("extra"), context=context, sources=sources)


def _totals(stream, carry: dict) -> tuple[dict | None, float | None]:
    """(usage, cost) of ``stream`` with an earlier, rejected attempt carried in (see :func:`_live_events`)."""
    usage = getattr(stream, "usage", None)
    cost = usage_cost(usage, getattr(stream, "model", None))
    prior = carry.get("cost_usd")
    return _sum_usage(carry.get("usage"), usage), (cost if prior is None else round((cost or 0.0) + prior, 6))


def _buffered_events(stream, *, postprocess, question, strategy, valid_ids, chunk_ids, context_chars, carry=None,
                     context=None, sources=None):
    """:func:`_live_events` for text that must be edited before anyone sees it: drain ``stream``, apply ``postprocess``, then
    release ONE delta and the ``done`` event built on the edited text (or an ``error`` whose ``partial`` is edited too).

    The M4 upload workspace answers through this: a link or an image in an answer driven by user-uploaded text must never
    reach the client, even for the moment before it could be stripped from a live stream."""
    carry = carry or {}
    raw, error = _drain(stream)
    text = postprocess(raw)
    usage, cost = _totals(stream, carry)
    if error:
        yield {"event": "error", "detail": error, "partial": text, "usage": usage, "cost_usd": cost, "strategy": strategy}
        return
    yield {"event": "delta", "text": text}
    yield _done_event(text, stream, question=question, strategy=strategy, valid_ids=valid_ids, chunk_ids=chunk_ids,
                      context_chars=context_chars, usage=usage, cost_usd=cost, extra=carry.get("extra"), context=context,
                      sources=sources)


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


def _draft_then_escalate(prompt, *, llm_stream, escalation_stream, escalation_model, stream_kwargs, context,
                         postprocess=None, release=None, **ctx):
    """Cheap draft -> deterministic verification -> release it, or escalate to the strong model.

    The draft is buffered, so a draft the verifier rejects is never shown. A clean draft is released in one
    delta; a rejected one is announced with an ``escalated`` event (reasons included) and the strong model
    then streams live (or through ``release``, e.g. :func:`_buffered_events`). ``postprocess`` edits the draft before it
    is verified and released. Both attempts' tokens and cost land in the terminal event."""
    draft = llm_stream(prompt) if llm_stream else TextStream(prompt, **_draft_kwargs(stream_kwargs))
    text, error = _drain(draft)
    if postprocess is not None:
        text = postprocess(text)
    draft_model = getattr(draft, "model", None)
    if error:
        logger.warning("draft model %s failed (%s) - escalating to %s", draft_model, error[:300], escalation_model)
    reasons = ["draft_error"] if error else verify_answer(
        text, set(CITE_RE.findall(text)), ctx["valid_ids"], getattr(draft, "finish_reason", None), context=context,
        sources=ctx.get("sources"), question=ctx["question"])
    if not reasons:
        yield {"event": "delta", "text": text}
        yield _done_event(text, draft, extra={"escalated": False, "answered_by": draft_model, "routed": "cheap"},
                          context=context, **ctx)
        return
    logger.info("draft rejected (%s) - escalating to %s", ",".join(reasons), escalation_model)
    yield {"event": "escalated", "reasons": reasons, "from": draft_model, "to": escalation_model}
    strong_kwargs = {k: v for k, v in stream_kwargs.items() if k != "model"}
    strong = escalation_stream(prompt) if escalation_stream else TextStream(prompt, model=escalation_model, **strong_kwargs)
    draft_usage = getattr(draft, "usage", None)
    carry = {"usage": draft_usage, "cost_usd": usage_cost(draft_usage, draft_model) if draft_usage else None,
             "extra": {"escalated": True, "escalation_reasons": reasons, "routed": "cheap",
                       "answered_by": getattr(strong, "model", None) or escalation_model}}
    yield from (release or _live_events)(strong, carry=carry, context=context, **ctx)


def answer_stream(question: str, driver, embedder, strategy: str = "hybrid",
                  llm_stream=None, k_chunks: int = 8, hops: int = 2, *,
                  escalation_model: str | None = None, escalation_stream=None, **stream_kwargs):
    """Streaming variant of :func:`answer` — a generator of event dicts.

    Events, in order: ``{"event": "retrieval", "anchors", "counts", "anchor_defaulted"}``
    (``anchor_defaulted`` is True when no company was detected and retrieval fell back to
    the default anchor — additive; False when the retriever does not report it; a last key ``anchors_dropped``, the
    names of the companies the anchor cap left out, exists only when there were some), then
    ``{"event": "delta", "text"}`` per token batch, finally ``{"event": "done",
    "answer", "citations", "hallucinated", "checks", "finish_reason", "usage", "cost_usd",
    "chunk_ids", "context_chars"}``. Citations are post-verified exactly like
    ``answer``; ``checks`` (``citations_retrieved``, ``numbers_grounded``, ``numbers_checked``, ``unmatched_numbers``,
    ``echoed_numbers``, ``pseudo_citations``, ``has_citation``, ``is_refusal``, ``unsupported_removal_claim``,
    ``unsupported_removal_sentences``: see :class:`semigraph.retrieval.verify.AnswerChecks`) is on every ``done`` event,
    including answers that cannot escalate. ``llm_stream`` is injectable: ``callable(prompt) -> iterable[str]``
    (defaults to :class:`TextStream` with ``stream_kwargs``).

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
    yield from stream_answer_for_context(question, r, strategy, llm_stream=llm_stream, escalation_model=escalation_model,
                                         escalation_stream=escalation_stream, **stream_kwargs)


def dropped_anchors_field(r: dict) -> dict:
    """``{"anchors_dropped": [names]}`` when the retrieval left companies out (the anchor cap), else ``{}``: the
    additive tail of every ``retrieval`` event, so an event without dropped companies is exactly what it always was."""
    dropped = r.get("anchors_dropped")
    return {"anchors_dropped": list(dropped)} if dropped else {}


def stream_answer_for_context(question: str, r: dict, strategy: str, *, llm_stream=None, escalation_model: str | None = None,
                              escalation_stream=None, **stream_kwargs):
    """Everything :func:`answer_stream` does AFTER retrieval: build the six blocks from the retrieval dict ``r``, emit the
    ``retrieval`` event, then draft / verify / escalate or stream live, ending in the ``done`` event.

    A second retrieval planner (the agent, ``semigraph.agent``) hands its own merged ``r`` (the same dict shape as
    :func:`hybrid_retrieve` returns) to this function, so the verifier, the router, the escalation and the checks are the
    ONE implementation for both paths."""
    blocks, full_context, valid_ids = build_blocks(r)
    yield {"event": "retrieval", "anchors": r["anchors"],
           "counts": {k: len(r[k]) for k in ("edges", "metrics", "risks", "temporal", "chunks")},
           "anchor_defaulted": bool(r.get("anchor_defaulted", False)), **dropped_anchors_field(r)}
    yield from stream_answer_for_prompt(question, render_prompt(question, blocks), full_context, valid_ids,
                                        [c["chunk_id"] for c in r["chunks"]], strategy,
                                        sources=sources_from_context(full_context), llm_stream=llm_stream,
                                        escalation_model=escalation_model, escalation_stream=escalation_stream,
                                        **stream_kwargs)


def _identity(text: str) -> str:
    return text


def stream_answer_for_prompt(question: str, prompt: str, full_context: str, valid_ids: set[str], chunk_ids: list[str],
                             strategy: str, *, sources: dict[str, str] | None = None, llm_stream=None,
                             escalation_model: str | None = None, escalation_stream=None, postprocess=None,
                             force_buffered: bool = False, **stream_kwargs):
    """The writer's tail for an ALREADY RENDERED prompt: route, draft / verify / escalate or stream, ending in ``done``.

    :func:`stream_answer_for_context` renders the SEC template and calls this; the M4 upload workspace renders its own
    template (``answer_workspace.txt``) and calls it with ``force_buffered=True`` and a ``postprocess`` that strips links and
    images, so the verifier, the router, the escalation, the checks and the ``done`` grammar stay ONE implementation.
    ``sources`` defaults to :func:`sources_from_context` of ``full_context``. No ``retrieval`` event is emitted here (the
    caller owns retrieval). ``postprocess`` needs ``force_buffered``: a live stream cannot be edited after it is shown."""
    if postprocess is not None and not force_buffered:
        raise ValueError("postprocess needs force_buffered=True: a live stream cannot be edited after it is shown")
    ctx = {"question": question, "strategy": strategy, "valid_ids": valid_ids, "chunk_ids": list(chunk_ids),
           "context_chars": len(full_context),
           "sources": sources if sources is not None else sources_from_context(full_context)}
    release = partial(_buffered_events, postprocess=postprocess or _identity) if force_buffered else _live_events
    if escalation_model and escalation_model == (stream_kwargs.get("model") or get_settings().answer_model):
        escalation_model = None    # one model in both roles is plain live streaming (the documented rollback)
    if escalation_model and needs_strong_model(question):
        strong = (escalation_stream(prompt) if escalation_stream else
                  TextStream(prompt, model=escalation_model, **{k: v for k, v in stream_kwargs.items() if k != "model"}))
        extra = {"escalated": False, "routed": "strong", "answered_by": getattr(strong, "model", None) or escalation_model}
        yield from release(strong, carry={"extra": extra}, context=full_context, **ctx)
        return
    if escalation_model:
        yield from _draft_then_escalate(prompt, llm_stream=llm_stream, escalation_stream=escalation_stream,
                                        escalation_model=escalation_model, stream_kwargs=stream_kwargs,
                                        context=full_context, postprocess=postprocess,
                                        release=release if force_buffered else None, **ctx)
        return
    stream = llm_stream(prompt) if llm_stream else TextStream(prompt, **stream_kwargs)
    yield from release(stream, context=full_context, **ctx)
