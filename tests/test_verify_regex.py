"""Linear-time answer checks: the regexes of ``verify`` and ``removal_claims`` on adversarial text (2026-10-04 audit).

``answer_checks`` and ``verify_answer`` run on EVERY answer, and an answer is model output that a prompt-injected
document can influence. A CPU-bound regex holds the GIL, so a pattern that is quadratic (or worse) on a crafted answer
stalls the whole event loop, whatever thread it runs in. The audit found (10,000 characters, before the fix):

- ``verify._PERCENT_VALUE_RE`` on "1,1,1,...": 0.64 s. Every digit after a comma was a start that rescanned the run.
- ``verify._PERCENT_RE`` on "111...1": cubic, 1,000 digits took 1.2 s (``refusal_shaped`` runs it on up to 1,200
  characters).
- ``_CLAUSE_SPLIT_RE`` and ``_HARD_SPLIT_RE`` on a run of blanks (``\\s*;\\s*``): every blank a start.
  ``_TABLE_SEPARATOR_RE``, ``_LIST_CONTINUES_RE`` and ``_TERSE_REMOVAL_RE`` on a run of blanks: adjacent ``\\s*`` split
  every way (the last one cubic: "removed" and 800 blanks took 0.3 s). ``_CLAIM_THAT_RE``: a scan per "claim that".
  The inline pattern of ``_closes_soon``: adjacent iterations split one long word every way (cubic: 800 characters
  after a removal verb took 0.9 s). ``_INTRODUCES_LABEL_RE``: searched over all the text before each quoted label.
- ``_ECHO_RE`` (a list label and what follows it) backtracked EXPONENTIALLY: "No longer appears as a separate risk
  factor: " and 24 times "none " and a "#" took 16 s, and 30 "none" would take hours. It is now ``rc._echo_match``, a
  search that tries the same splits in the same order and remembers where it failed.
- plain code that scanned per match: the overlap test of ``_amounts`` / ``_known_amounts``, the "already seen" lists
  of the pseudo-citations and the figures, and ``_bullets_under`` walking every blank line behind each blank line.

Every rewritten pattern keeps its OLD pattern here, verbatim (the expanded string and flags of HEAD, not rebuilt from
the module's fragments), and each test compares old and new on 20,000 random strings of at most 300 characters (a
fixed seed, the tokens the pattern reads) and on every string literal of the existing tests: the same result, read
the way the callers read it (spans and groups where they use them; whether there is a match where they only ask that).

NOT fixed here, still quadratic and plain code rather than a regex (listed in the audit report): ``_survives``
re-reads the whole sentence before each removal verb, and ``_clause_claims`` joins and re-scans the other clauses of
a line for each.
"""

import ast
import gc
import random
import re
import statistics
import time
from pathlib import Path

import pytest

from semigraph.retrieval import removal_claims as rc
from semigraph.retrieval import verify as vf
from semigraph.retrieval.context_layout import CONTEXT_HEADERS

TESTS = Path(__file__).parent
DIFFERENTIAL_STRINGS = 20_000
MAX_CHARS = 300
PERF_CHARS = 20_000
PERF_RUNS = 10
P95_LIMIT_MS = 20.0

