// R24 (docs/specs/m5b-frontend.md): everything the evidence drawer shows, as DATA (a port of index.html's evidenceView, freshnessHtml,
// periodText, factsHtml and formatNumber). The drawer renders every value as a text node (S2, S11); the one link goes through
// safeUrl (S3). A payload is untrusted: a missing or odd field is an empty cell, never a throw.
import type { Badge } from "./answerMeta";
import { chipLabel, classifyId, docChipLabel, type CitationKind } from "./citations";
import { safeUrl } from "./safeUrl";

export interface FactRow {
  label: string;
  value: string;
}
export interface EvidenceLink {
  href: string;
  label: string;
}
export interface Freshness {
  badges: Badge[];
  hint: string;
}
export interface EvidenceView {
  kind: CitationKind | null;
  label: string;
  title: string;
  /** The line above the id: the provenance of the passage, or what kind of thing the id names. */
  note: string;
  id: string;
  fresh: Freshness;
  facts: FactRow[];
  /** The excerpt or abstract, for a text node. "" when there is none. */
  text: string;
  link: EvidenceLink | null;
}

type Dict = Record<string, unknown>;
const dictOf = (value: unknown): Dict => (typeof value === "object" && value !== null ? (value as Dict) : {});
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const str = (value: unknown): string => (value === null || value === undefined ? "" : String(value));
const joinTruthy = (parts: unknown[], by: string): string => parts.filter(Boolean).map(str).join(by);

/** Digits grouped as in en-US with at most 4 decimals; a value that is not a number is shown as it is; null and "" are "". */
export function formatNumber(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  const x = typeof value === "number" ? value : typeof value === "string" && value.trim() !== "" ? Number(value) : NaN;
  return Number.isFinite(x) ? new Intl.NumberFormat("en-US", { maximumFractionDigits: 4 }).format(x) : String(value);
}

/** The API sends period_start / period_end; a nested `period` (a string or {start, end}) is accepted too. */
function periodText(d: Dict, id: string): string {
  const nested = dictOf(typeof d.period === "object" ? d.period : null);
  const start = str(d.period_start || nested.start);
  const end = str(d.period_end || nested.end);
  if (start || end) return (start ? `${start} to ` : "") + end;
  if (typeof d.period === "string" && d.period) return d.period;
  return `FY ended ${id.split(":")[3] ?? ""}`;
}

/** The freshness badges of a passage: current, superseded, corrected, its end of validity and whether search returns it. */
export function freshnessOf(d: Dict): Freshness {
  const status = d.status ? str(d.status) : d.is_current === true ? "current" : d.is_current === false ? "not current" : "";
  const badges: Badge[] = [];
  if (status === "current") badges.push({ text: "current", cls: "ok" });
  else if (status === "superseded") badges.push({ text: "superseded" + (d.superseded_by ? " by " + str(d.superseded_by) : ""), cls: "warn" });
  else if (status === "corrected") badges.push({ text: "corrected" + (d.corrected_by ? " by " + str(d.corrected_by) : ""), cls: "bad" });
  else if (status) badges.push({ text: status, cls: "warn" });
  if (d.valid_to) badges.push({ text: `valid until ${str(d.valid_to)}`, cls: "" });
  if (d.retrievable === false) badges.push({ text: "not returned by default search", cls: "warn" });
  const hint = status === "corrected" ? "A later amendment restated this passage; the amending filing holds the current version." : "";
  return { badges, hint };
}

const fact = (label: string, value: unknown): FactRow => ({ label, value: str(value) });
const shown = (rows: FactRow[]): FactRow[] => rows.filter((row) => row.value !== "");
const linkOf = (url: unknown, label: string): EvidenceLink | null => {
  const href = safeUrl(url);
  return href === null ? null : { href, label };
};
const NO_FRESHNESS: Freshness = { badges: [], hint: "" };

function xbrlView(id: string, d: Dict, view: EvidenceView): void {
  view.note = "A reported financial fact taken from the company's XBRL data, not parsed by a model.";
  view.facts = shown([
    fact("Company", d.company),
    fact("Metric", str(d.metric || (id.split(":")[2] ?? "").replace(/_/g, " ")) + (d.concept ? ` (${str(d.concept)})` : "")),
    fact("Value", d.value === null || d.value === undefined ? "" : formatNumber(d.value) + (d.unit ? ` ${str(d.unit)}` : "")),
    fact("Period", periodText(d, id)),
    fact("Disclosed in", joinTruthy([d.form, d.filing_date ? `filed ${str(d.filing_date)}` : "", d.accession_no || d.accession], " \u00b7 ")),
  ]);
  view.link = linkOf(d.source_url || d.url, "Open the source");
}

function frView(id: string, d: Dict, view: EvidenceView): void {
  view.note = "External event: a Federal Register rule that keyword matching linked to companies. It is not a statement by the company in its filings.";
  view.facts = shown([
    fact("Title", d.title),
    fact("Published", d.publication_date),
    fact("Document number", d.document_number || id.slice(3)),
    fact("Kind", d.kind),
    fact("Topics", list(d.topics).join(", ")),
    fact("Kind flagged relevant", d.relevant === null || d.relevant === undefined ? "" : d.relevant ? "yes" : "no"),
  ]);
  view.text = typeof d.abstract === "string" ? d.abstract : "";
  view.link = linkOf(d.url, "Open the rule on federalregister.gov");
}

function docView(d: Dict, view: EvidenceView): void {
  view.note = (d.title ? str(d.title) + " \u2014 " : "") + "uploaded by you, not a filing";
  view.fresh = freshnessOf({ status: d.status, valid_to: d.valid_to, superseded_by: d.superseded_by_version });
  view.text = typeof d.text === "string" ? d.text : "";
}

function chunkView(d: Dict, view: EvidenceView): void {
  const mentions = list(d.mentions);
  view.note = joinTruthy([d.filer, d.form, d.filing_date, d.section_title], " \u00b7 ") + (mentions.length ? ` \u00b7 mentions: ${mentions.join(", ")}` : "");
  view.fresh = freshnessOf(d);
  view.facts = shown([fact("Risk item", list(d.item_headlines).filter(Boolean).join("; "))]);
  view.text = typeof d.text === "string" ? d.text : "";
  view.link = linkOf(d.source_url, "Open the filing on sec.gov");
}

/** The drawer's content for one id and its payload. An id outside the grammar is shown as a chunk, as the legacy page did. */
export function evidenceView(id: string, payload: unknown): EvidenceView {
  const d = dictOf(payload);
  const kind = classifyId(id);
  const label = kind === "doc" ? docChipLabel(id, d) : chipLabel(id, d);
  const view: EvidenceView = { kind, label, title: `Evidence \u00b7 ${label}`, note: "", id, fresh: NO_FRESHNESS, facts: [], text: "", link: null };
  if (kind === "xbrl") xbrlView(id, d, view);
  else if (kind === "fr") frView(id, d, view);
  else if (kind === "doc") docView(d, view);
  else chunkView(d, view);
  return view;
}
