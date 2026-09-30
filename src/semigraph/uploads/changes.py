"""Version-to-version change detection for uploaded documents (M4_PLAN.md 4.2, Worker A).

``compare_versions`` runs ``graph.alignment.align`` (LEXICAL ONLY — uploads are never embedded for alignment) over
the units of two consecutive versions, then ``graph.passages.compute_passages`` to find WHAT changed inside a unit
that survived as ``reworded`` / ``merged`` / ``uncertain``. Read those two modules (and their own tests) before
touching any threshold here; this module tunes them only through ``AlignParams`` / ``PassageParams``, never by
editing them (docs/v2/M4_PLAN.md 14.9: those files are not ours to change).

Every unit the aligner marks ``reworded`` / ``merged`` / ``uncertain`` stays VISIBLE (M4_PLAN.md 15.6, finding
#16): one with at least one surviving passage is ``changed``; one with none goes to :data:`minor_rewordings`
instead — it is NEVER folded into ``unchanged_count`` (an earlier revision did that, which silently hid a real
meaning change whenever ``passages.py``'s own PRESENT rule happened to fire on the whole unit, not just a
tense-only edit). The invariant is restated precisely at the end of this docstring, after the negation fix below
changes what feeds ``unchanged_count``.

Uploads use :data:`UPLOAD_PASSAGE_PARAMS`, their OWN instance of ``graph.passages.PassageParams`` — calibrated
(below) on the reviewer's exact repro ("the market is expected to grow" -> "...to shrink..., reversing the prior
forecast") so it still produces a passage (and so counts as ``changed``) while a tense-only edit ("will impact" ->
"impacted") does not (and lands in ``minor_rewordings``). ``graph/passages.py`` and its SEC-tuned defaults are
untouched — this module only supplies a different instance as its OWN default, never edits the shared one.

NEGATION POLARITY (Worker A3, fixes the LIMITATION above): a pure-negation edit ("is expected to grow" -> "is not
expected to grow"; "we will not renew" -> "we will renew"; "no material impact" -> "a material impact") barely
moves ``fuzz.partial_ratio`` — the one word usually stays comfortably above both ``PassageParams.present_min_ratio``
(even the raised 90 above) and the aligner's own ``AlignParams.absence_min_ratio``, so neither threshold alone can
ever tell a negation flip from a tense-only edit. Fixing it needed two changes, both evaluated on the real pipeline
(``tests/test_serve_upload_changes.py``) before being adopted, never inside ``graph/alignment.py`` or
``graph/passages.py`` (frozen, G3):

(a) :data:`UPLOAD_ALIGN_PARAMS` — evaluated first, per the plan. Raising ``absence_min_ratio`` was tried and
    rejected: that threshold only ever chooses among ``merged`` / ``removed`` / ``uncertain`` for an item the
    hash/headline/body steps left unmatched (``graph/alignment.py`` step 5) — ``reworded`` is decided exclusively
    by those earlier steps, so no ``absence_min_ratio`` can ever turn an unmatched item into a ``reworded`` one.
    What DOES help: a short one-line item (a "no material impact" style boilerplate statement, well under 10
    words counting its headline) falls under the SEC default ``min_body_tokens`` (10) and so never even enters the
    one-to-one body assignment; it is left unmatched, and the absence check's probes need >= 40 characters
    (``min_term_chars``) each, which such a short sentence often cannot supply — the item ends up merely
    ``uncertain`` with NO matched partner at all, which is exactly the partner the check in (b) needs. Lowering
    ``min_body_tokens`` alone (to 4) DOES make such units ``reworded`` with a real ``matched_newer_id`` — checked
    empirically: identical items, labels and passages on every existing fixture (``MD_V1``/``V2``,
    ``MARKET_REVERSAL_V1``/``V2``, ``TENSE_ONLY_V1``/``V2``) at ``min_body_tokens=4`` as at the SEC default of 10;
    the lower floor only ever pulls in additional SHORT items, it never changes an existing decision.
(b) A deterministic negation-polarity check (:func:`_negation_flip_passages`) over every unit the aligner calls
    ``unchanged`` or ``reworded`` (the labels that mean "the older item is still identifiably the newer one"; a
    ``removed`` / ``new`` / ``merged`` / ``uncertain`` item has no single stable partner to run a sentence-by-
    sentence comparison against). Both units' text are split into sentences with the SAME splitter
    ``graph/passages.py`` uses (``graph.align_text.split_sentences`` — never re-implemented here); each older
    sentence is paired with its best-matching newer sentence by the aligner's own word-level lexical score
    (``align_text.lex_exact``, floored at :data:`MIN_NEGATION_PAIR_SIMILARITY` so two unrelated sentences are
    never compared). A pair's negation POLARITY is the PARITY (odd/even) of how many negator words it contains —
    a fixed vocabulary (not/no/never/none/nor/cannot/without) plus any ``-n't`` contraction, counted on lowercased
    word tokens, with the fixed boilerplate phrases "without limitation" and "not limited to" stripped first
    (round-4 review, finding C4, extended in round 5: dropping "including, without limitation," or swapping it for
    its equally common synonym "including but not limited to" is a routine legal no-op, never a polarity change, so
    neither must ever itself move the count) — so a double negation that keeps the same parity on both sides (two
    negators become two different ones) is deliberately NOT a flip, while a single negator present on only one
    side always is. A flipped pair promotes its unit to ``changed`` with two new passages quoting the older sentence
    (``removed``) and the newer one (``added``), each clipped to its own chunk by the SAME rule
    :func:`_clip_to_chunk` already applies to every other passage.

    ROUND-4 REVIEW FIX (finding C2): a unit already carrying a real passage from ``compute_passages`` is no longer
    skipped outright — that passage tells only ITS OWN sentence's story, so a genuine negation flip sitting on a
    DIFFERENT sentence of the same unit used to be silently dropped. The check now always runs for such a unit
    too; a flip pair is appended only when its older sentence is not already covered by an existing
    ``removed``/``reworded`` quote (never a duplicate report of the same edit). A promoted ``unchanged`` unit is
    still removed from ``unchanged_count`` so the invariant below holds.

    ROUND-4 REVIEW FIX (finding S3): each older/newer sentence pair costs one ``lex_exact`` call (a
    ``SequenceMatcher``); with no bound, a unit with thousands of short sentences on each side could spend minutes
    here while holding the machine-wide upload slot. :func:`_negation_flip_passages` now refuses to run — return-
    ing ``None``, never a silent empty result — once the whole call's :data:`MAX_NEGATION_WORK_BUDGET` is exhausted;
    every skip is counted in the report's ``negation_check_skipped`` field and logged, never dropped silently.

    ROUND-5 REVIEW FIX (finding corUi-MEDIUM): a bound that only ever counted sentence PAIRS punished an ordinary
    in-cap document — an annual refresh with a 100-150 sentence section, every sentence's fiscal year bumped so no
    pair is a cheap byte-identical match — exactly as hard as the reviewer's pathological one (thousands of
    one-word "sentences"), because ``lex_exact`` itself costs roughly the PRODUCT of the two sentences' own
    lengths, not a flat "1" per pair. The budget is now charged in that same currency — ``len(older_tokens) *
    len(newer_tokens)`` for every pair actually compared (:data:`MAX_NEGATION_WORK_PER_UNIT` per unit pair,
    :data:`MAX_NEGATION_WORK_BUDGET` summed across the whole call) — so a document built of many ordinary, short
    sentences comfortably fits while one built of very many, or very long, sentences still trips the same bound.
    Every older sentence's newer candidates are also pre-filtered to the ones sharing at least one non-negator
    content word (:func:`_content_tokens`) before either is charged against the budget or handed to ``lex_exact``:
    a genuine edit almost always keeps something in common with its own older sentence; two unrelated sentences
    essentially never do, so this is free work saved on every realistic document without ever hiding a real flip.

    ROUND-5 REVIEW FIX (finding secRel-LOW, S3 partial): the pair-count bound alone never protected against ONE
    pathologically long sentence — ``lex_exact``'s ``SequenceMatcher`` degrades to roughly cubic time on a sentence
    built of few distinct, highly repeated tokens (the reviewer's ~1,600-word adversarial pair took over 100s by
    itself, as a single "pair"). A sentence longer than :data:`MAX_NEGATION_SENTENCE_TOKENS` words is now excluded
    from the check entirely — never handed to ``lex_exact`` on either side, whatever its content — so this cost can
    never be reached at all; the exclusion is counted in ``negation_check_skipped`` exactly like a budget skip,
    never silently, while the unit's OTHER, ordinary-length sentences are still compared normally.

The invariant ``len(removed) + len(changed) + len(minor_rewordings) + unchanged_count == len(older units)`` still
always holds; ``unchanged_count`` counts the aligner's own ``unchanged`` label MINUS any unit promoted by (b).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..graph.align_text import lex_exact, split_sentences, word_tokens
from ..graph.alignment import AlignParams, OlderDecision, align
from ..graph.passages import Passage, PassageParams, compute_passages
from ..hashing import content_hash
from .units import Unit, unit_rows

logger = logging.getLogger("semigraph.uploads.changes")

NOT_COMPARED_REASONS = (
    "identical_content", "parse_method_mismatch", "low_text_yield", "heading_coverage_mismatch", "too_many_units",
    # The two reasons `uploads.compare.compare_in_subprocess` reports when the sandboxed comparison subprocess
    # (uploads/sandbox.py, uploads/compare_worker.py) times out or crashes/returns malformed output — this module's
    # own `compare_versions` never returns either reason itself (M4_PLAN.md 4.2 "Do" step 3).
    "comparison_timeout", "comparison_failed",
)
LOW_TEXT_YIELD_CHARS_PER_PAGE = 200.0
MAX_UNITS_FOR_COMPARISON = 400

# present_min_ratio raised from the SEC default (75) so that a meaning-reversing rewording (measured
# fuzz.partial_ratio ~87.5 on the "Market Outlook: grow -> shrink, reversing the prior forecast" fixture) is no
# longer treated as merely "still present" and so gets a real passage, while a genuine tense-only edit (measured
# ~95.5 on "will impact" -> "impacted") stays comfortably above the new floor and so still produces no passage.
# Calibrated on tests/upload_fixtures.py MARKET_REVERSAL_V1/V2 and TENSE_ONLY_V1/V2 (see test_serve_upload_changes.py).
UPLOAD_PASSAGE_PARAMS = PassageParams(present_min_ratio=90.0)

# min_body_tokens lowered from the SEC default (10, counted over headline+body combined) so a short one-line item
# reaches the aligner's one-to-one lexical body assignment instead of going unmatched to the item-level absence
# check (module docstring, "(a)"). Every other threshold stays the SEC default -- raising absence_min_ratio was
# evaluated and rejected (it cannot turn an unmatched item into a "reworded" one; see the module docstring).
# Checked empirically against every existing fixture (MD_V1/V2, MARKET_REVERSAL_V1/V2, TENSE_ONLY_V1/V2): identical
# items, labels and passages at min_body_tokens=4 as at the SEC default of 10.
UPLOAD_ALIGN_PARAMS = AlignParams(min_body_tokens=4)

# A fixed negator vocabulary plus any "-n't" contraction (module docstring, "(b)"); counted on lowercased word
# tokens so "Not", "NOT" and "not" all count alike. "cannot" has no apostrophe and is matched as a whole word;
# every other negative auxiliary ("isn't", "won't", "doesn't", ...) is caught by the contraction pattern instead.
_NEGATORS = frozenset({"not", "no", "never", "none", "nor", "cannot", "without"})
_CONTRACTION_RE = re.compile(r"n['’]t\b", re.IGNORECASE)

# Round-4 review, finding C4 (extended round-5): "including, without limitation," and its equally common synonym
# "including but not limited to" are fixed legal boilerplate, not a polarity change -- both stripped from a
# sentence BEFORE counting negators (module docstring, "(b)") so dropping, adding, or swapping between them never
# flips a unit's reported negation count. "not limited to" is matched generally (not only after "but"), since the
# "but" is itself optional boilerplate wording. "without" and "not" themselves stay in _NEGATORS (a real
# "without X" <-> "with X" edit, or any other genuine "not", must still be caught); only these exact fixed phrases
# are excluded.
_BOILERPLATE_NEGATOR_PHRASE_RE = re.compile(r"\b(?:without\s+limitation|not\s+limited\s+to)\b", re.IGNORECASE)

# A deliberately conservative floor on the aligner's own word-level lexical score (align_text.lex_exact): the
# negation-polarity check (module docstring, "(b)") only ever compares two sentences this close to being "the same
# sentence, edited" -- never an unrelated pair that happens to share a negator by coincidence.
MIN_NEGATION_PAIR_SIMILARITY = 0.5

# Round-5 review, finding corUi-MEDIUM (module docstring): the round-4 bound counted sentence PAIRS, so an ordinary
# in-cap annual-refresh section (100-150 real sentences, none a byte-identical match across versions) tripped the
# SAME cap as the reviewer's original pathological input (thousands of one-word "sentences") -- lex_exact's real
# cost tracks the PRODUCT of the two sentences' own token lengths, not a flat "1" per pair. The budget is now
# charged as len(older_tokens) * len(newer_tokens) for every pair actually compared (after the shared-content-word
# pre-filter, :func:`_content_tokens`): MAX_NEGATION_WORK_PER_UNIT bounds one unit pair's own total;
# MAX_NEGATION_WORK_BUDGET bounds the SAME total summed across every unit pair in one compare_versions call, so a
# document with several large-but-individually-in-cap units still cannot add up to an unbounded total. Calibrated
# (tests/test_serve_upload_changes.py) so a 150-sentence, ~17-token-average annual-refresh section (measured cost
# ~6.5M) comfortably fits with room for a second such section in the same document, while a document built almost
# entirely of such sections still trips the bound before it can cost real wall-clock time. Either bound being hit
# means the check is SKIPPED (never silently) for that unit pair -- counted in the report's
# ``negation_check_skipped`` field, never treated as "checked and found nothing".
MAX_NEGATION_WORK_PER_UNIT = 20_000_000
MAX_NEGATION_WORK_BUDGET = 30_000_000

# Round-5 review, finding secRel-LOW (S3 partial): a bound on PAIRS or on total token-work still never protects
# against ONE pathologically long sentence -- lex_exact's SequenceMatcher degrades to roughly cubic time on a
# sentence built of few distinct, highly repeated tokens (measured: a single ~1,600-word adversarial sentence pair,
# a single "pair" under any cap above, took over 100s by itself). A sentence longer than this many word tokens is
# therefore excluded from the negation check entirely -- on EITHER side, never handed to lex_exact at all -- while
# the unit's other, ordinary-length sentences are still compared normally; the exclusion is counted in
# ``negation_check_skipped`` exactly like any other skip, never silently.
MAX_NEGATION_SENTENCE_TOKENS = 200


@dataclass(frozen=True)
class VersionView:
    """The one version's-worth of state ``compare_versions`` needs: its canonical text, structural units, the
    embedded chunk spans of that side (``(chunk_id, char_start, char_end)``, filling out passage citations), the
    parser ``method`` and ``chars_per_page`` (the ``not_compared_reason`` guards)."""

    text: str
    units: tuple[Unit, ...]
    chunk_spans: tuple[tuple[str, int, int], ...]
    method: str
    chars_per_page: float


def _has_headings(units: tuple[Unit, ...]) -> bool:
    return any(u.kind == "heading" for u in units)


def _unit_dict(row: dict) -> dict:
    return {"unit_id": row["item_id"], "headline": row["headline"], "text": row["text"]}


def _not_compared(reason: str, older: VersionView) -> dict:
    return {"items_compared": False, "not_compared_reason": reason, "added": [], "removed": [], "changed": [],
           "minor_rewordings": [], "unchanged_count": len(older.units), "negation_check_skipped": 0}


def _guard_reason(older: VersionView, newer: VersionView) -> str | None:
    if content_hash(older.text) == content_hash(newer.text):
        return "identical_content"
    if older.method != newer.method:
        return "parse_method_mismatch"
    if older.chars_per_page < LOW_TEXT_YIELD_CHARS_PER_PAGE or newer.chars_per_page < LOW_TEXT_YIELD_CHARS_PER_PAGE:
        return "low_text_yield"
    if _has_headings(older.units) != _has_headings(newer.units):
        return "heading_coverage_mismatch"
    if len(older.units) > MAX_UNITS_FOR_COMPARISON or len(newer.units) > MAX_UNITS_FOR_COMPARISON:
        return "too_many_units"
    return None


def _clip_to_chunk(passage: Passage, view: VersionView) -> tuple[str, str] | None:
    """The passage's quote clipped to the first chunk that overlaps it, verified to be a substring of that
    chunk's own text; ``None`` (drop the passage — never raise — log ids and lengths only, never the text) for any
    reason it cannot be cited cleanly: no overlapping chunk, an empty clip, or (should the bounds ever be wrong) a
    clipped quote that fails the substring check."""
    if not passage.chunk_ids:
        return None
    chunk_id = passage.chunk_ids[0]
    span = next(((s, e) for cid, s, e in view.chunk_spans if cid == chunk_id), None)
    if span is None:
        return None
    start, end = max(passage.char_start, span[0]), min(passage.char_end, span[1])
    if end <= start:
        return None
    quote, chunk_text = view.text[start:end], view.text[span[0]:span[1]]
    if quote not in chunk_text:
        logger.info("dropping passage %s (%s): clipped quote (len=%d) is not a substring of chunk %s",
                   passage.passage_id, passage.kind, len(quote), chunk_id)
        return None
    return quote, chunk_id


def _seed_changed_entries(result, older_rows: list[dict]) -> dict[str, dict]:
    older_by_id = {r["item_id"]: r for r in older_rows}
    changed = {}
    for d in result.older:
        if d.label in ("reworded", "merged", "uncertain"):
            changed[d.item_id] = {"older_unit_id": d.item_id, "newer_unit_id": d.matched_newer_id,
                                  "headline": older_by_id[d.item_id]["headline"], "passages": []}
    return changed


def _attach_passages(passages: tuple[Passage, ...], changed: dict[str, dict],
                     newer_decision_by_id: dict, older: VersionView, newer: VersionView) -> None:
    for p in passages:
        if p.kind in ("removed", "reworded"):
            entry_key, view = p.item_id, older
        else:                                             # "added": item_id is the NEWER unit; group via its partner
            decision = newer_decision_by_id.get(p.item_id)
            entry_key, view = (decision.matched_older_id if decision else None), newer
        if entry_key is None or entry_key not in changed:
            continue
        clipped = _clip_to_chunk(p, view)
        if clipped is None:
            logger.info("dropping passage %s (%s): no chunk overlaps char_start=%d char_end=%d",
                       p.passage_id, p.kind, p.char_start, p.char_end)
            continue
        quote, chunk_id = clipped
        changed[entry_key]["passages"].append({"quote": quote, "chunk_id": chunk_id, "kind": p.kind})


def _negation_count(sentence: str) -> int:
    """How many negator occurrences ``sentence`` contains: fixed-vocabulary word tokens (case-insensitive) plus
    any ``-n't`` contraction. Negation POLARITY is this count's PARITY (module docstring, "(b)"). The fixed
    boilerplate phrases "without limitation" and "not limited to" (finding C4, extended round 5) are stripped
    FIRST so neither can ever itself move the count."""
    sentence = _BOILERPLATE_NEGATOR_PHRASE_RE.sub(" ", sentence)
    tokens = word_tokens(sentence)
    return sum(1 for t in tokens if t in _NEGATORS) + len(_CONTRACTION_RE.findall(sentence))


def _content_tokens(tokens: tuple[str, ...]) -> frozenset[str]:
    """``tokens`` minus the negator vocabulary (round-5 review, finding corUi-MEDIUM): the shared-vocabulary
    pre-filter in :func:`_negation_flip_passages` only ever compares two sentences that still have SOME ordinary
    wording in common — negators are excluded here so that two sentences differing ONLY in negation ("is expected
    to grow" / "is not expected to grow") still count as sharing every other word."""
    return frozenset(t for t in tokens if t not in _NEGATORS)


def _best_sentence_match(tokens: tuple[str, ...], candidates: list[tuple[str, ...]]) -> tuple[int | None, float]:
    """The index of the candidate closest to ``tokens`` by the aligner's own word-level lexical score, and that
    score; ``(None, -1.0)`` when there are no candidates. Ties keep the earliest index (strict ``>``)."""
    best_idx, best_score = None, -1.0
    for i, candidate in enumerate(candidates):
        score = lex_exact(tokens, candidate)
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx, best_score


def _clip_sentence(sentence: str, char_start: int, char_end: int, view: VersionView) -> tuple[str, str] | None:
    """``sentence`` (already known to be ``view.text[char_start:char_end]``) clipped to its first overlapping
    chunk, via the SAME rule (:func:`_clip_to_chunk`) every other passage in this module goes through."""
    chunk_ids = tuple(cid for cid, cs, ce in view.chunk_spans if cs < char_end and ce > char_start)
    stub = Passage(passage_id="negation:x", kind="removed", item_id="", seq=0, text=sentence,
                  char_start=char_start, char_end=char_end, counterpart_text=None, counterpart_span=None,
                  similarity=None, chunk_ids=chunk_ids, decided_by="sentence_absent")
    return _clip_to_chunk(stub, view)


def _negation_candidates(older_tokens: list[tuple[str, ...]], newer_tokens: list[tuple[str, ...]],
                         older_keep: list[int], newer_keep: list[int]) -> tuple[dict[int, list[int]], int]:
    """For every kept older sentence index, the kept newer sentence indices sharing at least one non-negator
    content token with it (round-5 review, finding corUi-MEDIUM; :func:`_content_tokens`), and the total
    token-work cost (``len(older_tokens) * len(newer_tokens)`` summed over every candidate pair) of comparing
    them all — the same currency :data:`MAX_NEGATION_WORK_PER_UNIT` / :data:`MAX_NEGATION_WORK_BUDGET` are
    charged in."""
    newer_content = {j: _content_tokens(newer_tokens[j]) for j in newer_keep}
    candidates_by_older: dict[int, list[int]] = {}
    total_cost = 0
    for i in older_keep:
        older_content = _content_tokens(older_tokens[i])
        candidates = [j for j in newer_keep if newer_content[j] & older_content]
        candidates_by_older[i] = candidates
        total_cost += len(older_tokens[i]) * sum(len(newer_tokens[j]) for j in candidates)
    return candidates_by_older, total_cost


def _negation_flip_quotes(older_row: dict, newer_row: dict, older: VersionView, newer: VersionView,
                          tokens: tuple[list[tuple[str, ...]], list[tuple[str, ...]]],
                          spans: tuple[list[tuple[int, int]], list[tuple[int, int]]],
                          older_keep: list[int], candidates_by_older: dict[int, list[int]]) -> list[dict]:
    """The removed/added quote pairs themselves, once the token-work budget check has already passed: every kept
    older sentence's best-matching candidate (the aligner's own word-level lexical score, floored at
    :data:`MIN_NEGATION_PAIR_SIMILARITY`), reported only when its negation polarity (module docstring) differs."""
    older_tokens, newer_tokens = tokens
    older_spans, newer_spans = spans
    older_text, older_base = older_row["text"], older_row["char_start"]
    newer_text, newer_base = newer_row["text"], newer_row["char_start"]
    out: list[dict] = []
    for i in older_keep:
        candidates = candidates_by_older[i]
        if not candidates:
            continue
        a, b = older_spans[i]
        older_sentence = older_text[a:b]
        local_idx, score = _best_sentence_match(older_tokens[i], [newer_tokens[j] for j in candidates])
        if local_idx is None or score < MIN_NEGATION_PAIR_SIMILARITY:
            continue
        j = candidates[local_idx]
        c, e = newer_spans[j]
        newer_sentence = newer_text[c:e]
        if _negation_count(older_sentence) % 2 == _negation_count(newer_sentence) % 2:
            continue                                          # same parity: no flip (a double negation cancels)
        removed = _clip_sentence(older_sentence, older_base + a, older_base + b, older)
        added = _clip_sentence(newer_sentence, newer_base + c, newer_base + e, newer)
        if removed is None or added is None:
            logger.info("dropping negation-flip quote (older_len=%d newer_len=%d): could not clip both sides "
                       "to a chunk", len(older_sentence), len(newer_sentence))
            continue
        out.append({"quote": removed[0], "chunk_id": removed[1], "kind": "removed"})
        out.append({"quote": added[0], "chunk_id": added[1], "kind": "added"})
    return out


def _negation_flip_passages(older_row: dict, newer_row: dict, older: VersionView, newer: VersionView,
                            budget: dict) -> list[dict] | None:
    """Removed/added quote pairs for every sentence of ``older_row`` whose negation polarity (module docstring)
    differs from its best-matching sentence of ``newer_row``. Both sides are split with the SAME sentence
    splitter ``graph/passages.py`` uses (``graph.align_text.split_sentences``).

    Any sentence over :data:`MAX_NEGATION_SENTENCE_TOKENS` word tokens, on EITHER side, is excluded from
    consideration entirely (round-5 review, finding secRel-LOW / S3 partial: ``lex_exact`` is roughly cubic in a
    long, low-diversity sentence's own length, regardless of how few pairs the unit has). Every remaining older
    sentence's candidates are pre-filtered by :func:`_negation_candidates` before either is charged against the
    budget or handed to ``lex_exact`` (:func:`_negation_flip_quotes`).

    Returns ``None`` — never a silently-empty list — when nothing is left to compare after the length exclusion,
    or when this unit pair's own token-work total exceeds :data:`MAX_NEGATION_WORK_PER_UNIT`, or once
    ``budget["remaining"]`` (the whole ``compare_versions`` call's shared :data:`MAX_NEGATION_WORK_BUDGET`) cannot
    cover it: the caller must record the skip, never treat ``None`` as "checked and found nothing".
    ``budget["remaining"]`` is decremented by the exact token-work spent whenever the check DOES run;
    ``budget["oversized_sentences"]`` is incremented whenever a length exclusion happened but the check still ran
    on the unit's other sentences — the caller must fold both into the report's skip count, never only one."""
    older_text, newer_text = older_row["text"], newer_row["text"]
    older_spans, newer_spans = split_sentences(older_text), split_sentences(newer_text)
    if not newer_spans or not older_spans:
        return []

    older_tokens = [word_tokens(older_text[a:b]) for a, b in older_spans]
    newer_tokens = [word_tokens(newer_text[a:b]) for a, b in newer_spans]
    oversized = any(len(toks) > MAX_NEGATION_SENTENCE_TOKENS for toks in older_tokens + newer_tokens)
    older_keep = [i for i, toks in enumerate(older_tokens) if len(toks) <= MAX_NEGATION_SENTENCE_TOKENS]
    newer_keep = [j for j, toks in enumerate(newer_tokens) if len(toks) <= MAX_NEGATION_SENTENCE_TOKENS]
    if not older_keep or not newer_keep:
        return None

    candidates_by_older, total_cost = _negation_candidates(older_tokens, newer_tokens, older_keep, newer_keep)
    if total_cost > MAX_NEGATION_WORK_PER_UNIT or total_cost > budget["remaining"]:
        return None
    budget["remaining"] -= total_cost

    out = _negation_flip_quotes(older_row, newer_row, older, newer, (older_tokens, newer_tokens),
                                (older_spans, newer_spans), older_keep, candidates_by_older)
    if oversized:
        budget["oversized_sentences"] = budget.get("oversized_sentences", 0) + 1
    return out


def _unclaimed_flip_pairs(flips: list[dict], existing_passages: list[dict]) -> list[dict]:
    """``flips`` (removed/added pairs, in that order) filtered down to the ones whose OLDER (``removed``) sentence
    is not already quoted, in whole or in part, by an existing ``removed``/``reworded`` passage of the same unit
    (finding C2): a real ``compute_passages`` passage on one sentence must never suppress a genuine negation flip
    on a DIFFERENT sentence of the same unit, but the same sentence must also never be reported twice."""
    out: list[dict] = []
    for i in range(0, len(flips) - 1, 2):
        removed, added = flips[i], flips[i + 1]
        already_quoted = any(p["kind"] in ("removed", "reworded") and removed["quote"] in p["quote"]
                             for p in existing_passages)
        if not already_quoted:
            out.extend((removed, added))
    return out


def _apply_negation_flips(older_decisions: Sequence[OlderDecision], older_by_id: dict, newer_by_id: dict,
                          older: VersionView, newer: VersionView, changed: dict[str, dict]) -> tuple[set[str], int]:
    """Promote a unit the aligner calls ``unchanged`` or ``reworded`` to ``changed`` (or append to it) when a
    sentence pair inside it flips negation polarity (module docstring, "(b)"). Mutates ``changed`` in place —
    appending to an existing entry's passages (finding C2: even one ``compute_passages`` already gave a real
    passage for a DIFFERENT sentence), or adding a fresh entry for a promoted ``unchanged`` unit — and returns
    ``(promoted, skipped)``: the ids of ``unchanged`` units promoted this way (the caller must subtract these from
    ``unchanged_count`` to keep the module's invariant) and a COUNT of unit pairs the check did not fully examine —
    a whole unit pair skipped under the token-work bound, or one where at least one oversized sentence was excluded
    (findings S3 and secRel-LOW; never silently — the caller must surface this in the report)."""
    promoted: set[str] = set()
    skipped = 0
    budget = {"remaining": MAX_NEGATION_WORK_BUDGET}
    for d in older_decisions:
        if d.label not in ("unchanged", "reworded") or not d.matched_newer_id:
            continue
        older_row, newer_row = older_by_id.get(d.item_id), newer_by_id.get(d.matched_newer_id)
        if older_row is None or newer_row is None or older_row["text"] == newer_row["text"]:
            continue
        entry = changed.get(d.item_id)
        existing_passages = entry["passages"] if entry is not None else []
        flips = _negation_flip_passages(older_row, newer_row, older, newer, budget)
        if flips is None:
            skipped += 1
            continue
        new_pairs = _unclaimed_flip_pairs(flips, existing_passages)
        if not new_pairs:
            continue
        if entry is None:
            changed[d.item_id] = {"older_unit_id": d.item_id, "newer_unit_id": d.matched_newer_id,
                                  "headline": older_row["headline"], "passages": new_pairs}
            promoted.add(d.item_id)
        else:
            entry["passages"].extend(new_pairs)
    skipped += budget.get("oversized_sentences", 0)
    if skipped:
        logger.warning("negation-flip check skipped for %d unit pair(s): sentence-count cap, token-work budget or "
                       "an oversized sentence", skipped)
    return promoted, skipped


def _minor_rewording_entry(entry: dict) -> dict:
    return {"older_unit_id": entry["older_unit_id"], "newer_unit_id": entry["newer_unit_id"],
           "headline": entry["headline"]}


def compare_versions(older: VersionView, newer: VersionView, *, align_params: AlignParams = UPLOAD_ALIGN_PARAMS,
                     passage_params: PassageParams = UPLOAD_PASSAGE_PARAMS) -> dict:
    """The ``ChangeReport`` dict between two consecutive versions of the SAME document (module docstring)."""
    reason = _guard_reason(older, newer)
    if reason is not None:
        return _not_compared(reason, older)

    older_rows, newer_rows = unit_rows(older.text, older.units), unit_rows(newer.text, newer.units)
    result = align(older_rows, newer_rows, newer.text, older_section_text=older.text, params=align_params)
    passages = compute_passages(older_rows, newer_rows, result, older.text, newer.text,
                                older.chunk_spans, newer.chunk_spans, params=passage_params)

    older_by_id = {r["item_id"]: r for r in older_rows}
    newer_by_id = {r["item_id"]: r for r in newer_rows}
    newer_decision_by_id = {d.item_id: d for d in result.newer}
    changed = _seed_changed_entries(result, older_rows)
    _attach_passages(passages, changed, newer_decision_by_id, older, newer)
    promoted_unchanged, negation_check_skipped = _apply_negation_flips(result.older, older_by_id, newer_by_id,
                                                                       older, newer, changed)

    removed = [_unit_dict(older_by_id[d.item_id]) for d in result.older if d.label == "removed"]
    added = [_unit_dict(newer_by_id[d.item_id]) for d in result.newer if d.label == "new"]
    changed_list = [entry for entry in changed.values() if entry["passages"]]
    minor_rewordings = [_minor_rewording_entry(entry) for entry in changed.values() if not entry["passages"]]
    unchanged_count = sum(1 for d in result.older if d.label == "unchanged") - len(promoted_unchanged)

    return {"items_compared": True, "not_compared_reason": None, "added": added, "removed": removed,
           "changed": changed_list, "minor_rewordings": minor_rewordings, "unchanged_count": unchanged_count,
           "negation_check_skipped": negation_check_skipped}
