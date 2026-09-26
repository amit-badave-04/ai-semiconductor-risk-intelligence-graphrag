"""Risk items: one risk-factor headline plus its body paragraphs
(M1b step 1 — the unit of truth for the temporal layer).

An annual filing's risk section (10-K Item 1A = ``I.1A``; 20-F Item 3.D =
``I.3``) is cut into items by CHARACTER SPAN over the very section text the
chunker persisted (``data/interim/section_texts``), so an item maps onto the
chunks it overlaps without touching any chunk id. No LLM, no network.

How headlines are found. sec-parser flattens the filing and loses most of the
typography, so headlines are recovered from the raw HTML and located in the
section text:

1. every leaf block element of the HTML becomes an ``HtmlBlock`` (visible text
   + the leading bold run + font size);
2. the blocks are aligned, in document order, onto the section text through an
   alphanumeric-only key (immune to NBSP / curly quotes / whitespace / the way
   sec-parser fuses neighbouring blocks). The alignment anchors on the block
   that opens the section, so a table of contents or a cross-reference earlier
   in the filing can never pull it off course;
3. a bold-led block is a *headline* when it reads as one risk subcaption
   (SEC Item 105 speaks of "subcaptions"): 3-90 words, sentence-like, not a
   group heading. Group headings ("Risks Related to ...", "Business Risks",
   the catch-all "General Risk Factors") and sub-headings inside a body are NOT
   items. A headline split over several consecutive bold blocks (TSMC 2026) is
   merged;
4. an item runs from its headline to the next boundary of ANY kind (next
   headline, group heading, summary heading), so a group heading never sits at
   the tail of an item's text or hash.

The "Risk Factors Summary" block that most 10-Ks carry repeats every headline
as a bullet (sometimes near-verbatim, sometimes a paraphrase). It is located by
POSITION, not by matching text: from the summary heading to the first "Risk
Factors" heading or the first repeated group heading. Nothing inside it becomes
an item, and its bullets are counted (``summary_excluded``).

Fallbacks, cheapest first, each recorded in ``DetectionResult.method``:

- ``bold``      bold headlines (most 10-Ks);
- ``size``      headlines set in a clearly larger font than the body (Intel);
- ``paragraph`` no reliable headlines (20-F filers, some 10-Ks): units are the
  HTML paragraphs (tiny lines and bullets merged into their neighbour, giants
  split at sentence boundaries), or the text's own paragraphs without HTML.

The result always carries a coverage figure (share of section characters inside
items). Coverage under ``COVERAGE_MIN`` sets ``low_coverage`` and a note; it is
never returned silently and never dropped: callers list it.

Layout: a PURE core (``extract_html_blocks``, ``detect_items``, ``coverage``,
``overlapping_chunk_ids`` — no filesystem) and the lake-reading driver
``build_risk_items`` that writes
``data/interim/risk_items/<TICKER>_risk_items.parquet``.

Battle scars (each cost a wrong first draft — do not "clean up"):

- Anchor the alignment on the block that STARTS the section. The first block
  found anywhere in the first paragraph may be a cross-reference far earlier in
  the filing; the section's first paragraph may also be split into blocks
  shorter than the probe (ASML), so either string may be the prefix.
- Never count a short block ("22", "Risk Factors") as an alignment anchor:
  matches are only accepted contiguously or when the key is long enough to be
  unique, otherwise a page number desynchronises everything after it.
- Category headings can be text of any weight (MSFT underlines them, QCOM
  upper-cases them), and real headlines can end in "Risks" ("We Are Subject to
  Payments-Related Risks"): the category test excludes sentence openers.
- Amazon headlines are title-case with no full stop; MSFT sub-headings are
  bold too. Bold alone is not enough: see ``_is_headline_lead``.
- ASML lists a page's captions first and the bodies after them, and its HTML is
  one block per printed line: a headline is trusted only when it owns a body.
  Its text blobs keep the original element seams as ``.We`` (no space).

Known limits (measured on the data lake):

- The section text itself can be wrong. Intel's FY23/FY24 risk sections run
  ~30k characters past Item 1A (sales and marketing, Item 7A, the auditor's
  report) and ASML's FY24 section contains the information-security and
  sustainability chapters. Items inside the true risk text are right; the extra
  text lowers coverage (INTC, flagged LOW) or hides in paragraph units (ASML,
  flagged by the length-change note). The fix belongs in segmentation.
- The last item of a section swallows whatever the section text holds after the
  final risk factor.
- Page numbers and running footers are trimmed from an item's TAIL only.
- MSFT mixes three heading styles (headline, italic sub-heading, underlined
  group heading); the general rules cover it, nothing is specialised.

Size: one cohesive ~1150-line module (HTML blocks -> alignment -> detection ->
lake driver) is a deliberate exception to the 800-line soft ceiling while the
rules are still being tuned on real filings; the natural split point is
``extract_html_blocks``/``_align_blocks`` versus the rest.
"""

import bisect
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

import pandas as pd
from lxml import etree
from lxml import html as lxml_html

from ..config import Settings, get_settings
from ..hashing import content_hash
from ..ingestion.edgar import load_manifest, resolve_local_path
from ..universe import FILERS, RISK_SECTIONS
from .chunker import _write_parquet_atomic, chunks_path_for, section_texts_path_for
from .segmentation import base_form

logger = logging.getLogger("semigraph.parsing.risk_items")

ANNUAL_FORMS = ("10-K", "20-F")
COVERAGE_MIN = 0.90            # M1b pair gate: below this a filing is listed, never compared silently

# --- headline recognition
MIN_HEADLINE_WORDS = 3         # AMZN "We Face Intense Competition" (4); the plan's 8-60 band lost these
MAX_HEADLINE_WORDS = 90        # QCOM and META run-in headlines reach 77 words
LONG_HEADLINE_WORDS = 10       # from here a bold line without a full stop still reads as a sentence
MAX_CATEGORY_WORDS = 14
MAX_RUN_BLOCKS = 6             # a headline wrapped over more consecutive bold blocks is not one headline
MIN_BODY_CHARS = 80            # a headline followed by less body than this is a heading/bullet, not an item
MIN_HEADLINE_ITEMS = 8         # fewer headline items than this and bold headlines are not trusted
MAX_BODYLESS_SHARE = 0.25      # more bold "headlines" than this without a body: not a headline-then-body layout (ASML)
HEADLINE_COVERAGE_MIN = 0.80   # ... and neither is a headline cut that leaves >20% of the section outside items
SIZE_EMPHASIS_RATIO = 1.15     # "clearly larger than the body font"
MAX_SUMMARY_SHARE = 0.35       # a summary window larger than this share of the section is a misdetection
MAX_LABEL_WORDS = 8            # "Item 1B, 1C" is a running footer, "Part II, Item 7 describes ..." is prose
MIN_END_SHARE = 0.50           # an end-of-risk heading in the first half of the section is not believed
TAIL_NOTE_SHARE = 0.01         # a cut-off tail shorter than this (a running footer) is not worth a note
PARAGRAPH_END_AFTER = 0.10     # ... and without headlines none is believed before this share of the section

