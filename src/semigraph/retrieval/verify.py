"""Deterministic checks on a drafted answer: what must hold before a cheap model's text may be shown.

Used two ways: the service releases a cheap draft only when this returns no reasons (otherwise it escalates
to the stronger model), and the bake-off measures how often each candidate would trigger that escalation.
Pure and free — no model call, no database — and importable in the slim serving image.

M1b added two checks that apply to EVERY answer, cited or not (:func:`answer_checks`, which also feeds the
``checks`` object of the terminal ``done`` event, so answers that cannot escalate still report what they fail):

- numeric grounding: every dollar value must match a value in the retrieved context (0.5% tolerance) and every
  percentage must match a code-computed year-over-year line in the context or appear in a chunk the same answer
  cites. Grounding is by VALUE, not by claim: it cannot see a real value attached to the wrong fiscal year.
- pseudo-citations: bracketed text that is not a citation id (``[Reported Metrics]``, ``[id1; id2]``).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .ids import CITE_RE

# Wordings that mark a deliberate "the corpus cannot answer this" reply. Started as the notebook 14 pattern;
# widened after the model bake-off showed correct refusals worded "do not state ..." / "I can’t determine ..."
# (typographic apostrophe) failing it. ``A`` accepts straight and typographic apostrophes.
_A = "['’‘ʼ]"
REFUSAL_RE = re.compile(
    r"(does|do) not (contain|include|provide|state|mention|specify|show|report)|"
    rf"does{_A}?n{_A}?t (contain|include|provide|state|mention|specify)|"
    r"not available|no (information|data|filings)|"
    rf"(cannot|can{_A}?t|can not|unable to) (be )?(determin|answer|find)|"
    rf"is{_A}?n{_A}?t|is not in the (context|filings|corpus)|not an SEC filer", re.I)

# Dollar amounts: "$215.9 billion", "$60,922 million", "$60,922,000,000".
_MONEY_RE = re.compile(r"\$\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(trillion|billion|million|thousand|tn|bn|mn|[TBMK])?\b", re.I)
# Bare comma-grouped integers as the METRICS block prints them: "215,938,000,000 USD". A leading minus ("-18,756,000,000
# USD", a net loss) is consumed, so the value is harvested by its absolute value: "a net loss of $18.8 billion" is grounded.
_GROUPED_RE = re.compile(r"(?<![0-9A-Za-z:.,-])-?([0-9]{1,3}(?:,[0-9]{3})+)(?![0-9A-Za-z:-])")
# A scaled amount with no dollar sign ("R&D expense of 4,304 million", "a 5.2 billion charge"): known to the context, never
# required of an answer.
_SCALED_RE = re.compile(r"(?<![0-9A-Za-z.,$-])([0-9][0-9,]*(?:\.[0-9]+)?)\s*(trillion|billion|million|thousand)\b", re.I)
_SCALE = {"trillion": 1e12, "tn": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
          "million": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
_MATCH_TOLERANCE = 0.005   # "$215.9 billion" is the context's 215,938,000,000


def _money_matches(text: str) -> list[re.Match]:
    return list(_MONEY_RE.finditer(text.replace(" ", " ")))


def _money_value(match: re.Match) -> float:
    return float(match.group(1).replace(",", "")) * _SCALE.get((match.group(2) or "").lower(), 1.0)


def money_values(text: str) -> list[float]:
    """Every dollar amount in ``text`` as an absolute value."""
    return [_money_value(m) for m in _money_matches(text)]


def _known_amounts(*texts: str) -> list[float]:
    """Every amount a text states: ``$``-amounts and the METRICS block's bare grouped integers (absolute values)."""
    known: list[float] = []
    for text in texts:
        known += money_values(text) + [float(m.group(1).replace(",", "")) for m in _GROUPED_RE.finditer(text)]
        known += [float(m.group(1).replace(",", "")) * _SCALE[m.group(2).lower()] for m in _SCALED_RE.finditer(text)]
    return known


def _amount_known(value: float, known: list[float]) -> bool:
    return any(abs(value - k) <= _MATCH_TOLERANCE * k for k in known if k)


def _grounded(answer_values: list[float], context: str) -> bool:
    """True when the answer states at least one dollar figure and every one of them is in the context."""
    if not answer_values:
        return False
    known = _known_amounts(context)
    return all(_amount_known(v, known) for v in answer_values)


