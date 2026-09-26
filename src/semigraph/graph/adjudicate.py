"""Cheap, safe LLM adjudication of the risk items the aligner could not settle (M1b step 4). OFF by default.

The aligner (``graph/alignment.py``) labels an older item ``uncertain`` (probes partly present, or a body match in the
uncertainty band) or ``removed`` (no probe found anywhere in the newer section). Adjudication asks a cheap model one
question per such item and lets the answer change the label ONLY through rules enforced here, in code:

* the prompt holds the item text (headline plus the start of the body, capped) and the 8 best CANDIDATE PASSAGES of the newer
  section, chosen by idf-weighted lexical similarity over sentence windows (1-3 consecutive sentences; each candidate is a
  verbatim slice of the section, no two overlap);
* the model answers JSON ``{"verdict": "same" | "reworded" | "removed", "quote": <verbatim text or null>}``;
* ``same`` / ``reworded`` need a ``quote`` that (a) is a verbatim slice of the newer section (whitespace/quote-normalised
  containment, the aligner's own rule: ``align_text.SectionIndex``) and (b) is related to the item (``partial_ratio`` of the
  quote against the best sentence of the item >= a floor: 70 for ``same``, 62 for ``reworded``, the floors and the measured
  unrelated-sentence noise are those of ``eval/gold.py``); otherwise the verdict falls back to ``uncertain`` = PRESENT.
  A valid ``same`` / ``reworded`` pairs the item with the newer item that holds the quote when that is the aligner's own
  candidate (``alignment.apply_adjudication`` settles it, decided_by ``llm``); when the quote lies in a DIFFERENT newer item the
  older item becomes ``merged`` into that item (decided_by ``llm``) and a newer item the aligner had called ``new`` becomes
  ``carried``; when no single newer item holds the quote the item stays ``uncertain``. A newer item the aligner had left
  ``uncertain`` (the candidate of an older item) that no settled older item claims afterwards stays EXACTLY as the aligner left
  it: never ``new`` and never ``carried`` as a side effect, whatever else was adjudicated in the pair (the newer labels are
  recomputed by ``apply_adjudication`` whenever a pair has a verdict, so this is enforced here to keep the outcome independent
  of how many verdicts the pair happens to hold);
* a final ``removed`` needs the aligner to have said removed / uncertain AND the model to say removed AND (for an aligner
  ``uncertain``) ``apply_adjudication``'s own text guard: an aligner ``removed`` item is turned into ``uncertain`` first, so a
  model that says ``same`` / ``reworded`` (or gives an invalid quote) can only ever move it towards PRESENT, and the settle
  rules of ``apply_adjudication`` are the single implementation of "removed";
* every raw model answer is checkpointed to ``adjudications.jsonl`` keyed by (item text hash, newer section hash, prompt
  version, model): a re-run never pays for an answer it holds. Validation is re-run on every read, so changing a floor never
  needs a new call.

The model call is INJECTED (``llm_json``-compatible: ``(prompt, model_cls, *, model, max_tokens, thinking_off)``). The
default, :func:`default_llm_json`, calls ``semigraph.llm.llm_json`` for Anthropic-shaped models and the provider-aware
hardened text call (``retrieval.answerer.llm_text``: ``llm_shape.completion_params``, transient-only backoff, truncation ->
regenerate) for every other provider, because ``llm_json`` always sends ``max_tokens`` + ``thinking``, which GPT-6 rejects
(400). The live call shape for the default model ``openai/gpt-6-luna`` was NOT probed here (no paid call is allowed in the
build); a first paid run must start with ``--dry-run`` and a tiny ``--max-usd``.
"""

import json
import logging
import math
import os
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel
from rapidfuzz import fuzz

from ..hashing import content_hash
from .align_text import SectionIndex, at_least, in_range, norm, split_sentences, word_tokens
from .alignment import AlignmentResult, NewerDecision, OlderDecision, apply_adjudication

logger = logging.getLogger("semigraph.graph.adjudicate")