# --- paragraph units
MIN_PARAGRAPH_CHARS = 120
MAX_UNIT_CHARS = 2500
PARAGRAPH_GRADE_RATIO = 0.6    # HTML blocks are usable as paragraphs when this share of the key text aligned ...
PARAGRAPH_GRADE_MEAN_CHARS = 60  # ... and blocks are not line fragments (ASML: mean ~6 chars)

# --- alignment
ALIGN_MIN_KEY = 12             # shorter blocks only align when contiguous with the previous one
ALIGN_NEAR = 300               # a longer block may skip at most this much unmatched text
ALIGN_FAR_MIN_KEY = 40         # ... and only a long, UNIQUE block may jump further (nav bars, page furniture never)
ALIGN_SHINGLE = 16
ANCHOR_PROBE = 50
ANCHOR_MIN_KEY = 20
MAX_ANCHORS = 8

SECTION_LENGTH_WARN = 0.6      # a risk section under 60% (or over 1/0.6 x) of the prior filing's length: parse suspect

ITEM_COLUMNS = [
    "item_id", "accession_no", "ticker", "filer_cik", "form", "filing_date", "section_id", "seq",
    "headline", "text", "text_hash", "char_start", "char_end", "unit_kind", "chunk_ids",
]

_BLOCK_TAGS = frozenset({
    "div", "p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "ul", "ol",
    "blockquote", "dd", "dt", "dl", "section", "article", "center",
})
_BOLD_TAGS = frozenset({"b", "strong", "th", "h1", "h2", "h3", "h4", "h5", "h6"})
_BOLD_RE = re.compile(r"font-weight\s*:\s*(bold|bolder|[6-9]00)\b", re.I)
_NORMAL_RE = re.compile(r"font-weight\s*:\s*(normal|lighter|[1-5]00)\b", re.I)
_UNDERLINE_RE = re.compile(r"text-decoration[^;]*underline", re.I)
_HIDDEN_RE = re.compile(r"display\s*:\s*none", re.I)
_SIZE_RE = re.compile(r"font-size\s*:\s*([\d.]+)\s*(pt|px)", re.I)

