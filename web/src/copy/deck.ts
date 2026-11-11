// docs/specs/m5b-redesign.md section 4: every user-facing sentence semigraph writes, in one place. Facts that must stay exact
// are NOT here: they live in pages/Method.copy.ts (pinned against the legacy page) and the KIND_* labels of Company.copy.ts.
// Plain strings and small pure functions; a caller renders each as a text node. tests/test_web_copy_deck.py checks where copy
// lives, the fact invariants and the cost line, and records this file in artifacts/m5b_copy_deck.json, so every wording change
// is a reviewable diff. Non-ASCII characters are written as unicode escapes.
import type { CheckedLine } from "@/lib/answerMeta";
import type { RetrievalParams, StatusView } from "@/lib/askStatus";
import type { HedgeGroup } from "@/lib/hedge";
import type { StampView } from "@/lib/stamp";
import type { StepCode, StepView } from "@/lib/steps";

// The legacy page's words that the redesigned page still shows, word for word, as named exports so that
// tests/test_web_copy_deck.py keeps pinning each against index.html (R4 retired pages/Ask.copy.ts): the client's error clauses
// (state/askState.ts), the Ask footer (pages/AskParts.tsx), and the two refusals the SERVER words (routes.py; the drawer says
// what the server would have, components/evidenceFetch.ts).
export const CLAUSE_TOKEN_EXPIRED = " (the check expired \u2014 please click Ask again)";
export const CLAUSE_NO_TOKEN = " (the bot check below has not completed yet \u2014 wait a moment and click Ask again)";
export const STATUS_NETWORK = "network error \u2014 is the service online?";
export const FOOTER_LEAD = "Live questions cost real money (";
export const FOOTER_AFTER_COST =
  "), so they are rate-limited per address and capped per day; benchmark questions are served from a saved evaluation run. Financial figures come from XBRL facts, not from model parsing. Each answer is checked so that every cited id was retrieved and every number matches the retrieved context; those checks do not prove that a sentence is supported by the passage it cites, so read the source. Source code and methodology: ";
export const FOOTER_LINK_TEXT = "GitHub";
export const FOOTER_END = ".";
export const MSG_NOT_PUBLIC_EVIDENCE = "not a public evidence id";
export const MSG_MALFORMED_EVIDENCE = "malformed evidence id";

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

/** R25's risk changes block (docs/specs/m5b-redesign.md R3/R4: moved from pages/Company.copy.ts, word for word): its heading
 *  ("Risk changes", as its On this page link names it, N2; the legacy page said "Compared risk disclosures"), a filing as a pair header names it, the prefix of a passage, the line for
 *  a pair that could not be compared (the legacy template), and the quote marks. */
interface FilingWords {
  form: string | null;
  filing_date: string | null;
}
const filingLabel = (f: FilingWords): string => [f.form, f.filing_date].filter((part): part is string => part !== null && part !== "").join(" ");
/** N2 (the owner's NVDA pass): the risk changes block is headed as its On this page link names it (it was the agent step's label,
 *  "Compared risk disclosures", which the agent's steps list keeps). */
const RISK_CHANGES = "Risk changes";

const COMPANY_CHANGES = {
  title: RISK_CHANGES,
  filingLabel,
  /** a passage names its side by that side's filing, or "earlier filing" / "later filing" when it has no label; nothing for none */
  passageLabel: (side: "older" | "newer" | null, older: FilingWords, newer: FilingWords): string => {
    if (side === null) return "";
    const label = filingLabel(side === "older" ? older : newer);
    return `${label === "" ? (side === "older" ? "earlier filing" : "later filing") : label}: `;
  },
  pairHeader: (older: FilingWords, newer: FilingWords): string => `${filingLabel(older)} \u2192 ${filingLabel(newer)}`,
  notCompared: (header: string, reason: string | null): string => `${header}: comparison not available (${reason ?? "unknown reason"}).`,
  quoted: (text: string): string => `\u201c${text}\u201d`,
};

