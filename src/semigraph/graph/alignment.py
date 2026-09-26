"""Alignment of risk ITEMS between two consecutive annual filings (M1b step 2, plan B.2-B.5).

Input: the older filing's items and the newer filing's items (rows with ``item_id``,
``text`` = headline + body, and optionally ``headline``, ``text_hash``, ``unit_kind``)
plus the newer filing's FULL section text. Output: per older item one of
``unchanged | reworded | merged | removed | uncertain`` and per newer item one of
``carried | new | uncertain``, each with the evidence that decided it and a
``decided_by`` in ``hash | headline | body | text_check | uncertain | llm | unmatched`` (``unmatched``, a newer item called
``new`` with no text check, is no longer produced: an unchecked item is ``uncertain``; the name stays for old tables).

The pipeline is pure and deterministic (no I/O, no network; the embedding is an
injected callable) and runs cheapest step first:

1. equal ``text_hash`` -> ``unchanged``                                    (decided_by ``hash``)
2. equal, non-empty body hash -> ``unchanged`` if the headlines agree, else ``reworded``
3. headline ``fuzz.ratio`` >= 90 (rapidfuzz, ``default_process``) -> ``reworded``  (``headline``)
4. one-to-one body assignment over the still-unmatched items (``body``): embedding
   cosine (only when ``embed`` is given) plus a lexical score; accept / reject / band
5. text-grounded absence check BEFORE any ``removed`` (``text_check``): the older item's
   headline and its two longest sentences are searched in the WHOLE newer section text
   (``rapidfuzz.fuzz.partial_ratio_alignment`` >= 85 for one sentence-sized needle against
   the un-split haystack, or exact normalised containment). Two of the three probes present
   -> ``merged`` with a verbatim quote; exactly one -> ``uncertain``; none -> ``removed``
   with the probes tried recorded.

Lexical score: word-level ``difflib.SequenceMatcher(autojunk=False)`` ratio ("greedy block
matching"), averaged over both argument orders because it is not symmetric. Character-level
ratios were rejected: unrelated 1,500-char chunks already score about 0.45. Word-level
Indel similarity is used only as a rigorous upper bound to skip pairs that cannot reach the
floor (every difflib match set is a common subsequence, so 2M/T <= Indel similarity).

Assignments use ``scipy.optimize.linear_sum_assignment`` (a modified Jonker-Volgenant
algorithm, Crouse 2016). It always fully assigns the smaller side and has no floor, so
infeasible cells are masked and every returned pair is filtered against the mask. Each step
runs in confidence tiers (accepted pairs before band pairs), so an item can never claim a
newer item that a higher-confidence step already claimed. Ties are broken by relative
document position, then deterministically by index. There is NO order constraint: sentences
and items move freely between filings.

One-to-one cannot express merges: an older item absorbed into a newer item's text is
``merged`` via step 5 and does not claim that newer item; a newer item that absorbed older
text is ``carried`` (decided_by ``text_check``) rather than ``new``.

FALSE-DROP GUARD (tested): an older item is ``removed`` only when NONE of its probes occurs
anywhere in the newer section, and ``apply_adjudication`` cannot turn an item with a text hit
into ``removed``. Every quote is a verbatim slice of the section text (span recorded) and
passes the ``extraction.gates.quote_in_chunk`` containment rule.

All thresholds live in ``AlignParams`` and are STARTING values (the plan's defaults where it
gives one; the lexical-only and probe-count values are this module's own) to be calibrated on
the frozen gold before any number is reported.

Complexity (n, m <= ~60 items, section of L chars, S sentences per item): hashing and headline
matching are O(nm); the lexical bound is O(nm) bit-parallel; exact difflib runs only for pairs
above the floor; one assignment is O(min(n,m)^2 * max(n,m)); each probe is one O(L) substring
scan plus, when not verbatim, one ``partial_ratio_alignment`` (about 3 ms on a 114k-char
section). Absence checks run only for unmatched items, so a full NVDA-size pair takes well
under a second without an embedding.

Limitations: an item that survives with sentences deleted stays ``reworded`` (the deleted
sentences are listed in ``Evidence.dropped_sentences`` as the audit trail, but item-level
alignment cannot call it removed, and a sentence edited by 20% or more of its words is listed
although it is still there: an audit trail, not proof); probe hits on generic same-company
boilerplate must be suppressed by the caller through ``boilerplate_sentences``; a probe
rewritten by ~30% of its words no longer hits (measured recall of the 85 cutoff on real
sentences: 5% edits 100%, 10% 99%, 15% 93%, 20% 64%, 30% 0%); items still ``uncertain`` after
adjudication must be treated as present, never Deleted; alignment quality is bounded by the
unit detector's segmentation.

Size: under the 800-line ceiling since the text-level helpers (normalisation, sentence splitting,
the word-level lexical score, ``SectionIndex`` and its verbatim ``Hit``) moved to
``graph/align_text.py``, which ``graph/passages.py`` (the sentence-level change layer) reuses.
``split_sentences`` is re-exported from here for existing importers. About 130 lines of this file
are documentation.
"""

