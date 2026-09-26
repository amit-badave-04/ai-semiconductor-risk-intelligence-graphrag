"""Passages: the change layer below the risk item (M1b plan L.2).

Item-level alignment (``graph/alignment.py``) says which risk items survive; it cannot say what
changed inside a surviving item (NVIDIA's single 26k-character export-control risk factor is
``reworded`` in both filings while its NAC sentence and its AI Diffusion IFR paragraphs disappear).
This module decomposes the items that changed into contiguous, verbatim ``Passage`` slices of each
filing's section text, each classified against the WHOLE other section:

* ``removed``  : a sentence of an older ``reworded`` / ``merged`` item that occurs nowhere in the
  newer section, not even reworded;
* ``reworded`` : the same, but a near counterpart exists (both sides quoted, similarity recorded);
* ``added``    : the mirror image, from the newer side: a sentence of a ``carried`` newer item that
  occurs nowhere in the older section and has no counterpart there.

Pure and deterministic (no I/O, no network, no LLM); the caller supplies rows, the alignment
result, the two section texts and the chunk spans.

Classification of one sentence (whitespace-trimmed sentences from ``split_sentences``; sentences
shorter than ``min_sentence_chars`` are headings or fragments and are never classified):

1. PRESENT: a near-verbatim occurrence anywhere in the other section, ``partial_ratio`` >=
   ``present_min_ratio`` on the first ``max_probe_chars`` characters. This is exactly the rule of
   ``alignment._dropped_sentences`` (default 85 / 600), so a tense-only edit ("impact" ->
   "impacted") is PRESENT and never reported, and the absent sentences of an item equal its
   ``Evidence.dropped_sentences`` (tested).
2. otherwise the best counterpart is the sentence of the other section with the highest word-level
   lexical similarity (``align_text.lex_exact``, the aligner's own lexical score; ties go to the
   earliest sentence). At or above ``reword_min`` the sentence is ``reworded``; below it ``removed``
   (older side) or ``added`` (newer side).

The lexical BAND (measured 2026-09-26 on the frozen development gold: precision 0.906 / recall 0.829 at
``reword_min`` 0.35). A low floor buys recall but creates FALSE counterparts: Nvidia's "we transitioned some operations
... out of China and Hong Kong" (gold: removed) matches an unrelated FY26 sentence about Hong Kong warehousing at
0.367 and is called reworded, while real paraphrases below the floor are called removed. Word overlap alone cannot tell the two
apart between ``reword_min`` and ``reword_confident`` (default 0.35 and 0.60), so a sentence whose best counterpart lies in
``[reword_min, reword_confident)`` is a BAND sentence, settled outside this module by a cheap model whose answer is checked
by code (``graph/passage_adjudicate.py``) and handed in as ``band_verdicts`` (band key -> ``BandVerdict``):

* no verdict: the sentence stays ``reworded`` (never a claim of removal or addition that cannot be supported), the passage is
  marked ``decided_by = "sentence_reworded_band"`` (newer side: the sentence is simply not ``added``, as before);
* verdict ``different``: the sentence is ``removed`` (older side) or ``added`` (newer side);
* verdict ``same`` with a VERIFIED counterpart span: ``reworded`` with exactly that span of the other section as counterpart
  (a ``same`` without a usable span is treated as no verdict). On the newer side ``same`` changes nothing (already not added);
* a verdict for a sentence that is not a band sentence under the current parameters is ignored.

A ``reword_confident`` of ``None`` (or one at or below ``reword_min``) means there is no band: exactly the classification above.
Enumeration (:func:`band_sentences`, :meth:`PairPassages.band_sentences`) lists every band sentence of a pair in older-then-
newer order with a stable key (item id + section offset), the hash of its text and of the other section (for caching) and its
candidate counterparts; the newer side is listed independently of what the older side quotes, so one enumeration covers every
verdict combination (an older ``different`` uncovers newer sentences that would otherwise be hidden behind a quoted counterpart).
Enumeration and classification share their code (``_in_band``, ``band_key``).

Runs: consecutive classified sentences of one kind in one item form one passage; a PRESENT sentence
(or, on the newer side, one whose rewording is already reported from the older side) ends the run,
so does a change of kind. A short sentence between two sentences of a run rides along inside the
verbatim slice but never starts or ends a passage. A run is closed before its slice would exceed
``max_passage_chars`` (cut at a sentence boundary; a single longer sentence stays whole). A
``reworded`` run only continues while the counterparts are adjacent (the same or the next sentence
of the other section), so ``counterpart_span`` is one contiguous slice; ``similarity`` is the
weakest sentence link of the run. Grouping does NOT depend on the band: a run mixes confident and band sentences, and its
``decided_by`` is the weakest provenance in it: ``sentence_reworded_band`` when any sentence has no verdict, else
``sentence_reworded_llm`` / ``sentence_absent_llm`` when any sentence was settled by a verdict, else the plain values;
``band_adjudicated`` is True when a verdict settled at least one sentence of the passage.

Decisions the plan did not specify (all covered by tests):

* Which items are decomposed. Older: decision ``reworded`` or ``merged`` (``unchanged`` has
  identical text, ``removed`` / ``uncertain`` belong to the item layer). Newer: ``carried`` unless
  its partner is ``unchanged``; ``new`` and ``uncertain`` items are never decomposed. A carried item
  without a resolvable partner id is decomposed too (its text was found in the older section).
* ``added`` suppression. The plan says a newer sentence with a counterpart "is already reported
  from the older side as reworded". That is applied literally: the older side runs first and a newer
  sentence is NOT added only when its section offset lies inside the ``counterpart_span`` of a
  reworded passage. A newer sentence that merely resembles an older sentence stays ``added`` when
  nothing reports it: the resembled older sentence survives verbatim elsewhere, sits in a
  ``removed`` / ``uncertain`` older item or outside every item, or is paired with another newer
  sentence. Measured on the six development pairs: 58 of the 390 added sentences (15%) resemble an
  older sentence at ``reword_min`` or more that nothing quotes.
* Ordering: older-side passages (removed and reworded) in older-filing item order then position,
  followed by added passages in newer-filing item order then position; ``seq`` is 0-based per item
  and kind.
* ``chunk_ids`` are the chunks of the passage's OWN filing whose half-open character range overlaps
  the passage (zero-length chunks are ignored), in text order.
* The item text must be a slice of its section starting at ``char_start`` (checked; a violation
  raises ``ValueError``) so that passage offsets are section-text offsets.

All thresholds are STARTING values (``PassageParams``), calibrated on the development gold before any number is reported.
Limitations: a sentence edited by ~15% or more of its words can be
absent although it is still there in edited form (the ``reworded`` class exists for that); the
probe looks only at the first ``max_probe_chars`` characters of a sentence, so an edit confined to
the tail of a longer sentence is not seen; a number-only update of a sentence ("12%" -> "22%")
falls below ``reword_min`` when the sentence is short and is then reported as removed plus added;
``reworded`` also holds when the counterpart is a sentence that already existed in the older filing.
"""

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Integral
from typing import Any, NamedTuple

