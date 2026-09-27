"""Deterministic answer expectations for benchmark questions (one checker for the runner and the bake-off scorer).

A question's ``expect`` object holds one or more of these keys; ALL that are present must hold:

- ``value``: one amount; the answer must state some number within 0.5% of it (any scale: ``$215.9 billion``).
- ``values``: a list of amounts; every one must be stated.
- ``pct``: a percentage (signed, e.g. the code-computed year-over-year); the answer must state a number followed by
  ``%`` within 0.1 percentage points. A bare number never satisfies it.
- ``any_of``: case-insensitive substrings; at least one must occur.
- ``direction`` (``up`` / ``down``): the answer must use a word of that direction. The number parsers read magnitudes
  (``-267`` and ``267`` are the same to them), so the sign of a ``value`` / ``pct`` is checked here, not there. It only
  qualifies another key.
- ``not_company_disclosure``: a list of company names (``["NVIDIA", "Nvidia"]``). No clause that cites a Federal Register
  rule (``[fr:...]``) may attribute it to one of those companies (see ``misattributed_sentences``: precision-first, so
  the misattribution probes are graded by this guard AND the judge).
"""

import re
from collections.abc import Mapping, Sequence

from ..retrieval.ids import CHUNK_ID_PATTERN, FR_ID_PATTERN

VALUE_TOLERANCE = 0.005
PCT_TOLERANCE_POINTS = 0.1
_PCT_RE = re.compile(r"([-+]?\d[\d,]*(?:\.\d+)?)\s*%")
_KEYS = ("value", "values", "pct", "any_of", "not_company_disclosure")
_DIRECTION_RE = {
    "up": re.compile(r"\b(increas\w*|grew|grow\w*|rose|ris(?:e|es|en|ing)|up|higher|gain\w*|expand\w*)\b|\+\s*\$?\d", re.I),
    "down": re.compile(r"\b(decreas\w*|declin\w*|fell|fall\w*|drop\w*|down|lower|reduc\w*|contract\w*|loss(?:es)?|negative)\b"
                       r"|[-\u2212]\s*\$?\d", re.I),
}

NUM_PAT = re.compile(r"\$?([0-9][0-9,\.]*)\s*(billion|bn|b\b|million|mn|m\b|trillion)?", re.I)


def parse_numbers(text: str) -> list[float]:
    """Extract dollar/scale-suffixed numbers as absolute values
    (notebook 14 numeric-consistency check)."""
    out = []
    for m in NUM_PAT.finditer(text.replace(",", "")):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = (m.group(2) or "").lower()
        mult = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mn": 1e6,
                "m": 1e6, "trillion": 1e12}.get(unit, 1)
        out.append(v * mult)
    return out


def parse_percentages(text: str) -> list[float]:
    """Every ``<number>%`` in the text as a float (sign kept, thousands commas removed)."""
    return [float(m.group(1).replace(",", "")) for m in _PCT_RE.finditer(text)]


def _states_amount(answer: str, target: float) -> bool:
    """Magnitude match (``parse_numbers`` returns absolute values; the sign is the ``direction`` check)."""
    return any(abs(v - abs(target)) / abs(target) < VALUE_TOLERANCE for v in parse_numbers(answer))


# --- not_company_disclosure --------------------------------------------------------------------------------------

_FR_CITE_RE = re.compile(rf"\[{FR_ID_PATTERN}\]")
_CHUNK_CITE_RE = re.compile(rf"\[{CHUNK_ID_PATTERN}\]")
_ANY_CITE_RE = re.compile(r"\[[^\[\]\n]{1,80}\]")
_CITES_ONLY_RE = re.compile(r"^\s*(?:\[[^\[\]\n]{1,80}\]\s*)+\.?\s*$")
_SENTENCE_END_RE = re.compile(r"(?<!\.[A-Z]\.)(?<=[.!?])\s+")            # not after "U.S."
_CLAUSE_RE = re.compile(r";|\b(?:while|whereas|but|however|although|though)\b", re.I)
# what "the company said" looks like: a saying verb, or a document the company files (its 10-K, annual report, filing)
_TERMS = (r"(?:disclos\w*|report(?:s|ed|ing)?|stat(?:e|es|ed|ing)|acknowledg\w*|announc\w*|describ\w*|mention\w*|warn\w*|"
          r"highlight\w*|say|says|said|cite[sd]?|fil(?:e|es|ed|ing|ings)|10-K|10-Q|20-F|annual report)")