import logging
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
from rapidfuzz import fuzz, process
from rapidfuzz.distance import Indel
from rapidfuzz.utils import default_process
from scipy.optimize import linear_sum_assignment

from ..hashing import content_hash
from .align_text import (
    TOKEN_RE,
    Hit,
    SectionIndex,
    at_least,
    in_range,
    lex_exact,
    norm,
    sentence_texts,
    word_tokens,
)
from .align_text import split_sentences as split_sentences        # noqa: F401  (re-exported public name)

logger = logging.getLogger("semigraph.graph.alignment")

OLDER_LABELS = ("unchanged", "reworded", "merged", "removed", "uncertain")
NEWER_LABELS = ("carried", "new", "uncertain")
DECIDED_BY = ("hash", "headline", "body", "text_check", "uncertain", "llm", "unmatched")
VERDICTS = ("same", "reworded", "removed")

_ASSIGN_BASE = 10.0        # cardinality first, then similarity (max similarity sum is 1 per pair)
_ASSIGN_TIE_EPS = 1e-6     # position-locality tie-break, far below any similarity difference


# --------------------------------------------------------------------------
# parameters and value objects
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AlignParams:
    """Every threshold of the alignment. STARTING values, to be calibrated on the frozen gold.

    ``headline_min_ratio``: ``fuzz.ratio`` (0-100, case/punctuation-insensitive) that makes two
    headlines "the same"; ``min_headline_tokens`` keeps one-word sub-headings out of that step.
    ``embed_accept`` / ``lex_accept`` / ``embed_reject`` are the plan's body rule: accept when
    embedding cosine >= 0.85 and lexical >= 0.45, reject when cosine < 0.65, the band between is
    ``uncertain``. Without an embedding this module uses ``lex_only_accept`` / ``lex_only_reject``
    (its own values: accept >= 0.75, reject < 0.30, band uncertain). ``min_body_tokens``: shorter
    items cannot be body-matched. ``absence_min_ratio``: ``partial_ratio`` (0-100) for a probe
    hit; probes are the headline plus the ``n_longest_sentences`` longest body sentences, each at
    least ``min_term_chars`` long (truncated to ``max_term_chars``); ``min_probe_hits`` of them
    must hit for ``merged`` and fewer hits are ``uncertain``. Deliberate relaxation: an item with
    fewer probes than that counts as present on a VERBATIM hit of all of them, never on a fuzzy one.
    """

    headline_min_ratio: float = 90.0
    min_headline_tokens: int = 4
    embed_accept: float = 0.85
    lex_accept: float = 0.45
    embed_reject: float = 0.65
    lex_only_accept: float = 0.75
    lex_only_reject: float = 0.30
    min_body_tokens: int = 10
    absence_min_ratio: float = 85.0
    min_term_chars: int = 40
    max_term_chars: int = 600
    n_longest_sentences: int = 2
    min_probe_hits: int = 2
    max_quote_chars: int = 400
    max_dropped_sentences: int = 200

    def __post_init__(self) -> None:
        in_range("headline_min_ratio", self.headline_min_ratio, 0.0, 100.0, open_low=True)
        in_range("absence_min_ratio", self.absence_min_ratio, 0.0, 100.0, open_low=True)
        for name in ("embed_accept", "lex_accept", "embed_reject", "lex_only_accept", "lex_only_reject"):
            in_range(name, getattr(self, name), 0.0, 1.0)
        if self.embed_reject > self.embed_accept:
            raise ValueError("embed_reject must not exceed embed_accept")
        if self.lex_only_reject > self.lex_only_accept:
            raise ValueError("lex_only_reject must not exceed lex_only_accept")
        at_least("min_headline_tokens", self.min_headline_tokens, 1)
        at_least("min_body_tokens", self.min_body_tokens, 0)
        at_least("min_term_chars", self.min_term_chars, 1)
        at_least("max_term_chars", self.max_term_chars, self.min_term_chars)
        at_least("n_longest_sentences", self.n_longest_sentences, 1)
        at_least("min_probe_hits", self.min_probe_hits, 1)
        at_least("max_quote_chars", self.max_quote_chars, 1)
        at_least("max_dropped_sentences", self.max_dropped_sentences, 0)