PROMPT_VERSION = "adj-v1"
CHECKPOINT_NAME = "adjudications.jsonl"
ADJUDICATED_LABELS = ("uncertain", "removed")       # aligner labels that are sent to the model
_CHARS_PER_TOKEN_WORST = 3.0                        # English is ~4; the cap must never under-estimate
_LIKELY_OUTPUT_TOKENS = 80                          # a verdict plus one quote


class AdjudicationVerdict(BaseModel):
    """The model's answer."""

    verdict: Literal["same", "reworded", "removed"]
    quote: str | None = None


@dataclass(frozen=True)
class AdjudicationParams:
    """Every knob of the adjudication. STARTING values, to be calibrated on the frozen gold.

    ``min_relatedness_same`` / ``min_relatedness_reworded``: ``partial_ratio`` (0-100) of the quote against the best sentence of
    the item (the floors of ``eval/gold.py``: unrelated same-filing sentences score median 46, p99 56, max 61).
    ``min_quote_chars``: a shorter (normalised) quote proves nothing. ``max_quote_chars``: cap of the verbatim slice kept.
    """

    n_candidates: int = 8
    max_item_chars: int = 2500
    max_window_sentences: int = 3
    max_window_chars: int = 900
    max_single_sentence_chars: int = 1500
    max_output_tokens: int = 1000
    min_quote_chars: int = 30
    max_quote_chars: int = 600
    min_relatedness_same: float = 70.0
    min_relatedness_reworded: float = 62.0

    def __post_init__(self) -> None:
        for name in ("n_candidates", "max_window_sentences", "min_quote_chars"):
            at_least(name, getattr(self, name), 1)
        for name in ("max_item_chars", "max_window_chars", "max_single_sentence_chars", "max_output_tokens", "max_quote_chars"):
            at_least(name, getattr(self, name), 100)
        for name in ("min_relatedness_same", "min_relatedness_reworded"):
            in_range(name, getattr(self, name), 0.0, 100.0)


# --------------------------------------------------------------------------
# candidate passages and the prompt
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    text: str
    start: int
    end: int
    score: float


def _content_tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in word_tokens(text) if len(t) >= 3)


def candidate_passages(item_text: str, section_text: str, params: AdjudicationParams = AdjudicationParams()) -> list[Candidate]:
    """The best ``n_candidates`` verbatim, non-overlapping sentence windows of ``section_text`` for ``item_text``.

    A window is 1-``max_window_sentences`` consecutive sentences (at most ``max_window_chars`` long unless it is one long
    sentence, which is cut to ``max_single_sentence_chars``). Score: the idf-weighted (over the section's sentences) cosine of
    the binary content-token sets of the item's first ``max_item_chars`` characters and the window. Ranked by score, then by
    position; the result is in rank order. Pure and deterministic."""
    spans = split_sentences(section_text)
    if not spans:
        return []
    tokens = [_content_tokens(section_text[a:b]) for a, b in spans]
    df = Counter(t for s in tokens for t in s)
    n = len(spans)

    def idf(token: str) -> float:
        return math.log((n + 1) / (df.get(token, 0) + 1)) + 1e-9

    query = {t: idf(t) for t in _content_tokens(item_text[:params.max_item_chars]) if t in df}
    total_query = sum(query.values())
    if total_query <= 0.0:
        return []
    scored: list[Candidate] = []
    for i in range(n):
        window: set[str] = set()
        for j in range(i, min(n, i + params.max_window_sentences)):
            start, end = spans[i][0], spans[j][1]
            if j > i and end - start > params.max_window_chars:
                break
            window |= tokens[j]
            overlap = sum(w for t, w in query.items() if t in window)
            if overlap <= 0.0:
                continue
            norm_w = sum(idf(t) for t in window)
            end = min(end, start + params.max_single_sentence_chars) if j == i else end
            scored.append(Candidate(section_text[start:end], start, end, overlap / math.sqrt(total_query * norm_w)))
    scored.sort(key=lambda c: (-c.score, c.start, c.end))
    chosen: list[Candidate] = []
    for cand in scored:
        if len(chosen) == params.n_candidates:
            break
        if all(cand.end <= c.start or cand.start >= c.end for c in chosen):
            chosen.append(cand)
    return chosen


