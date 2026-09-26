"""Text-level building blocks shared by the item aligner and the passage layer (M1b).

Pure and deterministic (no I/O, no network, no LLM); split out of ``graph/alignment.py`` so that
module stays under the 800-line ceiling and ``graph/passages.py`` can reuse the exact same
sentence splitting, normalisation and near-verbatim probe. Nothing here knows about items,
parameters or labels.

* ``norm`` / ``norm_with_offsets``: the ``extraction.gates.normalize`` rule (whitespace and quote
  collapsed, casefolded), reimplemented locally because ``extraction.gates`` pulls in litellm;
  the second also maps every normalised character back to its source offset.
* ``in_range`` / ``at_least``: parameter validators shared by the frozen parameter dataclasses.
* ``split_sentences`` / ``sentence_texts``: sentence spans (offsets into the input) and their text.
* ``word_tokens`` / ``lex_exact``: word-level ``difflib`` similarity, symmetrised.
* ``SectionIndex`` / ``Hit``: a section prepared for verbatim quoting and whole-haystack fuzzy
  probing (``rapidfuzz.fuzz.partial_ratio_alignment`` with the matched span mapped back to a
  verbatim, sentence-aligned slice of the section text).
"""

import re
from bisect import bisect_right
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache

from rapidfuzz import fuzz

def in_range(name: str, value: float, low: float, high: float, *, open_low: bool = False) -> None:
    """Parameter validation shared by ``AlignParams`` and ``PassageParams`` (NaN fails both bounds)."""
    ok = (low < value if open_low else low <= value) and value <= high
    if not ok:
        raise ValueError(f"{name} must be in {'(' if open_low else '['}{low}, {high}], got {value!r}")


def at_least(name: str, value: int, minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")


NORM_RE = re.compile(r"[\s’'\"“”]+")        # identical to extraction.gates (not imported: it pulls in litellm)
TOKEN_RE = re.compile(r"\w+")
SENTENCE_BREAK = re.compile(
    r"\n+"                                            # line / paragraph breaks
    r"|•"                                             # inline bullet glyph
    r"|(?<=[a-z0-9][.!?])(?=[A-Z])"                   # glued sentences: "cash flows.Further"
    r"|(?<!\b[A-Z]\.)(?<=[.!?])\s+(?=[A-Z“\"‘(])"     # ordinary boundary, never after "U.S."
)


def norm(text: str) -> str:
    """Whitespace/quote-collapsed, casefolded text (the ``extraction.gates.normalize`` rule)."""
    return NORM_RE.sub(" ", text).strip().lower()


def norm_with_offsets(text: str) -> tuple[str, list[int]]:
    """``norm(text)`` plus, per normalised character, the index of its source character in ``text``."""
    segments: list[tuple[int, int]] = []
    start = 0
    for match in NORM_RE.finditer(text):
        if match.start() > start:
            segments.append((start, match.start()))
        start = match.end()
    if len(text) > start:
        segments.append((start, len(text)))
    parts: list[str] = []
    offsets: list[int] = []
    for k, (a, b) in enumerate(segments):
        if k:
            parts.append(" ")
            offsets.append(segments[k - 1][1])
        low = text[a:b].lower()
        parts.append(low)
        offsets.extend(range(a, b) if len(low) == b - a else (min(a + i, b - 1) for i in range(len(low))))
    return "".join(parts), offsets


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Sentence ``(start, end)`` offsets into ``text``, whitespace-trimmed.

    Splits on line breaks, bullet glyphs, ordinary sentence ends and sentence ends glued to the
    next sentence without a space (which the filings' HTML flattening produces); never after an
    initial such as "U.S.".
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in SENTENCE_BREAK.finditer(text):
        _push_span(text, pos, match.start(), spans)
        pos = match.end()
    _push_span(text, pos, len(text), spans)
    return spans


def _push_span(text: str, start: int, end: int, spans: list[tuple[int, int]]) -> None:
    segment = text[start:end]
    stripped = segment.strip()
    if stripped:
        lead = len(segment) - len(segment.lstrip())
        spans.append((start + lead, start + lead + len(stripped)))


def sentence_texts(text: str) -> list[str]:
    return [text[a:b] for a, b in split_sentences(text)]


def word_tokens(text: str) -> tuple[str, ...]:
    """Lowercased word tokens (the unit ``lex_exact`` compares)."""
    return tuple(TOKEN_RE.findall(text.lower()))


@lru_cache(maxsize=8192)
def lex_exact(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """Word-level difflib ratio (greedy block matching), averaged over both argument orders."""
    if not a or not b:
        return 0.0
    forward = SequenceMatcher(None, a, b, autojunk=False).ratio()
    backward = SequenceMatcher(None, b, a, autojunk=False).ratio()
    return 0.5 * (forward + backward)


@dataclass(frozen=True)
class Hit:
    term: str
    score: float
    quote: str
    span: tuple[int, int]


class SectionIndex:
    """A section text prepared for verbatim quoting and whole-haystack fuzzy probing."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._norm, self._offsets = norm_with_offsets(text)
        self._spans = split_sentences(text)
        self._starts = [a for a, _ in self._spans]

    def probe(self, needle: str, min_ratio: float, max_quote: int, *, fuzzy: bool = True) -> Hit | None:
        """Best occurrence of one sentence-sized needle: exact after normalisation, else fuzzy."""
        needle_norm = norm(needle)
        if not needle_norm:
            return None
        pos = self._norm.find(needle_norm)
        if pos >= 0:
            start, end, score = pos, pos + len(needle_norm), 100.0
        elif fuzzy and len(self._norm) >= len(needle_norm):
            found = fuzz.partial_ratio_alignment(needle_norm, self._norm, score_cutoff=min_ratio)
            if found is None or found.score < min_ratio:
                return None
            start, end, score = found.dest_start, found.dest_end, float(found.score)
        else:
            return None
        quote, span = self._quote(start, end, max_quote)
        return Hit(needle, score, quote, span)

    def _quote(self, start: int, end: int, max_quote: int) -> tuple[str, tuple[int, int]]:
        """The whole sentence(s) around the matched normalised range, verbatim (capped)."""
        first, last = self._offsets[start], self._offsets[end - 1] + 1
        i = max(bisect_right(self._starts, first) - 1, 0)
        j = max(bisect_right(self._starts, last - 1) - 1, 0)
        q_start = min(self._spans[i][0], first) if self._spans else first
        q_end = max(self._spans[j][1], last) if self._spans else last
        if q_end - q_start > max_quote:
            q_start, q_end = first, min(last, first + max_quote)
        return self.text[q_start:q_end], (q_start, q_end)