_PERCENT_RE = re.compile(r"\d(?:[\d,]*\.?\d*)\s?%")
# A percentage as a number: "65.5%", "12 percent" ("percentage points" is not one).
_PERCENT_VALUE_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(?:%|percent\b)", re.I)
# The year-over-year lines the context computes in code: "computed: +65.5% vs fiscal year ended 2025-01-26".
_COMPUTED_PERCENT_RE = re.compile(r"computed:\s*[+-]?(\d[\d,]*(?:\.\d+)?)%")
_PERCENT_TOLERANCE_DECIMAL = 0.05   # "65.5%" must be 65.5 (a computed line prints one decimal)
_PERCENT_TOLERANCE_WHOLE = 0.5      # "about 66%" may round a computed 65.5; "64%" may not
_SOURCE_NOUN = r"(?:context|excerpts?|filings?|sources?|documents?|corpus|knowledge graph|information provided)"
_NEGATION = (rf"(?:(?:does|do) not (?:contain|include|provide|state|mention|specify|show|report)|"
             rf"does{_A}?n{_A}?t (?:contain|include|provide|state|mention|specify)|"
             rf"(?:cannot|can{_A}?t|unable to)\s+(?:be )?(?:determin|answer|find))")
# A refusal must OPEN the answer (first sentence, no clause break before it): either "<...source...> does not state ..."
# or a first-person "I cannot answer/determine ...". A disclaimer tacked on after a claim is not a refusal.
_CLAUSE = r"[^.;:!?\n]"   # no sentence or clause break: the refusal must be in the opening clause
_OPENING_REFUSAL_RE = re.compile(
    rf"^\W*(?:(?:based on|according to|from|given|in)\s+)?{_CLAUSE}{{0,80}}?\b{_SOURCE_NOUN}\b{_CLAUSE}{{0,60}}?{_NEGATION}|"
    rf"^\W*(?:I|we)\s+(?:cannot|can{_A}?t|am unable to|are unable to)\s+(?:be )?(?:determin|answer|find)", re.I)
_REFUSAL_MAX_CHARS = 1200
_METRIC_ANSWER_MAX_CHARS = 400


def refusal_shaped(text: str) -> bool:
    """A deliberate "the corpus cannot answer this": opens with the refusal, states no figures, and is short.

    Deliberately narrower than :data:`REFUSAL_RE` (which scores the benchmark's refusal questions): this one gates
    what reaches the client, and a stray "isn't" or a trailing "the filing does not specify" must not make an
    uncited claim look like a refusal."""
    t = text.strip()
    return (len(t) <= _REFUSAL_MAX_CHARS and not money_values(t) and not _PERCENT_RE.search(t)
            and bool(_OPENING_REFUSAL_RE.search(t)))


def _pure_metric_answer(text: str, context: str) -> bool:
    """A short, percentage-free statement whose every dollar figure is in the retrieved context."""
    t = text.strip()
    return len(t) <= _METRIC_ANSWER_MAX_CHARS and not _PERCENT_RE.search(t) and _grounded(money_values(t), context)


# --- M1b: numeric grounding and pseudo-citations, for every answer ---

def _percent_values(text: str) -> list[float]:
    return [float(m.group(1).replace(",", "")) for m in _PERCENT_VALUE_RE.finditer(text)]


def _percent_grounded(token: str, known: list[float]) -> bool:
    """``token`` (as written, e.g. ``"65.5"`` or ``"66"``) against known percentages, by absolute value."""
    value = float(token.replace(",", ""))
    tolerance = _PERCENT_TOLERANCE_DECIMAL if "." in token else _PERCENT_TOLERANCE_WHOLE
    return any(abs(value - k) <= tolerance + 1e-9 for k in known)


def _known_percentages(context: str, question: str, cited: set[str], sources: Mapping[str, str]) -> list[float]:
    """Percentages an answer may state: the computed lines, the question's own, and the chunks this answer cites."""
    known = [float(v.replace(",", "")) for v in _COMPUTED_PERCENT_RE.findall(context)]
    known += _percent_values(question)
    for citation in cited:
        known += _percent_values(sources.get(citation, ""))
    return known


def _unmatched_numbers(text: str, cited: set[str], context: str, sources: Mapping[str, str],
                       question: str) -> tuple[str, ...]:
    """Dollar values and percentages in ``text`` that nothing in the context supports, in order of appearance."""
    known_amounts = _known_amounts(context, question)
    known_percent = _known_percentages(context, question, cited, sources)
    found: list[tuple[int, str]] = []
    for m in _money_matches(text):
        if not _amount_known(_money_value(m), known_amounts):
            found.append((m.start(), m.group(0).strip()))
    for m in _PERCENT_VALUE_RE.finditer(text):
        if not _percent_grounded(m.group(1), known_percent):
            found.append((m.start(), m.group(0).strip()))
    unmatched: list[str] = []
    for _, shown in sorted(found):
        if shown not in unmatched:
            unmatched.append(shown)
    return tuple(unmatched)