import numpy as np
from rapidfuzz import fuzz, process
from rapidfuzz.utils import default_process
from rapidfuzz.distance import Indel

from ..hashing import content_hash
from .align_text import SectionIndex, at_least, in_range, lex_exact, norm, split_sentences, word_tokens
from .alignment import AlignmentResult

KINDS = ("removed", "reworded", "added")
KIND_CODE = {"removed": "r", "reworded": "w", "added": "a"}      # passage_id infix
DECIDED_BY = ("sentence_absent", "sentence_reworded", "sentence_reworded_band", "sentence_absent_llm", "sentence_reworded_llm")
BAND_VERDICTS = ("same", "different")
OLDER_DECOMPOSED = ("reworded", "merged")
OLDER, NEWER = "older", "newer"
_BOUND_SLACK = 1e-9        # the Indel similarity upper-bounds the difflib ratio; float slack only


@dataclass(frozen=True)
class PassageParams:
    """Every threshold of the passage layer. Chosen on the six DEVELOPMENT pairs of the frozen sentence gold
    (``scripts/tune_passages.py``; sha256 af1e810d): ``present_min_ratio`` 75 and ``reword_min`` 0.35 gave precision 0.906 /
    recall 0.829 of the removed+added sentences there (starting values 85 / 0.60 gave 0.75 / 0.90); the structural switches
    ``decompose_uncertain`` and ``suppress_added_with_counterpart`` are on. Held-out numbers: ``scripts/verify_temporal.py``.

    ``present_min_ratio``: ``partial_ratio`` (0-100) at which a sentence counts as still present in
    the other section (the aligner's ``absence_min_ratio``); ``max_probe_chars``: the sentence prefix
    that is probed (the aligner's ``max_term_chars``). ``reword_min``: word-level lexical similarity
    (0-1) from which an absent sentence with a counterpart is ``reworded`` rather than
    ``removed`` / ``added``. ``min_sentence_chars``: shorter sentences are never classified.
    ``max_passage_chars``: a passage is closed at a sentence boundary before exceeding it.
    ``decompose_uncertain``: items the aligner left ``uncertain`` (treated as present) are decomposed too.
    ``suppress_added_with_counterpart``: a newer sentence that has a counterpart (``reword_min``) in the older section
    is not ``added`` (the older side would call it ``reworded``), even when no older passage quoted it.
    ``partial_min`` (0-100, 0 = off): a sentence of the other section whose ``partial_ratio`` against the sentence is at
    least this value is ALSO a counterpart (paraphrases share too few words for ``reword_min`` but still contain a
    matching span; measured on this project's filings, unrelated sentence pairs of one filing peak at 61).

    ``reword_confident``: a best counterpart with word-level similarity from here up is confidently ``reworded``; one in
    ``[reword_min, reword_confident)`` (and every ``partial_min`` counterpart) is a BAND sentence that a model verdict may settle
    (module docstring). ``None``, or a value at or below ``reword_min``, means no band (the behaviour before the band existed;
    the legacy tests rely on both). ``band_candidates``: how many candidate sentences of the other section are listed per band
    sentence (the lexical best first, then alternately the best by word similarity and by ``partial_ratio``);
    ``max_candidate_chars``: a listed candidate is cut to this many characters (its span stays the whole sentence).
    """

    present_min_ratio: float = 75.0
    reword_min: float = 0.35
    min_sentence_chars: int = 40
    max_passage_chars: int = 450
    max_probe_chars: int = 600
    decompose_uncertain: bool = True
    suppress_added_with_counterpart: bool = True
    partial_min: float = 0.0
    reword_confident: float | None = 0.60
    band_candidates: int = 5
    max_candidate_chars: int = 1200

    def __post_init__(self) -> None:
        in_range("present_min_ratio", self.present_min_ratio, 0.0, 100.0, open_low=True)
        in_range("reword_min", self.reword_min, 0.0, 1.0, open_low=True)
        in_range("partial_min", self.partial_min, 0.0, 100.0)
        at_least("min_sentence_chars", self.min_sentence_chars, 1)
        at_least("max_passage_chars", self.max_passage_chars, self.min_sentence_chars)
        at_least("max_probe_chars", self.max_probe_chars, self.min_sentence_chars)
        if self.reword_confident is not None:
            in_range("reword_confident", self.reword_confident, 0.0, 1.0, open_low=True)
        at_least("band_candidates", self.band_candidates, 1)
        at_least("max_candidate_chars", self.max_candidate_chars, 100)

    @property
    def has_band(self) -> bool:
        return self.reword_confident is not None and self.reword_confident > self.reword_min