_TERM_RE = re.compile(rf"\b{_TERMS}\b", re.I)
_NEGATION_RE = re.compile(r"\b(?:not|no|never|neither|nor|without|none|nothing|cannot)\b|n['’]t\b", re.I)
_ATTRIBUTION_WINDOW_WORDS = 8     # how far after the company name a saying verb / filing noun still refers to it
_NEGATION_WINDOW_WORDS = 3        # a negation this close AFTER the company..term span cancels the attribution


def _hyphens_as_spaces(text: str) -> str:
    """``advanced-computing`` and ``advanced computing`` are the same phrase to a reader (and to ``any_of``)."""
    return re.sub(r"[\-‐-―]", " ", text)


def _company_pattern(companies: Sequence[str]) -> str:
    if not isinstance(companies, Sequence) or isinstance(companies, str) or not companies \
            or not all(isinstance(c, str) and c.strip() for c in companies):
        raise ValueError(f"not_company_disclosure needs a non-empty list of company names, got {companies!r}")
    return "|".join(re.escape(c) for c in sorted(companies, key=len, reverse=True))


def _sentences(text: str) -> list[str]:
    """Sentences of ``text`` (a line break also ends one); a fragment that is only citations belongs to the sentence before it."""
    out: list[str] = []
    for line in text.splitlines():
        for frag in _SENTENCE_END_RE.split(line.strip()):
            if not frag:
                continue
            if out and _CITES_ONLY_RE.match(frag):
                out[-1] += " " + frag
            else:
                out.append(frag)
    return out


# "the context does not include an Intel filing that ...": a limitation of the SOURCE whose negation reaches over the rest of
# the statement (further than the few-word window of ``_negated``), so it cancels an attribution that follows it, but only
# within the same statement: a comma, a semicolon or "and" starts a new one.
_SOURCE_LIMIT_RE = re.compile(
    r"\b(?:(?:does|do) not|doesn['’]t|cannot|can['’]t|unable to)\s+(?:be\s+)?"
    r"(?:contain|include|provide|state|mention|specify|show|report|say|identify|establish|determin\w*|find)\b", re.I)
_STATEMENT_START_RE = re.compile(r"[,;]|\band\b", re.I)


# "not as part of Nvidia's own filings", "not from Nvidia's 10-K": a negation and a preposition phrase right before the company.
_PREP_NEGATION_RE = re.compile(r"\b(?:not|never)\s+(?:as\s+part\s+of|part\s+of|from|in|among|one\s+of|by)\s*$", re.I)
# A past-tense saying verb right after the company presupposes the disclosure ("the exact date NVIDIA DISCLOSED the rule"); after a
# question word it is only hypothetical ("what Intel SAID about it"), which a source limitation may cancel.
_PAST_SAYING_RE = re.compile(r"\b(?:disclosed|reported|stated|said|announced|acknowledged|described|mentioned|warned|highlighted|cited|filed)\b", re.I)
_QUESTION_WORD_RE = re.compile(r"\b(?:what|whether|if|how|why)\s*$", re.I)


def _negated(clause: str, start: int, end: int) -> bool:
    left = clause[:start].split()[-_NEGATION_WINDOW_WORDS:]
    right = clause[end:].split()[:_NEGATION_WINDOW_WORDS]
    if _NEGATION_RE.search(" ".join(left) + " " + clause[start:end] + " " + " ".join(right)):
        return True
    head = clause[:start]
    if _PREP_NEGATION_RE.search(head):
        return True
    last_break = max((m.end() for m in _STATEMENT_START_RE.finditer(head)), default=0)
    if not _SOURCE_LIMIT_RE.search(head[last_break:]):
        return False
    presupposed = _PAST_SAYING_RE.search(clause[start:end]) and not _QUESTION_WORD_RE.search(head)
    return not presupposed


