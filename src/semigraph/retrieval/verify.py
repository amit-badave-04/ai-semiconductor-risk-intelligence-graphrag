"""Deterministic checks on a drafted answer: what must hold before a cheap model's text may be shown.

Used two ways: the service releases a cheap draft only when this returns no reasons (otherwise it escalates
to the stronger model), and the bake-off measures how often each candidate would trigger that escalation.
Pure and free — no model call, no database — and importable in the slim serving image.
"""

import re

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
# Bare comma-grouped integers as the METRICS block prints them: "215,938,000,000 USD".
_GROUPED_RE = re.compile(r"(?<![0-9A-Za-z:.,-])([0-9]{1,3}(?:,[0-9]{3})+)(?![0-9A-Za-z:-])")
_SCALE = {"trillion": 1e12, "tn": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
          "million": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
_MATCH_TOLERANCE = 0.005   # "$215.9 billion" is the context's 215,938,000,000


def money_values(text: str) -> list[float]:
    """Every dollar amount in ``text`` as an absolute value."""
    return [float(m.group(1).replace(",", "")) * _SCALE.get((m.group(2) or "").lower(), 1.0)
            for m in _MONEY_RE.finditer(text.replace(" ", " "))]


def _grounded(answer_values: list[float], context: str) -> bool:
    """True when the answer states at least one dollar figure and every one of them is in the context."""
    if not answer_values:
        return False
    known = money_values(context) + [float(m.group(1).replace(",", "")) for m in _GROUPED_RE.finditer(context)]
    return all(any(abs(v - k) <= _MATCH_TOLERANCE * k for k in known if k) for v in answer_values)


_PERCENT_RE = re.compile(r"\d(?:[\d,]*\.?\d*)\s?%")
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


def verify_answer(text: str, cited: set[str], valid_ids: set[str], finish_reason: str | None,
                  context: str | None = None) -> list[str]:
    """Reasons the draft must not be released as is (empty list = it may be).

    - ``empty``: nothing was produced.
    - ``truncated``: the model ran out of output budget mid-answer.
    - ``invalid_citation``: a cited id is not in the retrieved context (a fabricated source).
    - ``no_citation``: no source is cited and the answer is neither a clear refusal (:func:`refusal_shaped`)
      nor — when the retrieved ``context`` is given — a short statement whose every dollar figure appears in that
      context. (XBRL metrics have no chunk id to cite, so a correct "revenue was $215.9 billion" is uncited by
      design; it is accepted only because the number itself is verifiable against what was retrieved.)
      Everything else uncited is escalated: the safe direction, since it only costs a stronger-model call.
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
    return reasons