export const deck = {
  shell: {
    /** the first stop of the keyboard: jumps to the page's main content */
    skip: "Skip to content",
  },

  errors: {
    /** an evidence request that never reached the server, with no message of its own */
    network: "network error",
  },

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
    /** relative times, as the legacy page worded them (pages/Freshness.lib.ts relativeTime; pinned by its port tests) */
    time: {
      unknown: "unknown",
      justNow: "just now",
      ahead: (span: string): string => `in ${span}`,
      ago: (span: string): string => `${span} ago`,
      minutes: (n: number): string => `${n} min`,
      hours: (n: number): string => `${n} h`,
      days: (n: number): string => `${n} d`,
    },
    /** the legacy page's one freshness line (pages/Freshness.lib.ts freshnessLine, kept for its port tests; the page shows RD3's) */
    legacyLine: {
      failedAt: (when: string): string => `freshness check failed ${when}`,
      failed: "freshness check failed",
      unavailable: "freshness check unavailable",
      dataAsOf: (date: string): string => `Data as of ${date}`,
      dateUnknown: "Data freshness unknown",
      checked: (when: string): string => `checked ${when}`,
      pending: (n: number): string => `${n} filing${n === 1 ? "" : "s"} pending`,
      separator: " \u00b7 ",
    },
    /** RD27: the Freshness page. The summary is the header's own line; each detail in words, dates in UTC. */
    page: {
      intro: "When the filings and the government rules were last checked for anything newer than our data.",
      loading: "Loading\u2026",
      failed: (detail: string): string => `Data status unavailable: ${detail}`,
      list: "Freshness details",
      rows: {
        checks: "Checks",
        dataAsOf: "Data as of",
        lastChecked: "Last checked",
        lastFailed: "Last failed check",
        nextCheck: "Next check",
        pending: "Newer filings being added",
        rules: "Government rules",
        unresolved: "Companies the check could not look up",
      },
      pendingList: "Newer filings",
      /** the monitor's status (monitor.py); one it does not know is shown as the server wrote it */
      status: (status: string): string => {
        const words: Record<string, string> = { ok: "Working", stale: "Overdue", error: "The last check failed", disabled: "Off", unconfigured: "Not set up" };
        return Object.hasOwn(words, status) ? words[status] : status; // own keys only: "constructor" is server text, not a lookup
      },
      when: (relative: string, date: string): string => (date === "" ? relative : `${relative} (${date})`),
      pendingFiling: (f: { ticker: string | null; form: string | null; filed: string; accession: string | null }): string => {
        const head = [f.ticker, f.form].filter((v): v is string => v !== null).join(" ");
        const filed = f.filed === "" ? head : `${head} filed ${f.filed}`;
        return f.accession === null ? filed : `${filed} \u00b7 ${f.accession}`;
      },
      rules: (inData: number, published: number, newer: number): string =>
        `${inData} in our data, ${published} published, ${newer} not yet in our data`,
    },
  },

  method: {
    /** RD27: the newcomer's version first; INTRO (pages/Method.copy.ts) is the technical explanation that follows */
    lead: "Answers are written from passages of the companies' SEC filings, their reported figures and US government rules, and cite them, so you can open each source.",
    title: "Method & Evidence",
    checksHeading: "What the checks mean",
    freshnessLink: "Freshness",
  },

  workspace: {
    /** R28/R30: the open workspace's words, as the legacy page had them (moved from pages/Workspace.copy.ts under the same
     *  names; docs/specs/m5b-redesign.md R4). The first-time view's words are RD26's, as docs/decisions/002 settled them:
     *  what it is, who can open it, when it is deleted and what is sent, three steps, the types, and Restore behind a disclosure. */
    title: "Your documents",
    what: "Upload your own documents, then ask questions whose answers cite them and see what changed between versions.",
    access: "Only someone with the workspace's id and key can open it. It stays open in this browser tab.",
    deletion: "Access ends 24 hours after you create the workspace; everything in it is then deleted automatically. " +
      "You can delete it sooner with Delete workspace.",
    providers: "When you ask a question, the question and the matching passages from your documents are sent to the AI model " +
      "providers that write the answer.",
    /** An ordered list: the numbers are the list's, never the words' */
    steps: ["Create a workspace.", "Upload a document.", "Ask a question about it on the Ask page."],
    /** One type per extension of lib/workspace.ts FILE_ACCEPT (the page's test maps each) */
    types: "PDF with selectable text, Word (.docx), Markdown, plain text or HTML.",
    restoreTrigger: "I have a workspace id and key",
    idLabel: "Workspace id",
    /** The visible name; the storage field and the request header keep the name "token" */
    keyLabel: "Workspace key",
    CREATE_BUTTON: "Create a workspace",
    RESTORE_BUTTON: "Restore",
    CREATING: "creating\u2026",
    RESTORING: "restoring\u2026",
    DELETE_BUTTON: "Delete workspace",
    WORKSPACE_WORD: "Workspace",
    EXPIRES_WORD: "expires",
    EXPIRES_UNKNOWN: "unknown",
    AS_OF_TITLE: "Ask as of this date instead of the current version",
    AS_OF_HINT: 'optional: ask as of an earlier date (or pick "ask as of vN" under a document below)',
    UPLOAD_AS: "upload as:",
    DROP_TEXT: "Drag a PDF, DOCX, Markdown, TXT or HTML file here, or click to choose one (up to about 20 pages).",
    NO_DOCUMENTS: "No documents uploaded yet.",
    loadingChanges: (older: number, newer: number): string =>
      `loading what changed between v${older} and v${newer}\u2026`,
    firstVersion: (newer: number): string => `v${newer} is the first version \u2014 nothing to compare.`,
    notCompared: (older: number, newer: number, reason: string): string =>
      `v${older} \u2192 v${newer}: comparison not available (${reason}).`,
    comparedHeader: (older: number, newer: number, unchanged: number): string =>
      `v${older} \u2192 v${newer}, ${unchanged} section(s) unchanged:`,
    skippedNote: (skipped: number): string =>
      `${skipped} section(s) were compared as whole sections, not sentence by sentence for negation changes.`,
    SECTION_ADDED: "Added",
    SECTION_REMOVED: "Removed",
    SECTION_CHANGED: "Changed",
    SECTION_MINOR: "Minor rewordings",
    /** lib/workspace.ts: the create and restore refusals, the upload target and the as-of choices */
    createNetwork: "network error creating a workspace",
    createFailed: "could not create a workspace",
    notFound: "workspace not found",
    uploadNew: "Upload a new document",
    newVersionOf: (title: string): string => `New version of ${title}`,
    askAsOfCurrent: "ask as of: current",
    askAsOfVersion: (version: number | string): string => `ask as of v${version}`,
    /** pages/WorkspaceDocuments.tsx: after a document's title */
    latest: (version: number): string => ` (latest v${version})`,
  },

  jobs: {
    /** lib/jobs.ts: the upload queue's words, as the legacy page had them */
    botTimeout: "bot check did not complete in time \u2014 try again",
    configFailed: "could not load the bot check \u2014 reload the page and try again",
    network: "network error",
    cannotWatch: "could not watch this upload",
    connectionLost: "connection lost while watching this upload \u2014 check its status later",
    phase: { parsing: "reading the document", chunking: "splitting into sections", comparing: "comparing with the previous version" },
    left: (seconds: number): string => ` (~${seconds}s left)`,
    unknownError: "unknown error",
    unchanged: (version: string): string => `unchanged (still v${version})`,
  },

  changesReport: {
    /** lib/changes.ts: a workspace document's change report that could not load */
    unknownReason: "unknown reason",
    network: "network error loading the change report",
    failed: "could not load the change report",
  },

  notFound: {
    /** an address that is not a page (R9c) */
    title: "Page not found",
    text: "There is nothing at this address.",
    back: "Back to the start page",
  },

  ask: {
    /** lib/sse.ts: the stream ended before done; what is shown was not checked */
    streamCut: "connection lost before the answer finished: what is shown is incomplete and was not checked. Please ask again.",
    /** lib/sse.ts: the page itself failed to show the answer */
    pageFailed: "the page could not show this answer completely: what is shown may be incomplete and was not checked. Please ask again.",
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
    /** the accessible name of the steps list (the legacy page's, A3) */
    label: "What the research agent did",
    /** The research agent's steps (lib/steps.ts stepView), worded as the legacy page worded them. */
    line: (view: StepView): string => {
      const { tool, summary, failed } = view.params;
      const [verb, joiner] =
        view.code === "unknown" ? [tool ? `Ran ${tool.replace(/_/g, " ")}` : "Ran a tool", ": "] : STEP_WORDS[view.code];
      return verb + (summary ? joiner + summary : "") + (failed ? " (failed)" : "");
    },
  },

  evidence: {
    /** RD17: the evidence panel (components/EvidenceBody.tsx over lib/evidenceView.ts evidenceBody). */
    /** RD16: the panel's header: its visible title and Close (whose accessible name stays "Close evidence", A5) */
    title: "Evidence",
    close: "Close",
    /** the accessible name of Close (A5, unchanged from the legacy page) */
    closeName: "Close evidence",
    /** RD17/RD18: shown only beside a mark, which is made only when an answer copied the words exactly (docs/decisions/003) */
    legend: "Highlighted: the words copied exactly from the source",
    loading: "Loading the source\u2026",
    failed: (message: string): string => `Could not load this source: ${message}`,
    untitled: "Source",
    fact: { section: "Section", filed: "Filed", status: "Status", period: "Period", form: "Form", published: "Published" },
    status: (status: string): string =>
      status === "current" ? "Current filing" : status === "superseded" ? "Older filing" : status === "corrected" ? "Corrected" : status,
    band: (until: string): string =>
      until === "" ? "Older filing: a newer filing has replaced it." : `Older filing: a newer filing has replaced it. It was current until ${until}.`,
    docBand: "An older version of your document: a newer version has replaced it.",
    docNote: "Uploaded by you, not a filing.",
    ruleLabel: "Government rule",
    ruleNote: "Linked to companies by keyword matching: an action by the government, not statements the company made.",
    exactly: (value: string): string => `Exactly ${value}`,
    link: { filing: "Open the filing on SEC.gov", rule: "Open the rule on FederalRegister.gov", source: "Open the source" },
    details: "Details for analysts",
    detail: { id: "id", accession: "accession", replacedBy: "replaced by", metric: "metric", concept: "XBRL concept" },
  },

  checked: {
    /** RD14: "What was checked" under an answer, one plain sentence per line of lib/answerMeta.ts checkedView. Never claims more
     *  than the checks did: no "verified", no percentages (the figures named in a line are the answer's own). */
    heading: "What was checked",
    /** the disclosure that keeps the legacy badges (tokens, cost, model, counts) and the checks hint for analysts */
    technical: "Technical details",
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

  care: {
    /** RD13: "Read with care", one note per change-claim wording an answer holds (lib/hedge.ts hedgesIn), each with the counts
     *  pages/Method.copy.ts HEDGES pins for that wording (tests/test_web_method_copy.py checks them against RELIABILITY). */
    heading: "Read with care",
    line: (group: HedgeGroup, counts: readonly number[]): string => {
      switch (group) {
        case "removed":
          return `\u201cNo longer appears as a separate risk factor\u201d comes from an automatic comparison of two filings. Checked by hand, ${counts[0]} of ${counts[1]} such risk factors were gone as a standalone risk factor, but ${counts[2]} of ${counts[3]} had been folded into another risk factor rather than dropped.`;
        case "unmatchedNew":
          return `\u201cNo matching risk factor found in the earlier filing\u201d comes from an automatic comparison. Checked by hand, only ${counts[0]} of ${counts[1]} such risk factors were really new; the others had been disclosed before.`;
        case "passageRemoved":
          return `A sentence \u201cwhose wording was not found in the newer filing\u201d may still be there in other words: checked by hand, ${counts[0]} of ${counts[1]} such sentences were.`;
      }
    },
    link: "How reliable change claims are",
  },

  company: {
    /** docs/specs/m5b-redesign.md R3 (RD19-RD25), D-R3, D-R4 and docs/decisions/001-r3-company-copy.md. Server values (names,
     *  forms, dates already formatted in UTC) arrive as parameters and are rendered as text. */
    header: {
      annual: (r: { form: string; yearEnded: string | null; filed: string }): string =>
        r.yearEnded === null ? `${r.form} filed ${r.filed}` : `${r.form} for the fiscal year ended ${r.yearEnded}, filed ${r.filed}`,
      quarterly: (r: { form: string; filed: string }): string => `${r.form} filed ${r.filed}`,
      annualOnly: "Files an annual report on Form 20-F; no quarterly 10-Q",
      dataAsOf: (date: string): string => `Data as of ${date}`,
      askAbout: (name: string): string => `Ask about ${name}`,
      compare: "Compare filings",
      /** decision 4: the question "Ask about <company>" puts in the box (nothing is sent) */
      askPrefill: (name: string): string => `What risks does ${name} describe in its latest annual report?`,
    },
    nav: {
      label: "On this page",
      keyNumbers: "Key numbers",
      rules: "Government rules",
      relationships: "Relationships",
      filings: "Filings",
      changes: RISK_CHANGES,
    },
    glance: {
      heading: "At a glance",
      /** decision 3: counts the rules the page lists (the server lists at most 8) */
      rulesCount: (count: number): string => `${count} government ${count === 1 ? "rule" : "rules"} listed below`,
    },
    numbers: {
      heading: "Key numbers",
      fiscalNote: "Fiscal years are the company's own. Each column shows the date its year ended.",
      /** the names of the table's scrolling region and its first column header */
      tableRegion: "Key numbers table",
      metricColumn: "Metric",
      exact: "Show exact values",
      metric: {
        revenue: { name: "Revenue", meaning: "Money from sales" },
        net_income: { name: "Net income", meaning: "Profit after all costs and taxes" },
        rnd: { name: "Research and development", meaning: "Money spent on developing new products" },
        capex: { name: "Capital spending", meaning: "Money spent on buildings and equipment" },
      },
      /** D-R4 */
      notHeld: (year: string): string => `Not in our data for these years. The latest we hold is ${year}.`,
      /** decision 1: a metric we hold nothing of */
      absent: "Not in our data for these years.",
      /** WCAG 2.5.3: the visible text first, then what it is, the exact value when the compact one is shown */
      stampName: (f: { shown: string; metric: string; year: string; exact: string }): string =>
        f.shown === f.exact ? `${f.shown}, ${f.metric}, ${f.year}. Open source` : `${f.shown}, ${f.metric}, ${f.year}: ${f.exact}. Open source`,
    },
    rules: {
      heading: "Government rules",
      notice:
        "These US export-control rules come from the Federal Register and are linked to the company by keyword matching: actions by the government, not statements the company made.",
      /** D-R3 */
      none: (name: string): string => `No export-control rule in our data was linked to ${name} by keyword matching. This is not a finding that none applies.`,
    },
    relationships: {
      heading: "Relationships",
      ownWords: "In the filing's own words",
      competitors: "Competitors named in its filings",
    },
    filings: { heading: "Filings", tableRegion: "Filings table" },
    risks: {
      heading: "Active risks",
      /** D-R4 */
      label: "Summaries written by semigraph, not quotes",
    },
    /** the page's status words (unchanged from R25): while the dossier loads, an empty block, a 200 the page cannot read */
    status: {
      loading: "loading\u2026",
      none: "none",
      unreadable: "could not load the company data",
    },
    /** R25's risk changes block: its heading (as its On this page link names it, N2), the pair header, the
     *  passage prefix, the line for a pair that could not be compared (the legacy template), and the quote marks */
    changes: COMPANY_CHANGES,
    /** a request that never reached the server (the legacy page's words, index.html:680) */
    network: "network error \u2014 is the service online?",
  },

  chips: {
    /** RD11: a source stamp's label (lib/stamp.ts stampView). No pilcrow, no leading zeros; the company only once loaded. */
    label: (view: StampView): string => {
      switch (view.kind) {
        case "chunk": {
          const lead = [view.filer, view.form].filter((part) => part !== "").join(" ");
          const filed = lead === "" ? "Filed" : "filed";
          const year = view.year === "" ? "" : view.loaded ? `${filed} ${view.year}` : `Filing ${view.year}`;
          const head = [lead, year].filter((part) => part !== "").join(", ");
          return head === "" ? `Paragraph ${view.paragraph}` : `${head} \u00b7 paragraph ${view.paragraph}`;
        }
        case "xbrl":
          return view.periodEnd === "" ? `Reported ${view.metric}` : `Reported ${view.metric}, period ended ${view.periodEnd}`;
        case "fr":
          return `Government rule ${view.number}`;
        case "doc":
          return `Your document, version ${view.version} \u00b7 paragraph ${view.paragraph}`;
        case "other":
          return view.text;
      }
    },
    /** RD15: the list of an answer's distinct cited sources */
    sourcesHeading: (count: number): string => `Sources (${count})`,
    /** a chip's tooltip. N5 (the owner's NVDA pass): no raw id, which NVDA read out as the chip's description (the panel shows it) */
    title: (stale: boolean): string => `Open the source${stale ? " \u2014 a newer version exists" : ""}`,
    /** the legacy label of a document chip (lib/citations.ts chipLabel, kept for the differential tests) */
    legacyDoc: "your document",
    /** shown after a stale chip's label (a workspace answer cited an older version of the document) */
    stale: " (older version)",
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
