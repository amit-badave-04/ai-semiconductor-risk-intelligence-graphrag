// docs/specs/m5b-redesign.md section 4: every user-facing sentence semigraph writes, in one place. Facts that must stay exact
// are NOT here: they live in pages/Method.copy.ts (pinned against the legacy page) and the KIND_* labels of Company.copy.ts.
// Plain strings and small pure functions; a caller renders each as a text node. tests/test_web_copy_deck.py checks where copy
// lives, the fact invariants and the cost line, and records this file in artifacts/m5b_copy_deck.json, so every wording change
// is a reviewable diff. Non-ASCII characters are written as unicode escapes.

/** The per-address limits of live questions, as /api/stats reports them (lib/askStatus.ts liveLimits). */
export interface Limits {
  perWindow: number;
  windowMinutes: number;
  /** null: no per-address daily cap (the server's paid_per_ip_per_day is 0) */
  perDay: number | null;
}

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
    // "Click one to see an example." joins this when the stamps open their example (R1 slice 3b, SR3, SR9)
    groundingEnd: ".",
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
  },

  companies: {
    /** RD9: the company list, below the starters. */
    heading: "Companies covered",
    note: "Each has a page with key numbers, filings and any government rules linked to it.",
    rulesNote:
      "Plus US export-control rules from the Federal Register, linked to companies by keyword matching. A rule is always shown as a government rule: an action by the government, not statements the company made.",
  },
} as const;
