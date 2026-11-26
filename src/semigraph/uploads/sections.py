"""Section detection for uploaded SEC filings (docs/specs/m2-search.md R14, OD11: priority sections).

Pure and linear: it scans the canonical text line by line, looks only at the first :data:`MAX_HEADING_CHARS` characters of a
line, and every pattern is anchored with bounded quantifiers, so an untrusted text of a few hundred thousand characters
costs one pass (``test_sections_pathological_input_is_fast``). It runs in the API process on text the parse sandbox has
already produced and capped.

A form is recognised from its cover page (10-K, 10-Q, 20-F). Within it, the priority sections are, in embedding order
(OD11): 10-K Item 1A, Item 7, Item 1; 10-Q Part II Item 1A, Part I Item 2; 20-F Item 3.D, Item 5, Item 4. A section runs
from its heading to the next ``Item`` heading. A table-of-contents line is also a heading, so for each section the
occurrence with the longest span wins. When no section is found the document is embedded in document order and the result
carries a message (never an error).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NON_PRIORITY = 99                 # a chunk outside every priority section (embedded after them, in document order)
MAX_HEADING_CHARS = 200           # only the start of a line is matched
FORM_SCAN_CHARS = 8000            # the cover page
MIN_SECTION_CHARS = 1000          # a hit shorter than this is a table-of-contents or cross-reference line, not a section
NO_SECTIONS_MESSAGE = "No priority sections were found; the document is embedded in document order."
NO_FORM_MESSAGE = "The form type was not recognised; the document is embedded in document order."

_FORM_RE = re.compile(r"form\s{0,3}(10-k|10-q|20-f)\b", re.IGNORECASE)
_ITEM_RE = re.compile(r"(?:part\s{1,3}(ii|i)\b[\s.,:;\-–—]{0,6})?item\s{1,3}(\d{1,2}[a-c]?)(?:\s?\.\s?([a-e])\b)?",
                      re.IGNORECASE)
_PART_RE = re.compile(r"part\s{1,3}(ii|i)\b", re.IGNORECASE)
_SUB_RE = re.compile(r"([a-e])\s?\.\s{1,3}(\S.{0,100})", re.IGNORECASE)

# form -> {(part, item, sub): (key, priority)}; part is only meaningful for the 10-Q.
_TARGETS = {
    "10-K": {("", "1A", ""): ("10-K:1A", 0), ("", "7", ""): ("10-K:7", 1), ("", "1", ""): ("10-K:1", 2)},
    "10-Q": {("II", "1A", ""): ("10-Q:II.1A", 0), ("I", "2", ""): ("10-Q:I.2", 1)},
    "20-F": {("", "3", "D"): ("20-F:3.D", 0), ("", "5", ""): ("20-F:5", 1), ("", "4", ""): ("20-F:4", 2)},
}
_LABELS = {"10-K:1A": "Item 1A Risk Factors", "10-K:7": "Item 7 MD&A", "10-K:1": "Item 1 Business",
           "10-Q:II.1A": "Part II Item 1A Risk Factors", "10-Q:I.2": "Part I Item 2 MD&A",
           "20-F:3.D": "Item 3.D Risk Factors", "20-F:5": "Item 5 Operating and Financial Review",
           "20-F:4": "Item 4 Information on the Company"}


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    priority: int
    char_start: int
    char_end: int


@dataclass(frozen=True)
class SectionResult:
    form: str | None
    sections: tuple[Section, ...]
    message: str | None


def detect_form(text: str) -> str | None:
    """``10-K``, ``10-Q`` or ``20-F`` from the cover page, else ``None``."""
    match = _FORM_RE.search(text[:FORM_SCAN_CHARS])
    return match.group(1).upper() if match else None


def _lines_with_offsets(text: str):
    position = 0
    for line in text.split("\n"):
        yield position, line
        position += len(line) + 1


def _boundaries(text: str, form: str) -> list[tuple[int, tuple[str, str, str] | None]]:
    """``(offset, (part, item, sub) or None)`` for every ``Item`` heading (and the 20-F ``D.`` sub-heading under Item 3)."""
    out: list[tuple[int, tuple[str, str, str] | None]] = []
    part, item = "I", ""
    for offset, raw in _lines_with_offsets(text):
        line = raw.strip()[:MAX_HEADING_CHARS]
        if not line:
            continue
        match = _ITEM_RE.match(line)
        if match:
            part = (match.group(1) or part).upper()
            item, sub = match.group(2).upper(), (match.group(3) or "").upper()
            out.append((offset, (part if form == "10-Q" else "", item, sub)))
            continue
        part_only = _PART_RE.match(line)
        if part_only:
            part = part_only.group(1).upper()
            continue
        sub_match = _SUB_RE.match(line) if (form == "20-F" and item == "3") else None
        if sub_match and sub_match.group(1).upper() == "D" and "risk" in sub_match.group(2).lower():
            out.append((offset, ("", "3", "D")))
    return out


def detect_sections(text: str) -> SectionResult:
    """The priority sections of ``text`` (see the module docstring); ``message`` is set when none were found."""
    form = detect_form(text)
    if form is None:
        return SectionResult(None, (), NO_FORM_MESSAGE)
    boundaries = _boundaries(text, form)
    targets = _TARGETS[form]
    best: dict[str, Section] = {}
    for i, (start, ident) in enumerate(boundaries):
        target = targets.get(ident) if ident is not None else None
        if target is None:
            continue
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(text)
        key, priority = target
        if end - start >= MIN_SECTION_CHARS and (key not in best or end - start > best[key].char_end - best[key].char_start):
            best[key] = Section(key, _LABELS[key], priority, start, end)
    if not best:
        return SectionResult(form, (), NO_SECTIONS_MESSAGE)
    return SectionResult(form, tuple(sorted(best.values(), key=lambda s: s.char_start)), None)


def priority_for_span(result: SectionResult, char_start: int, char_end: int) -> int:
    """The priority (0 first) of the section that holds the start of ``[char_start, char_end)``, else :data:`NON_PRIORITY`."""
    for section in result.sections:
        if section.char_start <= char_start < section.char_end:
            return section.priority
    return NON_PRIORITY
