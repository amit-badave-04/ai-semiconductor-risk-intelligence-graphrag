"""Cheap, safe LLM settlement of the passage layer's lexical BAND (M1b). Calls are OFF by default; cached answers are always replayed.

The passage layer (``graph/passages.py``) cannot tell, from word overlap alone, a real paraphrase from a lookalike between
``reword_min`` and ``reword_confident`` (Nvidia's "we transitioned some operations ... out of China and Hong Kong" matches an
unrelated Hong Kong warehousing sentence at 0.367). Every such BAND sentence gets ONE question for a cheap model, and the answer
changes the passage layer ONLY through rules enforced here, in code (mirroring ``graph/adjudicate.py``, the item-level version):

* the prompt holds the sentence and up to 5 CANDIDATE SENTENCES of the other filing's section (the lexical best that put the
  sentence in the band, then alternately the best by word similarity and by ``rapidfuzz.fuzz.partial_ratio``, de-duplicated; each
  a verbatim sentence of the section);
* the model answers JSON ``{"verdict": "same" | "different", "candidate": <1-5> | null, "quote": <verbatim text> | null}``
  ("same": some candidate states the same fact; a changed tense, number or date is still the same fact);
* ``same`` needs a valid quote: the candidate number is one of the shown candidates, the quote (>= 30 normalised characters, the
  gold's floor) is contained, after whitespace / quote normalisation, in THAT candidate AND in the other section, the candidate is
  itself a verbatim slice of the section, and the quote is related to the sentence (``partial_ratio`` of the two >= 62, the gold's
  ``SENT_REWORDED_MIN_SIM``: unrelated same-filing sentences score median 46, p99 56, max 61). Otherwise there is NO verdict and
  the passage layer keeps the sentence ``reworded`` (as before the band existed): a wrong or invented ``same`` can never become a
  removal or an addition;
* ``different`` becomes a ``BandVerdict("different")`` (the passage layer then reports the sentence as removed / added). It cannot
  be checked by code beyond the candidate list; its accuracy is a measured property (``scripts/tune_passages.py --verdicts``);
* every raw answer is checkpointed to ``passage_adjudications.jsonl`` (one flushed line per call) keyed by (sentence hash |
  hash of the other section | prompt version | model) and stores the sentence and the candidates the model saw. A re-run never
  repays, and validation is re-run on every read, so changing a floor never needs a new call. ``align-items`` replays this file
  whether or not ``--adjudicate-passages`` is given (that flag only buys the missing answers);
* ``--max-usd`` is checked against the WORST case (every uncached call priced at its prompt size plus the full output cap) before
  any call, and a running total of per-call upper bounds stops a run that would pass it.

The model call is INJECTED (``llm_json``-compatible). The default is ``adjudicate.default_llm_json`` (Anthropic-shaped models via
``semigraph.llm.llm_json``; every other provider through the provider-aware ``llm_text``, which sends ``max_completion_tokens``
to GPT-6 and validates the JSON with correction turns). The live behaviour of the default model ``openai/gpt-6-luna`` on THIS
prompt was NOT probed while building (no paid call is allowed then): run ``semigraph align-items --probe-adjudicator`` first
(two calls, well under a cent; it prints the raw reply, the parsed verdict and whether the code rules accepted it).
"""

import json
import logging
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ValidationError
from rapidfuzz import fuzz

from . import adjudicate as adj
from .adjudicate import BudgetExceeded, Checkpoint, default_llm_json
from .align_text import SectionIndex, at_least, in_range, lex_exact, norm, word_tokens
from .passages import BandCandidate, BandSentence, BandVerdict

__all__ = ["BudgetExceeded", "Checkpoint", "default_llm_json"]     # re-exported: one budget error and one call dispatch

logger = logging.getLogger("semigraph.graph.passage_adjudicate")

