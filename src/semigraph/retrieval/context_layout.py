"""The layout of the answer context, written once: the block headers and the temporal block.

Two consumers must agree on this text, so it lives in one pure module (no model, no database, no litellm) that both
import: ``answerer`` WRITES the temporal block for the model, and ``verify`` READS it back to decide which chunk ids
may support a sentence that claims a removal (:func:`removal_supported_ids`). Every label the reader matches on is a
constant here and the writer builds its lines from the same constants, so the two cannot drift.

The temporal block (docs/v2/M1B_PLAN.md L.7), per compared company::

    <company>: <form> filed <date> (accession ...) compared with <form> filed <date> (accession ...)
    No longer appears as a separate risk factor - showing 2 of 21 risk factors (the text check found no matching text in the
    newer filing; parts of their content may be covered inside other risk factors):
    - "<headline or first sentence>" [chunk id] [chunk id]
    Not matched (the text check could not verify whether these older risk factors still appear; they may have been removed or
    absorbed into another risk factor) - showing 6 of 14:
    - "<headline or first sentence>" [older chunk id]
    No matching risk factor found in the earlier filing - showing 1 of 12 risk factors (new, or a restructured older risk factor):
    Reworded - ...
    Passages of surviving risk factors whose wording was not found in the newer filing (showing 4 of 12; a differently worded
    version of the same statement may exist):
    - in "<containing item>": "<quoted text>" [chunk id]
    Passages of surviving risk factors whose wording was not found in the older filing (showing N of M; a differently worded
    version of the same statement may exist): ...
    Passages of surviving risk factors that were reworded (showing N of M): ...

WHY THE HEADINGS ARE HEDGED (M1b held-out gold: 2 blind annotators, ties adjudicated). Every heading is exactly as strong as the
measured precision of the claim beneath it. An older item the pipeline calls ``removed`` was gone as a STANDALONE risk factor in 4
of 4 cases, but 2 of the 4 were absorbed into another risk factor (annotators label those ``merged``), so the heading says "no
longer appears as a separate risk factor", never "removed" or "text verified absent". A newer item called ``new`` was new in
only 6 of 12 (the rest were ``carried``: already disclosed earlier, e.g. split out of an older item), so the heading says "no
matching risk factor found in the earlier filing (new, or a restructured older risk factor)". Removed passages (sentences) are
right about 0.88 of the time (6 of 51 are still stated in different words), added passages about 0.98 (1 of 48), so both say the
wording "was not found" and that a differently worded version may exist. Only a heading's LABEL may be relied on by a reader
(:func:`removal_supported_ids`); the sub-headings are not part of ``template_fingerprint()`` (only ``CONTEXT_HEADERS`` is).

The "Not matched" section lists OLDER items the text check could not settle (``RiskItem.unsettled_in``): they are neither
verified present nor verified gone, the heading claims nothing, they cite the older filing's chunk ids like removed items, and
they are NOT a removal: :func:`removal_supported_ids` never counts them. It is printed only when there is something to say
(a comparison in which the text check settled every item has no such section, not a "none found" line).

A pair the loader marked ``items_compared = false`` renders ``comparison not available (<reason>)`` and nothing else:
no removed / not matched / added / reworded lines and no "none found", because "nothing changed" would be a claim nobody verified.
"""

import re
from collections.abc import Iterable, Mapping, Sequence

from .ids import CITE_RE

# The full context the answering model (and the faithfulness judge) sees, as one template: each header is followed by its
# block, in ContextBlocks order. Every consumer that must invert or rebuild the context (eval/bakeoff, verify) imports this:
# a header is never retyped elsewhere. The temporal header is static: the compared filings are named per company inside
# the block (several companies can be compared in one answer).
CONTEXT_HEADERS = (
    "RELATIONSHIPS:\n",
    "\n\nEXTERNAL REGULATORY EVENTS (Federal Register rules linked by keyword; not the company's disclosure):\n",
    "\n\nMETRICS:\n",
    "\n\nACTIVE RISKS:\n",
    "\n\nRISK FACTORS REMOVED / ADDED / REWORDED between annual filings (text-verified):\n",
    "\n\nEXCERPTS:\n",
)
# The pre-M1b template (five blocks; DROPPED RISK LINEAGES instead of the external and temporal blocks): saved contexts
# from earlier benchmark runs still carry these headers.
LEGACY_CONTEXT_HEADERS = ("RELATIONSHIPS:\n", "\n\nMETRICS:\n", "\n\nACTIVE RISKS:\n",
                          "\n\nDROPPED RISK LINEAGES:\n", "\n\nEXCERPTS:\n")
