// R22 / R30 (docs/specs/m5b-frontend.md): what is left of the Ask page's legacy copy. Every string here is copied from the legacy
// page (index.html) and pinned against it by tests/test_web_copy_deck.py (TestAskCopyUntilItFoldsIntoTheDeck). The words the
// redesigned page shows are in copy/deck.ts (docs/specs/m5b-redesign.md section 4). What stays here, until R4's copy sweep: the
// legacy status projection (lib/askStatus.ts statusText, kept for the differential tests), the client's own error clauses
// (state/askState.ts), the legacy footer (AskParts.tsx), the question's length limit, and two refusals the SERVER words.
// Non-ASCII characters are written as unicode escapes.

export const MAX_QUESTION_CHARS = 500;

// the status line (index.html: ask(), finish(), retrievalStatus)
export const STATUS_RETRIEVING = "retrieving from the graph\u2026";
export const STATUS_AGENT = "the research agent is choosing lookups\u2026";
export const STATUS_GENERATING = " \u2014 generating\u2026";
export const STATUS_CACHED = "served from cache \u2014 no model call";
export const STATUS_DONE = "done";
export const STATUS_NETWORK = "network error \u2014 is the service online?";
export const FORBIDDEN_STATUS = 403;
export const CLAUSE_TOKEN_EXPIRED = " (the check expired \u2014 please click Ask again)";
export const CLAUSE_NO_TOKEN = " (the bot check below has not completed yet \u2014 wait a moment and click Ask again)";
export const NO_COMPANY_ANCHOR = "no company detected; defaulting to Nvidia";

// the footer (index.html: the footer element and costNote)
export const FOOTER_LEAD = "Live questions cost real money (";
export const FOOTER_AFTER_COST =
  "), so they are rate-limited per address and capped per day; benchmark questions are served from a saved evaluation run. Financial figures come from XBRL facts, not from model parsing. Each answer is checked so that every cited id was retrieved and every number matches the retrieved context; those checks do not prove that a sentence is supported by the passage it cites, so read the source. Source code and methodology: ";
export const FOOTER_LINK_TEXT = "GitHub";
export const FOOTER_LINK_HREF = "https://github.com/amit-badave-04/ai-semiconductor-risk-intelligence-graphrag";
export const COST_NOTE_DEFAULT = "one or two model calls each";

// two refusals the SERVER words: a `doc:` id with no workspace
// open and an id outside the grammar make no request (S4), so the drawer says what the server would have (routes.py MSG_NOT_PUBLIC_EVIDENCE
// and the 400 detail of the evidence route).
export const MSG_NOT_PUBLIC_EVIDENCE = "not a public evidence id";
export const MSG_MALFORMED_EVIDENCE = "malformed evidence id";
export const FOOTER_END = ".";