PROMPT_VERSION = "pas-v1"
CHECKPOINT_NAME = "passage_adjudications.jsonl"
MIN_RELATEDNESS = 62.0            # eval.gold.SENT_REWORDED_MIN_SIM (a test keeps them equal; eval/ is not imported here)
MIN_QUOTE_CHARS = 30              # eval.gold.SENT_MIN_QUOTE_CHARS
_CHARS_PER_TOKEN_WORST = 3.0      # English is ~4; the cap must never under-estimate
_LIKELY_OUTPUT_TOKENS = 40        # {"verdict": "same", "candidate": 2, "quote": "..."} with a short quote
PROBE_MAX_USD = 0.01


class PassageVerdict(BaseModel):
    """The model's answer."""

    verdict: Literal["same", "different"]
    candidate: int | None = None
    quote: str | None = None


@dataclass(frozen=True)
class PassageAdjudicationParams:
    """Every knob of the settlement. ``max_output_tokens``: the answer is one short JSON object (a quote of 30-200 characters),
    so the cap is small; a hidden-reasoning model would spend more and ``llm_text`` doubles the budget on truncation (real spend
    can then exceed the bound: see the module doc). ``min_relatedness``: floor for ``partial_ratio`` (0-100) of quote vs sentence.
    ``max_sentence_chars``: the shown sentence is cut here. ``max_quote_chars``: cap of the verbatim probe slice."""

    max_output_tokens: int = 300
    min_quote_chars: int = MIN_QUOTE_CHARS
    min_relatedness: float = MIN_RELATEDNESS
    max_sentence_chars: int = 1500
    max_quote_chars: int = 1500

    def __post_init__(self) -> None:
        at_least("min_quote_chars", self.min_quote_chars, 1)
        for name in ("max_output_tokens", "max_sentence_chars", "max_quote_chars"):
            at_least(name, getattr(self, name), 100)
        in_range("min_relatedness", self.min_relatedness, 0.0, 100.0)


# --------------------------------------------------------------------------
# the prompt
# --------------------------------------------------------------------------

_PROMPT = """You compare two annual risk-factor disclosures of the same company, filed in different years.

SENTENCE (verbatim from one filing{cut}):
<<<
{sentence}
>>>

CANDIDATE SENTENCES (verbatim from the other filing):
{candidates}

Question: does any candidate state the SAME fact as the sentence? The same fact means the same subject, the same claim and the same \
qualifiers. A changed tense, number, date or wording is still the same fact. A candidate that only shares the topic, a place, a product \
or a few words is a DIFFERENT fact.
Answer with ONE JSON object and nothing else: {{"verdict": "same" | "different", "candidate": <candidate number> or null, "quote": <string> or null}}
- "same": one candidate states the same fact. Give its number as "candidate" and, as "quote", between 30 and 200 characters copied \
character for character from that candidate (no paraphrase, no ellipsis, no stitching).
- "different": no candidate states the same fact. Set "candidate" and "quote" to null.
Check every candidate before you answer "different": a paraphrase that reorders or condenses the sentence is still the same fact. \
Do not answer "same" for a candidate that merely shares the topic."""


def build_prompt(sentence: str, candidates: Sequence[BandCandidate], params: PassageAdjudicationParams = PassageAdjudicationParams()) -> str:
    """The settlement prompt (:data:`PROMPT_VERSION`): the sentence plus the numbered candidate sentences, all verbatim. It names
    no filing year and no side, so one answer serves a sentence whichever filing it comes from."""
    body = sentence.strip()
    numbered = "\n".join(f"[{k}] {c.text}" for k, c in enumerate(candidates, 1)) or "(no candidate sentence was found)"
    cut = f" - cut after the first {params.max_sentence_chars} characters" if len(body) > params.max_sentence_chars else ""
    return _PROMPT.format(sentence=body[:params.max_sentence_chars], cut=cut, candidates=numbered)


