"""The layout of the answer context, written once: the block headers and the temporal block.

Two consumers must agree on this text, so it lives in one pure module (no model, no database, no litellm) that both
import: ``answerer`` WRITES the temporal block for the model, and ``verify`` READS it back to decide which chunk ids
may support a sentence that claims a removal (:func:`removal_supported_ids`). Every label the reader matches on is a
constant here and the writer builds its lines from the same constants, so the two cannot drift.

The temporal block (docs/v2/M1B_PLAN.md L.7), per compared company::

    <company>: <form> filed <date> (accession ...) compared with <form> filed <date> (accession ...)
    Removed - showing 2 of 21 risk factors (text verified absent from the later filing):
    - "<headline or first sentence>" [chunk id] [chunk id]
    Added - ... / Reworded - ...
    Passages of surviving risk factors that no longer appear (showing 4 of 12):
    - in "<containing item>": "<quoted text>" [chunk id]
    Passages of surviving risk factors that are new (showing N of M): ...
    Passages of surviving risk factors that were reworded (showing N of M): ...

A pair the loader marked ``items_compared = false`` renders ``comparison not available (<reason>)`` and nothing else:
no removed / added / reworded lines and no "none found", because "nothing changed" would be a claim nobody verified.
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
_ITEM_SECTIONS = (("removed", "Removed", "text verified absent from the later filing"),
                  ("new", "Added", "new in the later filing"),
                  ("reworded", "Reworded", "still disclosed, wording changed"))
REMOVED_ITEMS_PREFIX = "Removed - showing "                 # the removed ITEMS list (an item that is gone)
PASSAGES_PREFIX = "Passages of surviving "                  # "... risk factors that no longer appear (showing N of M):"
PASSAGES_REMOVED_PHRASE = "that no longer appear"           # the removed PASSAGES list (a sentence that is gone)
_PASSAGE_SECTIONS = (("removed", PASSAGES_REMOVED_PHRASE), ("added", "that are new"),
                     ("reworded", "that were reworded"))
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
    if item["change"] == "removed":
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


def _pair_sections(pair: Mapping, mine: list[Mapping], passages: list[Mapping], valid_ids: set[str]) -> list[str]:
    lines = []
    for change, label, note in _ITEM_SECTIONS:
        group = [i for i in mine if i.get("change") == change]
        total = (pair.get("totals") or {}).get(change, len(group))
        if not group and not total:
            lines.append(f"{label} - none found.")
            continue
        lines.append(f"{label} - showing {len(group)} of {total} {_noun(group)} ({note}):")
        lines += [_item_line(i, valid_ids) for i in group]
    totals = pair.get("passage_totals") or {}
    for kind, phrase in _PASSAGE_SECTIONS:
        group = [p for p in passages if p.get("kind") == kind]
        total = totals.get(kind, len(group))
        if not group and not total:
            continue                 # no passage layer for this kind: say nothing rather than "none"
        lines.append(f"{PASSAGES_PREFIX}{_noun(group)} {phrase} (showing {len(group)} of {total}):")
        lines += [_passage_line(p, valid_ids) for p in group]
    return lines


def temporal_block(items: list[dict], pairs: list[dict], passages: Sequence[Mapping] = ()) -> tuple[str, set[str]]:
    """The text-verified temporal block and the chunk ids it makes citable.

    One section per compared company: the two filings, then the removed / added / reworded items with the TOTALS
    stated beside the capped lists ("showing 8 of 21"), then the passages of surviving items that changed. No comparison
    at all (no RiskItem data) is ``(none)``; a pair the loader could not compare says so; a comparison in which nothing
    changed says that."""
    if not pairs:
        return NONE_BLOCK, set()
    valid_ids: set[str] = set()
    sections = []
    for pair in pairs:
        lines = [f"{pair['company']}: {pair['older_form']} filed {pair['older_date']} (accession {pair['older_accession']}) "
                 f"compared with {pair['newer_form']} filed {pair['newer_date']} (accession {pair['newer_accession']})"]
        if pair.get("compared") is False:
            reason = _flat(pair.get("not_compared_reason")) or "reason not recorded"
            lines.append(f"{NOT_COMPARED_PREFIX} ({reason})")
        else:
            lines += _pair_sections(pair, [i for i in items if i.get("cik") == pair.get("cik")],
                                    [p for p in passages if p.get("cik") == pair.get("cik")], valid_ids)
        sections.append("\n".join(lines))
    return "\n\n".join(sections), valid_ids


def removal_supported_ids(context: str) -> set[str]:
    """The citation ids that appear under a REMOVED list of the temporal block: a removed item or a removed passage.

    A sentence that claims a removal may cite only these (docs/v2/M1B_PLAN.md L.7: a passage that is gone is not "the
    company dropped the risk", and a surviving item is not gone). Read back from the context string itself, so it holds
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
