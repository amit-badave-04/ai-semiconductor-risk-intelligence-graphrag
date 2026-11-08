// R25 (owner 2026-11-08: yes): the words of the risk changes block of the Company page. The legacy page has no renderer for
// /api/company/{ticker}/risk-changes, so every string here is one that a test already pins elsewhere; none is new.
//   RISK_CHANGES_TITLE  index.html:350, the label of the risk_changes step ("Compared risk disclosures"): the block heading is
//                       the one new use of it (listed for the owner like the other headings).
//   KIND_*              the section labels of the server's context layout (retrieval/context_layout.py _ITEM_SECTIONS), pinned
//                       by tests/test_answerer_blocks.py (NEW_LABEL, "Not matched", "Reworded") and tests/test_answerer_context.py
//                       ("No longer appears as a separate risk factor"); the reliability note of the Method page words the same
//                       labels (tests/test_static_ui.py). No kind is ever called "removed" or "dropped": that would overclaim.
//   notComparedLine     the legacy template of index.html (Workspace.copy.ts notCompared): "<a> -> <b>: comparison not available
//                       (<reason>)." with the two filings as the operands.
// Non-ASCII characters are written as unicode escapes.
export const MSG_LOADING = "loading\u2026";
export const NONE = "none";
export const RISK_CHANGES_TITLE = "Compared risk disclosures";
export const KIND_NEW = "No matching risk factor found in the earlier filing";
export const KIND_DROPPED = "No longer appears as a separate risk factor";
export const KIND_UNSETTLED = "Not matched";
export const KIND_CHANGED = "Reworded";

const ARROW = "\u2192";
const LEFT_QUOTE = "\u201c";
const RIGHT_QUOTE = "\u201d";

export interface FilingLabel {
  form: string | null;
  filing_date: string | null;
}

/** A filing as the pair header names it: its form and date, whichever the server gave. */
export function filingLabel(filing: FilingLabel): string {
  return [filing.form, filing.filing_date].filter((part): part is string => part !== null && part !== "").join(" ");
}

export function pairHeader(older: FilingLabel, newer: FilingLabel): string {
  return `${filingLabel(older)} ${ARROW} ${filingLabel(newer)}`;
}

export function notComparedLine(header: string, reason: string | null): string {
  return `${header}: comparison not available (${reason ?? "unknown reason"}).`;
}

export function quoted(text: string): string {
  return `${LEFT_QUOTE}${text}${RIGHT_QUOTE}`;
}