# --------------------------------------------------------------------------
# tasks, cost, run
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    """One model call. ``key`` identifies the answer in the checkpoint; ``band_key`` is the passage layer's key of the FIRST band
    sentence that needs it (identical sentences of one pair share the call)."""

    key: str
    pair_id: str
    band_key: str
    side: str
    sentence: str
    candidates: tuple[BandCandidate, ...]
    prompt: str


def task_key(text_hash: str, other_hash: str, model: str, prompt_version: str = PROMPT_VERSION) -> str:
    """The checkpoint key: sentence hash | hash of the other section | prompt version | model (a different model repays)."""
    return "|".join((text_hash, other_hash, prompt_version, model))


def plan_tasks(pair_id: str, bands: Sequence[BandSentence], model: str,
               params: PassageAdjudicationParams = PassageAdjudicationParams()) -> list[Task]:
    """One task per distinct (sentence, other section) of a pair's band sentences, in enumeration order."""
    tasks: dict[str, Task] = {}
    for b in bands:
        key = task_key(b.text_hash, b.other_hash, model)
        if key not in tasks:
            tasks[key] = Task(key, pair_id, b.key, b.side, b.text, b.candidates, build_prompt(b.text, b.candidates, params))
    return list(tasks.values())


def estimate_cost(tasks: Sequence[Task], cached_keys: Collection[str], model: str,
                  params: PassageAdjudicationParams = PassageAdjudicationParams()) -> adj.Estimate:
    """What a run would cost: ``worst_case_usd`` prices every uncached call at its prompt size (chars / 3) plus the full output
    cap, ``likely_usd`` at chars / 4 plus a typical verdict. ``n_items`` counts the distinct questions (band sentences)."""
    per_in, per_out, exact = adj.model_prices(model)
    todo = [t for t in tasks if t.key not in cached_keys]
    chars = sum(len(t.prompt) for t in todo)
    input_tokens = math.ceil(chars / _CHARS_PER_TOKEN_WORST)
    worst = input_tokens * per_in / 1e6 + len(todo) * params.max_output_tokens * per_out / 1e6
    likely = math.ceil(chars / 4) * per_in / 1e6 + len(todo) * _LIKELY_OUTPUT_TOKENS * per_out / 1e6
    return adj.Estimate(model, len(tasks), len(tasks) - len(todo), len(todo), input_tokens, len(todo) * params.max_output_tokens,
                        round(worst, 6), round(likely, 6), exact)


def call_cost_upper_bound(prompt: str, model: str, params: PassageAdjudicationParams = PassageAdjudicationParams()) -> float:
    per_in, per_out, _ = adj.model_prices(model)
    return math.ceil(len(prompt) / _CHARS_PER_TOKEN_WORST) * per_in / 1e6 + params.max_output_tokens * per_out / 1e6


def run_tasks(tasks: Sequence[Task], checkpoint: Checkpoint, llm: Callable[..., Any], *, model: str, max_usd: float,
              params: PassageAdjudicationParams = PassageAdjudicationParams()) -> int:
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
        answer = llm(task.prompt, PassageVerdict, model=model, max_tokens=params.max_output_tokens, thinking_off=True)
        spent += bound
        checkpoint.add({
            "key": task.key, "pair_id": task.pair_id, "band_key": task.band_key, "side": task.side, "model": model,
            "prompt_version": PROMPT_VERSION, "sentence": task.sentence,
            "candidates": [{"text": c.text, "start": c.start, "end": c.end} for c in task.candidates],
            "verdict": answer.verdict, "candidate": answer.candidate, "quote": answer.quote,
            "est_usd_upper_bound": round(bound, 6)})
    return len(todo)


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------

def relatedness(sentence: str, quote: str) -> float:
    """``partial_ratio`` (0-100) of the normalised quote against the normalised sentence (the gold's relatedness rule)."""
    return float(fuzz.partial_ratio(norm(sentence), norm(quote)))


