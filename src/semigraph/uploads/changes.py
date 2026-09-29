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
tense-only edit). The invariant ``len(removed) + len(changed) + len(minor_rewordings) + unchanged_count ==
len(older units)`` always holds; ``unchanged_count`` now counts ONLY the aligner's own ``unchanged`` label.

Uploads use :data:`UPLOAD_PASSAGE_PARAMS`, their OWN instance of ``graph.passages.PassageParams`` — calibrated
(below) on the reviewer's exact repro ("the market is expected to grow" -> "...to shrink..., reversing the prior
forecast") so it still produces a passage (and so counts as ``changed``) while a tense-only edit ("will impact" ->
"impacted") does not (and lands in ``minor_rewordings``). ``graph/passages.py`` and its SEC-tuned defaults are
untouched — this module only supplies a different instance as its OWN default, never edits the shared one.

LIMITATION (not fixed here, out of this module's scope): ``present_min_ratio`` only governs the SENTENCE-level
PRESENT rule inside ``passages.py``. A UNIT the item-level aligner already calls ``unchanged`` outright — using its
own ``AlignParams.absence_min_ratio`` (default 85, SEC-tuned, untouched here per M4_PLAN.md 14.9) — never reaches
this module's passage logic at all, so it never even reaches ``minor_rewordings``. A pure-negation edit ("is
expected to grow" -> "is not expected to grow") measures ``partial_ratio`` ~90 against the WHOLE other section,
which is at or above that aligner threshold, and is filtered out before ``compare_versions`` sees it. Only
``AlignParams`` governs that boundary, and it is explicitly not upload-tunable here (M4_PLAN.md 15.6 asks only for
an upload-specific ``PassageParams``); fixing the negation case would need its own decision about tuning (or adding
an upload-specific) ``AlignParams``, reported as a separate seam rather than folded into this fix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..graph.alignment import AlignParams, align
from ..graph.passages import Passage, PassageParams, compute_passages
from ..hashing import content_hash
from .units import Unit, unit_rows

logger = logging.getLogger("semigraph.uploads.changes")

NOT_COMPARED_REASONS = (
    "identical_content", "parse_method_mismatch", "low_text_yield", "heading_coverage_mismatch", "too_many_units",
)
LOW_TEXT_YIELD_CHARS_PER_PAGE = 200.0
MAX_UNITS_FOR_COMPARISON = 400

# present_min_ratio raised from the SEC default (75) so that a meaning-reversing rewording (measured
# fuzz.partial_ratio ~87.5 on the "Market Outlook: grow -> shrink, reversing the prior forecast" fixture) is no
# longer treated as merely "still present" and so gets a real passage, while a genuine tense-only edit (measured
# ~95.5 on "will impact" -> "impacted") stays comfortably above the new floor and so still produces no passage.
# Calibrated on tests/upload_fixtures.py MARKET_REVERSAL_V1/V2 and TENSE_ONLY_V1/V2 (see test_serve_upload_changes.py).
UPLOAD_PASSAGE_PARAMS = PassageParams(present_min_ratio=90.0)


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
           "minor_rewordings": [], "unchanged_count": len(older.units)}


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


def _minor_rewording_entry(entry: dict) -> dict:
    return {"older_unit_id": entry["older_unit_id"], "newer_unit_id": entry["newer_unit_id"],
           "headline": entry["headline"]}


def compare_versions(older: VersionView, newer: VersionView, *, align_params: AlignParams = AlignParams(),
                     passage_params: PassageParams = UPLOAD_PASSAGE_PARAMS) -> dict:
    """The ``ChangeReport`` dict between two consecutive versions of the SAME document (module docstring)."""
    reason = _guard_reason(older, newer)
    if reason is not None:
        return _not_compared(reason, older)

    older_rows, newer_rows = unit_rows(older.text, older.units), unit_rows(newer.text, newer.units)
    result = align(older_rows, newer_rows, newer.text, older_section_text=older.text, params=align_params)
    passages = compute_passages(older_rows, newer_rows, result, older.text, newer.text,
                                older.chunk_spans, newer.chunk_spans, params=passage_params)

    newer_by_id = {r["item_id"]: r for r in newer_rows}
    newer_decision_by_id = {d.item_id: d for d in result.newer}
    changed = _seed_changed_entries(result, older_rows)
    _attach_passages(passages, changed, newer_decision_by_id, older, newer)

    older_by_id = {r["item_id"]: r for r in older_rows}
    removed = [_unit_dict(older_by_id[d.item_id]) for d in result.older if d.label == "removed"]
    added = [_unit_dict(newer_by_id[d.item_id]) for d in result.newer if d.label == "new"]
    changed_list = [entry for entry in changed.values() if entry["passages"]]
    minor_rewordings = [_minor_rewording_entry(entry) for entry in changed.values() if not entry["passages"]]
    unchanged_count = sum(1 for d in result.older if d.label == "unchanged")

    return {"items_compared": True, "not_compared_reason": None, "added": added, "removed": removed,
           "changed": changed_list, "minor_rewordings": minor_rewordings, "unchanged_count": unchanged_count}
