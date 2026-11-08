// Every sentence the workspace page shows, character for character as the legacy page has it (index.html:87-120 and
// :865-1110), so the move to React changes no public copy (R30, R28). tests pin each one against index.html. Texts that the
// queue and the changes view produce live in lib/jobs.ts and lib/changes.ts; the server's own refusals are shown unchanged.
// An ellipsis, a middle dot or an arrow is written as a \u escape to keep this file ASCII.
export const PANEL_TITLE = "Your documents (private, 24h)";
export const INTRO =
  "Upload your own documents and ask questions that cite them, with versions and what-changed between them. " +
  "Private to you, never shown to anyone else, and deleted automatically after 24 hours.";
export const CREATE_BUTTON = "Create a workspace";
export const HAVE_ONE = "have one already?";
export const ID_LABEL = "workspace id";
export const TOKEN_LABEL = "token";
export const RESTORE_BUTTON = "Restore";
export const CREATING = "creating\u2026";
export const RESTORING = "restoring\u2026";
export const DELETE_BUTTON = "Delete workspace";
export const WORKSPACE_WORD = "Workspace";
export const EXPIRES_WORD = "expires";
export const EXPIRES_UNKNOWN = "unknown";
export const AS_OF_TITLE = "Ask as of this date instead of the current version";
export const AS_OF_HINT = 'optional: ask as of an earlier date (or pick "ask as of vN" under a document below)';
export const UPLOAD_AS = "upload as:";
export const DROP_TEXT = "Drag a PDF, DOCX, Markdown, TXT or HTML file here, or click to choose one (up to about 20 pages).";
export const FILE_ACCEPT = ".pdf,.docx,.md,.markdown,.txt,.html,.htm";
export const NO_DOCUMENTS = "No documents uploaded yet.";
export const loadingChanges = (older: number, newer: number): string =>
  `loading what changed between v${older} and v${newer}\u2026`;
export const firstVersion = (newer: number): string => `v${newer} is the first version \u2014 nothing to compare.`;
export const notCompared = (older: number, newer: number, reason: string): string =>
  `v${older} \u2192 v${newer}: comparison not available (${reason}).`;
export const comparedHeader = (older: number, newer: number, unchanged: number): string =>
  `v${older} \u2192 v${newer}, ${unchanged} section(s) unchanged:`;
export const skippedNote = (skipped: number): string =>
  `${skipped} section(s) were compared as whole sections, not sentence by sentence for negation changes.`;
export const SECTION_ADDED = "Added";
export const SECTION_REMOVED = "Removed";
export const SECTION_CHANGED = "Changed";
export const SECTION_MINOR = "Minor rewordings";