@dataclass(frozen=True)
class BandVerdict:
    """A settled answer for one band sentence (built by ``passage_adjudicate`` AFTER its code rules).

    ``same`` needs ``counterpart_span``: the verified ``(start, end)`` section-text range of the OTHER filing that states the
    same fact; a ``same`` without a usable span is ignored by :class:`PairPassages`. ``different``: no counterpart exists."""

    verdict: str
    counterpart_span: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.verdict not in BAND_VERDICTS:
            raise ValueError(f"verdict must be one of {BAND_VERDICTS}, got {self.verdict!r}")


@dataclass(frozen=True)
class BandCandidate:
    """One candidate counterpart of a band sentence: a verbatim sentence of the other filing (``text`` may be a cut prefix of
    the sentence at ``[start, end)``)."""

    text: str
    start: int
    end: int
    lex_sim: float             # word-level lexical similarity to the band sentence (0-1)
    partial: float             # rapidfuzz partial_ratio (0-100)


@dataclass(frozen=True)
class BandSentence:
    """A sentence whose best counterpart is in the lexical band (see the module docstring).

    ``key``: ``band_key(item_id, start)``; ``start`` / ``end`` / ``text`` refer to the sentence's OWN filing (``side``), as do
    ``text_hash`` (``hashing.content_hash`` of the text) and, for the OTHER filing's whole section, ``other_hash``.
    ``similarity`` / ``route``: the counterpart that put it in the band (``lexical`` word similarity, or ``partial`` for a
    ``partial_min`` counterpart, whose score is a ``partial_ratio`` / 100)."""

    key: str
    side: str
    item_id: str
    start: int
    end: int
    text: str
    text_hash: str
    other_hash: str
    similarity: float
    route: str
    candidates: tuple[BandCandidate, ...]


