"""Cheap, safe LLM settlement of the passage layer's lexical BAND and BELOW zones (M1b). Calls are OFF by default; cached answers
are replayed.

The passage layer (``graph/passages.py``) cannot tell, from word overlap alone, a real paraphrase from a lookalike between
``reword_min`` and ``reword_confident`` (Nvidia's "we transitioned some operations ... out of China and Hong Kong" matches an
unrelated Hong Kong warehousing sentence at 0.367), and a sentence with no counterpart at all ("confidently removed") may still be
paraphrased somewhere else in the other section. Every such sentence (zone ``band``; with ``adjudicate_below_band`` also zone
``below``) gets ONE question for a cheap model, and the answer changes the passage layer ONLY through rules enforced here, in code
(mirroring ``graph/adjudicate.py``, the item-level version):

* the prompt holds the sentence and up to ``max_candidates`` (8) CANDIDATE SENTENCES of the other filing's section, each a verbatim
  sentence of it: the lexical best (word similarity), the ``rapidfuzz.fuzz.partial_ratio`` best, then the sentences whose EMBEDDING
  is closest to the sentence's (``graph/sentence_embed``: cosine over ALL sentences of the other section; the model only ever saw
  the lexical top 5 before, so a paraphrase sharing few words was never among its candidates). Union in that order, de-duplicated
  on normalised text, the embedding ranks refilling slots that de-duplication frees;
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
  hash of the other section | prompt version | model) and stores the sentence, its zone and the candidates the model saw. A
  re-run never repays, and validation is re-run on every read, so changing a floor never needs a new call. ``align-items`` replays
  the recorded answers of ONE prompt version per run: ``pas-v3`` (this prompt, semantic candidates; what ``--adjudicate-passages``
  buys and replays) or, without that flag, the legacy ``pas-v2`` answers (the same prompt without the last sentence of the
  ``same`` rule, lexical candidates only; replay only, never bought again);
* ``--max-usd`` is checked against the WORST case (every uncached call priced at its prompt size plus the full output cap) before
  any call, and a running total of per-call upper bounds stops a run that would pass it. The estimate is kept per zone. A dry run
  embeds nothing: it prices the candidates that are not known yet at the longest sentences of the other section (worst case) and
  at the mean sentence length (likely).

The model call is INJECTED (``llm_json``-compatible). The default is ``adjudicate.default_llm_json`` (Anthropic-shaped models via
``semigraph.llm.llm_json``; every other provider through the provider-aware ``llm_text``, which sends ``max_completion_tokens``
to GPT-6 and validates the JSON with correction turns). The default model ``openai/gpt-6-luna`` runs with
``reasoning_effort="none"`` (a live probe on ``pas-v2`` showed reasoning made it stricter and ate the token budget); the ``pas-v3``
wording was NOT probed live while building: run ``semigraph align-items --probe-adjudicator`` first (two calls, well under a cent;
it prints the raw reply, the parsed verdict and whether the code rules accepted it).
"""

import json
import logging
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ValidationError
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from . import adjudicate as adj
from .adjudicate import BudgetExceeded, Checkpoint, default_llm_json
from .align_text import SectionIndex, at_least, in_range, lex_exact, norm, split_sentences, word_tokens
from .passages import BandCandidate, BandSentence, BandVerdict
from .sentence_embed import Neighbour

__all__ = ["BudgetExceeded", "Checkpoint", "default_llm_json"]     # re-exported: one budget error and one call dispatch

logger = logging.getLogger("semigraph.graph.passage_adjudicate")