def _candidate_of(record: Mapping[str, Any], other_text: str) -> tuple[dict | None, str | None]:
    """The candidate the model named, as stored in the record, if it is one of the shown candidates and a slice of the section."""
    number, shown = record.get("candidate"), record.get("candidates")
    if isinstance(number, bool) or not isinstance(number, int) or not isinstance(shown, list) or not 1 <= number <= len(shown):
        return None, "no valid candidate number for a 'same' verdict"
    cand = shown[number - 1]
    try:
        text, start, end = str(cand["text"]), int(cand["start"]), int(cand["end"])
    except (KeyError, TypeError, ValueError):
        return None, "the recorded candidate is malformed"
    if not 0 <= start < end <= len(other_text) or other_text[start:start + len(text)] != text:
        return None, "the recorded candidate is not a verbatim slice of the other section"
    return {"text": text, "start": start, "end": end}, None


def effective_verdict(record: Mapping[str, Any], sentence: str, other_text: str,
                      params: PassageAdjudicationParams = PassageAdjudicationParams(), *,
                      index: SectionIndex | None = None) -> tuple[BandVerdict | None, str | None]:
    """``(verdict | None, reason | None)``: the model's stored answer after the code's rules.

    ``different`` passes through. ``same`` needs (in this order) a valid candidate number, a quote of at least ``min_quote_chars``
    normalised characters, contained in that candidate AND in the other section, and related to ``sentence`` (``min_relatedness``);
    the verdict's counterpart span is the candidate's own section range. Any failure returns ``(None, reason)`` = no verdict."""
    verdict = record.get("verdict")
    if verdict == "different":
        return BandVerdict("different"), None
    if verdict != "same":
        return None, f"unusable verdict {verdict!r}"
    cand, reason = _candidate_of(record, other_text)
    if cand is None:
        return None, reason
    quote = record.get("quote")
    if not isinstance(quote, str) or len(norm(quote)) < params.min_quote_chars:
        return None, "no usable quote"
    if norm(quote) not in norm(cand["text"]):
        return None, "quote is not a verbatim slice of the chosen candidate"
    if (index or SectionIndex(other_text)).probe(quote, 100.0, params.max_quote_chars, fuzzy=False) is None:
        return None, "quote is not a verbatim slice of the other section"
    score = relatedness(sentence, quote)
    if score < params.min_relatedness:
        return None, f"quote is not related to the sentence (partial_ratio {score:.0f} < {params.min_relatedness:.0f})"
    return BandVerdict("same", (cand["start"], cand["end"])), None


class Resolved(NamedTuple):
    """``verdicts``: band key -> verdict; ``rejected``: band key -> why a recorded answer gave no verdict; ``unanswered``: band keys
    with no recorded answer (for this model and prompt version)."""

    verdicts: dict[str, BandVerdict]
    rejected: dict[str, str]
    unanswered: tuple[str, ...]


def resolve_verdicts(bands: Sequence[BandSentence], records: Mapping[str, Mapping[str, Any]], model: str, *,
                     older_text: str, newer_text: str,
                     params: PassageAdjudicationParams = PassageAdjudicationParams()) -> Resolved:
    """Turn the checkpointed answers into verdicts for these band sentences (validation is re-run here, on every read).

    An older-side sentence is validated against the NEWER section and a newer-side sentence against the OLDER one."""
    verdicts: dict[str, BandVerdict] = {}
    rejected: dict[str, str] = {}
    unanswered: list[str] = []
    indexes: dict[str, SectionIndex] = {}
    for band in bands:
        record = records.get(task_key(band.text_hash, band.other_hash, model))
        if record is None:
            unanswered.append(band.key)
            continue
        other = newer_text if band.side == "older" else older_text
        if record.get("verdict") == "same" and band.side not in indexes:      # built once per side, only when a quote must be checked
            indexes[band.side] = SectionIndex(other)
        verdict, reason = effective_verdict(record, band.text, other, params, index=indexes.get(band.side))
        if verdict is None:
            rejected[band.key] = reason or "no verdict"
        else:
            verdicts[band.key] = verdict
    return Resolved(verdicts, rejected, tuple(unanswered))