@dataclass(frozen=True)
class Passage:
    """A contiguous, verbatim slice of one filing's section text that changed (see module docstring).

    ``item_id``: the older item for ``removed`` / ``reworded``, the NEWER item for ``added``.
    ``char_start`` / ``char_end`` and ``text`` refer to that item's own filing; ``counterpart_*``
    (``reworded`` only) to the other filing's section. ``similarity`` is the weakest sentence link.
    ``band_adjudicated``: a model verdict settled at least one sentence of the passage.
    """

    passage_id: str
    kind: str
    item_id: str
    seq: int
    text: str
    char_start: int
    char_end: int
    counterpart_text: str | None
    counterpart_span: tuple[int, int] | None
    similarity: float | None
    chunk_ids: tuple[str, ...]
    decided_by: str
    band_adjudicated: bool = False


def band_key(item_id: str, section_offset: int) -> str:
    """The stable key of a band sentence: its item id and its section-text offset (unique per sentence of a filing)."""
    return f"{item_id}@{section_offset}"


class _Cp(NamedTuple):
    """The best counterpart of a sentence in the other section: sentence index, similarity, found by the lexical score?"""

    idx: int
    sim: float
    lexical: bool


@dataclass(frozen=True)
class _Sentence:
    start: int                   # offsets inside the item text
    end: int
    kind: str | None             # removed | reworded | added; None = present / already reported elsewhere
    cp: int | None = None        # reworded: index of the first counterpart sentence in the other section
    cp_end: int | None = None    # reworded: index of the last counterpart sentence (== cp for a single sentence)
    sim: float | None = None
    unresolved: bool = False     # reworded only because it is a band sentence without a verdict
    adjudicated: bool = False    # a model verdict settled this sentence


def _in_band(params: PassageParams, hit: _Cp | None) -> bool:
    """Is this counterpart a band counterpart? A ``partial_min`` counterpart has no word-similarity guarantee: always band."""
    return hit is not None and params.has_band and (not hit.lexical or hit.sim < params.reword_confident)