TEMPORAL_HEADER, AFTER_TEMPORAL_HEADER = CONTEXT_HEADERS[4], CONTEXT_HEADERS[5]

NONE_BLOCK = "(none)"
MAX_CHUNK_IDS_PER_ITEM = 3          # ids printed per side of a temporal item, per passage (and per relation edge)
HEADLINE_MAX_CHARS = 240
LEAD_MAX_CHARS = 160                # a headline-less paragraph unit is labelled with its first sentence, cut here
PASSAGE_QUOTE_CHARS = 450           # a passage is quoted up to here: graph/passages.py closes a passage at max_passage_chars = 450
                                    # (only a single sentence longer than that stays whole and is clipped here)
PASSAGE_WHERE_CHARS = 100           # the containing item's label beside a passage

# --- the labels the reader matches on (and the writer builds from) ---
NOT_COMPARED_PREFIX = "comparison not available"
UNSETTLED_CHANGE = "unsettled"                               # the ``change`` of an older item the text check could not settle
# The item lists. ``{unit}`` / ``{units}`` are the noun of the listed units ("risk factor" / "paragraph"): a paragraph unit of a
# filing with no headlines is not a risk factor, and the heading never says it is. Each heading says only what the check found
# (see the module docstring for the measured precision behind every hedge).
REMOVED_ITEMS_PREFIX = "No longer appears as a separate "   # + "<unit>": the removed ITEMS list (no matching text found)
UNSETTLED_ITEMS_PREFIX = "Not matched ("                    # the unsettled ITEMS list (verified neither present nor gone)
NEW_ITEMS_PREFIX = "No matching "                           # + "<unit> found in the earlier filing": the newer items (new, or restructured)
_ITEM_SECTIONS = (
    ("removed", REMOVED_ITEMS_PREFIX + "{unit}",
     "the text check found no matching text in the newer filing; parts of their content may be covered inside other {units}"),
    (UNSETTLED_CHANGE, "Not matched", ""),                   # label and note unused: _unsettled_section writes its own heading
    ("new", NEW_ITEMS_PREFIX + "{unit} found in the earlier filing", "new, or a restructured older {unit}"),
    ("reworded", "Reworded", "still disclosed, wording changed"))
_UNIT_SINGULAR = {"risk factors": "risk factor", "paragraphs": "paragraph",
                  "risk factors and paragraphs": "risk factor or paragraph"}
# The heading claims nothing: "removed" appears only as a possibility, never as a fact. The unit noun is fixed on purpose (the
# wording is the owner's); a list of paragraph units is told to the model by the answer prompt.
_UNSETTLED_HEADING = (UNSETTLED_ITEMS_PREFIX + "the text check could not verify whether these older risk factors still appear; they "
                      "may have been removed or absorbed into another risk factor) - showing {shown} of {total}:")
PASSAGES_PREFIX = "Passages of surviving "                  # "... risk factors whose wording was not found in the newer filing (...):"
# The two lists differ in the LAST word pair ("newer" / "older"), and the reader matches the whole phrase, so an added passage
# (whose wording was not found in the OLDER filing) can never be read as a removal.
PASSAGES_REMOVED_PHRASE = "whose wording was not found in the newer filing"   # the removed PASSAGES list (an older sentence with no match)
PASSAGES_ADDED_PHRASE = "whose wording was not found in the older filing"      # the added PASSAGES list (a newer sentence with no match)
_PASSAGE_HEDGE = "; a differently worded version of the same statement may exist"
_PASSAGE_SECTIONS = (("removed", PASSAGES_REMOVED_PHRASE, _PASSAGE_HEDGE), ("added", PASSAGES_ADDED_PHRASE, _PASSAGE_HEDGE),
                     ("reworded", "that were reworded", ""))
_ITEM_LINE_PREFIX = "- "


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def first_sentence(text: str | None, limit: int = LEAD_MAX_CHARS) -> str:
    """The first sentence of ``text`` on one line, cut at ``limit`` characters ("" when there is none)."""
    flat = _flat(text)
    end = re.search(r"[.!?](?=\s+[A-Z(\"“])", flat)
    return _cut(flat[:end.end()] if end else flat, limit)


def item_label(item: Mapping) -> str:
    """How a risk item is named: its headline, else (a paragraph unit) its first sentence, else a placeholder."""
    headline = _flat(item.get("headline"))
    if headline:
        return _cut(headline, HEADLINE_MAX_CHARS)
    lead = first_sentence(item.get("lead_text"))
    return lead or f"(untitled paragraph, section {item.get('section_id')})"


