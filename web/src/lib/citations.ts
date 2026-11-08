// The citation grammar as the page uses it (R5, S2, S4, S11): a tokenizer that finds `[id]` tokens in model text, a classifier
// that gates every evidence fetch, and the chip labels. A port of index.html:152-178 and :454, over the GENERATED patterns of
// ids.generated.ts, so the browser and the server cannot disagree. Everything here is a pure function of its arguments.
import { CITE_PATTERN, CHUNK_ID_RE, DOC_ID_RE, FR_ID_RE, XBRL_ID_RE } from "./ids.generated";

export type CitationKind = "chunk" | "xbrl" | "fr" | "doc";

/** One piece of a tokenized text: plain text (rendered as a text node) or a citation token (rendered as a chip). */
export type TextPart = { type: "text"; text: string } | { type: "cite"; id: string };

const FORMS: readonly (readonly [CitationKind, RegExp])[] = [
  ["chunk", CHUNK_ID_RE],
  ["xbrl", XBRL_ID_RE],
  ["fr", FR_ID_RE],
  ["doc", DOC_ID_RE],
];

/**
 * Which of the four id forms `id` is, or null. The match is anchored on the whole string, so a trailing newline, a path
 * fragment (`../`, `%2f`, `?x=`) or a different case is null. A value that is not a string is null. The result gates every
 * evidence request (S4): only a non-null kind may be fetched.
 */
export function classifyId(id: unknown): CitationKind | null {
  if (typeof id !== "string") return null;
  for (const [kind, pattern] of FORMS) if (pattern.test(id)) return kind;
  return null;
}

/**
 * Split `text` into text and `[id]` citation parts, in order, losing no character that is not a token's brackets. Runs in
 * time linear in the length: one global scan with the generated pattern (no backtracking across tokens). Returns no parts for
 * an empty string. Never trusts the text: the id of a cite part has matched the generated grammar, nothing else.
 */
export function tokenize(text: string): TextPart[] {
  const parts: TextPart[] = [];
  const pattern = new RegExp(CITE_PATTERN, "g"); // its own instance: a shared global RegExp keeps lastIndex between uses
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) parts.push({ type: "text", text: text.slice(last, match.index) });
    parts.push({ type: "cite", id: match[1] });
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push({ type: "text", text: text.slice(last) });
  return parts;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

const asText = (value: unknown): string => (value === null || value === undefined ? "" : String(value));

/**
 * A readable chip label instead of a bare four-digit number. `evidence` is the optional /api/evidence payload: once known, a
 * chunk chip also shows the form and period. Plain text, never markup. An unknown id is returned as text; null and undefined
 * give an empty string.
 */
export function chipLabel(id: unknown, evidence?: unknown): string {
  const text = asText(id);
  const kind = classifyId(text);
  if (kind === "chunk") {
    const match = /^[0-9-]+:[IVX]+\.([0-9A-Z]+):([0-9]{4})$/.exec(text);
    const base = `Item ${match?.[1] ?? ""} \u00b6${match?.[2] ?? ""}`;
    const ev = asRecord(evidence);
    const period = ev.fiscal_year ? `FY${asText(ev.fiscal_year)}` : ev.filing_date ? `filed ${asText(ev.filing_date).slice(0, 10)}` : "";
    const head = [ev.form ? asText(ev.form) : "", period].filter(Boolean).join(" ");
    return head ? `${head} \u00b7 ${base}` : base;
  }
  const parts = text.split(":");
  if (kind === "xbrl") return `XBRL ${parts[2].replace(/_/g, " ")} FY ended ${parts[3]}`;
  if (kind === "fr") return `BIS rule ${text.slice(3)}`;
  if (kind === "doc") return `your document ${parts[2]} \u00b6${parts[3]}`;
  return text;
}

/**
 * The richer label of a `doc:` chip once its evidence is known (the uploaded title), or the bare label meanwhile. It keeps
 * the distinct from-your-document wording (S11). PLAIN text: a title is user-controlled, so a caller renders it as a text node.
 */
export function docChipLabel(id: unknown, evidence?: unknown): string {
  const ev = asRecord(evidence);
  const parts = asText(id).split(":");
  const version = (parts[2] ?? "").replace(/^v/, "") || "?";
  const base = `v${version} \u00b6${parts[3] || "?"}`;
  return ev.title ? `${asText(ev.title)} \u00b7 ${base}` : `your document \u00b7 ${base}`;
}

/** Where a chip lives: the streamed answer or the what-changed view. Both open the same drawer. */
export type ChipPlace = "answer" | "changes";

/**
 * A chip's identity for focus return (A2): its place, the answer sequence, the id and the occurrence of that id in the text.
 * Streaming appends keep the key of every chip already shown; a new answer (a higher sequence) never reuses one.
 */
export function chipKey(place: ChipPlace, gen: number, id: string, nth: number): string {
  return `${place}:${gen}:${id}:${nth}`;
}
