"""Structural units and token-bounded chunks over parsed upload blocks (M4_PLAN.md 4.2, 14).

``canonical_text`` flattens a :class:`~semigraph.uploads.parse.Block` sequence into the one string everything
else (units, chunks, the change report) works against. ``detect_units`` groups it into heading-led SECTIONS — or,
when the document has too little heading structure to trust, one PARAGRAPH unit per block — after a mandatory
running header/footer filter (a line repeated across most pages is never a heading candidate). ``chunk_units``
slices the canonical text into token-bounded, unit-aware chunks for embedding. ``unit_rows`` shapes units into the
row form ``graph.alignment.align`` and ``graph.passages.compute_passages`` take (item_id / text / headline /
text_hash / unit_kind / char_start): read those two modules before changing this one.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..graph.align_text import split_sentences
from ..hashing import content_hash

if TYPE_CHECKING:
    from .parse import Block

HEADING = "heading"
PARAGRAPH = "paragraph"
UNIT_KINDS = (HEADING, PARAGRAPH)

# kind_hint values that are an unambiguous structural heading signal (Markdown '#', HTML h1-h6, DOCX "Heading N"
# style): these skip the generic font-based test entirely. "table" is body content (a cell) and is NEVER
# heading-eligible, even when bold (an HTML <th> is bold by default) — only "paragraph" goes through the generic
# font-based test. "table" still counts toward the body-size statistic (visually part of the body).
_STRUCTURAL_HEADING_HINTS = frozenset({"heading_md", "heading_html", "heading_style"})
_BODY_MODE_HINTS = frozenset({"paragraph", "table"})

_ALL_DIGITS_RE = re.compile(r"^[\d\s]+$")
_WORD_RE = re.compile(r"\S+")
_DIGIT_RE = re.compile(r"\d+")
_SEPARATOR = "\n\n"                                  # canonical_text's own join string

DEFAULT_MAX_TOKENS = 512
DEFAULT_TARGET_CHARS = 1200
DEFAULT_MAX_CHARS = 1800
MIN_HEADINGS_FOR_SECTIONS = 3
HEADER_FOOTER_MIN_PAGES = 3
HEADER_FOOTER_MIN_SHARE = 0.5
HEADER_FOOTER_MAX_WORDS = 15                         # a real heading rarely repeats verbatim; a running title does


@dataclass(frozen=True)
class Unit:
    """One structural unit of a parsed document: ``kind`` is ``heading`` (a section: ``headline`` + its body, up
    to the next heading) or ``paragraph`` (a preamble before the first heading, or — when the document does not
    have enough heading structure — one unit per block). ``char_start`` / ``char_end`` index ``canonical_text``."""

    unit_id: str
    kind: str
    headline: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class Chunk:
    """An embedding-sized, exact slice of the canonical text: ``text[char_start:char_end]``."""

    seq: int
    char_start: int
    char_end: int
    tokens: int


def canonical_text(blocks: Sequence["Block"]) -> str:
    """``blocks`` joined with two newlines (``hashing.content_hash`` does its own whitespace normalisation)."""
    return _SEPARATOR.join(b.text for b in blocks)


# --------------------------------------------------------------------------
# running header / footer filter
# --------------------------------------------------------------------------

def _normalize_line(text: str) -> str:
    """Whitespace-collapsed, casefolded, digit-blind form, so "Page 3 of 30" matches "Page 4 of 30"."""
    return _DIGIT_RE.sub("#", " ".join(text.split())).strip().lower()


def _page_edge_indices(blocks: Sequence["Block"]) -> frozenset[int]:
    """Indices of the FIRST and LAST block of each page — where a running header/footer actually lives. A
    numbered mid-page heading ("Note 1" ... "Note 12") never qualifies, however often its digit-blind form
    repeats, because it never sits at a page edge."""
    first: dict[int, int] = {}
    last: dict[int, int] = {}
    for i, b in enumerate(blocks):
        first.setdefault(b.page, i)
        last[b.page] = i
    return frozenset(first.values()) | frozenset(last.values())


def _boilerplate_block_indices(blocks: Sequence["Block"]) -> frozenset[int]:
    """Indices of page-edge blocks, <= :data:`HEADER_FOOTER_MAX_WORDS` words, whose normalised text repeats on
    >= 50% of the document's pages (>= 3 pages total)."""
    pages = {b.page for b in blocks}
    if len(pages) < HEADER_FOOTER_MIN_PAGES:
        return frozenset()
    edge_indices = _page_edge_indices(blocks)
    candidates = {i for i in edge_indices if len(_WORD_RE.findall(blocks[i].text)) <= HEADER_FOOTER_MAX_WORDS}
    pages_seen: dict[str, set[int]] = {}
    for i in candidates:
        norm = _normalize_line(blocks[i].text)
        if norm:
            pages_seen.setdefault(norm, set()).add(blocks[i].page)
    threshold = max(HEADER_FOOTER_MIN_PAGES, HEADER_FOOTER_MIN_SHARE * len(pages))
    boilerplate = {norm for norm, seen in pages_seen.items() if len(seen) >= threshold}
    return frozenset(i for i in candidates if _normalize_line(blocks[i].text) in boilerplate)