def _noun(rows: Iterable[Mapping]) -> str:
    """``risk factors`` or ``paragraphs`` (20-F filers, filings with no headlines) according to ``unit_kind``."""
    kinds = {row.get("unit_kind") or row.get("item_unit_kind") or "headline" for row in rows}
    if kinds == {"paragraph"}:
        return "paragraphs"
    return "risk factors and paragraphs" if "paragraph" in kinds else "risk factors"


def _id_list(chunk_ids: Sequence[str] | None, valid_ids: set[str]) -> str:
    """`` [id] [id]`` for the first ids of a list (the ones printed become citable); empty when there are none."""
    shown = list(chunk_ids or [])[:MAX_CHUNK_IDS_PER_ITEM]
    valid_ids.update(shown)
    return "".join(f" [{i}]" for i in shown)


def _item_line(item: Mapping, valid_ids: set[str]) -> str:
    line = f'- "{item_label(item)}"'
    older, newer = _id_list(item.get("older_chunk_ids"), valid_ids), _id_list(item.get("newer_chunk_ids"), valid_ids)
    if item["change"] in ("removed", UNSETTLED_CHANGE):       # both cite the OLDER filing: the item is quoted from it
        return line + older
    if item["change"] == "new":
        return line + newer
    meta = [f'earlier wording: "{_flat(item["older_headline"])}"'] if item.get("older_headline") else []
    meta += [f"decided by {item['decided_by']}"] if item.get("decided_by") else []
    line += f" ({'; '.join(meta)})" if meta else ""
    return line + (f" earlier{older}" if older else "") + (f" later{newer}" if newer else "")


def _quote(text: str | None) -> str:
    return _cut(_flat(text).replace('"', "'"), PASSAGE_QUOTE_CHARS)


def _passage_line(passage: Mapping, valid_ids: set[str]) -> str:
    where = _cut(item_label({"headline": passage.get("item_headline"), "lead_text": passage.get("lead_text"),
                             "section_id": passage.get("section_id")}), PASSAGE_WHERE_CHARS)
    ids = _id_list(passage.get("chunk_ids"), valid_ids)
    if passage.get("kind") != "reworded":
        return f'- in "{where}": "{_quote(passage.get("text"))}"{ids}'
    # A reworded passage carries the older text (cited by its own chunk ids) and the counterpart found in the newer
    # section. The counterpart's chunk ids are not part of the graph contract: without them it is quoted uncited,
    # never under a guessed id.
    later = _id_list(passage.get("counterpart_chunk_ids"), valid_ids)
    return (f'- in "{where}": earlier wording: "{_quote(passage.get("text"))}"{ids} | later wording'
            f'{"" if later else " (no citable id)"}: "{_quote(passage.get("counterpart_text"))}"{later}')


def _unsettled_section(group: list[Mapping], total: int, valid_ids: set[str]) -> list[str]:
    """The "Not matched" heading and its item lines; nothing at all when the text check settled every item of the pair."""
    if not group and not total:
        return []
    return [_UNSETTLED_HEADING.format(shown=len(group), total=total)] + [_item_line(i, valid_ids) for i in group]


def _pair_sections(pair: Mapping, mine: list[Mapping], passages: list[Mapping], valid_ids: set[str]) -> list[str]:
    lines = []
    for change, label, note in _ITEM_SECTIONS:
        group = [i for i in mine if i.get("change") == change]
        total = (pair.get("totals") or {}).get(change, len(group))
        if change == UNSETTLED_CHANGE:
            lines += _unsettled_section(group, total, valid_ids)
            continue
        units = _noun(group)
        label, note = (text.format(unit=_UNIT_SINGULAR[units], units=units) for text in (label, note))
        if not group and not total:
            lines.append(f"{label} - none found.")
            continue
        lines.append(f"{label} - showing {len(group)} of {total} {units} ({note}):")
        lines += [_item_line(i, valid_ids) for i in group]
    totals = pair.get("passage_totals") or {}
    for kind, phrase, hedge in _PASSAGE_SECTIONS:
        group = [p for p in passages if p.get("kind") == kind]
        total = totals.get(kind, len(group))
        if not group and not total:
            continue                 # no passage layer for this kind: say nothing rather than "none"
        lines.append(f"{PASSAGES_PREFIX}{_noun(group)} {phrase} (showing {len(group)} of {total}{hedge}):")
        lines += [_passage_line(p, valid_ids) for p in group]
    return lines


def _belongs(row: Mapping, pair: Mapping) -> bool:
    """A temporal item or passage row belongs to the pair of its company that it names by accession; a row that names none
    (a saved fixture) belongs to its company's pair."""
    return (row.get("cik") == pair.get("cik") and row.get("newer_accession") in (None, pair.get("newer_accession"))
            and row.get("older_accession") in (None, pair.get("older_accession")))