@dataclass(frozen=True)
class Evidence:
    """What decided a label. Every field is optional; absent means "not computed".

    ``embed_sim`` / ``lex_sim`` / ``headline_ratio``: scores against the matched item, or against
    the nearest unmatched candidate when there is no match. ``quote``: a verbatim slice of the
    section text searched (older decisions: the newer section; newer decisions: the older section
    when it was supplied) with ``quote_span`` its character offsets, ``quote_score`` the probe
    score and ``quote_item_id`` the counterpart item that holds the passage. ``search_terms`` are
    the probes tried (recorded for ``removed``), ``hit_terms`` those that hit.
    ``dropped_sentences`` (reworded items): sentences of the older item verifiably absent from the
    entire newer section.
    """

    embed_sim: float | None = None
    lex_sim: float | None = None
    headline_ratio: float | None = None
    quote: str | None = None
    quote_span: tuple[int, int] | None = None
    quote_score: float | None = None
    quote_item_id: str | None = None
    search_terms: tuple[str, ...] = ()
    hit_terms: tuple[str, ...] = ()
    dropped_sentences: tuple[str, ...] = ()
    nearest_id: str | None = None


@dataclass(frozen=True)
class OlderDecision:
    item_id: str
    label: str
    matched_newer_id: str | None
    decided_by: str
    evidence: Evidence


@dataclass(frozen=True)
class NewerDecision:
    item_id: str
    label: str
    matched_older_id: str | None
    decided_by: str
    evidence: Evidence


@dataclass(frozen=True)
class AlignmentResult:
    """Decisions in input order plus the parameters that produced them."""

    older: tuple[OlderDecision, ...]
    newer: tuple[NewerDecision, ...]
    params: AlignParams

    @property
    def uncertain_ids(self) -> tuple[str, ...]:
        """Older items awaiting adjudication (see ``apply_adjudication``)."""
        return tuple(d.item_id for d in self.older if d.label == "uncertain")


# --------------------------------------------------------------------------
# items
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Item:
    idx: int
    item_id: str
    headline: str
    text: str
    body: str
    text_hash: str
    body_hash: str
    tokens: tuple[str, ...]
    headline_tokens: int
    norm: str


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""      # None / NaN -> ""


def _split_body(headline: str, text: str) -> str:
    """The text without its leading headline (the whole text when it does not start with it)."""
    stripped = text.strip()
    if headline and stripped.startswith(headline):
        return stripped[len(headline):].strip()
    collapsed_text, collapsed_head = " ".join(stripped.split()), " ".join(headline.split())
    if collapsed_head and collapsed_text.startswith(collapsed_head):
        return collapsed_text[len(collapsed_head):].strip()
    return stripped


def _prepare(rows: Sequence[Mapping[str, Any]], side: str) -> list[_Item]:
    items: list[_Item] = []
    seen: set[str] = set()
    for idx, row in enumerate(rows):
        item_id = row.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError(f"{side} row {idx} has no usable 'item_id'")
        if item_id in seen:
            raise ValueError(f"duplicate {side} item_id {item_id!r}")
        seen.add(item_id)
        text = row.get("text")
        if not isinstance(text, str):
            raise ValueError(f"{side} item {item_id!r} has no 'text'")
        headline = "" if _clean(row.get("unit_kind")) == "paragraph" else _clean(row.get("headline"))
        body = _split_body(headline, text)
        items.append(_Item(
            idx=idx, item_id=item_id, headline=headline, text=text, body=body,
            text_hash=_clean(row.get("text_hash")) or content_hash(text),
            body_hash=content_hash(body) if body else "",
            tokens=word_tokens(text),
            headline_tokens=len(TOKEN_RE.findall(headline)), norm=norm(text)))
    return items


# --------------------------------------------------------------------------
# scores
# --------------------------------------------------------------------------

def _lex_matrix(o_tokens: Sequence[tuple[str, ...]], n_tokens: Sequence[tuple[str, ...]],
                floor: float, mask: np.ndarray | None = None) -> np.ndarray:
    """Exact lexical scores where they can reach ``floor``; NaN elsewhere.

    The word-level Indel similarity upper-bounds the difflib ratio, so cells whose bound is below
    the floor are skipped without changing any decision; ``mask`` skips further cells.
    """
    shape = (len(o_tokens), len(n_tokens))
    out = np.full(shape, np.nan)
    if 0 in shape:
        return out
    bound = process.cdist(list(o_tokens), list(n_tokens), scorer=Indel.normalized_similarity,
                          dtype=np.float64, workers=1)
    todo = bound >= floor - 1e-9
    if mask is not None:
        todo &= mask
    for i, j in zip(*np.nonzero(todo)):
        out[i, j] = lex_exact(o_tokens[i], n_tokens[j])
    return out