def _clause_attributes(clause: str, company: str) -> bool:
    """True when ``clause`` (citations removed) has the company as the source of a saying verb or filing noun, unnegated:
    ``<company> ... disclosed`` / ``<company>'s 10-K`` (within a few words) or ``disclosed by <company>`` / ``according to
    <company>``. A rule that "states that <company> is affected" (the company is the object) is not an attribution."""
    forward = re.compile(rf"\b(?:{company})\b(?:['’]s)?", re.I)
    for m in forward.finditer(clause):
        tail = clause[m.end():]
        words = tail.split()[:_ATTRIBUTION_WINDOW_WORDS]
        reach = len(" ".join(words))
        term = _TERM_RE.search(tail[:reach + 1])
        if term and not _negated(clause, m.start(), m.end() + term.end()):
            return True
    passive = re.compile(rf"(?:\b{_TERMS}\b\W+(?:\w+\W+){{0,3}}by\W+(?:the\W+)?|\baccording to\W+(?:the\W+)?)\b(?:{company})\b", re.I)
    return any(not _negated(clause, m.start(), m.end()) for m in passive.finditer(clause))


def misattributed_sentences(answer: str, companies: Sequence[str]) -> list[str]:
    """The sentences of ``answer`` that present a Federal Register rule as ``companies``' own disclosure.

    A clause counts only when it cites an ``[fr:...]`` id, its sentence cites NO filing passage (a sentence that also
    cites the company's own filing may truthfully say the filing discusses the rule's subject), a listed company is the
    source of a saying verb or filing noun in it, and no negation sits next to that phrase. The clauses of a sentence
    are split at ``;`` and contrast words (while, whereas, but, however, although, though), so "NVIDIA's 10-K covers
    export controls, while the BIS rule [fr:...] is separate" is not flagged. Known limits (left to the judge): an
    attribution with a chunk citation or with no citation, and an attribution in a clause that does not itself cite the rule."""
    company = _company_pattern(companies)
    flagged = []
    for sentence in _sentences(answer):
        if _CHUNK_CITE_RE.search(sentence):
            continue
        for clause in _CLAUSE_RE.split(sentence):
            if _FR_CITE_RE.search(clause) and _clause_attributes(_ANY_CITE_RE.sub("", clause), company):
                flagged.append(sentence)
                break
    return flagged


def check_expectation(expect: Mapping, answer: str) -> bool:
    """True when the answer satisfies every expectation key in ``expect``; ValueError when it has none."""
    if not any(k in expect for k in _KEYS):
        raise ValueError(f"expect has none of {_KEYS}: {dict(expect)!r}")
    if "direction" in expect and expect["direction"] not in _DIRECTION_RE:
        raise ValueError(f"expect.direction must be 'up' or 'down', got {expect['direction']!r}")
    checks = []
    if "not_company_disclosure" in expect:
        checks.append(not misattributed_sentences(answer, expect["not_company_disclosure"]))
    if "value" in expect:
        checks.append(_states_amount(answer, expect["value"]))
    if "values" in expect:
        checks.append(all(_states_amount(answer, v) for v in expect["values"]))
    if "pct" in expect:
        target = float(expect["pct"])
        checks.append(any(abs(abs(p) - abs(target)) <= PCT_TOLERANCE_POINTS + 1e-9 for p in parse_percentages(answer)))
    if "direction" in expect:
        checks.append(bool(_DIRECTION_RE[expect["direction"]].search(answer)))
    if "any_of" in expect:
        lowered = _hyphens_as_spaces(answer.lower())
        checks.append(any(_hyphens_as_spaces(s.lower()) in lowered for s in expect["any_of"]))
    return all(checks)