class _Filing:
    """One section text prepared for near-verbatim probing, counterpart search and chunk mapping."""

    def __init__(self, text: str, chunk_spans: Sequence[tuple[str, int, int]], params: PassageParams) -> None:
        if not isinstance(text, str):
            raise TypeError("section text must be a string")
        self.text = text
        self.params = params
        self.spans = split_sentences(text)
        self._index = SectionIndex(text)
        self._tokens = [word_tokens(text[a:b]) for a, b in self.spans]
        self._texts = [text[a:b] for a, b in self.spans]
        self._starts = [a for a, _ in self.spans]
        self._ends = [b for _, b in self.spans]
        self._chunks = _read_chunks(chunk_spans)
        self._present: dict[str, bool] = {}
        self._counterparts: dict[str, _Cp | None] = {}
        self._eligible_idx: list[int] | None = None
        self._hash: str | None = None

    @property
    def content_hash(self) -> str:
        if self._hash is None:
            self._hash = content_hash(self.text)
        return self._hash

    def contains(self, sentence: str) -> bool:
        """Near-verbatim occurrence of ``sentence`` anywhere in this section (the aligner's absence rule)."""
        found = self._present.get(sentence)
        if found is None:
            p = self.params
            found = self._index.probe(sentence[:p.max_probe_chars], p.present_min_ratio, p.max_probe_chars) is not None
            self._present[sentence] = found
        return found

    def counterpart(self, sentence: str) -> _Cp | None:
        """Best sentence of this section by word-level lexical similarity, if at least ``reword_min``.

        Exact: candidates are visited by descending Indel bound (an upper bound of the difflib ratio)
        and the walk stops when no remaining candidate can beat the best; ties go to the earliest.
        """
        if sentence not in self._counterparts:
            self._counterparts[sentence] = self._find_counterpart(sentence)
        return self._counterparts[sentence]

    def _find_counterpart(self, sentence: str) -> _Cp | None:
        tokens = word_tokens(sentence)
        if not tokens or not self._tokens:
            return None
        lexical = self._lexical_counterpart(tokens)
        if lexical is not None:
            return _Cp(lexical[0], lexical[1], True)
        if not self.params.partial_min:
            return None
        return self._partial_counterpart(sentence)

    def _partial_counterpart(self, sentence: str) -> _Cp | None:
        """Best sentence by ``partial_ratio`` (0-100) when at least ``partial_min``; the score is returned on 0-1."""
        hit = process.extractOne(sentence, self._texts, scorer=fuzz.partial_ratio, processor=default_process,
                                 score_cutoff=self.params.partial_min)
        return None if hit is None else _Cp(int(hit[2]), float(hit[1]) / 100.0, False)

    def _lexical_counterpart(self, tokens: tuple[str, ...]) -> tuple[int, float] | None:
        bound = process.cdist([tokens], self._tokens, scorer=Indel.normalized_similarity,
                              dtype=np.float64, workers=1)[0]
        floor = self.params.reword_min
        best_idx, best = -1, 0.0
        for j in np.argsort(-bound, kind="stable"):
            if bound[j] < max(floor, best) - _BOUND_SLACK:
                break
            sim = lex_exact(tokens, self._tokens[j])
            if sim >= floor and (sim > best or (sim == best and j < best_idx)):
                best_idx, best = int(j), sim
        return None if best_idx < 0 else (best_idx, best)

    # --- verified counterparts and candidates -------------------------------------------------------------------

    def sentence_range(self, span: tuple[int, int]) -> tuple[int, int] | None:
        """Indices ``(first, last)`` of the sentences that overlap the section range ``span``; None for an unusable range."""
        try:
            start, end = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not 0 <= start < end <= len(self.text):
            return None
        first, last = bisect_right(self._ends, start), bisect_left(self._starts, end) - 1
        return (first, last) if first <= last < len(self.spans) else None

    def similarity(self, sentence: str, first: int, last: int) -> float:
        """``partial_ratio`` / 100 of ``sentence`` against the sentences ``first..last`` of this section."""
        other = self.text[self._starts[first]:self._ends[last]]
        return float(fuzz.partial_ratio(sentence, other, processor=default_process)) / 100.0

    def _eligible(self) -> list[int]:
        if self._eligible_idx is None:
            floor = self.params.min_sentence_chars
            self._eligible_idx = [i for i, t in enumerate(self._texts) if len(t) >= floor]
        return self._eligible_idx

    def _rank_lexical(self, tokens: tuple[str, ...], count: int) -> list[int]:
        eligible = self._eligible()
        if not tokens or not eligible:
            return []
        bound = process.cdist([tokens], [self._tokens[i] for i in eligible], scorer=Indel.normalized_similarity,
                              dtype=np.float64, workers=1)[0]
        pool = np.argsort(-bound, kind="stable")[:2 * count]
        scored = sorted(((lex_exact(tokens, self._tokens[eligible[j]]), eligible[j]) for j in pool), key=lambda t: (-t[0], t[1]))
        return [i for _, i in scored[:count]]

    def _rank_partial(self, sentence: str, count: int) -> list[int]:
        eligible = self._eligible()
        if not eligible:
            return []
        found = process.extract(sentence, [self._texts[i] for i in eligible], scorer=fuzz.partial_ratio,
                                processor=default_process, limit=None)
        ranked = sorted(found, key=lambda r: (-r[1], eligible[r[2]]))
        return [eligible[r[2]] for r in ranked[:count]]

    def candidates(self, sentence: str, hit: _Cp) -> tuple[BandCandidate, ...]:
        """The counterpart candidates of a band sentence: the counterpart that decided the band first, then alternately the best
        by word similarity and by ``partial_ratio`` (sentences shorter than ``min_sentence_chars`` are only listed when they are
        that counterpart), de-duplicated, at most ``band_candidates``."""
        p, tokens = self.params, word_tokens(sentence)
        by_words, by_partial = self._rank_lexical(tokens, p.band_candidates), self._rank_partial(sentence, p.band_candidates)
        order = [hit.idx]
        for k in range(max(len(by_words), len(by_partial))):
            order += by_words[k:k + 1] + by_partial[k:k + 1]
        chosen, seen = [], set()
        for i in order:                       # one entry per distinct text (a heading can occur twice in a section)
            text_key = norm(self._texts[i])
            if text_key not in seen and len(chosen) < p.band_candidates:
                seen.add(text_key)
                chosen.append(i)
        return tuple(
            BandCandidate(self._texts[i][:p.max_candidate_chars], self._starts[i], self._ends[i], lex_exact(tokens, self._tokens[i]),
                          float(fuzz.partial_ratio(sentence, self._texts[i], processor=default_process)))
            for i in chosen)

    def chunk_ids(self, start: int, end: int) -> tuple[str, ...]:
        return tuple(cid for cs, ce, cid in self._chunks if cs < end and ce > start)


def _read_chunks(chunk_spans: Sequence[tuple[str, int, int]]) -> list[tuple[int, int, str]]:
    chunks = []
    for span in chunk_spans:
        try:
            cid, start, end = span
            entry = (int(start), int(end), str(cid))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"chunk spans must be (chunk_id, char_start, char_end) triples, got {span!r}") from exc
        if entry[1] > entry[0]:
            chunks.append(entry)
    return sorted(chunks)


