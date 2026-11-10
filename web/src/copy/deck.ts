// docs/specs/m5b-redesign.md section 4: every user-facing sentence semigraph writes, in one place. Facts that must stay exact
// are NOT here: they live in pages/Method.copy.ts (pinned against the legacy page) and the KIND_* labels of Company.copy.ts.
// Plain strings and small pure functions; a caller renders each as a text node. tests/test_web_copy_deck.py checks where copy
// lives, the fact invariants and the cost line, and records this file in artifacts/m5b_copy_deck.json, so every wording change
// is a reviewable diff. Non-ASCII characters are written as unicode escapes.
import type { CheckedLine } from "@/lib/answerMeta";
import type { RetrievalParams, StatusView } from "@/lib/askStatus";
import type { StepCode, StepView } from "@/lib/steps";

/** The per-address limits of live questions, as /api/stats reports them (lib/askStatus.ts liveLimits). */
export interface Limits {
  perWindow: number;
  windowMinutes: number;
  /** null: no per-address daily cap (the server's paid_per_ip_per_day is 0) */
  perDay: number | null;
}

/** "Nvidia", "Nvidia and TSMC", "Nvidia, AMD and TSMC". */
function listWords(items: readonly string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

const plural = (n: number, one: string, many: string): string => `${n} ${n === 1 ? one : many}`;

/** RD10: what retrieval found, in a sentence ("Found 8 passages and 1 relationship about Nvidia. Writing the answer..."). */
function foundSentence(p: RetrievalParams): string {
  const found = [
    p.passages > 0 ? plural(p.passages, "passage", "passages") : "",
    p.figures > 0 ? plural(p.figures, "reported figure", "reported figures") : "",
    p.relationships > 0 ? plural(p.relationships, "relationship", "relationships") : "",
    p.risks > 0 ? plural(p.risks, "risk disclosure", "risk disclosures") : "",
    p.changes > 0 ? plural(p.changes, "risk change", "risk changes") : "",
  ].filter((part) => part !== "");
  const lead = p.defaulted && p.anchors.length > 0 ? `No company was named, so this looks at ${listWords(p.anchors)}. ` : "";
  const about = p.anchors.length > 0 ? ` about ${listWords(p.anchors)}` : "";
  const what = found.length > 0 ? `Found ${listWords(found)}${about}. ` : "";
  return `${lead}${what}Writing the answer\u2026`;
}

/** The research agent's steps, as the legacy page worded them: the verb, and what joins it to the summary. */
const STEP_WORDS: Record<Exclude<StepCode, "unknown">, readonly [string, string]> = {
  lookup_company: ["Looked up", " "],
  search_filings: ["Searched the filings", ": "],
  financial_metrics: ["Fetched", " "],
  risk_changes: ["Compared risk disclosures", ": "],
  relationships: ["Checked relationships", ": "],
  active_risks: ["Listed active risks", ": "],
  compute_change: ["Computed", " "],
};

export const deck = {
  nav: {
    label: "Main",
    ask: "Ask",
    companies: "Companies",
    workspace: "Workspace",
    howItWorks: "How it works",
  },

  freshness: {
    /** RD3: the header's freshness line (lib/shellFreshness.ts), a link to the Freshness page. */
    dataAsOf: (date: string): string => `Data as of ${date}`,
    dateUnknown: "Data date unknown",
    newer: (count: number): string => (count === 1 ? "1 newer filing being added" : `${count} newer filings being added`),
    checkFailedAfterDate: "the check for newer filings failed",
    checkFailed: "The check for newer filings failed",
    checkOverdueAfterDate: "the check for newer filings is overdue",
    checkOverdue: "The check for newer filings is overdue",
    unavailable: "Data status unavailable",
    /** read before the line by screen readers, so the link names where it goes */
    linkPrefix: "Freshness:",
  },

  ask: {
    /** RD4: the h1, with the count of covered companies. */
    heading: (companies: number): string => `Ask about ${companies} chip and AI companies.`,
    /** RD4: the grounding sentence, around the three specimen source stamps (each opens a recorded example). */
    groundingLead: "Answers cite their sources: a ",
    groundingBetween: ", a ",
    groundingBeforeLast: " or a ",
    groundingEnd: ". Click one to see an example.",
    stamp: {
      passage: "filing passage",
      number: "reported number",
      rule: "government rule",
    },
    questionLabel: "Your question",
    placeholder: "For example: Which export rules affect AMD?",
    askButton: "Ask",
    /** RD4a, decision D-R5: the cost line, with the limits /api/stats reports (or the approved defaults until it loads). With
     *  no per-address daily cap (the server's 0 = off) the bracket is left out rather than naming a cap that does not exist. */
    costLine: (limits: Limits): string =>
      limits.perDay === null
        ? `Free to use, up to ${limits.perWindow} live questions every ${limits.windowMinutes} minutes.`
        : `Free to use, up to ${limits.perWindow} live questions every ${limits.windowMinutes} minutes (${limits.perDay} a day).`,
    examplesFree: "Example questions answer instantly and are free.",
    notAdvice: "Not financial advice.",
    howLink: "How answers are made",
    /** RD8: Ask pressed with an empty box. */
    emptyQuestion: "Type a question first, or pick an example below.",
  },

  status: {
    /** RD10: the status line of an answer (lib/askStatus.ts statusView), in plain words. Server values arrive as params and are
     *  rendered as text: company names, counts, the server's own error words. */
    line: (view: StatusView): string => {
      switch (view.code) {
        case "idle":
          return "";
        case "error":
          return view.params !== undefined && "text" in view.params ? view.params.text : "";
        case "retrieving":
          return "Looking in the filings\u2026";
        case "agent":
          return "The research agent is choosing where to look\u2026";
        case "escalated":
          return "The first draft did not pass the automatic checks, so a stronger model is answering again\u2026";
        case "retrieval":
          return view.params !== undefined && "anchors" in view.params ? foundSentence(view.params) : "Writing the answer\u2026";
        case "cachedSaved":
          return "Saved answer, shown instantly.";
        case "cachedEarlier":
          return "Answered earlier, shown instantly.";
        case "done":
          return "Done.";
      }
    },
  },

  steps: {
    /** The research agent's steps (lib/steps.ts stepView), worded as the legacy page worded them. */
    line: (view: StepView): string => {
      const { tool, summary, failed } = view.params;
      const [verb, joiner] =
        view.code === "unknown" ? [tool ? `Ran ${tool.replace(/_/g, " ")}` : "Ran a tool", ": "] : STEP_WORDS[view.code];
      return verb + (summary ? joiner + summary : "") + (failed ? " (failed)" : "");
    },
  },

  checked: {
    /** RD14: "What was checked" under an answer, one plain sentence per line of lib/answerMeta.ts checkedView. Never claims more
     *  than the checks did: no "verified", no percentages (the figures named in a line are the answer's own). */
    heading: "What was checked",
    line: (line: CheckedLine): string => {
      const figures = line.params?.figures ?? "";
      const count = line.params?.count ?? 0;
      switch (line.code) {
        case "saved":
          return "Checked when the answer was saved. Open a source to read it.";
        case "earlier":
          return "Answered earlier. The checks are shown only when an answer is first written; open a source to read it.";
        case "sourcesOk":
          return "Every cited source was among the passages found for this question.";
        case "sourcesMissing":
          return count > 0
            ? `${count} cited ${count === 1 ? "source was" : "sources were"} not among the passages found for this question.`
            : "Some cited sources were not among the passages found for this question.";
        case "noCitation":
          return "This answer cites no source: read it as unsupported.";
        case "declines":
          return "This answer declines: there was nothing to cite.";
        case "figuresMatched":
          return "All figures matched the filings.";
        case "figuresUnmatched":
          return figures === "" ? "Some figures could not be matched." : `Some figures could not be matched: ${figures}.`;
        case "figuresNone":
          return "No dollar amounts or percentages in this answer.";
        case "figuresEchoed":
          return `A figure came from your question and was not looked up in the filings: ${figures}.`;
        case "figuresUnchecked":
          return "Figures were not checked for this answer.";
        case "pseudoCitation":
          return `Bracketed text that is not a source: ${line.params?.items ?? ""}.`;
        case "removalClaim":
          return "This answer says something no longer appears, and the lists of removed items do not support that.";
        case "truncated":
          return "The answer was cut short by the length limit.";
      }
    },
  },

  sample: {
    /** RD6: a static sample answer. `quote` and `quoteAfter` are copied from Nvidia's FY2024 10-K, Item 1A, paragraph 162 (the
     *  recorded fixture; components/__tests__/answerSample.test.tsx checks they are there word for word). */
    heading: "What an answer looks like",
    answer: "Nvidia had $9.7 billion of notes outstanding as of January 28, 2024.",
    stamp: "Nvidia 10-K, paragraph 162",
    quote: "As of January 28, 2024, we had net outstanding a total of $9.7 billion in notes due by 2060.",
    quoteAfter: " As each series of senior notes matures, unless redeemed or repurchased, we must repay or refinance the notes.",
    source: "Nvidia 10-K filed Feb 21, 2024 \u00b7 Item 1A. Risk Factors \u00b7 paragraph 162",
    legend: "Highlighted: the words copied exactly from the source",
  },

  specimen: {
    /** RD4: the example a specimen stamp opens (lib/specimens.ts). */
    passageHeading: (filer: string, form: string, section: string): string => `${filer} ${form}, ${section}`,
    filed: (date: string): string => `Filed ${date}`,
    paragraph: (n: string): string => `paragraph ${n}`,
    olderFiling: "From an older filing; a newer one has replaced it.",
    numberHeading: (company: string, metric: string, year: string): string => `${company} ${metric}, ${year}`,
    exactly: (value: string): string => `Exactly ${value}`,
    reportedIn: (form: string, date: string): string => `Reported in the ${form} filed ${date}`,
    ruleLabel: "Government rule",
    published: (date: string): string => `Published in the Federal Register on ${date}`,
    ruleNote:
      "Linked to companies by keyword matching: an action by the government, not statements the company made.",
    openFiling: "Open the filing on SEC.gov",
    openRule: "Open the rule on FederalRegister.gov",
    loading: "Loading the example\u2026",
    failed: "The example could not be loaded right now.",
    close: "Close",
    dialogName: (stamp: string): string => `Example: ${stamp}`,
  },

  options: {
    /** RD7: the "More options" disclosure and its Depth radio group. */
    toggle: "More options",
    summary: (depth: string): string => `Depth: ${depth}`,
    legend: "Depth",
    standard: "Standard",
    standardLabel: "Standard (recommended)",
    standardHint: "Searches filings, numbers and rules together.",
    deep: "Deep research",
    deepHint: "Plans extra lookups first. Slower.",
    basic: "Basic text search",
    basicHint: "Comparison only; cannot track changes over time.",
    /** R23: with a workspace open only Standard is offered. */
    workspaceOnlyStandard: "With a workspace open, questions use Standard.",
  },

  starters: {
    /** RD5: six starters grouped by job, each group naming the kind of proof it gives. */
    heading: "Or try an example",
    note: "These answer instantly.",
    company: "Understand a company",
    companyProof: "You get passages quoted from the company's own filings.",
    number: "Check a number",
    numberProof: "You get the figure as the company reported it, with its source.",
    rules: "Track export rules",
    rulesProof: "You get the government rules linked to the company, marked as rules.",
    changes: "See what changed",
    changesProof: "You get risk factors compared between two annual reports.",
    showAll: "Show all examples, grouped by company",
    loading: "Loading examples\u2026",
    none: "No saved example answers are available right now. You can still ask your own question.",
    failed: "The examples could not be loaded right now. You can still ask your own question.",
    /** R23: with a workspace open an example would be a paid live question about the uploaded documents. */
    workspaceOff: "With a workspace open, the examples are off: each would become a paid live question about your documents.",
  },

  companies: {
    /** RD9: the company list, below the starters. */
    heading: "Companies covered",
    note: "Each has a page with key numbers, filings and any government rules linked to it.",
    rulesNote:
      "Plus US export-control rules from the Federal Register, linked to companies by keyword matching. A rule is always shown as a government rule: an action by the government, not statements the company made.",
  },
} as const;
