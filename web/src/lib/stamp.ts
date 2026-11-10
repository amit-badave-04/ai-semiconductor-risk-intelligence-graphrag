// docs/specs/m5b-redesign.md RD11: what a chip's label says, as data for copy/deck.ts chips.label. A sibling of citations.ts
// chipLabel (kept unchanged for the legacy differential tests). The filer and form come only from the evidence payload, never
// from the accession prefix; before the evidence loads a filing chip shows only the filing year, read from an accession-shaped id
// and only when that year is a plausible filing year. Pure apart from reading the current year; every value is plain text.
import { classifyId } from "./citations";
import { formatDate, paragraphNumber } from "./format";

export type StampView =
  | { kind: "chunk"; loaded: boolean; filer: string; form: string; year: string; paragraph: string }
  | { kind: "xbrl"; metric: string; periodEnd: string }
  | { kind: "fr"; number: string }
  | { kind: "doc"; version: string; paragraph: string }
  | { kind: "other"; text: string };

/** The longest filer or form name a label carries (payload values are the server's, but a label stays short). */
const MAX_NAME_CHARS = 60;
const ACCESSION_YEAR = /^\d{10}-(\d{2})-\d{6}:/;
const ISO_YEAR = /^(\d{4})-\d{2}-\d{2}/;
const FIRST_FILING_YEAR = 2001;

function saneYear(year: number): string {
  return year >= FIRST_FILING_YEAR && year <= new Date().getUTCFullYear() + 1 ? String(year) : "";
}

function name(value: unknown): string {
  return typeof value === "string" ? value.trim().slice(0, MAX_NAME_CHARS) : "";
}

function accessionYear(id: string): string {
  const match = ACCESSION_YEAR.exec(id);
  return match === null ? "" : saneYear(2000 + Number(match[1]));
}

function chunkView(id: string, evidence: unknown): StampView {
  const paragraph = paragraphNumber(id.slice(id.lastIndexOf(":") + 1));
  if (evidence !== null && typeof evidence === "object") {
    const ev = evidence as Record<string, unknown>;
    const match = typeof ev.filing_date === "string" ? ISO_YEAR.exec(ev.filing_date) : null;
    const dated = match === null ? "" : saneYear(Number(match[1]));
    // R2 review C3: a payload with no usable date keeps the year the accession number gives, as before it loaded
    return { kind: "chunk", loaded: true, filer: name(ev.filer), form: name(ev.form), year: dated === "" ? accessionYear(id) : dated, paragraph };
  }
  return { kind: "chunk", loaded: false, filer: "", form: "", year: accessionYear(id), paragraph };
}

/** What the chip of `id` names; `evidence` is the chunk's /api/evidence payload once the drawer has loaded it. */
export function stampView(id: unknown, evidence?: unknown): StampView {
  const text = id === null || id === undefined ? "" : String(id);
  const kind = classifyId(text);
  const parts = text.split(":");
  if (kind === "chunk") return chunkView(text, evidence);
  if (kind === "xbrl") return { kind, metric: parts[2].replace(/_/g, " "), periodEnd: formatDate(parts[3]) };
  if (kind === "fr") return { kind, number: text.slice(3) };
  if (kind === "doc") return { kind, version: parts[2].slice(1).replace(/^0+(?=\d)/, ""), paragraph: paragraphNumber(parts[3]) };
  return { kind: "other", text };
}