def _embedding_matrix(embed: Callable[[list[str]], np.ndarray],
                      older_texts: list[str], newer_texts: list[str]) -> np.ndarray:
    """Cosine similarities (older x newer) from one batched call of the injected embedder."""
    texts = older_texts + newer_texts
    vectors = np.asarray(embed(texts), dtype=float)
    if vectors.ndim != 2 or vectors.shape[0] != len(texts):
        raise ValueError(f"embed must return one row per text: got {vectors.shape} for {len(texts)} texts")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / np.where(norms == 0.0, 1.0, norms)
    return np.clip(vectors[:len(older_texts)] @ vectors[len(older_texts):].T, -1.0, 1.0)


def _headline_ratio(o: _Item, n: _Item) -> float | None:
    if not o.headline or not n.headline:
        return None
    return float(fuzz.ratio(o.headline, n.headline, processor=default_process))


def _same_headline(o: _Item, n: _Item, params: AlignParams) -> bool:
    if not o.headline and not n.headline:
        return True
    ratio = _headline_ratio(o, n)
    return ratio is not None and ratio >= params.headline_min_ratio


def _bodies_equal(o: _Item, n: _Item) -> bool:
    return bool(o.body) and o.body_hash == n.body_hash


def _assign_pairs(score: np.ndarray, allowed: np.ndarray) -> list[tuple[int, int]]:
    """One-to-one maximum-similarity matching restricted to ``allowed`` cells.

    scipy's solver always fully assigns the smaller side (no floor), so forbidden cells get weight 0
    and every returned pair is checked against ``allowed``. Allowed cells weigh ``base + score`` so
    a larger matching always beats a smaller one; exact ties go to the pair closest in relative
    document position.
    """
    if score.size == 0 or not allowed.any():
        return []
    n, m = score.shape
    locality = np.abs(np.arange(n)[:, None] / n - np.arange(m)[None, :] / m)
    weight = np.where(allowed, _ASSIGN_BASE + score - _ASSIGN_TIE_EPS * locality, 0.0)
    rows, cols = linear_sum_assignment(weight, maximize=True)
    return [(int(r), int(c)) for r, c in zip(rows, cols) if allowed[r, c]]


# --------------------------------------------------------------------------
# steps 2-4: pairing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Pair:
    o: int
    n: int
    label: str                       # unchanged | reworded | uncertain
    decided_by: str
    embed_sim: float | None = None
    lex_sim: float | None = None


@dataclass(frozen=True)
class _Nearest:
    n: int
    embed_sim: float | None
    lex_sim: float | None


def _pair_matrix(older: Sequence[_Item], newer: Sequence[_Item], o_free: Sequence[int],
                 n_free: Sequence[int], fn: Callable[[_Item, _Item], float]) -> np.ndarray:
    return np.array([[fn(older[i], newer[j]) for j in n_free] for i in o_free], dtype=float)


def _step_hash(older, newer, o_free, n_free, params) -> list[_Pair]:
    allowed = _pair_matrix(older, newer, o_free, n_free, lambda o, n: o.text_hash == n.text_hash) > 0
    return [_Pair(o_free[r], n_free[c], "unchanged", "hash")
            for r, c in _assign_pairs(np.zeros(allowed.shape), allowed)]


def _step_body_hash(older, newer, o_free, n_free, params) -> list[_Pair]:
    allowed = _pair_matrix(older, newer, o_free, n_free, lambda o, n: float(_bodies_equal(o, n))) > 0
    pairs = []
    for r, c in _assign_pairs(np.zeros(allowed.shape), allowed):
        o, n = older[o_free[r]], newer[n_free[c]]
        label = "unchanged" if _same_headline(o, n, params) else "reworded"
        pairs.append(_Pair(o.idx, n.idx, label, "hash"))
    return pairs


def _step_headline(older, newer, o_free, n_free, params) -> list[_Pair]:
    def ratio(o: _Item, n: _Item) -> float:
        if min(o.headline_tokens, n.headline_tokens) < params.min_headline_tokens:
            return 0.0
        return _headline_ratio(o, n) or 0.0

    ratios = _pair_matrix(older, newer, o_free, n_free, ratio)
    allowed = ratios >= params.headline_min_ratio
    score = np.zeros(ratios.shape)
    for r, c in zip(*np.nonzero(allowed)):       # equal headlines: the closer body wins
        score[r, c] = ratios[r, c] / 100.0 + 0.01 * lex_exact(older[o_free[r]].tokens, newer[n_free[c]].tokens)
    # equal bodies were paired by the previous step, so a headline pair here always has differing bodies
    return [_Pair(o_free[r], n_free[c], "reworded", "headline",
                  lex_sim=lex_exact(older[o_free[r]].tokens, newer[n_free[c]].tokens))
            for r, c in _assign_pairs(score, allowed)]