# --------------------------------------------------------------------------
# heading detection
# --------------------------------------------------------------------------

def _body_mode_size(blocks: Sequence["Block"], skip: frozenset[int]) -> float:
    """The most common block size among non-heading-hinted, non-boilerplate blocks (0.0 if there are none)."""
    sizes = Counter(round(b.size, 1) for i, b in enumerate(blocks)
                    if i not in skip and b.kind_hint in _BODY_MODE_HINTS and b.size > 0)
    return sizes.most_common(1)[0][0] if sizes else 0.0


def _looks_like_heading(text: str) -> bool:
    """<= 12 words, no terminal period, not all digits (the generic font-based test's non-font guards)."""
    stripped = text.strip()
    if not stripped or stripped.endswith("."):
        return False
    if _ALL_DIGITS_RE.match(stripped):
        return False
    return len(_WORD_RE.findall(stripped)) <= 12


def _is_heading(block: "Block", body_mode: float) -> bool:
    if block.kind_hint in _STRUCTURAL_HEADING_HINTS:
        return True
    if block.kind_hint != PARAGRAPH:                 # "table" (a cell) is never heading-eligible, however bold
        return False
    return (block.size >= body_mode + 1.0 or block.bold) and _looks_like_heading(block.text)


def _unit_id(seq: int, text: str) -> str:
    return f"u{seq:04d}{content_hash(text)[:10]}"


# --------------------------------------------------------------------------
# detect_units
# --------------------------------------------------------------------------

def _block_starts(blocks: Sequence["Block"]) -> list[int]:
    starts, pos = [], 0
    for b in blocks:
        starts.append(pos)
        pos += len(b.text) + len(_SEPARATOR)
    return starts


def _paragraph_units(blocks: Sequence["Block"], starts: Sequence[int], skip: frozenset[int]) -> tuple[Unit, ...]:
    units: list[Unit] = []
    for i, b in enumerate(blocks):
        if i in skip or not b.text.strip():
            continue
        start = starts[i]
        units.append(Unit(_unit_id(len(units), b.text), PARAGRAPH, "", start, start + len(b.text)))
    return tuple(units)


def _section_units(blocks: Sequence["Block"], starts: Sequence[int], text_len: int,
                   is_heading: Sequence[bool]) -> tuple[Unit, ...]:
    heading_idx = [i for i, h in enumerate(is_heading) if h]
    units: list[Unit] = []
    if heading_idx[0] > 0:
        preamble_end = starts[heading_idx[0]] - len(_SEPARATOR)
        if preamble_end > starts[0]:
            units.append(Unit(_unit_id(0, ""), PARAGRAPH, "", starts[0], preamble_end))
    for k, i in enumerate(heading_idx):
        start = starts[i]
        end = starts[heading_idx[k + 1]] - len(_SEPARATOR) if k + 1 < len(heading_idx) else text_len
        headline = blocks[i].text.strip()
        units.append(Unit(_unit_id(len(units), headline), HEADING, headline, start, end))
    return tuple(units)