_PROMPT = """You compare two consecutive annual risk-factor disclosures of the same company.

OLDER RISK FACTOR (verbatim from the earlier filing{cut}):
<<<
{item}
>>>

CANDIDATE PASSAGES from the risk-factor section of the NEWER filing (verbatim; the passages most similar to the older risk factor):
{candidates}

Question: does the newer filing still disclose this risk?
Answer with ONE JSON object and nothing else: {{"verdict": "same" | "reworded" | "removed", "quote": <string or null>}}
- "same": the newer filing makes this risk disclosure unchanged or almost unchanged.
- "reworded": the same risk is still disclosed in different words, or inside another risk factor.
- "removed": none of the passages expresses this risk (and nothing else you can see in them does).
Rules: for "same" and "reworded" give "quote": at least 30 characters copied character for character from ONE candidate passage \
that expresses the older risk (no paraphrase, no ellipsis, no stitching). If you cannot copy such a quote, answer "removed". \
For "removed" set "quote" to null. Being wrong about "removed" is the costly mistake: when a passage plausibly expresses the risk, \
do not answer "removed"."""


def build_prompt(item_text: str, candidates: Sequence[Candidate], params: AdjudicationParams = AdjudicationParams()) -> str:
    """The adjudication prompt (:data:`PROMPT_VERSION`): item text plus the numbered candidate passages, all verbatim."""
    body = item_text.strip()
    shown = body[:params.max_item_chars]
    numbered = "\n\n".join(f"[{k}] {c.text}" for k, c in enumerate(candidates, 1)) or "(no similar passage was found)"
    return _PROMPT.format(item=shown, cut=" - cut after the first %d characters" % params.max_item_chars
                          if len(body) > params.max_item_chars else "", candidates=numbered)


# --------------------------------------------------------------------------
# tasks, cost, checkpoint
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    """One model call: an older item of one pair. ``key`` identifies the answer in the checkpoint."""

    key: str
    pair_id: str
    item_id: str
    prompt: str


def task_key(item_text_hash: str, section_hash: str, model: str, prompt_version: str = PROMPT_VERSION) -> str:
    """The checkpoint key: item text hash | newer section hash | prompt version | model (a different model repays)."""
    return "|".join((item_text_hash, section_hash, prompt_version, model))


def section_hash(newer_section_text: str) -> str:
    return content_hash(newer_section_text)


def plan_tasks(pair_id: str, result: AlignmentResult, older_rows: Sequence[Mapping[str, Any]], newer_section_text: str,
               model: str, params: AdjudicationParams = AdjudicationParams()) -> list[Task]:
    """One task per older item the aligner labelled ``uncertain`` or ``removed`` (in item order)."""
    wanted = {d.item_id for d in result.older if d.label in ADJUDICATED_LABELS}
    sec = section_hash(newer_section_text)
    tasks = []
    for row in older_rows:
        if row["item_id"] not in wanted:
            continue
        text = str(row["text"])
        prompt = build_prompt(text, candidate_passages(text, newer_section_text, params), params)
        tasks.append(Task(task_key(row.get("text_hash") or content_hash(text), sec, model), pair_id, row["item_id"], prompt))
    return tasks


@dataclass(frozen=True)
class Estimate:
    """What an adjudication run would cost. ``worst_case_usd`` prices every uncached call at its prompt size (chars / 3) plus the
    full output cap; ``likely_usd`` at chars / 4 plus a typical verdict."""

    model: str
    n_items: int
    n_cached: int
    n_calls: int
    input_tokens: int
    output_tokens_cap: int
    worst_case_usd: float
    likely_usd: float
    priced: bool