def _classify(embed: np.ndarray | None, lex: np.ndarray, params: AlignParams) -> tuple[np.ndarray, np.ndarray]:
    """(accept, reject) masks over a similarity matrix; everything else is the uncertain band."""
    lex0 = np.nan_to_num(lex, nan=0.0)
    if embed is None:
        return lex0 >= params.lex_only_accept, lex0 < params.lex_only_reject
    return (embed >= params.embed_accept) & (lex0 >= params.lex_accept), embed < params.embed_reject


def _step_body(older: Sequence[_Item], newer: Sequence[_Item], o_free: Sequence[int], n_free: Sequence[int],
               embed: Callable[[list[str]], np.ndarray] | None,
               params: AlignParams) -> tuple[list[_Pair], dict[int, _Nearest]]:
    """Step 4: accepted pairs first, then band pairs among what is left; plus each loser's nearest candidate."""
    o_c = [i for i in o_free if len(older[i].tokens) >= params.min_body_tokens]
    n_c = [j for j in n_free if len(newer[j].tokens) >= params.min_body_tokens]
    if not o_c or not n_c:
        return [], {}
    emb = _embedding_matrix(embed, [older[i].text for i in o_c], [newer[j].text for j in n_c]) if embed else None
    lex = _lex_matrix([older[i].tokens for i in o_c], [newer[j].tokens for j in n_c],
                      min(params.lex_accept, params.lex_only_reject),
                      None if emb is None else emb >= params.embed_reject)
    accept, reject = _classify(emb, lex, params)
    lex0 = np.nan_to_num(lex, nan=0.0)
    score = lex0 if emb is None else 0.5 * (emb + lex0)
    tier1 = _assign_pairs(score, accept)
    band = ~accept & ~reject
    for r, c in tier1:
        band[r, :] = False
        band[:, c] = False
    tier2 = _assign_pairs(score, band)

    def pair(r: int, c: int, label: str, decided_by: str) -> _Pair:
        return _Pair(o_c[r], n_c[c], label, decided_by,
                     None if emb is None else float(emb[r, c]),
                     None if np.isnan(lex[r, c]) else float(lex[r, c]))

    pairs = [pair(r, c, "reworded", "body") for r, c in tier1] + [pair(r, c, "uncertain", "uncertain") for r, c in tier2]
    paired_rows = {r for r, _ in tier1 + tier2}
    nearest = {o_c[r]: _nearest(r, score, emb, lex, n_c) for r in range(len(o_c)) if r not in paired_rows}
    return pairs, {k: v for k, v in nearest.items() if v is not None}


def _nearest(r: int, score: np.ndarray, emb: np.ndarray | None, lex: np.ndarray, n_c: list[int]) -> _Nearest | None:
    if score.shape[1] == 0 or not np.any(score[r] > 0):
        return None
    c = int(np.argmax(score[r]))
    return _Nearest(n_c[c], None if emb is None else float(emb[r, c]),
                    None if np.isnan(lex[r, c]) else float(lex[r, c]))


def _pair_items(older: Sequence[_Item], newer: Sequence[_Item],
                embed: Callable[[list[str]], np.ndarray] | None,
                params: AlignParams) -> tuple[list[_Pair], dict[int, _Nearest]]:
    """Steps 2-4: hash, body-hash, headline, then body assignment over what is still unmatched."""
    o_free, n_free = list(range(len(older))), list(range(len(newer)))
    pairs: list[_Pair] = []
    for step in (_step_hash, _step_body_hash, _step_headline):
        if not o_free or not n_free:
            break
        found = step(older, newer, o_free, n_free, params)
        pairs.extend(found)
        claimed_o, claimed_n = {p.o for p in found}, {p.n for p in found}
        o_free = [i for i in o_free if i not in claimed_o]
        n_free = [j for j in n_free if j not in claimed_n]
    body_pairs, nearest = _step_body(older, newer, o_free, n_free, embed, params)
    return pairs + body_pairs, nearest


# --------------------------------------------------------------------------
# step 5: text-grounded absence check
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Absence:
    probes: tuple[str, ...]
    hits: tuple[Hit, ...]


def _probes(item: _Item, boilerplate: frozenset[str], params: AlignParams) -> tuple[str, ...]:
    """The headline plus the longest body sentences (each long enough to be specific, none boilerplate)."""
    probes: list[str] = []
    if len(item.headline) >= params.min_term_chars and norm(item.headline) not in boilerplate:
        probes.append(item.headline)
    sentences = sentence_texts(item.body)
    ranked = sorted(range(len(sentences)), key=lambda k: (-len(sentences[k]), k))
    chosen: list[str] = []
    for k in ranked:
        sentence = sentences[k]
        if len(sentence) < params.min_term_chars or norm(sentence) in boilerplate:
            continue
        if norm(sentence) in {norm(p) for p in probes + chosen}:
            continue
        chosen.append(sentence)
        if len(chosen) == params.n_longest_sentences:
            break
    return tuple(p[:params.max_term_chars] for p in probes + chosen)