def _read_item(row: Mapping[str, Any], section: str) -> tuple[str, str, int]:
    """(item_id, text, char_start) of a row whose text is verified to be a slice of ``section``."""
    item_id, text, start = row.get("item_id"), row.get("text"), row.get("char_start")
    if not isinstance(item_id, str) or not item_id:
        raise ValueError("item row has no usable 'item_id'")
    if not isinstance(text, str):
        raise ValueError(f"item {item_id!r} has no 'text'")
    if isinstance(start, bool) or not isinstance(start, Integral):
        raise ValueError(f"item {item_id!r} has no integer 'char_start' (section-text offset)")
    start = int(start)
    if start < 0 or section[start:start + len(text)] != text:
        raise ValueError(f"item {item_id!r}: text is not a slice of its section at char_start={start}")
    return item_id, text, start


class _Spans:
    """Section-text ranges already quoted as the counterpart of a reworded passage."""

    def __init__(self, spans: Sequence[tuple[int, int]]) -> None:
        merged: list[list[int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        self._starts = [m[0] for m in merged]
        self._ends = [m[1] for m in merged]

    def covers(self, pos: int) -> bool:
        i = bisect_right(self._starts, pos) - 1
        return i >= 0 and pos < self._ends[i]


def _older_sentence(item_id: str, a: int, b: int, sentence: str, base: int, other: _Filing,
                    verdicts: Mapping[str, BandVerdict]) -> _Sentence:
    """An older sentence that is not present in the newer section: removed, reworded, or a band sentence settled by a verdict."""
    hit = other.counterpart(sentence)
    if hit is None:
        return _Sentence(a, b, "removed")
    if not _in_band(other.params, hit):
        return _Sentence(a, b, "reworded", hit.idx, hit.idx, hit.sim)
    verdict = verdicts.get(band_key(item_id, base + a))
    if verdict is not None and verdict.verdict == "different":
        return _Sentence(a, b, "removed", adjudicated=True)
    span = other.sentence_range(verdict.counterpart_span) if verdict is not None and verdict.counterpart_span else None
    if span is not None:
        return _Sentence(a, b, "reworded", span[0], span[1], other.similarity(sentence, *span), adjudicated=True)
    return _Sentence(a, b, "reworded", hit.idx, hit.idx, hit.sim, unresolved=True)


def _newer_sentence(item_id: str, a: int, b: int, sentence: str, base: int, other: _Filing, reported: _Spans,
                    verdicts: Mapping[str, BandVerdict]) -> _Sentence:
    """A newer sentence that is not present in the older section: added, unless the older side already quotes it or it has a
    counterpart (a band counterpart only when no ``different`` verdict says the counterpart is another fact)."""
    if reported.covers(base + a):
        return _Sentence(a, b, None)
    if not other.params.suppress_added_with_counterpart:
        return _Sentence(a, b, "added")
    hit = other.counterpart(sentence)
    if hit is None:
        return _Sentence(a, b, "added")
    verdict = verdicts.get(band_key(item_id, base + a)) if _in_band(other.params, hit) else None
    if verdict is not None and verdict.verdict == "different":
        return _Sentence(a, b, "added", adjudicated=True)
    return _Sentence(a, b, None)


def _classify(item_id: str, text: str, base: int, side: str, own: _Filing, other: _Filing, reported: _Spans | None,
              verdicts: Mapping[str, BandVerdict]) -> list[_Sentence]:
    """Classify every long-enough sentence of an item against the whole other section.

    Older side: absent sentences are ``reworded`` with their best counterpart or ``removed``. Newer side: an absent sentence is
    ``added`` unless the older side already quotes it (its section offset lies in a reworded passage's ``counterpart_span``),
    which is exactly "already reported from the older side as reworded"."""
    out: list[_Sentence] = []
    for a, b in split_sentences(text):
        sentence = text[a:b]
        if len(sentence) < own.params.min_sentence_chars:
            continue
        if other.contains(sentence):
            out.append(_Sentence(a, b, None))
        elif side == OLDER:
            out.append(_older_sentence(item_id, a, b, sentence, base, other, verdicts))
        else:
            out.append(_newer_sentence(item_id, a, b, sentence, base, other, reported, verdicts))
    return out


def _extends(run: list[_Sentence], nxt: _Sentence, params: PassageParams) -> bool:
    last = run[-1]
    if nxt.kind != last.kind or nxt.end - run[0].start > params.max_passage_chars:
        return False
    return nxt.kind != "reworded" or 0 <= nxt.cp - last.cp_end <= 1


def _runs(sentences: Sequence[_Sentence], params: PassageParams) -> list[list[_Sentence]]:
    runs: list[list[_Sentence]] = []
    current: list[_Sentence] = []
    for s in sentences:
        if s.kind is None:
            current = []
        elif current and _extends(current, s, params):
            current.append(s)
        else:
            current = [s]
            runs.append(current)
    return runs


def _decided_by(kind: str, run: Sequence[_Sentence]) -> str:
    """The weakest provenance of a run (module docstring)."""
    adjudicated = any(s.adjudicated for s in run)
    if kind == "reworded":
        if any(s.unresolved for s in run):
            return "sentence_reworded_band"
        return "sentence_reworded_llm" if adjudicated else "sentence_reworded"
    return "sentence_absent_llm" if adjudicated else "sentence_absent"


def _passage(run: list[_Sentence], item_id: str, seq: int, text: str, base: int, own: _Filing,
             other: _Filing) -> Passage:
    kind = run[0].kind
    start, end = base + run[0].start, base + run[-1].end
    counterpart_text = counterpart_span = similarity = None
    if kind == "reworded":
        counterpart_span = (other.spans[run[0].cp][0], other.spans[run[-1].cp_end][1])
        counterpart_text = other.text[counterpart_span[0]:counterpart_span[1]]
        similarity = min(s.sim for s in run)
    return Passage(
        passage_id=f"{item_id}:{KIND_CODE[kind]}{seq:03d}", kind=kind, item_id=item_id, seq=seq,
        text=text[run[0].start:run[-1].end], char_start=start, char_end=end,
        counterpart_text=counterpart_text, counterpart_span=counterpart_span, similarity=similarity,
        chunk_ids=own.chunk_ids(start, end), decided_by=_decided_by(kind, run),
        band_adjudicated=any(s.adjudicated for s in run))


def _item_passages(item: tuple[str, str, int], side: str, own: _Filing, other: _Filing, reported: _Spans | None,
                   verdicts: Mapping[str, BandVerdict]) -> list[Passage]:
    item_id, text, base = item
    sentences = _classify(item_id, text, base, side, own, other, reported, verdicts)
    seq: Counter[str] = Counter()
    out = []
    for run in _runs(sentences, own.params):
        out.append(_passage(run, item_id, seq[run[0].kind], text, base, own, other))
        seq[run[0].kind] += 1
    return out


def _select(rows: Sequence[Mapping[str, Any]], decisions: Mapping[str, Any], side: str,
            wanted) -> list[Mapping[str, Any]]:
    """The rows (in filing order) whose decision is to be decomposed; every such decision needs a row."""
    row_ids = {row.get("item_id") for row in rows}
    missing = sorted(i for i, d in decisions.items() if wanted(d) and i not in row_ids)
    if missing:
        raise ValueError(f"{side} decision(s) without an item row: {missing}")
    return [row for row in rows if row.get("item_id") in decisions and wanted(decisions[row["item_id"]])]


class PairPassages:
    """The passage layer of ONE pair of consecutive filings, prepared once so that the band sentences can be listed and the
    passages computed (with or without verdicts) from the same classification work.

    Arguments are those of :func:`compute_passages` without the verdicts. ``*_items``: rows with ``item_id``, ``text`` and
    ``char_start`` (the section-text offset of the item; the text must be the slice of its section from there), in filing order.
    ``result``: the ``align`` output (after any item-level adjudication) for the same items. ``*_chunk_spans``:
    ``(chunk_id, char_start, char_end)`` of the chunks of that filing's section (what ``parsing.risk_items`` maps items with).
    """

    def __init__(self, older_items: Sequence[Mapping[str, Any]], newer_items: Sequence[Mapping[str, Any]],
                 result: AlignmentResult, older_section_text: str, newer_section_text: str,
                 older_chunk_spans: Sequence[tuple[str, int, int]] = (),
                 newer_chunk_spans: Sequence[tuple[str, int, int]] = (),
                 params: PassageParams = PassageParams()) -> None:
        self.params = params
        self._older = _Filing(older_section_text, older_chunk_spans, params)
        self._newer = _Filing(newer_section_text, newer_chunk_spans, params)
        old_by_id = {d.item_id: d for d in result.older}
        new_by_id = {d.item_id: d for d in result.newer}

        def older_wanted(d) -> bool:
            return d.label in OLDER_DECOMPOSED or (params.decompose_uncertain and d.label == "uncertain")

        def newer_wanted(d) -> bool:
            partner = old_by_id.get(d.matched_older_id)
            if params.decompose_uncertain and d.label == "uncertain":
                return True
            return d.label == "carried" and not (partner is not None and partner.label == "unchanged")

        self._older_items = [_read_item(row, older_section_text)
                             for row in _select(older_items, old_by_id, "older", older_wanted)]
        self._newer_items = [_read_item(row, newer_section_text)
                             for row in _select(newer_items, new_by_id, "newer", newer_wanted)]

    def passages(self, band_verdicts: Mapping[str, BandVerdict] | None = None) -> tuple[Passage, ...]:
        """Removed / reworded / added passages, in the order of the module docstring. ``band_verdicts``: band key -> verdict."""
        verdicts = band_verdicts or {}
        out: list[Passage] = []
        for item in self._older_items:
            out.extend(_item_passages(item, OLDER, self._older, self._newer, None, verdicts))
        reported = _Spans([p.counterpart_span for p in out if p.kind == "reworded"])
        for item in self._newer_items:
            out.extend(_item_passages(item, NEWER, self._newer, self._older, reported, verdicts))
        return tuple(out)

    def band_sentences(self, *, candidates: bool = True) -> tuple[BandSentence, ...]:
        """Every band sentence of the pair: older side first (item order, then text order), then the newer side. The newer side
        is listed whatever the older side quotes (module docstring); it needs ``suppress_added_with_counterpart`` because
        otherwise a newer sentence with a counterpart is added anyway and there is nothing to settle. ``candidates=False`` skips
        the (costly) candidate search when only keys, hashes and texts are needed (replaying recorded answers)."""
        out: list[BandSentence] = []
        sides = [(OLDER, self._older_items, self._older, self._newer)]
        if self.params.suppress_added_with_counterpart:
            sides.append((NEWER, self._newer_items, self._newer, self._older))
        for side, items, own, other in sides:
            for item_id, text, base in items:
                for a, b in split_sentences(text):
                    sentence = text[a:b]
                    if len(sentence) < self.params.min_sentence_chars or other.contains(sentence):
                        continue
                    hit = other.counterpart(sentence)
                    if not _in_band(self.params, hit):
                        continue
                    out.append(BandSentence(
                        key=band_key(item_id, base + a), side=side, item_id=item_id, start=base + a, end=base + b,
                        text=sentence, text_hash=content_hash(sentence), other_hash=other.content_hash,
                        similarity=hit.sim, route="lexical" if hit.lexical else "partial",
                        candidates=other.candidates(sentence, hit) if candidates else ()))
        return tuple(out)


def compute_passages(older_items: Sequence[Mapping[str, Any]], newer_items: Sequence[Mapping[str, Any]],
                     result: AlignmentResult, older_section_text: str, newer_section_text: str,
                     older_chunk_spans: Sequence[tuple[str, int, int]] = (),
                     newer_chunk_spans: Sequence[tuple[str, int, int]] = (),
                     params: PassageParams = PassageParams(),
                     band_verdicts: Mapping[str, BandVerdict] | None = None) -> tuple[Passage, ...]:
    """Removed / reworded / added passages of the items that changed between two consecutive filings.

    ``*_items``: rows with ``item_id``, ``text`` and ``char_start`` (the section-text offset of the
    item; the text must be the slice of its section from there), in filing order. ``result``: the
    ``align`` output for the same items. ``*_chunk_spans``: ``(chunk_id, char_start, char_end)`` of
    the chunks of that filing's section (what ``parsing.risk_items`` maps items with).
    ``band_verdicts``: settled answers for band sentences, band key -> :class:`BandVerdict` (module docstring); without them
    every band sentence stays ``reworded`` (never removed or added).
    """
    return PairPassages(older_items, newer_items, result, older_section_text, newer_section_text,
                        older_chunk_spans, newer_chunk_spans, params).passages(band_verdicts)


def band_sentences(older_items: Sequence[Mapping[str, Any]], newer_items: Sequence[Mapping[str, Any]],
                   result: AlignmentResult, older_section_text: str, newer_section_text: str,
                   params: PassageParams = PassageParams()) -> tuple[BandSentence, ...]:
    """The band sentences of a pair (pure; same inputs as :func:`compute_passages`; chunk spans are not needed)."""
    return PairPassages(older_items, newer_items, result, older_section_text, newer_section_text,
                        params=params).band_sentences()


def summarize_passages(passages: Sequence[Passage]) -> dict:
    """Counts per kind and per item, plus the total passage characters."""
    by_kind = Counter(p.kind for p in passages)
    per_item: dict[str, Counter[str]] = {}
    for p in passages:
        per_item.setdefault(p.item_id, Counter())[p.kind] += 1
    return {
        "total": len(passages),
        "by_kind": {kind: by_kind.get(kind, 0) for kind in KINDS},
        "by_item": {item: {k: c[k] for k in KINDS if c[k]} for item, c in per_item.items()},
        "chars": sum(len(p.text) for p in passages),
    }