def model_prices(model: str) -> tuple[float, float, bool]:
    """(USD per million input tokens, per million output tokens, exact). Unknown models fall back to the configured list prices
    (Sonnet's: an over-estimate for anything cheaper) and are flagged ``exact=False``."""
    from ..llm_shape import KNOWN_PRICES_PER_MTOK

    if model in KNOWN_PRICES_PER_MTOK:
        per_in, per_out = KNOWN_PRICES_PER_MTOK[model]
        return per_in, per_out, True
    from ..config import get_settings

    s = get_settings()
    logger.warning("no known price for %s: estimating with the configured list prices", model)
    return s.llm_input_price_per_mtok, s.llm_output_price_per_mtok, False


def estimate_cost(tasks: Sequence[Task], cached_keys: Collection[str], model: str,
                  params: AdjudicationParams = AdjudicationParams()) -> Estimate:
    per_in, per_out, exact = model_prices(model)
    todo = [t for t in tasks if t.key not in cached_keys]
    chars = sum(len(t.prompt) for t in todo)
    input_tokens = math.ceil(chars / _CHARS_PER_TOKEN_WORST)
    worst = input_tokens * per_in / 1e6 + len(todo) * params.max_output_tokens * per_out / 1e6
    likely = math.ceil(chars / 4) * per_in / 1e6 + len(todo) * _LIKELY_OUTPUT_TOKENS * per_out / 1e6
    return Estimate(model, len(tasks), len(tasks) - len(todo), len(todo), input_tokens, len(todo) * params.max_output_tokens,
                    round(worst, 6), round(likely, 6), exact)


def call_cost_upper_bound(prompt: str, model: str, params: AdjudicationParams = AdjudicationParams()) -> float:
    per_in, per_out, _ = model_prices(model)
    return math.ceil(len(prompt) / _CHARS_PER_TOKEN_WORST) * per_in / 1e6 + params.max_output_tokens * per_out / 1e6