def _run_absence(item: _Item, index: SectionIndex, boilerplate: frozenset[str],
                 params: AlignParams) -> _Absence:
    probes = _probes(item, boilerplate, params)
    hits = tuple(h for h in (index.probe(p, params.absence_min_ratio, params.max_quote_chars) for p in probes)
                 if h is not None)
    return _Absence(probes, hits)


def _probe_verdict(evidence: Evidence, params: AlignParams) -> str:
    """absent | present | partial | unverifiable, from how many probes were found.

    ``present`` needs ``min_probe_hits`` hits. Deliberate relaxation: an item with FEWER probes than
    that (a headline-only item, a one-sentence paragraph) is present on all of them, but only when
    the hit is verbatim (score 100); a fuzzy single hit stays ``partial``.
    """
    n_probes, n_hits = len(evidence.search_terms), len(evidence.hit_terms)
    if n_probes == 0:
        return "unverifiable"
    if n_hits == 0:
        return "absent"
    needed = params.min_probe_hits
    if n_probes < needed and evidence.quote_score == 100.0:
        needed = n_probes
    return "present" if n_hits >= needed else "partial"


def _locate_item(quote: str, items: Sequence[_Item]) -> str | None:
    """The first counterpart item whose text contains the quoted passage."""
    needle = norm(quote)
    return next((item.item_id for item in items if needle and needle in item.norm), None)


def _with_absence(base: Evidence, absence: _Absence, counterpart: Sequence[_Item]) -> Evidence:
    best = max(absence.hits, key=lambda h: h.score, default=None)      # ties: first probe
    return replace(
        base, search_terms=absence.probes, hit_terms=tuple(h.term for h in absence.hits),
        quote=best.quote if best else None, quote_span=best.span if best else None,
        quote_score=best.score if best else None,
        quote_item_id=_locate_item(best.quote, counterpart) if best else None)


def _partner_quote(n: _Item, index: SectionIndex, params: AlignParams) -> Hit | None:
    """A verbatim quote for a reworded pair: the newer item's headline or first long sentence."""
    first_sentence = next(iter(sentence_texts(n.body)), "")
    for candidate in (n.headline, first_sentence):
        if len(candidate) >= params.min_term_chars:
            hit = index.probe(candidate[:params.max_term_chars], 100.0, params.max_quote_chars, fuzzy=False)
            if hit:
                return hit
    return None


def _dropped_sentences(o: _Item, index: SectionIndex, params: AlignParams) -> tuple[str, ...]:
    """Sentences of the older item verifiably absent from the whole newer section (audit trail)."""
    dropped: list[str] = []
    if params.max_dropped_sentences == 0:
        return ()
    for sentence in sentence_texts(o.text):
        if len(sentence) < params.min_term_chars:
            continue
        if index.probe(sentence[:params.max_term_chars], params.absence_min_ratio, params.max_quote_chars) is None:
            dropped.append(sentence)
            if len(dropped) >= params.max_dropped_sentences:
                break
    return tuple(dropped)


# --------------------------------------------------------------------------
# decisions
# --------------------------------------------------------------------------

def _pair_evidence(o: _Item, n: _Item, pair: _Pair) -> Evidence:
    return Evidence(embed_sim=pair.embed_sim, lex_sim=pair.lex_sim, headline_ratio=_headline_ratio(o, n))


def _older_paired(o: _Item, n: _Item, pair: _Pair, index: SectionIndex, params: AlignParams) -> OlderDecision:
    evidence = _pair_evidence(o, n, pair)
    if pair.label == "reworded":
        hit = _partner_quote(n, index, params)
        if hit:
            evidence = replace(evidence, quote=hit.quote, quote_span=hit.span, quote_score=hit.score,
                               quote_item_id=n.item_id)
        if pair.decided_by != "hash":
            evidence = replace(evidence, dropped_sentences=_dropped_sentences(o, index, params))
    return OlderDecision(o.item_id, pair.label, n.item_id, pair.decided_by, evidence)


def _older_unpaired(o: _Item, near: _Nearest | None, absence: _Absence, newer: Sequence[_Item],
                    params: AlignParams) -> OlderDecision:
    base = Evidence() if near is None else Evidence(
        embed_sim=near.embed_sim, lex_sim=near.lex_sim, headline_ratio=_headline_ratio(o, newer[near.n]),
        nearest_id=newer[near.n].item_id)
    evidence = _with_absence(base, absence, newer)
    verdict = _probe_verdict(evidence, params)
    if verdict == "absent":
        return OlderDecision(o.item_id, "removed", None, "text_check", evidence)
    if verdict == "present":
        return OlderDecision(o.item_id, "merged", evidence.quote_item_id, "text_check", evidence)
    return OlderDecision(o.item_id, "uncertain", None, "uncertain", evidence)