# --------------------------------------------------------------------------
# the live probe: two fixed toy questions, run ONCE by hand before the first paid run
# --------------------------------------------------------------------------

class ProbeReply(NamedTuple):
    text: str
    finish_reason: str | None
    usage: dict | None


@dataclass(frozen=True)
class ProbeCase:
    name: str
    expect: str                       # the verdict a correct model gives
    sentence: str
    section: str
    candidates: tuple[BandCandidate, ...]
    prompt: str


@dataclass(frozen=True)
class ProbeResult:
    """What one probe call showed. ``effective``: ``same`` (accepted by the code rules), ``different``, ``rejected`` (a ``same`` the
    rules refused, see ``reason``) or ``unparsed`` (the reply was not the JSON object)."""

    name: str
    expect: str
    prompt_chars: int
    raw: str
    finish_reason: str | None
    usage: dict | None
    parsed: PassageVerdict | None
    parse_error: str | None
    effective: str
    reason: str | None
    relatedness: float | None
    as_expected: bool


_PROBE_CASES = (
    ("paraphrase", "same",
     "Our wafers are manufactured by a single foundry located in Taiwan, and a disruption at that foundry would delay our shipments to customers.",
     ("We lease office space for our headquarters and several regional design centers under long term operating leases.",
      "Our credit facility contains covenants that restrict our ability to pay dividends or to repurchase our shares.",
      "We depend on one foundry in Taiwan to manufacture our wafers, and any disruption at that foundry would delay shipments to our customers.",
      "Changes in accounting standards could affect the way we report revenue and could reduce our reported earnings.",
      "Our common stock price has been volatile and may continue to be volatile for reasons unrelated to our operations.")),
    ("lookalike", "different",
     "Following these 2022 export controls, we transitioned some operations, including certain testing, validation, and supply and "
     "distribution operations out of China and Hong Kong.",
     ("We lease warehouse space in Hong Kong to store finished products before shipment to customers in Asia.",
      "Export controls may disrupt our supply and distribution chain for a substantial portion of our products.",
      "We are subject to export controls that restrict sales of certain advanced products to customers in China.",
      "Our common stock price has been volatile and may continue to be volatile for reasons unrelated to our operations.",
      "Changes in accounting standards could affect the way we report revenue and could reduce our reported earnings.")),
)


def probe_prompts(params: PassageAdjudicationParams = PassageAdjudicationParams()) -> list[ProbeCase]:
    """The two fixed toy cases and their exact prompts (no model is involved)."""
    cases = []
    for name, expect, sentence, texts in _PROBE_CASES:
        section, candidates = "", []
        for text in texts:
            start = len(section) + (1 if section else 0)
            section += (" " if section else "") + text
            candidates.append(BandCandidate(text, start, start + len(text), lex_exact(word_tokens(sentence), word_tokens(text)),
                                            float(fuzz.partial_ratio(sentence, text))))
        cases.append(ProbeCase(name, expect, sentence, section, tuple(candidates), build_prompt(sentence, candidates, params)))
    return cases


def estimate_probe(model: str, params: PassageAdjudicationParams = PassageAdjudicationParams()) -> adj.Estimate:
    """The worst-case cost of the two probe calls."""
    tasks = [Task(str(i), "probe", "probe", "older", c.sentence, c.candidates, c.prompt) for i, c in enumerate(probe_prompts(params))]
    return estimate_cost(tasks, set(), model, params)


def _usage_dict(usage: Any) -> dict | None:
    if usage is None:
        return None
    for attr in ("model_dump", "dict"):
        if hasattr(usage, attr):
            try:
                return json.loads(json.dumps(getattr(usage, attr)(), default=str))
            except (TypeError, ValueError):
                break
    return {"repr": repr(usage)}