# A bracket that is not a markdown link. Shorter or symbol-only content ("[1]", "[sic]", "[...]", "[T]he") is editorial
# noise, not a citation attempt.
_BRACKET_RE = re.compile(r"\[([^\[\]\n]+)\](?!\()")
_PSEUDO_MIN_CHARS = 4


def _pseudo_citations(text: str, cited: set[str], valid_ids: set[str]) -> tuple[str, ...]:
    """Bracketed text that resolves to nothing: prose labels (``[Reported Metrics]``) and composites (``[a; b]``).

    A well-formed id is never one (a fabricated one is ``invalid_citation``); neither is a known id."""
    found: list[str] = []
    for m in _BRACKET_RE.finditer(text):
        content = m.group(1).strip()
        if (len(content) < _PSEUDO_MIN_CHARS or not re.search(r"[A-Za-z0-9]", content)
                or content in cited or content in valid_ids or CITE_RE.fullmatch(f"[{content}]")):
            continue
        if content not in found:
            found.append(content)
    return tuple(found)


@dataclass(frozen=True)
class AnswerChecks:
    """What the deterministic checks found in one answer (reported for every answer, escalatable or not)."""

    citations_retrieved: bool          # every cited id was in the retrieved context
    numbers_grounded: bool             # every dollar value / percentage is supported (True when nothing to check against)
    unmatched_numbers: tuple[str, ...]
    pseudo_citations: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"citations_retrieved": self.citations_retrieved, "numbers_grounded": self.numbers_grounded,
                "unmatched_numbers": list(self.unmatched_numbers), "pseudo_citations": list(self.pseudo_citations)}


def answer_checks(text: str, cited: set[str], valid_ids: set[str], context: str | None = None, *,
                  sources: Mapping[str, str] | None = None, question: str | None = None) -> AnswerChecks:
    """The M1b checks for one answer. ``sources`` maps a citation id to the text behind it (a chunk's text, a risk
    summary), so a percentage stated in a chunk the answer cites is grounded; ``question`` lets an echoed figure
    ("did revenue exceed $100 billion?") pass. Without a ``context`` there is nothing to ground numbers against and
    none is claimed unmatched."""
    unmatched = () if context is None else _unmatched_numbers(text, cited, context, sources or {}, question or "")
    return AnswerChecks(citations_retrieved=not (cited - valid_ids), numbers_grounded=not unmatched,
                        unmatched_numbers=unmatched, pseudo_citations=_pseudo_citations(text, cited, valid_ids))


def verify_answer(text: str, cited: set[str], valid_ids: set[str], finish_reason: str | None,
                  context: str | None = None, *, sources: Mapping[str, str] | None = None,
                  question: str | None = None) -> list[str]:
    """Reasons the draft must not be released as is (empty list = it may be), in a fixed order.

    - ``empty``: nothing was produced.
    - ``truncated``: the model ran out of output budget mid-answer.
    - ``invalid_citation``: a cited id is not in the retrieved context (a fabricated source).
    - ``no_citation``: no source is cited and the answer is neither a clear refusal (:func:`refusal_shaped`)
      nor — when the retrieved ``context`` is given — a short statement whose every dollar figure appears in that
      context. (An XBRL figure is now citable as ``[xbrl:...]``, but a short uncited statement of a figure that is
      verifiably in the context is still accepted.) Everything else uncited is escalated: the safe direction,
      since it only costs a stronger-model call.
    - ``ungrounded_number`` (needs ``context``): a dollar value or percentage nothing in the context supports
      (see :func:`answer_checks`); applies to cited answers too.
    - ``pseudo_citation``: bracketed text that is not a citation id.
    """
    reasons = []
    if not text.strip():
        return ["empty"]
    if finish_reason == "length":
        reasons.append("truncated")
    if cited - valid_ids:
        reasons.append("invalid_citation")
    if not cited and not refusal_shaped(text) and (context is None or not _pure_metric_answer(text, context)):
        reasons.append("no_citation")
    checks = answer_checks(text, cited, valid_ids, context, sources=sources, question=question)
    if not checks.numbers_grounded:
        reasons.append("ungrounded_number")
    if checks.pseudo_citations:
        reasons.append("pseudo_citation")
    return reasons