def _older_decisions(older: Sequence[_Item], newer: Sequence[_Item], pairs: Sequence[_Pair],
                     nearest: Mapping[int, _Nearest], index: SectionIndex, boilerplate: frozenset[str],
                     params: AlignParams) -> list[OlderDecision]:
    pair_by_o = {p.o: p for p in pairs}
    decisions = []
    for o in older:
        pair = pair_by_o.get(o.idx)
        if pair is None:
            absence = _run_absence(o, index, boilerplate, params)
            decisions.append(_older_unpaired(o, nearest.get(o.idx), absence, newer, params))
        elif pair.label == "uncertain":
            evidence = _with_absence(_pair_evidence(o, newer[pair.n], pair),
                                     _run_absence(o, index, boilerplate, params), newer)
            decisions.append(OlderDecision(o.item_id, "uncertain", newer[pair.n].item_id, "uncertain", evidence))
        else:
            decisions.append(_older_paired(o, newer[pair.n], pair, index, params))
    return decisions


def _newer_hints(newer: Sequence[_Item], older: Sequence[_Item], pairs: Sequence[_Pair],
                 older_index: SectionIndex | None, boilerplate: frozenset[str],
                 params: AlignParams) -> dict[str, Evidence]:
    """Per newer item: pair scores, plus (when the older section text is known) the absence check of
    the item against it. Reused when adjudication changes what a newer item is."""
    pair_by_n = {p.n: p for p in pairs}
    hints: dict[str, Evidence] = {}
    for n in newer:
        pair = pair_by_n.get(n.idx)
        evidence = _pair_evidence(older[pair.o], n, pair) if pair else Evidence()
        if older_index is not None and (pair is None or pair.label == "uncertain"):
            evidence = _with_absence(evidence, _run_absence(n, older_index, boilerplate, params), older)
        hints[n.item_id] = evidence
    return hints


def _derive_newer(newer_ids: Sequence[str], older: Sequence[OlderDecision], hints: Mapping[str, Evidence],
                  params: AlignParams) -> tuple[NewerDecision, ...]:
    """Newer labels as a pure function of the older decisions (so adjudication can recompute them)."""
    partner = {d.matched_newer_id: d for d in older if d.label in ("unchanged", "reworded")}
    pending = {d.matched_newer_id: d for d in older if d.label == "uncertain" and d.matched_newer_id}
    absorbed: dict[str, OlderDecision] = {}
    for d in older:
        if d.label == "merged" and d.matched_newer_id:
            absorbed.setdefault(d.matched_newer_id, d)
    out = []
    for nid in newer_ids:
        evidence = hints.get(nid, Evidence())
        if nid in partner:
            out.append(NewerDecision(nid, "carried", partner[nid].item_id, partner[nid].decided_by, evidence))
        elif nid in pending:
            out.append(NewerDecision(nid, "uncertain", pending[nid].item_id, "uncertain", evidence))
        elif nid in absorbed:
            out.append(NewerDecision(nid, "carried", absorbed[nid].item_id, "text_check", evidence))
        else:
            out.append(_unmatched_newer(nid, evidence, params))
    return tuple(out)


def _unmatched_newer(nid: str, evidence: Evidence, params: AlignParams) -> NewerDecision:
    """``new`` only when the item's probes were searched in the older section and none was found; ``uncertain`` when they could
    not be searched (no older section text, or the item has no probe): nothing was checked, so nothing is claimed."""
    verdict = _probe_verdict(evidence, params)
    if verdict == "absent":
        return NewerDecision(nid, "new", None, "text_check", evidence)
    if verdict == "present":
        return NewerDecision(nid, "carried", evidence.quote_item_id, "text_check", evidence)
    return NewerDecision(nid, "uncertain", None, "uncertain", evidence)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def align(older: Sequence[Mapping[str, Any]], newer: Sequence[Mapping[str, Any]],
          newer_section_text: str, *,
          embed: Callable[[list[str]], np.ndarray] | None = None,
          params: AlignParams = AlignParams(),
          older_section_text: str | None = None,
          boilerplate_sentences: Collection[str] = ()) -> AlignmentResult:
    """Align two consecutive annual filings' risk items, verified against the newer full text.

    ``older`` / ``newer``: rows with ``item_id``, ``text`` and optionally ``headline``,
    ``text_hash`` (computed with ``content_hash`` when absent) and ``unit_kind`` (``paragraph``
    units have no headline). ``embed``: batch embedder ``list[str] -> (n, d)`` array, called at
    most once and only with the items still unmatched after the hash and headline steps;
    ``None`` selects the lexical-only rule. ``older_section_text``: when given, unmatched newer
    items are verified against it too (a hit means ``carried``, no hit ``new``); without it, or for
    an item that has no probe, an unmatched newer item is ``uncertain`` (never ``new`` unchecked).
    ``boilerplate_sentences``: sentences (compared after normalisation) never used as probes.
    Decisions are returned in input order.
    """
    if not isinstance(newer_section_text, str):
        raise TypeError("newer_section_text must be a string")
    o_items, n_items = _prepare(older, "older"), _prepare(newer, "newer")
    boilerplate = frozenset(norm(s) for s in boilerplate_sentences)
    pairs, nearest = _pair_items(o_items, n_items, embed, params)
    newer_index = SectionIndex(newer_section_text)
    older_index = None if older_section_text is None else SectionIndex(older_section_text)
    older_decisions = _older_decisions(o_items, n_items, pairs, nearest, newer_index, boilerplate, params)
    hints = _newer_hints(n_items, o_items, pairs, older_index, boilerplate, params)
    result = AlignmentResult(tuple(older_decisions),
                             _derive_newer([n.item_id for n in n_items], older_decisions, hints, params), params)
    logger.info("aligned %d older / %d newer items: %s", len(o_items), len(n_items), summarize(result)["older"])
    return result