def detect_units(blocks: Sequence["Block"], kind: str) -> tuple[Unit, ...]:
    """Units of ``canonical_text(blocks)`` (the SAME ``blocks``, unfiltered): heading-led sections, or one
    paragraph unit per block when fewer than :data:`MIN_HEADINGS_FOR_SECTIONS` headings survive the running
    header/footer filter (module docstring). ``kind`` (``pdf`` / ``docx`` / ``html`` / ``md`` / ``txt``) is
    accepted for callers that branch on it; detection itself only looks at each block's own fields."""
    if not blocks:
        return ()
    boilerplate = _boilerplate_block_indices(blocks)
    body_mode = _body_mode_size(blocks, boilerplate)
    is_heading = [False if i in boilerplate else _is_heading(b, body_mode) for i, b in enumerate(blocks)]
    starts = _block_starts(blocks)
    text_len = starts[-1] + len(blocks[-1].text)
    if sum(is_heading) < MIN_HEADINGS_FOR_SECTIONS:
        return _paragraph_units(blocks, starts, boilerplate)
    return _section_units(blocks, starts, text_len, is_heading)


# --------------------------------------------------------------------------
# chunk_units
# --------------------------------------------------------------------------

def _chunk_boundaries(text: str, units: Sequence[Unit]) -> list[tuple[int, int]]:
    """Non-overlapping spans covering all of ``text``; an uncovered gap between units (filtered boilerplate, or a
    document with no units at all) becomes its own span so nothing is dropped from chunking."""
    if not units:
        return [(0, len(text))] if text else []
    spans = sorted({(u.char_start, u.char_end) for u in units if u.char_end > u.char_start})
    out: list[tuple[int, int]] = []
    pos = 0
    for start, end in spans:
        if start > pos:
            out.append((pos, start))
        out.append((max(start, pos), end))
        pos = max(pos, end)
    if pos < len(text):
        out.append((pos, len(text)))
    return out


def _too_big(text: str, start: int, end: int, count_tokens: Callable[[str], int],
            max_tokens: int, max_chars: int) -> bool:
    if end <= start:
        return False
    if end - start > max_chars:
        return True
    return count_tokens(text[start:end]) > max_tokens


def _make_chunk(seq: int, start: int, end: int, text: str, count_tokens: Callable[[str], int]) -> Chunk:
    return Chunk(seq=seq, char_start=start, char_end=end, tokens=count_tokens(text[start:end]))


def _snap_to_boundary(text: str, start: int, best: int, end: int) -> int:
    """The LAST sentence boundary at or before ``best``, else the last whitespace, else ``best`` itself (a hard
    cut; guarantees termination).

    Sentence spans are computed over the WHOLE remaining unit (``text[start:end]``), never over ``text[start:best]``
    alone: ``split_sentences`` always closes its final span at the end of whatever text it is given (an
    unterminated trailing fragment becomes a "sentence" too), so truncating at ``best`` first would make an
    artifact of that truncation indistinguishable from a real sentence end. Computed against the full remaining
    text, every span but (possibly) the very last one is a genuine ``SENTENCE_BREAK`` match, and only spans ending
    at or before ``best`` are ever considered — so the artificial final span is excluded whenever a real cut is
    actually needed (``end > best``, i.e. more text remains than fits)."""
    boundaries = [start + b for _, b in split_sentences(text[start:end]) if start + b <= best]
    if boundaries and boundaries[-1] > start:
        return boundaries[-1]
    ws = max(text.rfind(" ", start, best), text.rfind("\n", start, best))
    return ws + 1 if ws > start else best


