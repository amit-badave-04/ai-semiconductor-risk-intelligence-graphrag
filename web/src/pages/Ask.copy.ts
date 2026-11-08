// R22 / R30 (docs/specs/m5b-frontend.md): the Ask page copy. Every string here is copied from the legacy page (index.html: its markup
// and its script constants); the web app is a port, not a rewrite, and nothing here is new. tests/test_web_ask_copy.py pins each one
// against index.html character for character. Non-ASCII characters are written as unicode escapes.

export const EXAMPLES_HEADING = "Benchmark questions (instant, cached)";
export const EXAMPLES_LOADING = "loading\u2026";
export const NO_EXAMPLES =
  "No saved example answers are available right now. Ask your own question below (live questions are rate-limited and capped per day).";

export const ASK_HEADING = "Ask the graph";
export const QUESTION_LABEL = "Your question";
export const QUESTION_PLACEHOLDER =
  "e.g. Which export-control rules affect Nvidia, and which risk factors no longer appear as separate risk factors in its latest 10-K?";
export const MAX_QUESTION_CHARS = 500;
export const STRATEGY_TITLE = "Retrieval strategy";
export const HYBRID_LABEL = "hybrid GraphRAG (graph + XBRL + text-checked risk changes + vector search)";
export const VECTOR_LABEL = "vector-only: comparison baseline, no temporal reasoning";
export const ASK_BUTTON = "Ask";

// the opt-in research agent (index.html: AGENT_OPTION_LABEL, AGENT_HINT, TRACING_NOTE)
export const AGENT_OPTION_LABEL = "Deep research (agent): plans extra lookups first";
export const AGENT_HINT =
  "Deep research: a small planning model chooses a few extra graph lookups, listed below as they run, before the answer is written. It is slower and costs a little more; the same checks apply to the answer.";
export const TRACING_NOTE =
  "Deep research questions are traced for debugging, at a sampling rate the operator sets. A trace holds the length of the question and a one-way hash of it, the names of the tools and models used, token counts, cost, timings and any fallback reason; never the text of the question or the answer, and never your address.";
export const STEPS_LABEL = "What the research agent did";

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

// the evidence drawer (index.html: the aside, showEvidence, openCitation) and two refusals the SERVER words: a `doc:` id with no workspace
// open and an id outside the grammar make no request (S4), so the drawer says what the server would have (routes.py MSG_NOT_PUBLIC_EVIDENCE
// and the 400 detail of the evidence route).
export const EVIDENCE_LABEL = "Evidence";
export const EVIDENCE_CLOSE_LABEL = "Close evidence";
export const EVIDENCE_CLOSE_TITLE = "close";
export const EVIDENCE_LOADING = "loading\u2026";
export const EVIDENCE_ERROR_PREFIX = "could not load evidence: ";
export const EVIDENCE_ID_PREFIX = "id: ";
export const EVIDENCE_LINK_ARROW = " \u2197";
export const MSG_NOT_PUBLIC_EVIDENCE = "not a public evidence id";
export const MSG_MALFORMED_EVIDENCE = "malformed evidence id";
export const FOOTER_END = ".";
