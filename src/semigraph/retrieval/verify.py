"""Deterministic checks on a drafted answer: what must hold before a cheap model's text may be shown.

Used two ways: the service releases a cheap draft only when this returns no reasons (otherwise it escalates
to the stronger model), and the bake-off measures how often each candidate would trigger that escalation.
Pure and free — no model call, no database — and importable in the slim serving image.

The checks apply to EVERY answer, cited or not (:func:`answer_checks`, which also feeds the ``checks`` object of the
terminal ``done`` event, so answers that cannot escalate still report what they fail):

- numeric grounding: every amount must match an amount IN THE SAME CURRENCY in the retrieved context (0.5% tolerance;
  ``$``/``USD``, ``€``/``EUR``, ``NT$``/``TWD`` before or after the number) and every percentage a code-computed
  year-over-year line (with its direction) or a chunk the same answer cites. A figure that only the QUESTION states is
  never grounded (``echoed_numbers``): repeating the asker's number would confirm it. Grounding is by VALUE, not by
  claim: it cannot see a real value attached to the wrong fiscal year.
- pseudo-citations: bracketed text that is not a citation id (``[Reported Metrics]``, ``[id1; id2]``).
- uncited answers: no citation and not a refusal (``has_citation`` / ``is_refusal``).
- removal claims: a sentence that says a risk disclosure was dropped / removed / "no longer appears" (or that its wording
  "was not found in the newer filing", the hedge the answer prompt teaches for a removed passage) may cite only ids
  listed under the temporal block's REMOVED lists (:func:`context_layout.removal_supported_ids`: the items that "no longer
  appear as a separate risk factor" and the passages whose wording "was not found in the newer filing"). The ids of the "Not
  matched" list (older items the text check could not settle) and of the NEW / ADDED lists are citable but never support a
  removal, and the check is deliberately not softened for hedged wording ("may have been removed"): the answer prompt tells
  the model to say those items "could not be verified" instead. "Not found in the OLDER filing" (the added-passage hedge) is
  not a removal claim. Judged clause by clause: the statement and its ids must share one clause (no semicolon between them).
  What the answer prompt itself tells the model to write is not a claim either: a NEGATED or "none found" statement ("not
  evidence that any risk factor was dropped", "the text check found no risk factor that no longer appears", "did not verify any
  risk factor as removed", "the check cannot tell whether it was removed") says the disclosure survives, and a list LABEL copied
  from the context ("### No longer appears as a separate risk factor", the quoted heading) names a list, not a removal: the items
  written UNDER a removed-list label are judged by their ids, exactly like a claim, and a label followed only by "none found" in
  the model's own words (nothing cited, nothing excepted) names an empty list. A statement and its ids share one SENTENCE: a
  comma-connective ("..., and parts of its content may be covered [id]") does not strand them, and a clause that only restates
  the claim ("..., which means the text check found no matching text [id]") shares its ids. Negation reaches at most eight words
  ahead of the verb (see the block comment above ``_NEGATORS`` for the exceptions, for what ends it: an "and", a comma that opens a
  new statement or an aside, a contrast or presupposing word, "did not add new risks and removed X" claims, and for the removals a
  negation does not reach: "did not comment on the removal of X", "the risk factor it removed").

The removal-claim detector lives in :mod:`semigraph.retrieval.removal_claims` (split out on 2026-09-27; verify.py keeps the
numeric, citation and refusal checks and the checks object).

:func:`failed_check_names` is the ONE predicate for "this answer failed a check": the service (cache, log), example
seeding and the page all read it, so they cannot disagree.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .ids import CITE_RE
from .removal_claims import unsupported_removal_claims
from .textutil import A as _A
from .textutil import plain_spaces as _plain_spaces

# Wordings that mark a deliberate "the corpus cannot answer this" reply. Started as the notebook 14 pattern;
# widened after the model bake-off showed correct refusals worded "do not state ..." / "I can’t determine ..."
# (typographic apostrophe) failing it. ``A`` accepts straight and typographic apostrophes.
_REFUSAL_VERBS = "contain|include|provide|give|state|mention|specify|show|report|establish|identify|list|say|describe|have"
REFUSAL_RE = re.compile(
    rf"(does|do) not (?:\w+ )?({_REFUSAL_VERBS})|"
    rf"do(?:es)?{_A}?n{_A}?t (?:\w+ )?({_REFUSAL_VERBS})|"
    r"(contains?|includes?|provides?) no\b|"
    r"not available|no (information|data|filings)|"
    rf"(cannot|can{_A}?t|can not|unable to) (be )?(determin|answer|find)|"
    rf"is{_A}?n{_A}?t|is not in the (context|filings|corpus)|not an SEC filer", re.I)

# --- amounts: a value AND a currency ---------------------------------------------------------------------------------
# A prefix currency (``$215.9 billion``, ``NT$3.81 trillion``, ``EUR 4.3 billion``) or a suffix one (``215,938,000,000
# USD``, the METRICS block's own form; ``4.3 billion euros``). ``NT$`` and ``US$`` are matched before a bare ``$``; any
# other ``XX$`` (HK$, C$, A$) is a currency nothing in the context is ever tagged with.
_NUM = r"[0-9][0-9,]*(?:\.[0-9]+)?"
_PREFIX_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<cur>NT\$|US\$|[A-Z]{{1,2}}\$|\$|€|USD|EUR|TWD)\s?(?P<num>{_NUM})"
    r"(?:\s*(?P<scale>(?i:trillion|billion|million|thousand|tn|bn|mn|[TBMK])))?\b")
_SUFFIX_RE = re.compile(
    rf"(?<![A-Za-z0-9$€.,])(?P<num>{_NUM})(?:\s*(?P<scale>(?i:trillion|billion|million|thousand|tn|bn|mn)))?"
    r"\s*(?P<cur>USD|EUR|TWD|(?i:new taiwan dollars?|taiwan dollars?|nt dollars?|u\.s\. dollars?|us dollars?|dollars?|euros?))\b")
# Bare comma-grouped integers with NO unit ("215,938" in a table): known to the context, currency-agnostic. A leading minus
# ("-18,756,000,000 USD", a net loss) is consumed, so the value is harvested by its absolute value.
_GROUPED_RE = re.compile(r"(?<![0-9A-Za-z:.,-])-?([0-9]{1,3}(?:,[0-9]{3})+)(?![0-9A-Za-z:-])")
# A scaled amount with no currency word ("R&D expense of 4,304 million"): known to the context (any currency), never
# required of an answer.
_SCALED_RE = re.compile(r"(?<![0-9A-Za-z.,$-])([0-9][0-9,]*(?:\.[0-9]+)?)\s*(trillion|billion|million|thousand)\b", re.I)
_SCALE = {"trillion": 1e12, "tn": 1e12, "t": 1e12, "billion": 1e9, "bn": 1e9, "b": 1e9,
          "million": 1e6, "mn": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}
_MATCH_TOLERANCE = 0.005   # "$215.9 billion" is the context's 215,938,000,000


@dataclass(frozen=True)
class _Amount:
    value: float
    currency: str | None      # "USD" / "EUR" / "TWD" / "?XX$" (unknown, matches nothing); None = untagged (context only)
    start: int
    end: int
    shown: str




def _currency(token: str) -> str:
    t = token.replace(".", "").lower()
    if t in ("nt$", "twd") or "taiwan" in t or t.startswith("nt dollar"):
        return "TWD"
    if t in ("$", "us$", "usd") or t.startswith("us dollar"):
        return "USD"
    if t in ("€", "eur") or t.startswith("euro"):
        return "EUR"
    if t.startswith("dollar"):
        return "USD"
    return "?" + token


def _value(number: str, scale: str | None) -> float:
    return float(number.replace(",", "")) * _SCALE.get((scale or "").lower(), 1.0)


def _amounts(text: str) -> list[_Amount]:
    """Every currency-tagged amount in ``text``, in order of appearance."""
    text = _plain_spaces(text)
    found = [_Amount(_value(m["num"], m["scale"]), _currency(m["cur"]), m.start(), m.end(), m.group(0).strip())
             for m in _PREFIX_RE.finditer(text)]
    taken = [(a.start, a.end) for a in found]
    for m in _SUFFIX_RE.finditer(text):
        if not any(m.start() < e and s < m.end() for s, e in taken):
            found.append(_Amount(_value(m["num"], m["scale"]), _currency(m["cur"]), m.start(), m.end(),
                                 m.group(0).strip()))
    return sorted(found, key=lambda a: a.start)


def money_values(text: str) -> list[float]:
    """Every amount in ``text`` (any currency) as an absolute value."""
    return [a.value for a in _amounts(text)]


def _known_amounts(text: str) -> list[tuple[float, str | None]]:
    """(value, currency) of every amount a text states: the tagged ones, and the untagged grouped / scaled numbers
    (currency ``None``, absolute values). An untagged number lying INSIDE a tagged amount is that amount, not a second one:
    otherwise a TWD line would also count as a currency-agnostic value and ground a dollar figure."""
    tagged = _amounts(text)
    spans = [(a.start, a.end) for a in tagged]
    known: list[tuple[float, str | None]] = [(a.value, a.currency) for a in tagged]
    text = _plain_spaces(text)
    for m in _GROUPED_RE.finditer(text):
        if not any(m.start() < e and s < m.end() for s, e in spans):
            known.append((float(m.group(1).replace(",", "")), None))
    for m in _SCALED_RE.finditer(text):
        if not any(m.start() < e and s < m.end() for s, e in spans):
            known.append((float(m.group(1).replace(",", "")) * _SCALE[m.group(2).lower()], None))
    return known


def _amount_known(amount: _Amount, known: list[tuple[float, str | None]]) -> bool:
    return any(k and abs(amount.value - k) <= _MATCH_TOLERANCE * k and currency in (None, amount.currency)
               for k, currency in known)


# --- percentages: magnitude, and direction when the answer states one -------------------------------------------------
_PERCENT_RE = re.compile(r"\d(?:[\d,]*\.?\d*)\s?%")
# A percentage as a number: "65.5%", "12 percent" ("percentage points" is not one).
_PERCENT_VALUE_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(?:%|percent\b)", re.I)
# The year-over-year lines the context computes in code, WITH their sign: "computed: +65.5% vs fiscal year ended ...".
_COMPUTED_PERCENT_RE = re.compile(r"computed:\s*([+-]?\d[\d,]*(?:\.\d+)?)%")
_PERCENT_TOLERANCE_DECIMAL = 0.05   # "65.5%" must be 65.5 (a computed line prints one decimal)
_PERCENT_TOLERANCE_WHOLE = 0.5      # "about 66%" may round a computed 65.5; "64%" may not
_DIRECTION_WINDOW = 40              # a direction word this close before the figure states its direction
_AFTER_WINDOW = 14                  # ... or a direction NOUN right after it ("a 65.5% decline")
_UP_WORDS = (r"rose|rise|rises|rising|grew|grow|grows|growing|growth|increas\w*|jump\w*|surg\w*|climb\w*|expand\w*|"
             r"gain\w*|higher|improv\w*|up(?!\s+to\b)")
_DOWN_WORDS = (r"fell|fall|falls|falling|declin\w*|decreas\w*|drop\w*|shrank|shrunk|shrink\w*|contract\w*|reduc\w*|"
               r"dip\w*|slump\w*|plung\w*|lower|down")
_UP_RE, _DOWN_RE = re.compile(rf"\b(?:{_UP_WORDS})\b", re.I), re.compile(rf"\b(?:{_DOWN_WORDS})\b", re.I)
_UP_NOUN_RE = re.compile(r"\b(?:increase|rise|growth|gain|improvement|jump|surge)\b", re.I)
_DOWN_NOUN_RE = re.compile(r"\b(?:decrease|decline|drop|fall|reduction|contraction|dip|decrease)\b", re.I)
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

# --- clauses: the unit a refusal, a metric answer and a removal claim are judged in ------------------------------------
# A sentence end (followed by a capital, digit or bullet: "U.S. revenue" does not split), a semicolon, a line break, or a
# comma that opens a new statement ("..., and Nvidia plans ..."). A citation after the full stop stays with its sentence.
_CLAUSE_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z0-9\-*(\"“])|\s*;\s*|\n+|,\s+(?=(?:and|but|while|whereas|which|although|though|yet|however|so)\b)")
# A statement that negates: the shapes a limitation takes ("the context does not ...", "no reported metrics ...", "nor ...").
_LIMITATION_RE = re.compile(rf"\b(?:no|not|cannot|unable|nothing|none|neither|nor|without|absent|lacks?)\b|n{_A}t\b", re.I)
# ... and a limitation is about the SOURCE ("the context does not ...", "the knowledge graph contains no ...") or continues
# the refusal ("it also has no data", "nor does it", "I cannot"). A negation about the WORLD ("however NVIDIA no longer has
# operations in Russia", "Nvidia does not sell to Huawei") is a claim of its own and is not a limitation.
_SOURCE_NOUN_RE = re.compile(rf"\b{_SOURCE_NOUN}\b", re.I)
_LIMITATION_LEAD_RE = re.compile(
    r"^\W*(?:(?:however|but|also|and|yet|still|so|although|though)\W+)*(?:it|they|this|that|these|those|nor|neither|there|I|we)\b",
    re.I)


def _clauses(text: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(text) if c and c.strip()]


def _is_limitation(clause: str) -> bool:
    """A clause that only states a limitation of the source: it negates AND speaks of the source or continues the refusal."""
    return bool(_LIMITATION_RE.search(clause)) and bool(_SOURCE_NOUN_RE.search(clause) or _LIMITATION_LEAD_RE.match(clause))


def refusal_shaped(text: str) -> bool:
    """A deliberate "the corpus cannot answer this": opens with the refusal, states no figures, is short, and every
    further clause only states a further limitation ("nor does it ..."), never a fact of its own.

    Deliberately narrower than :data:`REFUSAL_RE` (which scores the benchmark's refusal questions): this one gates
    what reaches the client, and a stray "isn't" or a trailing "the filing does not specify" must not make an
    uncited claim look like a refusal; neither does "The filings do not state the date; however NVIDIA exited Russia"."""
    t = text.strip()
    return (len(t) <= _REFUSAL_MAX_CHARS and not money_values(t) and not _PERCENT_RE.search(t)
            and bool(_OPENING_REFUSAL_RE.search(t)) and all(_is_limitation(c) for c in _clauses(t)))


# --- numeric grounding ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _Figures:
    unmatched: tuple[str, ...] = ()     # supported by nothing
    echoed: tuple[str, ...] = ()        # supported only by the question
    checked: int = 0                    # figures examined (amounts and percentages)


def _percent_tolerance(token: str) -> float:
    return (_PERCENT_TOLERANCE_DECIMAL if "." in token else _PERCENT_TOLERANCE_WHOLE) + 1e-9


def _plain_percentages(text: str) -> list[float]:
    """Percentages a text states, magnitudes only. The computed year-over-year values are NOT among them: they carry a
    sign, and a metric line cited as a source is where they would otherwise slip in unsigned."""
    stripped = _COMPUTED_PERCENT_RE.sub(" ", text)
    return [float(v.replace(",", "")) for v in (m.group(1) for m in _PERCENT_VALUE_RE.finditer(stripped))]


def _direction(text: str, match: re.Match) -> int | None:
    """+1 / -1 when the answer states the figure's direction (a sign, a direction word up to 40 characters before it, or
    a direction noun right after it), else None."""
    start = match.start(1)
    if start and text[start - 1] in "+-−" and (start == 1 or text[start - 2] in " \t\n(\"'“["):
        return 1 if text[start - 1] == "+" else -1
    before = text[max(0, start - _DIRECTION_WINDOW):start]
    up = [m.end() for m in _UP_RE.finditer(before)]
    down = [m.end() for m in _DOWN_RE.finditer(before)]
    if up or down:
        return 1 if max(up, default=-1) > max(down, default=-1) else -1
    after = text[match.end():match.end() + _AFTER_WINDOW]
    if _UP_NOUN_RE.search(after):
        return 1
    return -1 if _DOWN_NOUN_RE.search(after) else None


def _percent_status(text: str, match: re.Match, computed: list[float], cited: list[float], asked: list[float]) -> str:
    """``grounded`` / ``echoed`` / ``unmatched`` for one percentage of the answer."""
    token = match.group(1)
    value, tolerance = float(token.replace(",", "")), _percent_tolerance(token)
    near = [c for c in computed if abs(value - abs(c)) <= tolerance]
    direction = _direction(text, match)
    if any(abs(value - k) <= tolerance for k in cited):
        return "grounded"                     # a filing sentence: unsigned, so compared by magnitude
    if near and (direction is None or any((c > 0) == (direction > 0) for c in near if c)):
        return "grounded"
    if any(abs(value - k) <= tolerance for k in asked):
        return "echoed"
    return "unmatched"


def _amount_status(amount: _Amount, known: list[tuple[float, str | None]], asked: list[_Amount]) -> str:
    if _amount_known(amount, known):
        return "grounded"
    asked_known = [(a.value, a.currency) for a in asked]
    return "echoed" if _amount_known(amount, asked_known) else "unmatched"


_QUOTED_SPAN_RE = re.compile(r'["“”]([^"“”\n]{2,120})["“”]')


def _squash(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .,;:!?\"'“”‘’")


def _quoted_from_question(text: str, position: int, question: str) -> bool:
    """True when the figure at position sits inside a QUOTED phrase that the question itself contains verbatim: the answer is
    naming the thing the asker named ("a 'BIS 50% affiliates rule'"), not asserting the asker's figure. A figure quoted with other
    words, or stated outside a quote, is still an echo."""
    asked = _squash(question)
    return any(m.start() <= position < m.end() and _squash(m.group(1)) in asked for m in _QUOTED_SPAN_RE.finditer(text))


def _check_figures(text: str, cited: set[str], context: str, sources: Mapping[str, str], question: str) -> _Figures:
    """Every amount and percentage of ``text`` against the context; what only the question states is ``echoed``."""
    known, asked_amounts = _known_amounts(context), _amounts(question)
    computed = [float(v.replace(",", "")) for v in _COMPUTED_PERCENT_RE.findall(context)]
    cited_percent = [k for citation in cited for k in _plain_percentages(sources.get(citation, ""))]
    asked_percent = _plain_percentages(question) + [
        float(v.replace(",", "")) for v in _COMPUTED_PERCENT_RE.findall(question)]
    found: list[tuple[int, str, str]] = []
    def status_of(position: int, status: str) -> str:
        return "quoted" if status == "echoed" and _quoted_from_question(text, position, question) else status

    for a in _amounts(text):
        found.append((a.start, a.shown, status_of(a.start, _amount_status(a, known, asked_amounts))))
    for m in _PERCENT_VALUE_RE.finditer(text):
        found.append((m.start(), m.group(0).strip(),
                      status_of(m.start(), _percent_status(text, m, computed, cited_percent, asked_percent))))
    unmatched: list[str] = []
    echoed: list[str] = []
    for _, shown, status in sorted(found):
        bucket = {"unmatched": unmatched, "echoed": echoed}.get(status)
        if bucket is not None and shown not in bucket:
            bucket.append(shown)
    return _Figures(tuple(unmatched), tuple(echoed), len(found))


# --- pseudo-citations ------------------------------------------------------------------------------------------------

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


# --- the checks object -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AnswerChecks:
    """What the deterministic checks found in one answer (reported for every answer, escalatable or not).

    ``numbers_grounded`` is only meaningful when ``numbers_checked`` > 0 (an answer with no figure has nothing to ground
    and reports True); ``echoed_numbers`` are figures only the question states (never grounded); ``has_citation`` is
    a plain fact and ``is_refusal`` says a zero-citation answer is a deliberate refusal; ``removal_claims`` are the
    sentences that claim a removal the removed lists do not support."""

    citations_retrieved: bool          # every cited id was in the retrieved context
    numbers_grounded: bool             # every figure is supported by the context (and none only by the question)
    unmatched_numbers: tuple[str, ...]
    pseudo_citations: tuple[str, ...]
    echoed_numbers: tuple[str, ...] = ()
    numbers_checked: int = 0
    has_citation: bool = True
    is_refusal: bool = False
    removal_claims: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"citations_retrieved": self.citations_retrieved, "numbers_grounded": self.numbers_grounded,
                "numbers_checked": self.numbers_checked, "unmatched_numbers": list(self.unmatched_numbers),
                "echoed_numbers": list(self.echoed_numbers), "pseudo_citations": list(self.pseudo_citations),
                "has_citation": self.has_citation, "is_refusal": self.is_refusal,
                "unsupported_removal_claim": bool(self.removal_claims),
                "unsupported_removal_sentences": list(self.removal_claims)}


def failed_check_names(checks: object) -> list[str]:
    """The names of the checks a ``checks`` dict fails, in a fixed order ([] for clean checks, or for no checks at all).

    The single definition of "failed": ``routes`` (an answer that failed is not cached), example seeding and the page's
    ``checksPassed`` all read it. Keys a payload does not carry count as clean, so an older payload is never failed for
    a check it predates."""
    if not isinstance(checks, Mapping):
        return []
    names = []
    if checks.get("citations_retrieved") is False:
        names.append("citations_not_retrieved")
    if checks.get("numbers_grounded") is False or checks.get("unmatched_numbers") or checks.get("echoed_numbers"):
        names.append("ungrounded_number")
    if checks.get("pseudo_citations"):
        names.append("pseudo_citation")
    if checks.get("unsupported_removal_claim"):
        names.append("unsupported_removal_claim")
    if checks.get("has_citation") is False and not checks.get("is_refusal"):
        names.append("no_citation")
    return names


def checks_failed(checks: object) -> bool:
    return bool(failed_check_names(checks))


def answer_checks(text: str, cited: set[str], valid_ids: set[str], context: str | None = None, *,
                  sources: Mapping[str, str] | None = None, question: str | None = None) -> AnswerChecks:
    """The checks for one answer. ``sources`` maps a citation id to the text behind it (a chunk's text, a risk summary,
    a metric line), so a percentage stated in a chunk the answer cites is grounded; ``question`` identifies the figures
    that only the asker stated (``echoed_numbers``, never grounded). Without a ``context`` there is nothing to ground
    numbers or removal claims against, and none is claimed unmatched."""
    figures = (_Figures() if context is None
               else _check_figures(text, cited, context, sources or {}, question or ""))
    return AnswerChecks(
        citations_retrieved=not (cited - valid_ids), numbers_grounded=not (figures.unmatched or figures.echoed),
        unmatched_numbers=figures.unmatched, pseudo_citations=_pseudo_citations(text, cited, valid_ids),
        echoed_numbers=figures.echoed, numbers_checked=figures.checked, has_citation=bool(cited),
        is_refusal=refusal_shaped(text),
        removal_claims=() if context is None else unsupported_removal_claims(text, context))


def verify_answer(text: str, cited: set[str], valid_ids: set[str], finish_reason: str | None,
                  context: str | None = None, *, sources: Mapping[str, str] | None = None,
                  question: str | None = None) -> list[str]:
    """Reasons the draft must not be released as is (empty list = it may be), in a fixed order.

    - ``empty``: nothing was produced.
    - ``truncated``: the model ran out of output budget mid-answer.
    - ``invalid_citation``: a cited id is not in the retrieved context (a fabricated source).
    - ``no_citation``: no source is cited and the answer is neither a clear refusal (:func:`refusal_shaped`)
      nor — when the retrieved ``context`` is given — a short statement, with no claim beyond it, whose every amount
      appears in that context in its own currency. (That last exemption RELEASES the draft; the answer's ``checks``
      still report ``has_citation`` False, so it is not cached and the page warns.) Everything else uncited is
      escalated: the safe direction, since it only costs a stronger-model call.
    - ``ungrounded_number`` (needs ``context``): a figure nothing in the context supports, or that only the question
      states (see :func:`answer_checks`); applies to cited answers too.
    - ``pseudo_citation``: bracketed text that is not a citation id.
    - ``unsupported_removal_claim`` (needs ``context``): a sentence claims a disclosure was removed without citing the
      removed lists.
    """
    if not text.strip():
        return ["empty"]
    reasons = []
    if finish_reason == "length":
        reasons.append("truncated")
    if cited - valid_ids:
        reasons.append("invalid_citation")
    checks = answer_checks(text, cited, valid_ids, context, sources=sources, question=question)
    if not checks.has_citation and not checks.is_refusal:
        reasons.append("no_citation")
    if not checks.numbers_grounded:
        reasons.append("ungrounded_number")
    if checks.pseudo_citations:
        reasons.append("pseudo_citation")
    if checks.removal_claims:
        reasons.append("unsupported_removal_claim")
    return reasons