def complete_once(model: str, prompt: str, max_tokens: int) -> ProbeReply:
    """ONE plain completion with the provider-aware parameters of ``llm_shape.completion_params`` (the call ``llm_text`` makes
    for a non-Anthropic model), no retry, returning the raw text, the finish reason and the usage. Not called by any test."""
    from litellm import completion

    from ..llm_shape import completion_params

    resp = completion(model=model, messages=[{"role": "user", "content": prompt}], num_retries=0,
                      **completion_params(model, max_tokens))
    choice = resp.choices[0]
    return ProbeReply(choice.message.content or "", choice.finish_reason, _usage_dict(getattr(resp, "usage", None)))


def _parse(reply_text: str) -> tuple[PassageVerdict | None, str | None]:
    try:
        return PassageVerdict.model_validate_json(adj._json_object(reply_text.strip())), None
    except ValidationError as err:
        return None, str(err)[:300]


def probe(model: str, *, complete: Callable[[str, str, int], ProbeReply] | None = None, max_usd: float = PROBE_MAX_USD,
          params: PassageAdjudicationParams = PassageAdjudicationParams()) -> list[ProbeResult]:
    """Ask the two fixed toy questions (a paraphrase and an unrelated lookalike) ONCE each and report what came back.

    Refuses (``BudgetExceeded``) when the worst case of the two calls is above ``max_usd`` (default one cent). A reply that
    is not the JSON object is reported as ``unparsed``, not raised, so the raw text is always shown."""
    worst = estimate_probe(model, params).worst_case_usd
    if worst > max_usd:
        raise BudgetExceeded(f"the probe's worst case ${worst:.4f} exceeds ${max_usd:.4f}")
    complete = complete or complete_once
    results = []
    for case in probe_prompts(params):
        reply = complete(model, case.prompt, params.max_output_tokens)
        parsed, error = _parse(reply.text)
        effective, reason, score = "unparsed", None, None
        if parsed is not None and parsed.verdict == "different":
            effective = "different"
        elif parsed is not None:
            record = {"verdict": "same", "candidate": parsed.candidate, "quote": parsed.quote,
                      "candidates": [{"text": c.text, "start": c.start, "end": c.end} for c in case.candidates]}
            verdict, reason = effective_verdict(record, case.sentence, case.section, params)
            effective = "same" if verdict is not None else "rejected"
            score = relatedness(case.sentence, parsed.quote) if isinstance(parsed.quote, str) else None
        results.append(ProbeResult(case.name, case.expect, len(case.prompt), reply.text, reply.finish_reason, reply.usage, parsed,
                                   error, effective, reason, score, effective == case.expect))
    return results


def format_probe(results: Sequence[ProbeResult]) -> str:
    """The printed report of :func:`probe`: per call the raw reply, the parsed verdict and the rules' outcome."""
    lines = []
    for k, r in enumerate(results, 1):
        lines += [f"--- probe {k}/{len(results)}: {r.name} (a correct model answers {r.expect!r}); prompt {r.prompt_chars} chars",
                  f"raw reply: {r.raw!r}",
                  f"finish_reason: {r.finish_reason}   usage: {json.dumps(r.usage) if r.usage is not None else 'n/a'}",
                  f"parsed: {r.parsed!r}" if r.parsed is not None else f"parsed: NOT PARSED ({r.parse_error})"]
        if r.effective == "same":
            lines.append(f"code rules: ACCEPTED same (quote relatedness {r.relatedness:.0f} >= "
                         f"{PassageAdjudicationParams().min_relatedness:.0f})")
        elif r.effective == "different":
            lines.append("code rules: different needs no quote: passed through")
        elif r.effective == "rejected":
            lines.append(f"code rules: REJECTED, no verdict: {r.reason}")
        else:
            lines.append("code rules: not evaluated (the reply is not the JSON object)")
        lines.append(f"as expected: {'yes' if r.as_expected else 'NO'}")
    ok = sum(r.as_expected for r in results)
    lines.append(f"probe result: {ok}/{len(results)} as expected")
    return "\n".join(lines)