def _settle(d: OlderDecision, verdict: str, params: AlignParams) -> OlderDecision:
    """Apply one verdict to an uncertain decision, keeping the false-drop guard."""
    if d.label != "uncertain":
        raise ValueError(f"item {d.item_id!r} is {d.label}, not uncertain: nothing to adjudicate")
    if verdict in ("same", "reworded"):
        if d.matched_newer_id is None:
            raise ValueError(f"item {d.item_id!r} has no candidate newer item to pair with")
        return replace(d, label="unchanged" if verdict == "same" else "reworded", decided_by="llm")
    text_verdict = _probe_verdict(d.evidence, params)
    if text_verdict == "absent":
        return replace(d, label="removed", matched_newer_id=None, decided_by="llm")
    if text_verdict == "present":
        return replace(d, label="merged", matched_newer_id=d.evidence.quote_item_id, decided_by="text_check")
    logger.warning("item %s: LLM verdict 'removed' blocked, %s", d.item_id,
                   "the item has no probe the newer section could be searched for" if text_verdict == "unverifiable"
                   else "its text is partly present in the newer section")
    return d


def apply_adjudication(result: AlignmentResult, verdicts: Mapping[str, str]) -> AlignmentResult:
    """Apply LLM verdicts (``same | reworded | removed``) to ``uncertain`` older items.

    Returns a new result; the input is untouched. ``same`` / ``reworded`` pair the item with its
    candidate newer item (decided_by ``llm``). ``removed`` is subject to the false-drop guard: it
    stands (``llm``) only when the item's probes were searched and none is in the newer section;
    with enough hits the item becomes ``merged`` (``text_check``); with a single hit, or with no
    probe to search (the model's word alone is not a text check), it stays ``uncertain``.
    Newer labels are recomputed from the settled older decisions. Unknown ids, invalid verdicts,
    non-uncertain items, and same/reworded without a candidate raise ``ValueError``.

    An ``uncertain`` item WITHOUT a candidate (a fuzzy single probe hit, or no searchable probe)
    cannot be paired and cannot be removed: it stays ``uncertain``, and consumers must treat
    every item still ``uncertain`` as PRESENT (Active, never Deleted).
    """
    known = {d.item_id for d in result.older}
    unknown = sorted(set(verdicts) - known)
    if unknown:
        raise ValueError(f"unknown older item id(s): {unknown}")
    bad = {k: v for k, v in verdicts.items() if v not in VERDICTS}
    if bad:
        raise ValueError(f"invalid verdict(s) {bad}; expected one of {VERDICTS}")
    if not verdicts:
        return result
    older = tuple(_settle(d, verdicts[d.item_id], result.params) if d.item_id in verdicts else d
                  for d in result.older)
    newer = _derive_newer([n.item_id for n in result.newer], older,
                          {n.item_id: n.evidence for n in result.newer}, result.params)
    return AlignmentResult(older, newer, result.params)


def summarize(result: AlignmentResult) -> dict:
    """Counts: older labels, newer labels, ``decided_by`` over older decisions, dropped sentences."""
    older, newer = Counter(d.label for d in result.older), Counter(d.label for d in result.newer)
    deciders = Counter(d.decided_by for d in result.older)
    return {
        "older": {"total": len(result.older), **{label: older.get(label, 0) for label in OLDER_LABELS}},
        "newer": {"total": len(result.newer), **{label: newer.get(label, 0) for label in NEWER_LABELS}},
        "decided_by": {name: deciders.get(name, 0) for name in DECIDED_BY},
        "dropped_sentences": sum(len(d.evidence.dropped_sentences) for d in result.older),
    }