class Checkpoint:
    """Append-only JSONL of raw model answers (one line per call, flushed at once). A torn last line is ignored."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, dict] = {}
        if path.exists():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    self.records[record["key"]] = record
                except (json.JSONDecodeError, KeyError):
                    logger.warning("%s line %d is not a valid checkpoint record: ignored", path.name, number)

    def add(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.records[record["key"]] = record


class BudgetExceeded(RuntimeError):
    """The worst-case cost of the calls still to make is above the cap (raised before any call is made)."""


def run_tasks(tasks: Sequence[Task], checkpoint: Checkpoint, llm: Callable[..., Any], *, model: str, max_usd: float,
              params: AdjudicationParams = AdjudicationParams()) -> int:
    """Answer every task the checkpoint does not hold yet; returns the number of calls made.

    Refuses to start when the worst case of ALL remaining calls is above ``max_usd`` and stops (raising) if a running total of
    per-call upper bounds would pass it. Each answer is checkpointed the moment it arrives, so an interruption or a failing call
    never repays what was bought; a failing call raises (nothing is guessed)."""
    todo = [t for t in tasks if t.key not in checkpoint.records]
    worst = estimate_cost(todo, set(), model, params).worst_case_usd
    if worst > max_usd:
        raise BudgetExceeded(f"worst case ${worst:.4f} for {len(todo)} call(s) exceeds --max-usd ${max_usd:.2f}")
    spent = 0.0
    for calls, task in enumerate(todo, 1):
        bound = call_cost_upper_bound(task.prompt, model, params)
        if spent + bound > max_usd + 1e-12:
            raise BudgetExceeded(f"stopped after {calls - 1} call(s): the next call could pass --max-usd ${max_usd:.2f}")
        answer = llm(task.prompt, AdjudicationVerdict, model=model, max_tokens=params.max_output_tokens, thinking_off=True)
        spent += bound
        checkpoint.add({"key": task.key, "pair_id": task.pair_id, "item_id": task.item_id, "model": model,
                        "prompt_version": PROMPT_VERSION, "verdict": answer.verdict, "quote": answer.quote,
                        "est_usd_upper_bound": round(bound, 6)})
    return len(todo)


# --------------------------------------------------------------------------
# validation and settling
# --------------------------------------------------------------------------

def _item_sentences(row: Mapping[str, Any]) -> list[str]:
    text = str(row["text"])
    sentences = [norm(text[a:b]) for a, b in split_sentences(text)]
    return [s for s in sentences if s]


def relatedness(quote: str, older_row: Mapping[str, Any]) -> float:
    """Best ``partial_ratio`` (0-100) of the normalised quote against one sentence of the item (its headline included)."""
    needle = norm(quote)
    return max((float(fuzz.partial_ratio(needle, s)) for s in _item_sentences(older_row)), default=0.0)


def validate_verdict(record: Mapping[str, Any], older_row: Mapping[str, Any], index: SectionIndex,
                     params: AdjudicationParams = AdjudicationParams()):
    """``(effective verdict, Hit | None, reason | None)``: the model's answer after the code's rules.

    ``removed`` passes through. ``same`` / ``reworded`` need a verbatim, related quote; otherwise the effective verdict is
    ``uncertain`` (present) and ``reason`` says why."""
    verdict = record.get("verdict")
    if verdict == "removed":
        return "removed", None, None
    if verdict not in ("same", "reworded"):
        return "uncertain", None, f"unusable verdict {verdict!r}"
    quote = record.get("quote")
    if not isinstance(quote, str) or len(norm(quote)) < params.min_quote_chars:
        return "uncertain", None, "no usable quote"
    hit = index.probe(quote, 100.0, params.max_quote_chars, fuzzy=False)
    if hit is None:
        return "uncertain", None, "quote is not a verbatim slice of the newer section"
    floor = params.min_relatedness_same if verdict == "same" else params.min_relatedness_reworded
    score = relatedness(quote, older_row)
    if score < floor:
        return "uncertain", None, f"quote is not related to the item (partial_ratio {score:.0f} < {floor:.0f})"
    return verdict, hit, None


def holder_of(span: tuple[int, int], newer_rows: Sequence[Mapping[str, Any]]) -> str | None:
    """The id of the newer item whose character range holds ``span`` (None when it lies in none, or straddles two)."""
    for row in newer_rows:
        if row["char_start"] <= span[0] and span[1] <= row["char_end"]:
            return row["item_id"]
    return None


@dataclass(frozen=True)
class Settled:
    """The result after adjudication and what happened per adjudicated item (``notes``: item id -> outcome, for the audit)."""

    result: AlignmentResult
    adjudicated: frozenset[str]
    notes: Mapping[str, str]


def settle(result: AlignmentResult, older_rows: Sequence[Mapping[str, Any]], newer_rows: Sequence[Mapping[str, Any]],
           newer_section_text: str, records: Mapping[str, Mapping[str, Any]],
           params: AdjudicationParams = AdjudicationParams()) -> Settled:
    """Apply the model's raw answers (``records``: older item id -> ``{"verdict", "quote"}``) under the rules of the module doc.

    Only older items the aligner labelled ``uncertain`` / ``removed`` may have a record (anything else raises ``ValueError``);
    an item without a record keeps the aligner's label. Every item with a record is ``adjudicated``."""
    older_by_id = {r["item_id"]: r for r in older_rows}
    index = SectionIndex(newer_section_text)
    settled: list[OlderDecision] = []
    verdicts: dict[str, str] = {}
    merged_into: dict[str, str] = {}
    notes: dict[str, str] = {}
    for d in result.older:
        record = records.get(d.item_id)
        if record is None:
            settled.append(d)
            continue
        if d.label not in ADJUDICATED_LABELS:
            raise ValueError(f"item {d.item_id!r} is {d.label}: only uncertain / removed items are adjudicated")
        effective, hit, reason = validate_verdict(record, older_by_id[d.item_id], index, params)
        base = replace(d, label="uncertain", decided_by="uncertain") if d.label == "removed" else d
        if effective == "removed":
            verdicts[d.item_id] = "removed"
            settled.append(base)
            notes[d.item_id] = f"model: removed (aligner: {d.label})"
        elif effective == "uncertain":
            settled.append(base)
            notes[d.item_id] = f"model {record.get('verdict')} rejected: {reason}"
        else:
            evidence = replace(d.evidence, quote=hit.quote, quote_span=hit.span, quote_score=100.0,
                               quote_item_id=holder_of(hit.span, newer_rows))
            holder = evidence.quote_item_id
            if holder is not None and d.label == "uncertain" and d.matched_newer_id == holder:
                verdicts[d.item_id] = effective
                settled.append(replace(base, evidence=evidence))
                notes[d.item_id] = f"model: {effective}"
            elif holder is not None:
                settled.append(replace(d, label="merged", matched_newer_id=holder, decided_by="llm", evidence=evidence))
                merged_into.setdefault(holder, d.item_id)
                notes[d.item_id] = f"model: {effective}, quote lies in {holder}: merged"
            else:
                settled.append(replace(base, evidence=evidence))
                notes[d.item_id] = f"model: {effective}, quote lies in no single newer item: uncertain"
    converted = AlignmentResult(tuple(settled), result.newer, result.params)
    final = apply_adjudication(converted, verdicts)
    return Settled(AlignmentResult(final.older, _settled_newer(result, final, merged_into), final.params), frozenset(records), notes)