def _covers_line(pair: Mapping) -> str | None:
    """Which fiscal years a pair chosen for the question compares: none for the current pair (what every question saw before
    pairs could be chosen), so a question that names no pair renders exactly as it always did. Period-end wording on purpose:
    the answer prompt forbids "FY2025" labels."""
    why = {"named": "the question names these fiscal years", "multi": "the question spans several annual reports"}.get(pair.get("selection"))
    if not why:
        return None
    return (f"covers the fiscal year ended {pair.get('older_period_end') or 'an unknown date'} -> the fiscal year ended "
            f"{pair.get('newer_period_end') or 'an unknown date'} (shown because {why})")


def _note_lines(notices: Sequence[Mapping]) -> list[str]:
    return [f"Note for {n.get('company')}: {_flat(n.get('text')).rstrip('.')}." for n in notices]


def temporal_block(items: list[dict], pairs: list[dict], passages: Sequence[Mapping] = (),
                   notices: Sequence[Mapping] = ()) -> tuple[str, set[str]]:
    """The text-verified temporal block and the chunk ids it makes citable.

    One section per compared pair (a company has one, or several when the question names fiscal years or spans several annual
    reports, oldest first): the two filings, the fiscal years a chosen pair covers, then the removed / added / reworded items
    with the TOTALS stated beside the capped lists ("showing 8 of 21"), then the passages of surviving items that changed. A
    notice (a named year the graph has no comparison for; a cap on the pairs shown) heads its company's first section, or is
    a section of its own when the company has none. No comparison at all (no RiskItem data) is ``(none)``; a pair the loader
    could not compare says so; a comparison in which nothing changed says that."""
    if not pairs and not notices:
        return NONE_BLOCK, set()
    by_company: dict[object, list[Mapping]] = {}
    for notice in notices:
        by_company.setdefault(notice.get("cik"), []).append(notice)
    valid_ids: set[str] = set()
    sections = []
    for pair in pairs:
        lines = _note_lines(by_company.pop(pair.get("cik"), []))
        lines.append(f"{pair['company']}: {pair['older_form']} filed {pair['older_date']} (accession {pair['older_accession']}) "
                     f"compared with {pair['newer_form']} filed {pair['newer_date']} (accession {pair['newer_accession']})")
        if covers := _covers_line(pair):
            lines.append(covers)
        if pair.get("compared") is False:
            reason = _flat(pair.get("not_compared_reason")) or "reason not recorded"
            lines.append(f"{NOT_COMPARED_PREFIX} ({reason})")
        else:
            lines += _pair_sections(pair, [i for i in items if _belongs(i, pair)], [p for p in passages if _belongs(p, pair)],
                                    valid_ids)
        sections.append("\n".join(lines))
    sections += ["\n".join(_note_lines(rest)) for rest in by_company.values()]
    return "\n\n".join(sections), valid_ids


def removal_supported_ids(context: str) -> set[str]:
    """The citation ids that appear under a REMOVED list of the temporal block: an item that "no longer appears as a separate
    risk factor" (:data:`REMOVED_ITEMS_PREFIX`) or a passage whose wording "was not found in the newer filing"
    (:data:`PASSAGES_REMOVED_PHRASE`). The lists of NEW items and ADDED passages (whose wording was not found in the OLDER filing)
    are not among them.

    A sentence that claims a removal may cite only these (docs/v2/M1B_PLAN.md L.7: a passage that is gone is not "the
    company dropped the risk", and a surviving item is not gone). The ids under the "Not matched" list (older items the text
    check could not settle) are NOT among them: that heading does not start with :data:`REMOVED_ITEMS_PREFIX`, so its lines
    never count, and an unsettled item is not verified removed. Read back from the context string itself, so it holds
    for any answer that saw exactly this context; a context with no temporal block yields the empty set."""
    _, marker, rest = context.partition(TEMPORAL_HEADER)
    if not marker:
        return set()
    block = rest.partition(AFTER_TEMPORAL_HEADER)[0]
    supported: set[str] = set()
    removed_list = False
    for line in block.splitlines():
        if line.startswith(_ITEM_LINE_PREFIX):
            if removed_list:
                supported.update(CITE_RE.findall(line))
        else:       # a company header, a section label or a blank line ends the previous list
            removed_list = line.startswith(REMOVED_ITEMS_PREFIX) or (
                line.startswith(PASSAGES_PREFIX) and PASSAGES_REMOVED_PHRASE in line)
    return supported