# --- the patterns of HEAD (e918563), expanded, with their flags ------------------------------------------------------
OLD_SOURCES = {
    "PERCENT": (r"\d(?:[\d,]*\.?\d*)\s?%", 0),
    "PERCENT_VALUE": (r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(?:%|percent\b)", re.I),
    "CLAUSE_SPLIT": (r'(?<=[.!?])\s+(?=[A-Z0-9\-*(\"“])|\s*;\s*|\n+|,\s+(?=(?:and|but|while|whereas|which|'
                     r'although|though|yet|however|so)\b)', 0),
    "HARD_SPLIT": (r'(?<=[.!?…])[*_`\"”’)\]]*\s+(?=[A-Z0-9\-*(\"“])|\s*;\s*|\n+', 0),
    "TABLE_SEPARATOR": (r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$", 0),
    "LIST_CONTINUES": (r"\s*,?\s*(?:or|nor|and)\s+\w", re.I),
    "TERSE_REMOVAL": (
        r"(?:^|[:|—–]|\s-\s)\s*(?:was\s+|were\s+)?(?:(?:removed|dropped|deleted|eliminated|omitted|discontinued)|"
        r"no\s+longer\s+(?:included|disclosed|listed|present|appears?))(?:\s*,\s*(?:not|never)\s+\w+)?"
        r"\s*\|*\s*[.!]*\s*$", re.I),
    "CLAIM_THAT": (r"\b(?:claims?|assertions?|suggestions?|assumptions?|notions?|ideas?|conclusions?|inferences?|"
                   r"beliefs?)\s+that\b[^.;:]*$", re.I),
    "CLOSES_SOON": (r"(?:\s*[\w'’-]+){0,3}\s*,", 0),
    "INTRODUCES_LABEL": (r"\b(?:under|in|see|per|within|below|above|from)\s*$", re.I),
    "ECHO": (
        r"""^(?P<label>(?P<removed>No\s+longer\s+appears\s+as\s+a\s+separate\s+(?:risk\s+factors?|paragraphs?)"""
        r"""(?:\s+(?:or|and)\s+(?:risk\s+factors?|paragraphs?))?|Passages\s+(?:of\s+surviving\s+(?:risk\s+factors?|"""
        r"""paragraphs?)(?:\s+(?:or|and)\s+(?:risk\s+factors?|paragraphs?))?\s+)?whose\s+wording\s+was\s+not\s+found"""
        r"""\s+in\s+the\s+newer\s+filing)|(?P<unsettled>Not\s+matched))(?:[\s\-–—:]*(?:(?P<none>"""
        r"""(?:(?:the|this)\s+)?"""
        r"""(?:(?:automated|text)\s+)*(?:(?:check|comparison|list)\s+(?:found|lists?|shows?|identified|contains?|"""
        r"""reports?)\s+|there\s+(?:are|is|were|was)\s+)?(?:none|nothing|zero|0|n/a|empty|no\s+(?:risk\s+factors?|"""
        r"""items?|entries|passages?|paragraphs?|matches?|results?))\b(?:\s+(?!(?:and|but|however|although|while|yet|"""
        r"""though|because|since|except|besides|unless|where|when|whereas|so|given|due|other|apart|aside|save|"""
        r"""excluding|beyond|barring|as(?!\s+(?:a|an|the|separate|removed|dropped|being|having)\b)|now(?=\s+that\b))"""
        r"""\b)[\w'’/-]+){0,8})|showing\s+\d+\s+of\s+\d+(?:\s+(?!(?:and|but|however|although|while|yet|though|"""
        r"""because|since|except|besides|unless|where|when|whereas|so|given|due|other|apart|aside|save|excluding|"""
        r"""beyond|barring|as(?!\s+(?:a|an|the|separate|removed|dropped|being|having)\b)|now(?=\s+that\b))"""
        r"""\b)[\w'’/-]+)"""
        r"""{0,4}|\((?:(?P<none2>none|nothing|empty)\b(?:(?!(?:except|besides|other\s+than|apart|aside|but)\b)"""
        r"""[^()\[\]])*|(?:showing|the\s+text\s+check|a\s+differently\s+worded|parts\s+of)\b(?:(?!(?:except|besides|"""
        r"""other\s+than|apart|aside|but)\b)[^()\[\]])*)\)))*[\s:.]*$""", re.I),
}
OLD = {name: re.compile(source, flags) for name, (source, flags) in OLD_SOURCES.items()}


# --- the old code that the plain-code fixes replace ------------------------------------------------------------------
def _old_amounts(text):
    text = vf._plain_spaces(text)
    found = [vf._Amount(vf._value(m["num"], m["scale"]), vf._currency(m["cur"]), m.start(), m.end(), m.group(0).strip())
             for m in vf._PREFIX_RE.finditer(text)]
    taken = [(a.start, a.end) for a in found]
    for m in vf._SUFFIX_RE.finditer(text):
        if not any(m.start() < e and s < m.end() for s, e in taken):
            found.append(vf._Amount(vf._value(m["num"], m["scale"]), vf._currency(m["cur"]), m.start(), m.end(),
                                    m.group(0).strip()))
    return sorted(found, key=lambda a: a.start)


def _old_known_amounts(text):
    tagged = _old_amounts(text)
    spans = [(a.start, a.end) for a in tagged]
    known = [(a.value, a.currency) for a in tagged]
    text = vf._plain_spaces(text)
    for m in vf._GROUPED_RE.finditer(text):
        if not any(m.start() < e and s < m.end() for s, e in spans):
            known.append((float(m.group(1).replace(",", "")), None))
    for m in vf._SCALED_RE.finditer(text):
        if not any(m.start() < e and s < m.end() for s, e in spans):
            known.append((float(m.group(1).replace(",", "")) * vf._SCALE[m.group(2).lower()], None))
    return known


# --- what the callers read, old and new ------------------------------------------------------------------------------
def _span(match):
    return None if match is None else match.span()


def _old_percent_values(s):
    return [(m.span(), m.group(1)) for m in OLD["PERCENT_VALUE"].finditer(s)]


def _new_percent_values(s):
    return [(m.span(), m.group(1)) for m in vf._percent_matches(s)]


def _introduces_case(s):
    """(string, end) from a string with an optional cut marker: the label would start at the marker."""
    return s.replace("\x00", ""), (s.index("\x00") if "\x00" in s else len(s))


def _old_introduces(case):
    string, end = case
    return bool(OLD["INTRODUCES_LABEL"].search(string[:end]))


def _new_introduces(case):
    string, end = case
    return rc._introduces_label(string, end)


def _old_echo(s):
    m = OLD["ECHO"].match(s)
    return None if m is None else (bool(m["removed"]), m.end("label"), bool(m["none"] or m["none2"]))


def _new_echo(s):
    found = rc._echo_match(s)
    return None if found is None else (found.removed, found.label_end, found.states_none)


def _amount_view(amounts):
    return [(a.value, a.currency, a.start, a.end, a.shown) for a in amounts]


# --- the tokens each pattern reads, and generators of strings built from them ----------------------------------------
BLANKS = [" ", "  ", "\n", "\t", "\u00a0", "\u2028", "\x1f", "\x85", "\r"]
NUMBERS = ["0", "1", "2", "5", "9", "12", "65", "3,", "1,234", ",", ",,", ".", "1.5", "%", "percent", "Percent",
           "PERCENT", "percentage", "x", "a", "$", "-", "+", "(", ")", "_", "e", "\u0663", "\u00b2"] + BLANKS
SPLITS = [".", "!", "?", ";", ",", ", and", "and", "but", "so", "A", "B", "1", "-", "*", "(", '"', "“", "”", ")",
          "x", "word", "U.S.", "Inc"] + BLANKS
HARD_SPLITS = SPLITS + ["…", "’", "]", "_", "`"]
SEPARATORS = ["|", "-", "--", "---", ":", "x", "a", "1", "="] + BLANKS
CONTINUES = [",", "or", "nor", "and", "OR", "And", "x", "a", "1", ", ", "andy", "ordinal"] + BLANKS
REMOVAL_WORDS = ["removed", "dropped", "deleted", "eliminated", "omitted", "discontinued", "Removed", "DROPPED"]
TERSE = [":", "|", "-", " - ", "—", "–", "was", "were", "no longer", "included", "disclosed", "listed", "present",
         "appears", "appear", ",", "not", "never", "reworded", ".", "!", "x", "a", "risk"] + REMOVAL_WORDS + BLANKS
CLAIMS = [".", ";", ":", "claim", "claims", "assertion", "suggestions", "Idea", "belief", "that", "x", "a", "the",
          ",", "!", "that's", "thatch", "noclaim", "reclaim", "-"] + BLANKS
CLOSERS = ["a", "b", "word", "it's", "x-y", ",", ", ", "'", "’", "-", "1", ".", ";", "removed", "\n"] + BLANKS
INTRODUCERS = ["under", "in", "see", "per", "within", "below", "above", "from", "Within", "inside", "within-", "xin",
               "in.", ",", "x", '"', "\x00"] + BLANKS
MONEY = ["$", "\u20ac", "USD", "EUR", "NT$", "US$", "HK$", "TWD", "1", "2,5", "10", "1,234", "1,234,567", ".", ",",
         "million", "billion", "bn", "dollars", "euros", "new taiwan dollars", "x", "(", ")", "-", ":"] + BLANKS
LABELS = ["No longer appears as a separate risk factor", "No longer appears as a separate risk factors",
          "No longer appears as a separate paragraph", "No longer appears as a separate risk factor or paragraph",
          "No longer appears as a separate paragraphs and risk factors", "no longer appears as a separate risk factor",
          "NO LONGER APPEARS AS A SEPARATE RISK FACTOR", "Passages whose wording was not found in the newer filing",
          "Passages of surviving risk factors whose wording was not found in the newer filing",
          "Passages of surviving paragraphs or risk factors whose wording was not found in the newer filing",
          "Not matched", "not matched", "Not  matched"]
STATEMENTS = ["none", "None", "nothing", "zero", "0", "n/a", "empty", "no risk factors", "no items", "no entries",
              "no passages", "no matches", "no results", "showing 2 of 21", "showing 8 of 20", "showing 1 of 2",
              "(none)", "(nothing)", "(empty)", "(showing 2 of 21)", "(the text check found none)",
              "(a differently worded version)", "(parts of it)", "(none, except x)", "the check found none",
              "the text check found none", "this comparison shows none", "there are none", "abcnone", "x0",
              "showing 10 of 100",
              "showing 3 of 30 risk factors", "x none"]
FILLER = ["a", "b", "word", "x", "-", "it's", "of", "20", "the", "text", "none", "showing", "found", "abcnone", "no",
          "0", "(", "10", "and", "but", "as", "as a", "now that", "other", ")", ":", "–", "—", ".", ",", "!",
          "factors", "risk"]
GAPS = ["", "", " ", " ", " ", "  ", "\n", "\t", "\u00a0", " - ", " : ", " – "]


def _random_tokens(alphabet):
    def gen(rng):
        count = rng.choice((rng.randint(0, 6), rng.randint(0, 25), rng.randint(0, 90)))
        out, size = [], 0
        for _ in range(count):
            token = rng.choice(alphabet)
            if size + len(token) > MAX_CHARS:
                break
            out.append(token)
            size += len(token)
        return "".join(out)
    return gen


def _with_noise(rng, text):
    """Most structured strings are valid; a third get one stray token, so the near misses are covered too."""
    if rng.random() < 0.35:
        at = rng.randint(0, len(text))
        text = text[:at] + rng.choice(["x", ".", " ", "|", "-", ":", "a b", ",", "\n", "\u00a0", "1"]) + text[at:]
    return text


def _mixed(structured, alphabet):
    plain = _random_tokens(alphabet)
    return lambda rng: structured(rng) if rng.random() < 0.5 else plain(rng)


def _table_row(rng):
    def segment():
        return (rng.choice(("", " ", "  ", "\t")) + rng.choice(("", ":")) + "-" * rng.randint(0, 4)
                + rng.choice(("", ":")) + rng.choice(("", " ", "   ")))
    parts = [rng.choice(("", " ", "|", " | ", "\t|"))]
    for _ in range(rng.randint(1, 4)):
        parts += [segment(), rng.choice(("|", " |", "| ", "", "||"))]
    return _with_noise(rng, "".join(parts))


def _terse_clause(rng):
    pick = rng.choice
    return _with_noise(rng, "".join((
        pick(("", "Risk factor", "Debt", "- Debt", "x | y")), pick(("", ":", "|", " - ", "—", "–", "  :  ")),
        pick(("", " ", "   ")), pick(("", "was ", "were ")),
        pick(REMOVAL_WORDS + ["no longer included", "No Longer appears"]),
        pick(("", ", not reworded", " , never added", ",not x")), pick(("", " ", "  ")), pick(("", "|", "||", " |")),
        pick(("", " ", "  ")), pick(("", ".", "!", "!.", " .")), pick(("", " ", "\n", "  ")),
        pick(("", "", " x", "[id]", "\n")))))


def _claim_text(rng):
    segments = []
    for _ in range(rng.randint(1, 4)):
        words = [rng.choice(("the", "a", "x", "that", "claim", "idea", "Notions", "belief", "claims that",
                             "inference  that", "suggestion that", "reclaim that")) for _ in range(rng.randint(0, 6))]
        segments.append(" ".join(words))
    return rng.choice((".", ";", ":", "; ", ". ", ":\n")).join(segments) + rng.choice(("", "", ".", "\n", " "))


def _closes_text(rng):
    words = [rng.choice(("a", "b-c", "it's", "word", "x'y", "’s", "1")) for _ in range(rng.randint(0, 6))]
    glue = [rng.choice(("", " ", "  ", "\n", " ", " ")) for _ in words]
    ending = rng.choice((",", " ,", "", ", x", " .", ",,"))
    return _with_noise(rng, "".join(g + w for g, w in zip(glue, words)) + ending)


def _within_the_limit(gen):
    """Resample until the case fits MAX_CHARS (cutting one would break the structure it was built with)."""
    def bounded(rng):
        while True:
            case = gen(rng)
            if len(case) <= MAX_CHARS:
                return case
    return bounded


def _list_label(rng):
    """A label and what may follow it: a few statements of the kinds the label line takes, with a few words between them
    (up to eleven: the cap is eight words for a none statement and four for a count), glued or spaced."""
    out = [rng.choice(LABELS)]
    if rng.random() < 0.5:
        out.append(rng.choice(GAPS))
        budget = 7
        for _ in range(rng.choice((rng.randint(0, 5), rng.randint(0, 12), rng.randint(0, 20)))):
            token = rng.choice(STATEMENTS) if budget > 0 and rng.random() < 0.45 else rng.choice(FILLER)
            budget -= token in STATEMENTS
            out += [rng.choice(GAPS), token]
    else:
        out.append(rng.choice((":", " :", " - ", " ", "", "\n")))
        for _ in range(rng.randint(1, 5)):
            out.append(rng.choice(STATEMENTS))
            for _ in range(rng.randint(0, 11)):
                out += [rng.choice((" ", " ", " ", "  ", "-", "")), rng.choice(FILLER)]
            out.append(rng.choice((" ", " ", "  ", " - ", ": ", ", ")))
    out.append(rng.choice(["", "", ".", " .", ":", " :", "!", "#", "\n", " x", " and", ",", "  ", " none", " (none)"]))
    return "".join(out)


# name: (old reading, new reading, generator of cases, how a string literal of the existing tests becomes a case)
SPECS = {
    "PERCENT": (lambda s: bool(OLD["PERCENT"].search(s)), lambda s: bool(vf._PERCENT_RE.search(s)),
                _random_tokens(NUMBERS), str),
    "PERCENT_VALUE": (_old_percent_values, _new_percent_values, _random_tokens(NUMBERS), str),
    "CLAUSE_SPLIT": (lambda s: OLD["CLAUSE_SPLIT"].split(s), lambda s: vf._CLAUSE_SPLIT_RE.split(s),
                     _random_tokens(SPLITS), str),
    "HARD_SPLIT": (lambda s: OLD["HARD_SPLIT"].split(s), lambda s: rc._HARD_SPLIT_RE.split(s),
                   _random_tokens(HARD_SPLITS), str),
    "TABLE_SEPARATOR": (lambda s: _span(OLD["TABLE_SEPARATOR"].match(s)),
                        lambda s: _span(rc._TABLE_SEPARATOR_RE.match(s)), _mixed(_table_row, SEPARATORS), str),
    "LIST_CONTINUES": (lambda s: _span(OLD["LIST_CONTINUES"].match(s)), lambda s: _span(rc._LIST_CONTINUES_RE.match(s)),
                       _random_tokens(CONTINUES), str),
    "TERSE_REMOVAL": (lambda s: _span(OLD["TERSE_REMOVAL"].search(s)), lambda s: _span(rc._TERSE_REMOVAL_RE.search(s)),
                      _mixed(_terse_clause, TERSE), str),
    "CLAIM_THAT": (lambda s: bool(OLD["CLAIM_THAT"].search(s)), lambda s: bool(rc._CLAIM_THAT_RE.search(s)),
                   _mixed(_claim_text, CLAIMS), str),
    "CLOSES_SOON": (lambda s: _span(OLD["CLOSES_SOON"].match(s)), lambda s: _span(rc._CLOSES_SOON_RE.match(s)),
                    _mixed(_closes_text, CLOSERS), str),
    "INTRODUCES_LABEL": (_old_introduces, _new_introduces,
                         lambda rng: _introduces_case(_random_tokens(INTRODUCERS)(rng)), _introduces_case),
    "ECHO": (_old_echo, _new_echo, _within_the_limit(_list_label), str),
    "AMOUNTS": (lambda s: _amount_view(_old_amounts(s)), lambda s: _amount_view(vf._amounts(s)),
                _random_tokens(MONEY), str),
    "KNOWN_AMOUNTS": (_old_known_amounts, vf._known_amounts, _random_tokens(MONEY), str),
}
MIN_POSITIVES = 400      # "not vacuous": strings the OLD reading finds something in (a match, a cut, a non-empty list)


def _nontrivial(value):
    if isinstance(value, list) and value and isinstance(value[0], str):
        return len(value) > 1                                    # a split that cut the text
    return bool(value)


@pytest.fixture(scope="module")
def literals():
    """Every string literal (4 to 1,500 characters) of the existing tests of these checks, and their lines."""
    found = set()
    files = [f for f in sorted(TESTS.glob("test_verify*.py")) if f.name != Path(__file__).name]
    files += sorted(TESTS.glob("test_answerer*.py"))
    files += [TESTS / "test_answer_checks_events.py", TESTS / "test_escalation.py"]
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and 4 <= len(node.value) <= 1500:
                found.add(node.value)
                found.update(line for line in node.value.split("\n") if line.strip())
    assert len(found) > 3000, f"the existing tests hold {len(found)} literals: the harvest is broken"
    return sorted(found)


def _differences(name, cases):
    old, new, _, _ = SPECS[name]
    return [f"{case!r:.160} old={old(case)!r:.100} new={new(case)!r:.100}" for case in cases if old(case) != new(case)]


@pytest.mark.parametrize("name", SPECS)
def test_rewrite_matches_the_old_pattern_on_random_strings(name):
    old, _, gen, _ = SPECS[name]
    rng = random.Random(f"verify-regex-{name}")
    cases = [gen(rng) for _ in range(DIFFERENTIAL_STRINGS)]
    longest = max(len(case[0] if isinstance(case, tuple) else case) for case in cases)
    assert longest <= MAX_CHARS, f"a generated string has {longest} characters (limit {MAX_CHARS})"
    wrong = _differences(name, cases)
    assert not wrong, f"{len(wrong)} of {len(cases)} differ; first: {wrong[:3]}"
    positives = sum(_nontrivial(old(case)) for case in cases)
    assert positives >= MIN_POSITIVES, f"only {positives} of {len(cases)} strings give the old pattern a match to find"


@pytest.mark.parametrize("name", SPECS)
def test_rewrite_matches_the_old_pattern_on_the_literals_of_the_existing_tests(name, literals):
    from_text = SPECS[name][3]
    wrong = _differences(name, [from_text(text) for text in literals])
    assert not wrong, f"{len(wrong)} of {len(literals)} literals differ; first: {wrong[:3]}"


def _echo_of_the_longest_label_only(s):
    label = rc._LABEL_RE.match(s)
    states_none = None if label is None else rc._echo_tail_states_none(s, label.end())
    return None if states_none is None else (bool(label["removed"]), label.end(), states_none)


def test_the_differential_catches_a_wrong_rewrite():
    """A rewrite that forgets a branch (a ';' after blanks that an earlier match took, the digit a percentage needs, the
    case-insensitivity of 'percent', the trailing '!', the shorter ends of a label) shows up in the same corpus."""
    mutants = {
        "CLAUSE_SPLIT": lambda s: re.compile(vf._CLAUSE_SPLIT_RE.pattern.replace(r"|;\s*|", "|")).split(s),
        "PERCENT": lambda s: bool(re.search(r"(?<![\d,])[\d,]++(?:\.\d*+)?\s?%", s)),
        "PERCENT_VALUE": lambda s: [(m.span(), m.group(1)) for m in re.compile(vf._PERCENT_VALUE_RE.pattern).finditer(s)
                                    if m.group(1) is not None],
        "TERSE_REMOVAL": lambda s: _span(re.compile(rc._TERSE_REMOVAL_RE.pattern.replace("[.!]+", "[.]+"),
                                                    re.I).search(s)),
        "ECHO": lambda s: (lambda m: m and (m.removed, m.label_end, not m.states_none))(rc._echo_match(s)),
    }
    for name, mutant in mutants.items():
        old, _, gen, _ = SPECS[name]
        rng = random.Random(f"verify-regex-{name}")
        cases = [gen(rng) for _ in range(DIFFERENTIAL_STRINGS)]
        assert any(old(case) != mutant(case) for case in cases), f"{name}: the wrong rewrite went unnoticed"


def test_the_label_differential_catches_a_matcher_that_forgets_a_shorter_label_or_a_word(monkeypatch):
    old, _, gen, _ = SPECS["ECHO"]
    rng = random.Random("verify-regex-ECHO")
    cases = [gen(rng) for _ in range(DIFFERENTIAL_STRINGS)]
    assert any(old(case) != _echo_of_the_longest_label_only(case) for case in cases), "a shorter label goes unnoticed"
    monkeypatch.setattr(rc, "_NONE_STATEMENT_WORDS", 7)          # one word fewer for a none statement (the cap is 8)
    assert any(old(case) != _new_echo(case) for case in cases), "the cap of eight words goes unnoticed"
    monkeypatch.setattr(rc, "_NONE_STATEMENT_WORDS", 8)
    monkeypatch.setattr(rc, "_SHOWING_WORDS", 5)                 # and one word more for a count (the cap is four)
    assert any(old(case) != _new_echo(case) for case in cases), "the cap of four words goes unnoticed"


@pytest.mark.parametrize("text, pieces", [
    ("a; ;b", ["a", "", "b"]),                     # the second ';' follows blanks the first match took
    ("a;\n ;b", ["a", "", "b"]),
    ("x  ;  y", ["x", "y"]),
    (" ;", ["", ""]),
    ("a. ; B", ["a.", "B"]),
])
def test_semicolons_after_blanks_split_as_before(text, pieces):
    assert vf._CLAUSE_SPLIT_RE.split(text) == pieces
    assert rc._HARD_SPLIT_RE.split(text) == pieces


# --- the plain-code fixes keep their results -------------------------------------------------------------------------
REMOVED_ID, KEPT_ID = "0001045810-24-000029:I.1A:0002", "0001045810-24-000029:I.1A:0003"
COMPARISON = (CONTEXT_HEADERS[0] + "x\n" + CONTEXT_HEADERS[4]
              + "Nvidia: fiscal year ended January 26, 2025 -> fiscal year ended January 25, 2026\n"
              "No longer appears as a separate risk factor (the text check found no matching text):\n"
              f"- Old risk [{REMOVED_ID}]\n"
              + CONTEXT_HEADERS[5] + "text\n")


@pytest.mark.parametrize("text, claims", [
    (f"Nvidia dropped the export risk factor [{KEPT_ID}].", (f"Nvidia dropped the export risk factor [{KEPT_ID}].",)),
    (f"Nvidia dropped the export risk factor [{REMOVED_ID}].", ()),
    (f"Nvidia dropped the export risk factor [{KEPT_ID}].\n\n\n\nSome other line.\n\n   \n\t\nMore.",
     (f"Nvidia dropped the export risk factor [{KEPT_ID}].",)),
    (f"**Removed risk factors:**\n\n\n- Export controls [{KEPT_ID}]\n- Debt [{REMOVED_ID}]",
     ("**Removed risk factors:**",)),
    ("**Removed risk factors:**\n\n\n\nNone found.\n", ()),
    (f"No longer appears as a separate risk factor\n\n\n- Export controls [{KEPT_ID}]",
     ("No longer appears as a separate risk factor",)),
    (f"   \n \t \n### Removed risk factors\n\n- a [{KEPT_ID}]", ("### Removed risk factors",)),
])
def test_blank_lines_are_skipped_without_changing_the_claims(text, claims):
    assert rc.unsupported_removal_claims(text, COMPARISON) == claims


@pytest.mark.parametrize("line, echo", [
    ("No longer appears as a separate risk factor", ("removed", False)),
    ("No longer appears as a separate risk factor: none found", ("removed", True)),
    ("### No longer appears as a separate risk factor", ("removed", False)),
    ("**No longer appears as a separate risk factor:** The text check found none", ("removed", True)),
    ("No longer appears as a separate risk factor (the text check found no matching text in the newer filing; parts of "
     "their content may be covered inside other risk factors) - showing 2 of 21:", ("removed", False)),
    ("- Passages of surviving risk factors whose wording was not found in the newer filing (a differently worded "
     "version of the same statement may exist): none", ("removed", True)),
    ("Not matched (the text check could not verify whether these older risk factors still appear; they may have been "
     "removed or absorbed into another risk factor) - showing 3 of 8:", ("unsettled", False)),
    ("No longer appears as a separate risk factor: none none none x", ("removed", True)),
    ("Not matched: none", ("unsettled", True)),
    ("No longer appears as a separate risk factor or paragraph: none found", ("removed", True)),
    ("No longer appears as a separate risk factor removed", None),
    ("no longer appears as a separate risk factor: none found", None),
    ("No longer appears as a separate risk factor: the indebtedness risk factor", None),
    ("Not matched and nothing else", None),
])
def test_list_labels_are_read_as_before(line, echo):
    assert rc._echo(line) == echo


def test_pseudo_citations_and_figures_keep_their_order_and_drop_repeats():
    text = "[Label one] [Label two] [Label one] [1] [Label two] [Label three]"
    assert vf._pseudo_citations(text, set(), set()) == ("Label one", "Label two", "Label three")
    figures = vf._check_figures("$9 then $8 then $9, and 5% then 4% then 5%", set(), "", {}, "")
    assert figures.unmatched == ("$9", "$8", "5%", "4%") and figures.checked == 6


def test_overlap_test_agrees_with_the_scan_it_replaces():
    spans = [(2, 5), (7, 8), (10, 20), (30, 31)]
    for start in range(0, 34):
        for end in range(start + 1, 36):
            assert vf._overlaps(spans, start, end) == any(start < e and s < end for s, e in spans), (start, end)
    assert not vf._overlaps([], 0, 5)


# --- performance: each fixed pattern, in the form its callers use it, on its worst text ------------------------------
def _repeat(unit, n=PERF_CHARS):
    return (unit * (n // len(unit) + 1))[:n]


def _labels(n):
    return _repeat('"no longer appears as a separate risk factor" ', n)


LABEL = "No longer appears as a separate risk factor"


def _echo_of(unit, end="#"):
    return lambda n: LABEL + _repeat(unit, n - len(LABEL) - 1) + end


# id, text of n characters, the call, the limit in ms at 20,000 characters (a regex call: 20 ms; code with a
# per-match or per-position cost of its own: what that costs, so that the linear-scaling check is the real guard)
PERF_CASES = [
    ("percent_value/comma_digits", lambda n: _repeat("1,", n), lambda s: list(vf._percent_matches(s)), P95_LIMIT_MS),
    ("percent_value/grouped", lambda n: _repeat("1,234,", n), lambda s: list(vf._percent_matches(s)), P95_LIMIT_MS),
    ("percent_value/comma_digits_then_x", lambda n: _repeat("1,", n - 1) + "x", lambda s: list(vf._percent_matches(s)),
     P95_LIMIT_MS),
    ("percent/digits", lambda n: "1" * n, lambda s: vf._PERCENT_RE.search(s), P95_LIMIT_MS),
    ("percent/comma_digits", lambda n: _repeat("1,", n - 1) + "x", lambda s: vf._PERCENT_RE.search(s), P95_LIMIT_MS),
    ("percent/leading_commas", lambda n: "," * n, lambda s: vf._PERCENT_RE.search(s), P95_LIMIT_MS),
    ("clause_split/blanks", lambda n: "x" + " " * n + "x", lambda s: vf._CLAUSE_SPLIT_RE.split(s), P95_LIMIT_MS),
    ("clause_split/nbsp", lambda n: "\u00a0" * n, lambda s: vf._CLAUSE_SPLIT_RE.split(s), P95_LIMIT_MS),
    ("hard_split/blanks", lambda n: "x" + " " * n + "x", lambda s: rc._HARD_SPLIT_RE.split(s), P95_LIMIT_MS),
    ("hard_split/tabs", lambda n: "\t" * n, lambda s: rc._HARD_SPLIT_RE.split(s), P95_LIMIT_MS),
    ("hard_split/blanks_then_semicolon", lambda n: _repeat(" " * 20 + ";", n), lambda s: rc._HARD_SPLIT_RE.split(s),
     P95_LIMIT_MS),
    ("table_separator/blanks", lambda n: " " * n + "x", lambda s: rc._TABLE_SEPARATOR_RE.match(s), P95_LIMIT_MS),
    ("table_separator/trailing_blanks", lambda n: "--" + " " * n + "x", lambda s: rc._TABLE_SEPARATOR_RE.match(s),
     P95_LIMIT_MS),
    ("list_continues/blanks", lambda n: " " * n + "x", lambda s: rc._LIST_CONTINUES_RE.match(s), P95_LIMIT_MS),
    ("terse_removal/blanks", lambda n: "removed" + " " * n + "x", lambda s: rc._TERSE_REMOVAL_RE.search(s),
     P95_LIMIT_MS),
    ("terse_removal/pipe_and_blanks", lambda n: "|removed" + " " * n + "x", lambda s: rc._TERSE_REMOVAL_RE.search(s),
     P95_LIMIT_MS),
    ("terse_removal/repeated", lambda n: _repeat(": removed" + " " * 30 + "x ", n),
     lambda s: rc._TERSE_REMOVAL_RE.search(s), P95_LIMIT_MS),
    ("claim_that/semicolon_last", lambda n: _repeat("claim that ", n - 30) + "; the risk factor was removed",
     lambda s: rc._CLAIM_THAT_RE.search(s), P95_LIMIT_MS),
    ("closes_soon/one_long_word", lambda n: "-" + "a" * n, lambda s: rc._closes_soon(s), P95_LIMIT_MS),
    ("closes_soon/word_run", lambda n: "a" * n + " b", lambda s: rc._closes_soon(s), P95_LIMIT_MS),
    ("introduces_label/many_labels", _labels, lambda s: rc._QUOTED_LABEL_RE.sub(rc._quoted_label, s), 30.0),
    ("amounts/euro_run", lambda n: _repeat("EUR 1 ", n), lambda s: vf.money_values(s), 60.0),
    ("known_amounts/grouped_and_tagged", lambda n: _repeat("1,234 EUR 1 1 million ", n), lambda s: vf._known_amounts(s),
     30.0),
    ("pseudo_citations/unique_brackets", lambda n: "".join(f"[w{i}] " for i in range(n // 7)),
     lambda s: vf._pseudo_citations(s, set(), set()), 12.0),
    ("blank_lines/between_lines", lambda n: "x" + "\n" * n + "x",
     lambda s: rc.unsupported_removal_claims(s, COMPARISON), 400.0),
    ("echo/none_words_then_hash", _echo_of(" none"), lambda s: rc._echo_match(s), 150.0),
    ("echo/colon_and_none", _echo_of(": none "), lambda s: rc._echo_match(s), 150.0),
    ("echo/dashes_and_none", _echo_of(" - none"), lambda s: rc._echo_match(s), 150.0),
    ("echo/subject_and_none", _echo_of(" the check found none"), lambda s: rc._echo_match(s), 150.0),
    ("echo/showing_counts", _echo_of(" showing 1 of 2"), lambda s: rc._echo_match(s), 150.0),
    ("echo/none_words_then_and", _echo_of(" none", " and"), lambda s: rc._echo_match(s), 150.0),
    ("echo/one_long_word", lambda n: LABEL + ": none " + "a" * (n - 60) + "#", lambda s: rc._echo_match(s), 150.0),
    ("echo/as_a_line_of_an_answer", _echo_of(" none"), lambda s: rc._echo(s), 150.0),
]


def _timings_ms(call, text, runs):
    """Wall time of ``call(text)`` in ms, ``runs`` times. The collector is paused (as ``timeit`` does): a full
    collection with the many live strings of the differential tests behind it takes tens of ms and lands in one run."""
    out = []
    gc.collect()
    gc.disable()
    try:
        for _ in range(runs):
            t0 = time.perf_counter()
            call(text)
            out.append((time.perf_counter() - t0) * 1000)
    finally:
        gc.enable()
    return out


@pytest.mark.parametrize("case", PERF_CASES, ids=[c[0] for c in PERF_CASES])
def test_p95_of_ten_runs_at_20000_characters(case):
    _, build, call, limit = case
    text = build(PERF_CHARS)
    assert len(text) >= PERF_CHARS - 10
    times = _timings_ms(call, text, PERF_RUNS)
    p95 = statistics.quantiles(times, n=20, method="inclusive")[18]
    assert p95 < limit, (f"p95 {p95:.1f} ms over {PERF_RUNS} runs at {len(text)} characters (limit {limit} ms, "
                         f"median {statistics.median(times):.1f} ms)")


@pytest.mark.parametrize("case", PERF_CASES, ids=[c[0] for c in PERF_CASES])
def test_time_at_40000_characters_is_at_most_three_times_the_time_at_20000(case):
    _, build, call, _ = case
    short, long = build(PERF_CHARS), build(2 * PERF_CHARS)
    for _ in range(2):                                       # one retry: a timing is only a measurement
        t20, t40 = min(_timings_ms(call, short, 3)), min(_timings_ms(call, long, 3))
        if t40 <= 3 * max(t20, 0.5):                         # below half a millisecond the ratio is noise
            return
    pytest.fail(f"{t40:.1f} ms at 40,000 characters, {t20:.1f} ms at 20,000: {t40 / max(t20, 0.5):.1f}x (limit 3x)")


# --- the whole check on the worst texts (a coarse tripwire: the unfixed cases named above are NOT in it) ----------
ANSWER_CLASSES = [
    ("comma_digits", lambda n: _repeat("1,", n)),
    ("grouped_numbers", lambda n: _repeat("1,234,", n)),
    ("dollar_comma_run", lambda n: "$" + _repeat("1,", n - 2) + "x"),
    ("blanks", lambda n: "x" + " " * n + "x"),
    ("line_breaks", lambda n: "x" + "\n" * n + "x"),
    ("terse_removal_blanks", lambda n: "removed" + " " * n + "x"),
    ("list_continues_blanks", lambda n: "No risk factor was added, removed" + " " * n + "x"),
    ("table_heading_blanks", lambda n: "| Removed risk factors |\n" + " " * n + "x\n"),
    ("one_long_word_after_a_verb", lambda n: "The risk factor was removed-" + "a" * n),
    ("claim_that_then_semicolon", lambda n: _repeat("claim that ", n - 30) + "; the risk factor was removed"),
    ("euro_amounts", lambda n: _repeat("EUR 1 ", n)),
    ("list_label_and_none_words", _echo_of(" none")),
    ("list_label_and_dashes", _echo_of(" - none")),
]


@pytest.mark.parametrize("name, build", ANSWER_CLASSES, ids=[c[0] for c in ANSWER_CLASSES])
def test_answer_checks_and_verify_answer_finish_on_the_worst_answers(name, build):
    text = build(PERF_CHARS)
    ids = {REMOVED_ID}
    t0 = time.perf_counter()
    vf.answer_checks(text, ids, ids, COMPARISON, sources={}, question="q")
    vf.verify_answer(text, ids, ids, "stop", COMPARISON, sources={}, question="q")
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.5, f"{name}: {elapsed:.2f} s for answer_checks and verify_answer on {len(text)} characters"


def test_refusal_check_on_the_longest_text_it_reads():
    """``refusal_shaped`` reads up to 1,200 characters; digits there took 2 s (cubic)."""
    for text in ("1" * 1200, "1," * 600, "1." * 600, "1" * 1199 + "x"):
        t0 = time.perf_counter()
        assert vf.refusal_shaped(text) is False
        assert time.perf_counter() - t0 < 0.1, repr(text[:12])