def _settled_newer(original: AlignmentResult, final: AlignmentResult, merged_into: Mapping[str, str]) -> tuple[NewerDecision, ...]:
    """The newer side after settling, independent of how many verdicts the pair holds.

    * a newer item into which an older item was merged by the model is ``carried`` (matched to that older item, ``llm``), whether
      the aligner had called it ``new`` or ``apply_adjudication``'s recomputation already made it ``carried``;
    * a newer item the aligner had left ``uncertain`` and that no settled older item claims (unchanged / reworded partner, or
      merge target) keeps the aligner's decision unchanged."""
    before = {n.item_id: n for n in original.newer}
    claimed = {d.matched_newer_id for d in final.older if d.label in ("unchanged", "reworded", "merged") and d.matched_newer_id}
    out = []
    for n in final.newer:
        host = merged_into.get(n.item_id)
        if host is not None and (n.label == "new" or (n.label == "carried" and n.matched_older_id == host)):
            out.append(replace(n, label="carried", matched_older_id=host, decided_by="llm"))
        elif before[n.item_id].label == "uncertain" and n.item_id not in claimed:
            out.append(before[n.item_id])
        else:
            out.append(n)
    return tuple(out)


# --------------------------------------------------------------------------
# the default model call
# --------------------------------------------------------------------------

def _json_object(text: str) -> str:
    """The first balanced ``{...}`` of a reply (models wrap JSON in fences or a sentence)."""
    start = text.find("{")
    depth = 0
    for i in range(max(start, 0), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text


def default_llm_json(prompt: str, model_cls, *, model: str, max_tokens: int, thinking_off: bool = True):
    """``llm_json`` for Anthropic-shaped models; the provider-aware hardened text call for every other provider.

    ``semigraph.llm.llm_json`` always sends ``max_tokens`` and ``thinking``; ``llm_shape.completion_params`` documents that
    GPT-6 answers 400 to ``max_tokens`` and takes no ``thinking``. So a non-Anthropic model goes through
    ``retrieval.answerer.llm_text`` (which uses ``completion_params``, retries only transient errors, regenerates a truncated
    answer) and this function validates the JSON, with two correction turns. Not probed live (see the module doc)."""
    from ..llm_shape import completion_params

    if "thinking" in completion_params(model, max_tokens):
        from ..llm import llm_json

        return llm_json(prompt, model_cls, model=model, max_tokens=max_tokens, thinking_off=thinking_off)
    from pydantic import ValidationError

    from ..retrieval.answerer import llm_text

    turn, last = prompt, "unknown"
    for _ in range(3):
        text = llm_text(turn, model=model, max_tokens=max_tokens)
        try:
            return model_cls.model_validate_json(_json_object(text.strip()))
        except ValidationError as err:
            last = str(err)[:300]
            turn = (f"{prompt}\n\nYour previous reply was not valid JSON for the schema ({last}). "
                    "Reply with the corrected JSON object only.")
    raise RuntimeError(f"adjudication reply was not valid JSON after 3 attempts: {last}")