def _cut_point(text: str, start: int, end: int, count_tokens: Callable[[str], int],
              max_tokens: int, max_chars: int) -> int:
    """The furthest cut in ``(start, end]`` that fits both maxima, snapped to a sentence/whitespace boundary."""
    limit = min(end, start + max_chars)
    if limit <= start + 1:
        return max(start + 1, limit)
    lo, hi, best = start + 1, limit, start + 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if count_tokens(text[start:mid]) <= max_tokens:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return _snap_to_boundary(text, start, best, end)


def _drop_trailing_gap_chunk(chunks: list[Chunk], text: str, count_tokens: Callable[[str], int]) -> list[Chunk]:
    """Merges a final all-whitespace chunk into the one before it (a trailing filtered header/footer with nothing
    after it can otherwise end up as its own near-empty chunk; every other gap is absorbed during packing)."""
    if len(chunks) < 2 or text[chunks[-1].char_start:chunks[-1].char_end].strip():
        return chunks
    prev, last = chunks[-2], chunks[-1]
    return chunks[:-2] + [_make_chunk(prev.seq, prev.char_start, last.char_end, text, count_tokens)]


def chunk_units(text: str, units: Sequence[Unit], *, count_tokens: Callable[[str], int],
                max_tokens: int = DEFAULT_MAX_TOKENS, target_chars: int = DEFAULT_TARGET_CHARS,
                max_chars: int = DEFAULT_MAX_CHARS) -> tuple[Chunk, ...]:
    """Exact, token-bounded slices of ``text`` (never above ``max_tokens``, target ``target_chars``, hard ceiling
    ``max_chars``): units pack together up to the soft target, a unit bigger than either maximum is split at the
    last sentence or whitespace boundary under the limit, and a unit shorter than both maxima is never split.

    A gap between units (a filtered header/footer, or a document with no units at all) rides along with whichever
    neighbour it packs with; when the NEXT unit is itself oversized, the gap is folded into that unit's OWN split
    (starting the split at the pending buffer, not at the oversized unit's own start) rather than flushed alone as
    a near-empty chunk.
    """
    if not text:
        return ()
    spans = _chunk_boundaries(text, units)
    chunks: list[Chunk] = []
    buf_start = buf_end = spans[0][0] if spans else 0
    for start, end in spans:
        if _too_big(text, start, end, count_tokens, max_tokens, max_chars):
            pos = buf_start if buf_end > buf_start else start
            while pos < end:
                cut = _cut_point(text, pos, end, count_tokens, max_tokens, max_chars)
                chunks.append(_make_chunk(len(chunks), pos, cut, text, count_tokens))
                pos = cut
            buf_start = buf_end = pos
            continue
        candidate_end = max(buf_end, end)
        if buf_end > buf_start and _too_big(text, buf_start, candidate_end, count_tokens, max_tokens, max_chars):
            chunks.append(_make_chunk(len(chunks), buf_start, buf_end, text, count_tokens))
            buf_start = buf_end
        if buf_end > buf_start and len(text[buf_start:buf_end]) >= target_chars:
            chunks.append(_make_chunk(len(chunks), buf_start, buf_end, text, count_tokens))
            buf_start = buf_end
        buf_end = end
    if buf_end > buf_start:
        chunks.append(_make_chunk(len(chunks), buf_start, buf_end, text, count_tokens))
    return tuple(_drop_trailing_gap_chunk(chunks, text, count_tokens))


# --------------------------------------------------------------------------
# unit_rows: the row shape graph.alignment.align / graph.passages.compute_passages take
# --------------------------------------------------------------------------

def unit_rows(text: str, units: Sequence[Unit]) -> list[dict]:
    """One row per unit: ``item_id``, ``text``, ``headline``, ``text_hash``, ``unit_kind``, ``char_start``."""
    rows = []
    for u in units:
        body = text[u.char_start:u.char_end]
        rows.append({
            "item_id": u.unit_id,
            "text": body,
            "headline": u.headline,
            "text_hash": content_hash(body),
            "unit_kind": u.kind,
            "char_start": u.char_start,
        })
    return rows