# v3: semantic candidates (up to 8) and a hint on which stretch to quote. v2 (legacy, replay only): lexical candidates, model called with
# reasoning_effort="none". v1: a reasoning Luna.
PROMPT_VERSION = "pas-v3"
LEGACY_PROMPT_VERSION = "pas-v2"
ZONES = ("band", "below")         # the order in which a budget-limited run buys them
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
    ``max_sentence_chars``: the shown sentence is cut here. ``max_quote_chars``: cap of the verbatim probe slice.
    ``max_candidates``: the most candidates shown per sentence (lexical best, partial best, then the embedding ranks)."""

    max_output_tokens: int = 600      # Luna reasons a little before answering (39-96 reasoning tokens in the live probe)
    min_quote_chars: int = MIN_QUOTE_CHARS
    min_relatedness: float = MIN_RELATEDNESS
    max_sentence_chars: int = 1500
    max_quote_chars: int = 1500
    max_candidates: int = 8

    def __post_init__(self) -> None:
        at_least("min_quote_chars", self.min_quote_chars, 1)
        at_least("max_candidates", self.max_candidates, 2)
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
character for character from that candidate (no paraphrase, no ellipsis, no stitching), taking the stretch of the candidate whose \
wording is closest to the sentence.
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
# candidates: lexical (from the passage layer) + semantic (embedding neighbours)
# --------------------------------------------------------------------------

def combine_candidates(lexical: Sequence[BandCandidate], semantic: Sequence[BandCandidate],
                       params: PassageAdjudicationParams = PassageAdjudicationParams()) -> tuple[BandCandidate, ...]:
    """The candidates shown to the model: the lexical best (word similarity), the ``partial_ratio`` best, then ``semantic`` in rank
    order; de-duplicated on normalised text (a heading can occur twice in a section) and capped at ``max_candidates``, so the
    embedding ranks refill any slot that de-duplication frees. ``lexical`` is the passage layer's candidate list (both bests are in
    it); ties between equal scores go to the earlier sentence. Deterministic."""
    best_words = max(lexical, key=lambda c: (c.lex_sim, -c.start), default=None)
    best_partial = max(lexical, key=lambda c: (c.partial, -c.start), default=None)
    chosen: list[BandCandidate] = []
    seen: set[str] = set()
    for cand in (best_words, best_partial, *semantic):
        if cand is None or norm(cand.text) in seen:
            continue
        seen.add(norm(cand.text))
        chosen.append(cand)
        if len(chosen) == params.max_candidates:
            break
    return tuple(chosen)


def _as_candidate(sentence: str, other_text: str, start: int, end: int, max_chars: int, cosine: float | None) -> BandCandidate:
    text = other_text[start:end]
    return BandCandidate(text[:max_chars], start, end, lex_exact(word_tokens(sentence), word_tokens(text)),
                         float(fuzz.partial_ratio(sentence, text, processor=default_process)), cosine)


def with_semantic_candidates(bands: Sequence[BandSentence], neighbours_of: Callable[[str, int, int, str, int], Sequence[Neighbour]],
                             older_text: str, newer_text: str, params: PassageAdjudicationParams = PassageAdjudicationParams(),
                             *, max_chars: int = 1200) -> list[BandSentence]:
    """``bands`` (with their lexical candidates) whose candidate lists are the union of the lexical best, the ``partial_ratio``
    best and the embedding neighbours (:func:`combine_candidates`). ``neighbours_of(side, start, end, text, count)`` is
    ``sentence_embed.PairNeighbours.neighbours``; a listed candidate is cut to ``max_chars`` (its span stays the whole sentence)."""
    out = []
    for band in bands:
        other_text = newer_text if band.side == "older" else older_text
        found = neighbours_of(band.side, band.start, band.end, band.text, 2 * params.max_candidates)
        semantic = [_as_candidate(band.text, other_text, n.start, n.end, max_chars, n.cosine) for n in found]
        out.append(replace(band, candidates=combine_candidates(band.candidates, semantic, params)))
    return out


def with_proxy_candidates(bands: Sequence[BandSentence], older_text: str, newer_text: str,
                          params: PassageAdjudicationParams = PassageAdjudicationParams(), *, min_chars: int = 40,
                          max_chars: int = 1200) -> tuple[list[BandSentence], dict[str, int]]:
    """Stand-ins for the semantic candidates of a dry run, which embeds nothing: after the lexical best and the ``partial_ratio``
    best, the LONGEST eligible sentences of the other section (cut at ``max_chars``) until there are ``max_candidates``, a true
    upper bound on the prompt size. Also returns, per band key, how many characters a typical prompt is shorter by (the proxies
    replaced by sentences of the mean length), for the likely estimate."""
    per_side = {}
    for side, text in (("older", older_text), ("newer", newer_text)):
        spans = [(a, b) for a, b in split_sentences(text) if b - a >= min_chars]
        spans.sort(key=lambda s: (-min(s[1] - s[0], max_chars), s[0]))
        cut = [min(b - a, max_chars) for a, b in spans]
        per_side[side] = (spans, sum(cut) / len(cut) if cut else 0.0)
    out, saving = [], {}
    for band in bands:
        other_text = newer_text if band.side == "older" else older_text
        spans, mean = per_side["newer" if band.side == "older" else "older"]
        kept = list(combine_candidates(band.candidates, (), params))
        seen = {norm(c.text) for c in kept}
        filler = []
        for a, b in spans:
            if len(kept) + len(filler) >= params.max_candidates:
                break
            text = other_text[a:a + min(b - a, max_chars)]
            if norm(text) not in seen:
                seen.add(norm(text))
                filler.append(BandCandidate(text, a, b, 0.0, 0.0))
        out.append(replace(band, candidates=tuple(kept) + tuple(filler)))
        saving[band.key] = max(0, sum(len(c.text) for c in filler) - round(mean * len(filler)))
    return out, saving


# --------------------------------------------------------------------------
# tasks, cost, run
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    """One model call. ``key`` identifies the answer in the checkpoint; ``band_key`` is the passage layer's key of the FIRST band
    sentence that needs it (identical sentences of one pair share the call); ``zone``: ``band`` or ``below``. ``likely_chars``: the
    prompt length a typical run would have when it differs from ``len(prompt)`` (a dry run's stand-in candidates)."""

    key: str
    pair_id: str
    band_key: str
    side: str
    sentence: str
    candidates: tuple[BandCandidate, ...]
    prompt: str
    zone: str = "band"
    likely_chars: int | None = None


def task_key(text_hash: str, other_hash: str, model: str, prompt_version: str = PROMPT_VERSION) -> str:
    """The checkpoint key: sentence hash | hash of the other section | prompt version | model (a different model repays)."""
    return "|".join((text_hash, other_hash, prompt_version, model))


def plan_tasks(pair_id: str, bands: Sequence[BandSentence], model: str,
               params: PassageAdjudicationParams = PassageAdjudicationParams(), *,
               likely_saving: Mapping[str, int] | None = None) -> list[Task]:
    """One task per distinct (sentence, other section) of a pair's band and below sentences, in enumeration order.
    ``likely_saving``: band key -> characters a typical prompt is shorter than the (stand-in) one, from :func:`with_proxy_candidates`."""
    tasks: dict[str, Task] = {}
    for b in bands:
        key = task_key(b.text_hash, b.other_hash, model)
        if key not in tasks:
            prompt = build_prompt(b.text, b.candidates, params)
            saving = (likely_saving or {}).get(b.key)
            tasks[key] = Task(key, pair_id, b.key, b.side, b.text, b.candidates, prompt, b.zone,
                              len(prompt) - saving if saving else None)
    return list(tasks.values())


def estimate_cost(tasks: Sequence[Task], cached_keys: Collection[str], model: str,
                  params: PassageAdjudicationParams = PassageAdjudicationParams()) -> adj.Estimate:
    """What a run would cost: ``worst_case_usd`` prices every uncached call at its prompt size (chars / 3) plus the full output
    cap, ``likely_usd`` at chars / 4 (``Task.likely_chars`` when set) plus a typical verdict. ``n_items`` counts the distinct
    questions (band and below sentences)."""
    per_in, per_out, exact = adj.model_prices(model)
    todo = [t for t in tasks if t.key not in cached_keys]
    chars = sum(len(t.prompt) for t in todo)
    likely_chars = sum(len(t.prompt) if t.likely_chars is None else t.likely_chars for t in todo)
    input_tokens = math.ceil(chars / _CHARS_PER_TOKEN_WORST)
    worst = input_tokens * per_in / 1e6 + len(todo) * params.max_output_tokens * per_out / 1e6
    likely = math.ceil(likely_chars / 4) * per_in / 1e6 + len(todo) * _LIKELY_OUTPUT_TOKENS * per_out / 1e6
    return adj.Estimate(model, len(tasks), len(tasks) - len(todo), len(todo), input_tokens, len(todo) * params.max_output_tokens,
                        round(worst, 6), round(likely, 6), exact)


def estimates_by_zone(tasks: Sequence[Task], cached_keys: Collection[str], model: str, zones: Sequence[str] = ZONES,
                      params: PassageAdjudicationParams = PassageAdjudicationParams()) -> dict[str, adj.Estimate]:
    """One estimate per requested zone (an empty zone is an estimate of zero calls)."""
    return {z: estimate_cost([t for t in tasks if t.zone == z], cached_keys, model, params) for z in zones}


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
        try:
            answer = llm(task.prompt, PassageVerdict, model=model, max_tokens=params.max_output_tokens, thinking_off=True)
        except RuntimeError as err:      # one bad call must not abort the run: no verdict = the safe direction; retried next run
            logger.warning("no verdict for a band sentence (%s): %s", task.pair_id, str(err)[:160])
            continue
        spent += bound
        checkpoint.add({
            "key": task.key, "pair_id": task.pair_id, "band_key": task.band_key, "side": task.side, "zone": task.zone,
            "model": model, "prompt_version": PROMPT_VERSION, "sentence": task.sentence,
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
                     older_text: str, newer_text: str, params: PassageAdjudicationParams = PassageAdjudicationParams(),
                     prompt_version: str = PROMPT_VERSION) -> Resolved:
    """Turn the checkpointed answers of ``prompt_version`` into verdicts for these band / below sentences (validation is re-run
    here, on every read).

    An older-side sentence is validated against the NEWER section and a newer-side sentence against the OLDER one."""
    verdicts: dict[str, BandVerdict] = {}
    rejected: dict[str, str] = {}
    unanswered: list[str] = []
    indexes: dict[str, SectionIndex] = {}
    for band in bands:
        record = records.get(task_key(band.text_hash, band.other_hash, model, prompt_version))
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