_TERMINAL_RE = re.compile(r"[.?!][\"'”’)\]]*$")
_OPENER_RE = re.compile(r"^(we|our|if|the company)\b", re.I)
_CATEGORY_START_RE = re.compile(
    r"^(risks?|general risks?|other risks?)\s+(related|relating|specific|associated|inherent|arising|applicable|regarding)\b",
    re.I,
)
_CATEGORY_WORD_RE = re.compile(r"\b(risks?|risk factors?|summary)\b", re.I)
_RF_HEADING_RE = re.compile(r"^risk factors?\s*:?$", re.I)
# Headings that open whatever follows Item 1A / Item 3.D (standard 10-K and 20-F vocabulary). A lone
# "Properties" is a heading; "Properties of X ..." is prose, hence the exact form.
_END_EXACT_RE = re.compile(
    r"^(?:unresolved staff comments|cybersecurity|properties|legal proceedings|mine safety disclosures|"
    r"other information|controls and procedures|financial statements and supplementary data)[\s.:]*$", re.I
)
_END_LABEL_RE = re.compile(r"^(?:item\s+\d+[a-c]?\b|part\s+(?:ii|iii|iv)\b)", re.I)     # short lines only
_END_PHRASE_RE = re.compile(                                                             # distinctive: any continuation
    r"^(?:other key information|information about our executive officers|executive officers of the registrant|"
    r"quantitative and qualitative disclosures? about market risk|market for (?:the )?registrant|"
    r"management.s discussion and analysis|disclosure pursuant to section 13\(r\)|stock performance graph|"
    r"issuer purchases of equity securities)", re.I
)
_SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])(?:[\"”’)]*\s+|(?=[A-Z]))")
# ".We": where sec-parser fused two elements without a space ("N.V." and "U.S." never match)
_SEAM_RE = re.compile(r"(?<=[a-z0-9)][.!?])(?=[A-Z])")
_FURNITURE_RE = re.compile(
    r"^(?:\d{1,4}|part\s+[ivx]+|item\s+\d+[a-c]?(?:\s*,\s*\d+[a-c]?)*|table of contents)$", re.I
)
_TRAILING_PAGE_RE = re.compile(r"(?<=[.!?\"”’)\]])[  ]+\d{1,3}$")     # "... results of operations. 5"
_BULLETS = "•▪●◦‣·■□*"
_LEADING_GLYPHS = _BULLETS + "“‘\"'(["


# ------------------------------------------------------------------- data types

class _Style(NamedTuple):
    bold: bool = False
    underline: bool = False
    size: float | None = None


@dataclass(frozen=True)
class HtmlBlock:
    """One leaf block element of the filing HTML."""

    text: str                    # visible text, whitespace collapsed (NFKC)
    lead: str                    # leading bold run ("" when the block does not start bold)
    heading_style: bool          # every non-space character is bold or underlined
    size: float | None           # font size in pt when uniform across the block


@dataclass(frozen=True)
class _Seg:
    """A block (or text paragraph) located in the section text."""

    start: int                   # offsets into the section text
    end: int
    text: str
    lead: str                    # the emphasised lead used for headline detection ("" = none)
    lead_end: int
    heading_style: bool
    size: float | None


@dataclass(frozen=True)
class DetectedItem:
    """``text`` is exactly ``section_text[char_start:char_end]``. ``headline`` is NFKC-normalised with
    whitespace collapsed (a headline wrapped over several blocks is one line): the normalised ``text``
    starts with it, but ``text[len(headline):]`` is NOT the body (offsets differ after collapsing)."""

    seq: int
    headline: str                # "" for paragraph units
    text: str                    # section_text[char_start:char_end]
    char_start: int
    char_end: int
    unit_kind: str               # 'headline' | 'paragraph'


@dataclass(frozen=True)
class DetectionResult:
    items: tuple[DetectedItem, ...]
    unit_kind: str               # 'headline' | 'paragraph' | 'none'
    method: str                  # 'bold' | 'size' | 'paragraph' | 'none'
    coverage: float              # share of section characters inside items
    low_coverage: bool
    summary_found: bool
    summary_excluded: int        # summary bullets kept out of the items
    n_candidates: int            # headline-like blocks before filtering (diagnostic)
    aligned_ratio: float         # share of the section's key text the HTML blocks aligned onto
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class _Cand:
    first: int                   # seg indices of the (possibly merged) headline
    last: int
    start: int
    head_end: int
    headline: str
    key: str


class _Marked(NamedTuple):
    kinds: list[str]             # per seg: other|headline|cont|category|summary|rf_heading
    cands: list[_Cand]


# --------------------------------------------------------------- text primitives

def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _key(text: str) -> str:
    """Alphanumeric-only lower-case key: immune to whitespace, punctuation and typography."""
    return "".join(c.lower() for c in unicodedata.normalize("NFKC", text) if c.isalnum())


def _section_key(text: str) -> tuple[str, list[int]]:
    """``_key(text)`` plus, per key character, its offset in ``text``."""
    chars: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        if ch.isascii():
            if ch.isalnum():
                chars.append(ch.lower())
                index.append(i)
            continue
        for c in unicodedata.normalize("NFKC", ch):
            if c.isalnum():
                chars.append(c.lower())
                index.append(i)
    return "".join(chars), index


def _is_terminal(text: str) -> bool:
    return bool(_TERMINAL_RE.search(text.rstrip()))


def is_category_text(text: str, *, strict: bool = False) -> bool:
    """A group heading ("Risks Related to Our Industry", "Business Risks", "General Risk
    Factors") as opposed to one risk's headline. Sentences (full stop) and sentence
    openers ("We Are Subject to Payments-Related Risks") are never categories.
    ``strict`` accepts only the "Risks related to ..." form (for unstyled text)."""
    t = text.strip().strip(":").strip()
    words = len(t.split())
    if not t or words > MAX_CATEGORY_WORDS or _is_terminal(t):
        return False
    if _CATEGORY_START_RE.match(t):
        return True
    if strict:
        return False
    return words <= 8 and not _OPENER_RE.match(t) and bool(_CATEGORY_WORD_RE.search(t))


def _is_summary_heading(text: str) -> bool:
    t = text.strip()
    if len(t.split()) > 8:
        return False
    return bool(re.search(r"\bsummary\b", t, re.I) and re.search(r"\brisks?\b", t, re.I))


def _is_end_heading(text: str) -> bool:
    """A heading that opens the material after the risk factors (Item 1B ..., "Properties", ...)."""
    t = text.strip()
    return bool(
        _END_EXACT_RE.match(t) or _END_PHRASE_RE.match(t)
        or (_END_LABEL_RE.match(t) and len(t.split()) <= MAX_LABEL_WORDS)
    )


def _is_headline_lead(lead: str) -> bool:
    words = len(lead.split())
    if not MIN_HEADLINE_WORDS <= words <= MAX_HEADLINE_WORDS:
        return False
    if lead.rstrip().endswith(":") or is_category_text(lead):
        return False
    return _is_terminal(lead) or words >= LONG_HEADLINE_WORDS or bool(_OPENER_RE.match(lead))


# ------------------------------------------------------------ HTML -> blocks

def _child_style(el, inherited: _Style) -> _Style:
    tag = el.tag if isinstance(el.tag, str) else ""
    style = el.get("style") or ""
    bold, underline, size = inherited
    if _BOLD_RE.search(style):
        bold = True
    elif _NORMAL_RE.search(style):
        bold = False
    elif tag in _BOLD_TAGS:
        bold = True
    if _UNDERLINE_RE.search(style) or tag == "u":
        underline = True
    if (m := _SIZE_RE.search(style)):
        size = float(m.group(1)) * (0.75 if m.group(2).lower() == "px" else 1.0)
    return _Style(bold, underline, size)


def _has_block_child(el) -> bool:
    return any(isinstance(d.tag, str) and d.tag in _BLOCK_TAGS for d in el.iterdescendants())


def _collect_segments(el, style: _Style, out: list[tuple[str, _Style]]) -> None:
    tag = el.tag if isinstance(el.tag, str) else ""
    if tag == "br":
        out.append(("\n", style))
    st = _child_style(el, style) if tag else style
    if el.text:
        out.append((el.text, st))
    for child in el:
        if not (isinstance(child.tag, str) and _HIDDEN_RE.search(child.get("style") or "")):
            _collect_segments(child, st, out)
        if child.tail:
            out.append((child.tail, st))


def _block_from_segments(segs: list[tuple[str, _Style]]) -> HtmlBlock | None:
    text = _norm("".join(s for s, _ in segs))
    if not text:
        return None
    lead: list[str] = []
    seen = False
    for s, st in segs:
        if not s.strip():
            if seen:
                lead.append(s)
            continue
        if not st.bold:
            break
        seen = True
        lead.append(s)
    solid = [st for s, st in segs if s.strip()]
    sizes = {st.size for st in solid}
    return HtmlBlock(
        text=text,
        lead=_norm("".join(lead)),
        heading_style=all(st.bold or st.underline for st in solid),
        size=sizes.pop() if len(sizes) == 1 else None,
    )


def extract_html_blocks(html: str) -> list[HtmlBlock]:
    """Leaf block elements of ``html`` in document order (hidden iXBRL headers skipped)."""
    if not html or not html.strip():
        return []
    try:
        doc = lxml_html.document_fromstring(html.encode("utf-8"), parser=lxml_html.HTMLParser(encoding="utf-8"))
    except (etree.ParserError, ValueError):
        return []
    body = doc.find("body")
    if body is None:
        return []
    blocks: list[HtmlBlock] = []
    for el in body.iter():
        if not isinstance(el.tag, str) or el.tag not in _BLOCK_TAGS:
            continue
        if _has_block_child(el):
            continue
        chain = [el, *el.iterancestors()]
        if any(_HIDDEN_RE.search(a.get("style") or "") for a in chain if isinstance(a.tag, str)):
            continue
        inherited = _Style()
        for ancestor in reversed(chain[1:]):
            inherited = _child_style(ancestor, inherited)
        segs: list[tuple[str, _Style]] = []
        try:
            _collect_segments(el, inherited, segs)
        except RecursionError:      # a pathologically nested block is skipped, never fatal
            continue
        if (block := _block_from_segments(segs)) is not None:
            blocks.append(block)
    return blocks


# --------------------------------------------------------- blocks -> section text

def _anchor_starts(keys: list[str], section_text: str) -> list[int]:
    """Block indices that can open the section: their key and the section's first
    paragraphs' key are prefixes of one another."""
    probes = []
    for para in section_text.split("\n\n")[:6]:
        k = _key(para)
        if len(k) >= ANCHOR_PROBE:
            probes.append(k[:ANCHOR_PROBE])
    if not probes:
        return []
    found = [
        i for i, k in enumerate(keys)
        if len(k) >= ANCHOR_MIN_KEY and any(k.startswith(p) or p.startswith(k) for p in probes)
    ]
    return found[:MAX_ANCHORS]


def _find_block(k: str, sec_key: str, cursor: int, shingles: dict[str, list[int]]) -> int:
    if sec_key.startswith(k, cursor):
        return cursor
    if len(k) < ALIGN_MIN_KEY:
        return -1
    pos = sec_key.find(k, cursor, cursor + ALIGN_NEAR + len(k))
    if pos >= 0 or len(k) < ALIGN_FAR_MIN_KEY:
        return pos
    hits = shingles.get(k[:ALIGN_SHINGLE], ())
    if len(hits) == 1 and hits[0] >= cursor and sec_key.startswith(k, hits[0]):
        return hits[0]
    return -1


def _align_from(start: int, keys: list[str], sec_key: str, shingles: dict[str, list[int]]) -> list[tuple[int, int, int]]:
    cursor, out = 0, []
    for bi in range(start, len(keys)):
        k = keys[bi]
        if not k:
            continue
        pos = _find_block(k, sec_key, cursor, shingles)
        if pos >= 0:
            cursor = pos + len(k)
            out.append((bi, pos, cursor))
    return out


def _extend_over_glyphs(text: str, start: int) -> int:
    while start > 0 and text[start - 1] in _LEADING_GLYPHS:
        start -= 1
    return start


def _extend_over_punctuation(text: str, end: int) -> int:
    """``end`` is one past a key character: take the punctuation glued to it ("results." )."""
    while end < len(text) and not text[end].isspace() and not text[end].isalnum():
        end += 1
    return end


def _align_blocks(blocks: Sequence[HtmlBlock], section_text: str) -> tuple[list[_Seg], float]:
    """Locate ``blocks`` in ``section_text`` (monotonic, tolerant). Returns the located
    segments and the share of the section's key text they cover."""
    if not blocks:
        return [], 0.0
    sec_key, index = _section_key(section_text)
    if not sec_key:
        return [], 0.0
    keys = [_key(b.text) for b in blocks]
    starts = _anchor_starts(keys, section_text)
    if not starts:
        return [], 0.0
    shingles: dict[str, list[int]] = {}
    for i in range(len(sec_key) - ALIGN_SHINGLE + 1):
        shingles.setdefault(sec_key[i : i + ALIGN_SHINGLE], []).append(i)
    best: list[tuple[int, int, int]] = []
    best_cover = 0
    for st in starts:
        run = _align_from(st, keys, sec_key, shingles)
        cover = sum(e - s for _, s, e in run)
        if cover > best_cover:
            best, best_cover = run, cover
    segs = []
    for bi, ks, ke in best:
        b = blocks[bi]
        start = _extend_over_glyphs(section_text, index[ks])
        lead_len = len(_key(b.lead))
        lead_end = _extend_over_punctuation(section_text, index[min(ks + lead_len, ke) - 1] + 1) if lead_len else start
        end = _extend_over_punctuation(section_text, index[ke - 1] + 1)
        segs.append(_Seg(start, end, b.text, b.lead if lead_len else "", lead_end, b.heading_style, b.size))
    return segs, best_cover / len(sec_key)


def _text_segs(section_text: str) -> list[_Seg]:
    """The section text's own paragraphs (no HTML available / usable), cut again at fused seams."""
    segs, pos = [], 0
    for para in section_text.split("\n\n"):
        cuts = [0, *(m.start() for m in _SEAM_RE.finditer(para)), len(para)]
        for a, b in zip(cuts, cuts[1:]):
            piece = para[a:b]
            if stripped := piece.strip():
                start = pos + a + len(piece) - len(piece.lstrip())
                segs.append(_Seg(start, start + len(stripped), _norm(stripped), "", start, False, None))
        pos += len(para) + 2
    return segs


# ------------------------------------------------------- marking: kinds + headlines

def _seg_kind(seg: _Seg) -> str:
    text = seg.text
    if _is_summary_heading(text):
        return "summary"
    if _RF_HEADING_RE.match(text):
        return "rf_heading"
    if _is_end_heading(text):
        return "end"
    styled_text = seg.lead or (text if seg.heading_style or text.isupper() else "")
    if styled_text and is_category_text(styled_text):
        return "category"
    if not seg.lead and len(text.split()) <= MAX_CATEGORY_WORDS and is_category_text(text, strict=True):
        return "category"
    return "lead" if seg.lead and _is_headline_lead(seg.lead) else "other"


def _run_end(segs: list[_Seg], i: int) -> int:
    """Last block of the headline that starts at ``i``: a headline wrapped over consecutive bold
    blocks continues while the line is unfinished and the next block begins in lower case."""
    j = i
    while (
        j - i < MAX_RUN_BLOCKS
        and j + 1 < len(segs)
        and segs[j].lead == segs[j].text
        and segs[j + 1].lead[:1].islower()      # a caption never starts in lower case: this is its wrapped line,
        and _seg_kind(segs[j + 1]) in ("lead", "other")   # even after a full stop ("... in the R.O.C.")
    ):
        j += 1
    return j


def _mark(segs: list[_Seg], section_text: str) -> _Marked:
    kinds = ["other"] * len(segs)
    cands: list[_Cand] = []
    i = 0
    while i < len(segs):
        kind = _seg_kind(segs[i])
        if kind != "lead":
            kinds[i] = kind
            i += 1
            continue
        j = _run_end(segs, i)
        start, head_end = segs[i].start, segs[j].lead_end
        headline = _norm(section_text[start:head_end])
        if len(headline.split()) <= MAX_HEADLINE_WORDS and _is_headline_lead(headline if j > i else segs[i].lead):
            kinds[i] = "headline"
            for k in range(i + 1, j + 1):
                kinds[k] = "cont"
            cands.append(_Cand(i, j, start, head_end, headline, _key(headline)))
        i = j + 1
    return _Marked(kinds, cands)


def _boundary_starts(segs: list[_Seg], marked: _Marked) -> list[int]:
    return sorted(
        s.start for s, kind in zip(segs, marked.kinds)
        if kind in ("headline", "category", "summary", "rf_heading", "end")
    )


def _summary_window(segs: list[_Seg], marked: _Marked, section_len: int) -> tuple[int, int] | None:
    """[start, end) character span of the "Risk Factors Summary" block, or None.

    Starts at the summary heading; ends at the first plain "Risk Factors" heading or the first
    REPEATED group heading (the full listing opens with the first group heading again), else at
    the first headline that owns a real body."""
    kinds = marked.kinds
    heading = next((i for i, k in enumerate(kinds) if k == "summary"), None)
    if heading is None:
        return None
    end = None
    seen: set[str] = set()
    for k in range(heading + 1, len(segs)):
        if kinds[k] == "rf_heading":
            end = segs[k].start
        elif kinds[k] == "category":
            key = _key(segs[k].text)
            if key in seen:
                end = segs[k].start
            seen.add(key)
        if end is not None:
            break
    if end is None:
        bounds = _boundary_starts(segs, marked)
        for c in marked.cands:
            if c.start > segs[heading].start and _body_chars(c, bounds, section_len) >= MIN_BODY_CHARS:
                end = c.start
                break
    if end is None or end - segs[heading].start > MAX_SUMMARY_SHARE * section_len:
        return None
    return segs[heading].start, end


def _end_of_risk(segs: list[_Seg], marked: _Marked, after: int, section_len: int) -> int | None:
    """Offset where the risk text ends: the first end-of-risk heading after ``after`` that lies in the
    back half of the section (sections can run on into Item 1B ... executive officers ...)."""
    for seg, kind in zip(segs, marked.kinds):
        if kind == "end" and seg.start > after and seg.start >= MIN_END_SHARE * section_len:
            return seg.start
    return None


def _next_stop(stops: list[int], start: int, section_len: int) -> int:
    """First boundary strictly after ``start`` (the section end when there is none)."""
    i = bisect.bisect_right(stops, start)
    return stops[i] if i < len(stops) else section_len


def _body_chars(cand: _Cand, bounds: list[int], section_len: int) -> int:
    return max(0, _next_stop(bounds, cand.start, section_len) - cand.head_end)


def _count_summary_bullets(section_text: str, window: tuple[int, int] | None, dropped: int) -> int:
    if window is None:
        return 0
    glyphs = sum(section_text[window[0] : window[1]].count(g) for g in _BULLETS)
    return glyphs or dropped


# ----------------------------------------------------------------- headline mode

def _headline_items(section_text: str, segs: list[_Seg], marked: _Marked) -> tuple[list[DetectedItem], dict]:
    n = len(section_text)
    window = _summary_window(segs, marked, n)
    bounds = _boundary_starts(segs, marked)
    in_window = [c for c in marked.cands if window and window[0] <= c.start < window[1]]
    stop = _end_of_risk(segs, marked, min((c.start for c in marked.cands), default=n), n)
    live = [c for c in marked.cands
            if not (window and window[0] <= c.start < window[1]) and (stop is None or c.start < stop)]

    best: dict[str, _Cand] = {}
    for c in live:      # duplicate headline: keep the occurrence that owns the largest body (ties: the later)
        k = c.key
        if k not in best or _body_chars(c, bounds, n) >= _body_chars(best[k], bounds, n):
            best[k] = c
    kept = sorted((c for c in best.values() if _body_chars(c, bounds, n) >= MIN_BODY_CHARS), key=lambda c: c.start)
    bodyless = len(best) - len(kept)

    items = []
    for seq, c in enumerate(kept):
        end = _trim_end(section_text, c.start, _next_stop(bounds, c.start, n))
        items.append(DetectedItem(seq, c.headline, section_text[c.start:end], c.start, end, "headline"))
    return items, {
        "window": window,
        "end_of_risk": stop,
        "n_candidates": len(marked.cands),
        "n_bodyless": bodyless,
        "n_live": len(best),
        "summary_excluded": _count_summary_bullets(section_text, window, len(in_window)),
    }


def _rstrip_end(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1].isspace():
        end -= 1
    return end


def _trim_end(text: str, start: int, end: int) -> int:
    """End of a unit with trailing whitespace and page furniture (a lone page number, a running
    "Item 1B, 1C" footer) cut off: a page number must not change an item's text or hash."""
    while True:
        end = _rstrip_end(text, start, end)
        if (cut := text.rfind("\n", start, end)) >= 0 and _FURNITURE_RE.match(text[cut + 1 : end].strip()):
            end = cut
        elif m := _TRAILING_PAGE_RE.search(text, start, end):
            end = m.start()
        else:
            return end


def strip_page_furniture(text: str) -> str:
    """``text`` without its lone page-number / running-footer paragraphs (interior page breaks stay in
    an item's text, which must equal the section-text slice): compare or hash items through this."""
    return "\n\n".join(p for p in text.split("\n\n") if not _FURNITURE_RE.match(p.strip()))


def _body_font_size(segs: Iterable[_Seg]) -> float | None:
    weight: Counter[float] = Counter()
    for s in segs:
        if s.size is not None:
            weight[round(s.size * 2) / 2] += s.end - s.start
    return weight.most_common(1)[0][0] if weight else None


def _size_led(segs: list[_Seg], body: float) -> list[_Seg]:
    """Segments re-led by font size: a block set clearly larger than the body is its own headline."""
    return [
        replace(s, lead=s.text, lead_end=s.end) if s.size is not None and s.size >= body * SIZE_EMPHASIS_RATIO
        else replace(s, lead="", lead_end=s.start)
        for s in segs
    ]


def _headline_reliable(items: Sequence[DetectedItem], section_len: int, info: dict) -> bool:
    """Enough headline items, covering the risk text: the summary block and whatever follows the end of
    the risk factors are excluded on purpose."""
    window = info.get("window")
    end = info.get("end_of_risk")
    scope = (section_len if end is None else end) - (window[1] - window[0] if window else 0)
    bodyless_share = info.get("n_bodyless", 0) / max(1, info.get("n_live", 0))
    return (len(items) >= MIN_HEADLINE_ITEMS and coverage(items, scope) >= HEADLINE_COVERAGE_MIN
            and bodyless_share <= MAX_BODYLESS_SHARE)


# --------------------------------------------------------------- paragraph mode

def _is_bullet(text: str) -> bool:
    return bool(text) and text[0] in _BULLETS


def _split_unit(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Cut [start, end) into pieces of at most MAX_UNIT_CHARS at sentence boundaries."""
    if end - start <= MAX_UNIT_CHARS:
        return [(start, end)]
    cuts = [start + m.end() for m in _SENTENCE_BREAK_RE.finditer(text[start:end])]
    pieces, lo = [], start
    while end - lo > MAX_UNIT_CHARS:
        limit = lo + MAX_UNIT_CHARS
        i = bisect.bisect_right(cuts, limit) - 1
        cut = cuts[i] if i >= 0 and cuts[i] > lo else limit
        pieces.append((lo, _rstrip_end(text, lo, cut)))
        lo = cut
        while lo < end and text[lo].isspace():
            lo += 1
    pieces.append((lo, end))
    return pieces


def _unit_starts(kept: list[_Seg], excluded: list[tuple[int, int]]) -> list[int]:
    """Start offsets of the paragraph units: tiny lines merge forward into the next block,
    bullets stay with the block that introduces them, a short tail joins the unit before it."""
    breaks = [
        pos == 0 or any(kept[pos - 1].end <= a < seg.start for a, _ in excluded)
        for pos, seg in enumerate(kept)
    ]
    starts: list[int] = []
    buf_start: int | None = None
    buf_chars = 0
    absorb = False            # the previous unit runs up to the buffer with no excluded text between
    for pos, seg in enumerate(kept):
        if breaks[pos]:
            if buf_start is not None and not (absorb and buf_chars < MIN_PARAGRAPH_CHARS):
                starts.append(buf_start)
            buf_start, absorb = None, False
        if buf_start is None:
            buf_start, buf_chars = seg.start, 0
        buf_chars += seg.end - seg.start
        nxt = kept[pos + 1] if pos + 1 < len(kept) else None
        if buf_chars >= MIN_PARAGRAPH_CHARS and (nxt is None or breaks[pos + 1] or not _is_bullet(nxt.text)):
            starts.append(buf_start)
            buf_start, absorb = None, True
    if buf_start is not None and not (absorb and buf_chars < MIN_PARAGRAPH_CHARS):
        starts.append(buf_start)
    return starts


def _paragraph_items(section_text: str, segs: list[_Seg], marked: _Marked, window: tuple[int, int] | None,
                     stop: int | None) -> list[DetectedItem]:
    """Units tile the section text; the summary block, group headings and everything after the end of
    the risk factors are excluded."""
    excluded = [
        (s.start, s.end) for s, k in zip(segs, marked.kinds) if k in ("summary", "category", "rf_heading", "end")
    ]
    if window:
        excluded.append(window)
    if stop is not None:
        excluded.append((stop, len(section_text)))
    kept = [s for s in segs if not any(a <= s.start < b for a, b in excluded)]
    starts = _unit_starts(kept, excluded)
    stops = sorted({a for a, _ in excluded} | set(starts))
    items: list[DetectedItem] = []
    for start in starts:
        end = _trim_end(section_text, start, _next_stop(stops, start, len(section_text)))
        for a, b in _split_unit(section_text, start, end):
            if b > a:
                items.append(DetectedItem(len(items), "", section_text[a:b], a, b, "paragraph"))
    return items


# ---------------------------------------------------------------- public core

def coverage(items: Sequence[DetectedItem], section_chars: int) -> float:
    """Share of the section's characters that lie inside items (0 for an empty section)."""
    if section_chars <= 0 or not items:
        return 0.0
    spans = sorted((it.char_start, it.char_end) for it in items)
    covered, hi = 0, 0
    for s, e in spans:
        s = max(s, hi)
        if e > s:
            covered += e - s
            hi = e
    return min(1.0, covered / section_chars)


def overlapping_chunk_ids(start: int, end: int, chunks: Iterable[tuple[str, int, int]]) -> list[str]:
    """Ids of the chunks whose half-open char range overlaps [start, end), in text order.
    ``chunks`` are ``(chunk_id, char_start, char_end)`` rows of the SAME section."""
    hits = sorted((cs, cid) for cid, cs, ce in chunks if cs < end and ce > start)
    return [cid for _, cid in hits]


def _empty_result(note: str) -> DetectionResult:
    return DetectionResult((), "none", "none", 0.0, True, False, 0, 0, 0.0, (note,))


def _largest_gap(items: Sequence[DetectedItem], section_len: int) -> tuple[int, int]:
    """Largest span of the section outside every item (start, end)."""
    best, hi = (0, 0), 0
    for it in sorted(items, key=lambda i: i.char_start):
        if it.char_start - hi > best[1] - best[0]:
            best = (hi, it.char_start)
        hi = max(hi, it.char_end)
    return (hi, section_len) if section_len - hi > best[1] - best[0] else best


def _finish(items: list[DetectedItem], method: str, marked_info: dict, section_len: int,
            aligned_ratio: float, min_coverage: float, notes: list[str]) -> DetectionResult:
    cov = coverage(items, section_len)
    low = cov < min_coverage
    if method == "paragraph":
        notes.append("paragraph units tile the section by construction: coverage cannot reveal an over-extended section")
    if (stop := marked_info.get("end_of_risk")) is not None and section_len - stop >= TAIL_NOTE_SHARE * section_len:
        notes.append(f"risk text ends at char {stop}: {section_len - stop} chars after it are outside the items")
    if low:
        a, b = _largest_gap(items, section_len)
        notes.append(f"coverage {cov:.1%} is below the {min_coverage:.0%} gate; largest span outside any item: "
                     f"chars {a}-{b} ({b - a} chars, {(b - a) / section_len:.0%} of the section)")
    kind = "headline" if method in ("bold", "size") else "paragraph"
    window = marked_info.get("window")
    return DetectionResult(
        items=tuple(items), unit_kind=kind, method=method, coverage=cov, low_coverage=low,
        summary_found=window is not None, summary_excluded=marked_info.get("summary_excluded", 0),
        n_candidates=marked_info.get("n_candidates", 0), aligned_ratio=aligned_ratio, notes=tuple(notes),
    )


def detect_items(html: str | None, section_text: str, *, min_coverage: float = COVERAGE_MIN) -> DetectionResult:
    """Cut one annual risk section into risk items. PURE.

    ``html`` is the filing's raw HTML (None/"" = unavailable: text paragraphs are used);
    ``section_text`` is the section text the chunker persisted, which every offset indexes.
    """
    text = section_text or ""
    if not text.strip():
        return _empty_result("empty risk section text")
    n = len(text)
    blocks = extract_html_blocks(html or "")
    segs, ratio = _align_blocks(blocks, text)
    notes: list[str] = []
    if blocks and not segs:
        notes.append("HTML blocks could not be aligned onto the section text")
    elif segs:
        notes.append(f"aligned {ratio:.0%} of the section text onto {len(segs)} HTML blocks")

    if segs:
        marked = _mark(segs, text)
        items, info = _headline_items(text, segs, marked)
        if _headline_reliable(items, n, info):
            return _finish(items, "bold", info, n, ratio, min_coverage, notes)
        notes.append(f"bold headlines not trusted ({len(items)} items, {coverage(items, n):.0%} coverage, "
                     f"{info['n_bodyless']} of {info['n_live']} without a body)")
        body = _body_font_size(segs)
        if body:
            sized = _size_led(segs, body)
            marked_s = _mark(sized, text)
            items_s, info_s = _headline_items(text, sized, marked_s)
            if _headline_reliable(items_s, n, info_s):
                return _finish(items_s, "size", info_s, n, ratio, min_coverage, notes)
            notes.append(f"larger-font headlines not trusted ({len(items_s)} items, "
                         f"{info_s['n_bodyless']} of {info_s['n_live']} without a body)")

    grade = bool(segs) and ratio >= PARAGRAPH_GRADE_RATIO and (
        sum(s.end - s.start for s in segs) / len(segs) >= PARAGRAPH_GRADE_MEAN_CHARS
    )
    para_segs = segs if grade else _text_segs(text)
    if segs and not grade:
        notes.append("HTML blocks are not paragraph-grade; using the text's paragraphs")
    marked_p = _mark(para_segs, text)
    window = _summary_window(para_segs, marked_p, n)
    stop = _end_of_risk(para_segs, marked_p, int(PARAGRAPH_END_AFTER * n), n)
    items_p = _paragraph_items(text, para_segs, marked_p, window, stop)
    dropped = sum(1 for s in para_segs if window and window[0] <= s.start < window[1] and s.text.strip())
    info_p = {"window": window, "end_of_risk": stop, "n_candidates": len(marked_p.cands),
              "summary_excluded": _count_summary_bullets(text, window, dropped)}
    return _finish(items_p, "paragraph", info_p, n, ratio, min_coverage, notes)


# ------------------------------------------------------------- lake orchestration

def risk_items_dir(settings: Settings) -> Path:
    return settings.interim_dir / "risk_items"


def risk_items_path_for(settings: Settings, ticker: str) -> Path:
    return risk_items_dir(settings) / f"{ticker}_risk_items.parquet"


def _read_optional(path: Path, warnings: list[str], what: str) -> pd.DataFrame | None:
    if not path.exists():
        warnings.append(f"{what} missing: {path.name}")
        return None
    return pd.read_parquet(path)


def _chunk_spans(chunks: pd.DataFrame | None) -> dict[tuple[str, str], list[tuple[str, int, int]]]:
    spans: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    if chunks is None:
        return spans
    for r in chunks.itertuples(index=False):
        spans.setdefault((r.accession_no, r.section_id), []).append((r.chunk_id, int(r.char_start), int(r.char_end)))
    return spans


def _read_html(settings: Settings, meta: dict, warnings: list[str]) -> str | None:
    path = resolve_local_path(settings, meta)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        warnings.append(f"{meta['accession_no']}: raw HTML unreadable ({exc}); used text paragraphs")
        logger.warning("%s %s: raw HTML unreadable (%s)", meta.get("ticker"), meta["accession_no"], exc)
        return None


def _absorb_unchunked(items: Sequence[DetectedItem], spans: list[tuple[str, int, int]],
                      text: str) -> tuple[list[DetectedItem], int]:
    """Paragraph units that no chunk overlaps (the chunker keeps headings out of chunk text) have no
    evidence span to cite: merge each into the next unit, or the previous one at the end. Their own
    text stays inside the merged item; chunk ids are never invented. Headline items are left alone."""
    if not spans:
        return list(items), 0
    out: list[DetectedItem] = []
    carry: int | None = None
    merged = 0
    for it in items:
        if it.unit_kind == "paragraph" and not overlapping_chunk_ids(it.char_start, it.char_end, spans):
            carry = it.char_start if carry is None else carry
            merged += 1
            if out and it is items[-1]:              # trailing unchunked units join the previous unit
                last = out.pop()
                out.append(replace(last, text=text[last.char_start : it.char_end], char_end=it.char_end))
                carry = None
            continue
        start = it.char_start if carry is None else carry
        out.append(replace(it, seq=len(out), text=text[start : it.char_end], char_start=start))
        carry = None
    return out, merged


def _item_rows(ticker: str, meta: dict, section_id: str, items: Sequence[DetectedItem],
               spans: list[tuple[str, int, int]]) -> list[dict]:
    acc = meta["accession_no"]
    return [
        {
            "item_id": f"{acc}:{section_id}:i{it.seq:03d}",
            "accession_no": acc,
            "ticker": ticker,
            "filer_cik": int(meta["cik"]),
            "form": meta["form"],
            "filing_date": meta["filing_date"],
            "section_id": section_id,
            "seq": it.seq,
            "headline": it.headline,
            "text": it.text,
            "text_hash": content_hash(it.text),
            "char_start": it.char_start,
            "char_end": it.char_end,
            "unit_kind": it.unit_kind,
            "chunk_ids": overlapping_chunk_ids(it.char_start, it.char_end, spans),
        }
        for it in items
    ]


def _filing_report(meta: dict, section_id: str, result: DetectionResult, items: Sequence[DetectedItem],
                   section_chars: int, n_merged: int) -> dict:
    n_head = sum(1 for it in items if it.unit_kind == "headline")
    return {
        "accession_no": meta["accession_no"], "form": meta["form"], "filing_date": meta["filing_date"],
        "section_id": section_id, "n_items": len(items), "n_headline": n_head,
        "n_paragraph": len(items) - n_head, "n_unchunked_merged": n_merged, "section_suspect": False,
        "coverage": round(result.coverage, 4),
        "method": result.method, "summary_found": result.summary_found,
        "summary_excluded": result.summary_excluded, "low_coverage": result.low_coverage,
        "aligned_ratio": round(result.aligned_ratio, 4), "section_chars": section_chars,
        "notes": list(result.notes),
    }


def _annual_rows(manifest_rows: list[dict]) -> list[dict]:
    rows = [r for r in manifest_rows if base_form(r["form"]) in ANNUAL_FORMS]
    return sorted(rows, key=lambda r: (r["filing_date"], r["accession_no"]))


def _length_note(text_len: int, prior_chars: int | None) -> str | None:
    """A risk section far from the prior filing's length: one of the two parses is suspect."""
    if not prior_chars or SECTION_LENGTH_WARN <= text_len / prior_chars <= 1 / SECTION_LENGTH_WARN:
        return None
    return (f"risk section is {text_len / prior_chars:.0%} of the prior filing's length "
            f"({text_len} vs {prior_chars} chars): one of the two section parses may be truncated or "
            "over-extended into other chapters")


def _ticker_summary(settings: Settings, ticker: str, rows: list[dict], filings: list[dict],
                    skipped: list[dict], warnings: list[str], write: bool) -> dict:
    path = risk_items_path_for(settings, ticker)
    if write and rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_parquet_atomic(pd.DataFrame(rows, columns=ITEM_COLUMNS), path)
    elif not rows:
        warnings.append("no risk items produced")
    return {
        "n_items": len(rows), "path": str(path), "written": bool(write and rows),
        "filings": filings, "skipped": skipped, "warnings": warnings,
        "below_coverage": [
            {"accession_no": f["accession_no"], "filing_date": f["filing_date"], "coverage": f["coverage"]}
            for f in filings if f["low_coverage"]
        ],
        "suspect": [
            {"accession_no": f["accession_no"], "filing_date": f["filing_date"], "section_chars": f["section_chars"]}
            for f in filings if f["section_suspect"]
        ],
    }


def _build_ticker(settings: Settings, ticker: str, manifest_rows: list[dict], write: bool) -> dict:
    warnings: list[str] = []
    skipped: list[dict] = []
    filings: list[dict] = []
    rows: list[dict] = []
    texts = _read_optional(section_texts_path_for(settings, ticker), warnings, "section texts")
    chunks = _read_optional(chunks_path_for(settings, ticker), warnings, "chunks")
    spans = _chunk_spans(chunks)
    section_by_key = (
        {(r.accession_no, r.section_id): r.text for r in texts.itertuples(index=False)} if texts is not None else {}
    )
    prior_chars: int | None = None
    for meta in _annual_rows(manifest_rows):
        sid = RISK_SECTIONS[base_form(meta["form"])]
        text = section_by_key.get((meta["accession_no"], sid))
        if not text or not text.strip():
            reason = f"no risk section text ({sid})" if texts is not None else "no section texts parquet"
            skipped.append({"accession_no": meta["accession_no"], "form": meta["form"],
                            "filing_date": meta["filing_date"], "reason": reason})
            logger.warning("%s %s %s %s: %s — skipped", ticker, meta["form"], meta["filing_date"],
                           meta["accession_no"], reason)
            continue
        result = detect_items(_read_html(settings, meta, warnings), text)
        file_spans = spans.get((meta["accession_no"], sid), [])
        items, n_merged = _absorb_unchunked(result.items, file_spans, text)
        report = _filing_report(meta, sid, result, items, len(text), n_merged)
        if note := _length_note(len(text), prior_chars):
            for r in (report, filings[-1]):      # the pair is suspect: flag both filings
                r["notes"].append(note)
                r["section_suspect"] = True
            warnings.append(f"{meta['accession_no']}: {note}")
            logger.warning("%s %s: %s", ticker, meta["accession_no"], note)
        prior_chars = len(text)
        if result.low_coverage:
            logger.warning("%s %s %s: LOW COVERAGE %.1f%% (%s, %d items)", ticker, meta["form"],
                           meta["filing_date"], 100 * result.coverage, result.method, len(items))
        rows.extend(_item_rows(ticker, meta, sid, items, file_spans))
        filings.append(report)
    if chunks is None and filings:
        warnings.append("no chunks parquet: chunk_ids are empty")
    return _ticker_summary(settings, ticker, rows, filings, skipped, warnings, write)


def build_risk_items(settings: Settings | None = None, tickers: list[str] | None = None, *,
                     write: bool = True) -> dict:
    """Detect risk items for every annual filing (10-K risk section ``I.1A``, 20-F ``I.3``).

    Reads the acquisition manifest, ``data/interim/section_texts``, ``data/processed/chunks`` and
    the raw HTML; writes ``data/interim/risk_items/<TICKER>_risk_items.parquet`` (idempotent —
    rewritten atomically from scratch) unless ``write`` is False (coverage report only).

    Returns ``{ticker: {"n_items", "path", "written", "filings": [per-filing report incl.
    coverage / method / summary_found / summary_excluded / low_coverage / section_suspect],
    "skipped": [annual filings without risk section text], "warnings": [...],
    "below_coverage": [filings under COVERAGE_MIN], "suspect": [filings whose section length is far
    from the neighbouring filing's: coverage cannot vouch for those, e.g. ASML's paragraph units]}}``.
    """
    settings = settings or get_settings()
    manifest = load_manifest(settings)
    if not manifest:
        raise RuntimeError("no acquisition manifest — run semigraph.ingestion.download_filings first")
    tickers = list(tickers) if tickers else [t for t in FILERS if t in manifest]
    return {t: _build_ticker(settings, t, manifest.get(t, []), write) for t in tickers}
