// R27 / D7 (docs/specs/m5b-frontend.md): the Method & Evidence copy. Every sentence here is copied from the legacy page
// (web is a port, not a rewrite) and pinned by a test there; nothing is new. Non-ASCII characters are written as unicode escapes.
// tests: tests/test_static_ui.py:54-55 (the header sentence), :57-61 (withdrawn notice), :64-79 (reliability note);
// tests/ui/checks.test.mjs:98 (the checks hint).
// A segment is a run of text; bold runs are <b>, and a segment with an href is a static link (the only links on the page).

export interface Segment {
  bold?: boolean;
  href?: string;
  text: string;
}

// index.html:58 (the header sentence; "13 keystone companies" is pinned by tests/test_static_ui.py:195)
export const INTRO: readonly Segment[] = [
  { text: "Ask multi-hop supply-chain and export-control questions over the SEC filings of 13 keystone companies (Nvidia, AMD, TSMC, ASML, Micron, Meta \u2026). Answers come from a " },
  { bold: true, text: "bitemporal knowledge graph" },
  { text: ": company relationships and risk disclosures are each backed by a verbatim filing excerpt, while the links to export-control rules are " },
  { bold: true, text: "keyword matches to external Federal Register rules" },
  { text: ": outside events, not statements the company made. Answers cite their sources; click a citation to read the text or data behind it." },
];

// index.html:79 (the withdrawn-accuracy notice)
export const WITHDRAWN: readonly Segment[] = [
  { text: "Accuracy figures shown on earlier versions of this page are withdrawn: the benchmark behind them could not detect false \u201cdropped risk\u201d claims. A source-text-grounded re-measurement is in progress (" },
  { href: "https://github.com/amit-badave-04/ai-semiconductor-risk-intelligence-graphrag/blob/v2/docs/v2/M1B_PLAN.md", text: "plan" },
  { text: ", " },
  { href: "https://github.com/amit-badave-04/ai-semiconductor-risk-intelligence-graphrag/blob/v2/docs/v2/REVIEW_2026-09-26.md", text: "review" },
  { text: ")." },
];

// index.html:135 (the reliability note; the first segment is its lead, shown as the section heading)
export const RELIABILITY: readonly Segment[] = [
  { bold: true, text: "How reliable are change claims?" },
  { text: " Statements about what changed between two annual filings come from an automatic text comparison, and each kind is worded only as strongly as it held up when two people checked a small held-out sample by hand (the samples are small, so read the counts as rough guides). " },
  { bold: true, text: "A risk factor labelled \u201cno longer appears as a separate risk factor\u201d:" },
  { text: " 4 of 4 checked cases were gone as a standalone risk factor, but 2 of the 4 had been folded into another risk factor rather than dropped, so it does not mean the company stopped disclosing the risk. " },
  { bold: true, text: "\u201cNot matched\u201d:" },
  { text: " the text check could not settle these older risk factors, so they are never shown as removed. " },
  { bold: true, text: "\u201cNo matching risk factor found in the earlier filing\u201d:" },
  { text: " only 6 of 12 checked cases were really new; the other 6 were already disclosed earlier, for example split out of an older risk factor. " },
  { bold: true, text: "A sentence whose \u201cwording was not found\u201d in the other filing:" },
  { text: " 6 of the 51 sentences flagged as missing from the newer filing were still stated in different words (sentences flagged as new did better: 1 of 48)." },
];

// index.html:259 (the checks hint, shown under the badges and checks of an answer)
export const CHECKS_HINT =
  "These checks confirm that each cited id was retrieved and that numbers match the retrieved context (a figure only your question states is never counted as a match); they do not prove that a sentence is supported by the passage it cites. Only amounts with a currency ($, \u20ac, USD, EUR, NT$, TWD) and percentages are checked: a bare number such as \"190 billion\" is not. Statements about what changed between two filings come from an automatic text comparison and are worded with hedges: see \"How reliable are change claims?\" at the bottom of the page. Click a chip to read the source.";

// index.html: AGENT_HINT and TRACING_NOTE (the research agent's hint and what a trace holds), moved here from the retired Ask.copy.ts by
// docs/specs/m5b-redesign.md section 4; pinned by tests/test_web_method_copy.py and tests/test_serve_agent_ui.py.
export const AGENT_HINT =
  "Deep research: a small planning model chooses a few extra graph lookups, listed below as they run, before the answer is written. It is slower and costs a little more; the same checks apply to the answer.";
export const TRACING_NOTE =
  "Deep research questions are traced for debugging, at a sampling rate the operator sets. A trace holds the length of the question and a one-way hash of it, the names of the tools and models used, token counts, cost, timings and any fallback reason; never the text of the question or the answer, and never your address.";

// docs/specs/m5b-redesign.md RD13: the change-claim wordings that set off "Read with care", each with the counts RELIABILITY
// states for that same wording. Pinned by tests/test_web_method_copy.py: every phrase is the server's own wording
// (retrieval/context_layout.py), and every count is the facts file's. The note's sentences are in copy/deck.ts.
export const HEDGES = {
  removed: { phrase: "no longer appears as a separate risk factor", counts: [4, 4, 2, 4] },
  unmatchedNew: { phrase: "no matching risk factor found in the earlier filing", counts: [6, 12] },
  passageRemoved: { phrase: "whose wording was not found in the newer filing", counts: [6, 51] },
} as const;
