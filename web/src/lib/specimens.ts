// docs/specs/m5b-redesign.md RD4: the three specimen source stamps of the Ask page each open a real example. The ids are cited by
// saved starter answers (T7 cites the passage, N2 the figure, R1 the rule); web/src/test/fixtures/specimens.recorded.json holds
// what the live service returned for them. A pure view over the untrusted payload: plain text, a link only through safeUrl.
import { deck } from "@/copy/deck";
import { compactMoney, exactMoney, fiscalYearLabel, formatDate, paragraphNumber } from "./format";
import { safeUrl } from "./safeUrl";

export const SPECIMENS = {
  passage: "0001045810-24-000029:I.1A:0162",
  number: "xbrl:1045810:revenue:2026-01-25",
  rule: "fr:2025-00711",
} as const;

export type SpecimenKind = keyof typeof SPECIMENS;

export interface SpecimenView {
  /** a stamp-style label over the heading (the government rule's) */
  label?: string;
  heading: string;
  meta: string;
  /** words copied from the source (shown in serif, in quotes) */
  quote?: string;
  figure?: { compact: string; exact: string };
  note?: string;
  link?: { href: string; text: string };
}

const w = deck.specimen;
const QUOTE_MAX = 240;

type Dict = Record<string, unknown>;
const str = (d: Dict, key: string): string => (typeof d[key] === "string" ? (d[key] as string) : "");

/** The first sentence of a passage, cut at a word with an ellipsis when it is long. */
function firstSentence(text: string): string {
  const squashed = text.replace(/\s+/g, " ").trim();
  const end = squashed.search(/[.!?](?=\s|[A-Z]|$)/);
  const sentence = end === -1 ? squashed : squashed.slice(0, end + 1);
  if (sentence.length <= QUOTE_MAX) return sentence;
  const cut = sentence.slice(0, QUOTE_MAX);
  return `${cut.slice(0, cut.lastIndexOf(" ")).trimEnd()}\u2026`;
}

function link(value: unknown, text: string): { link?: { href: string; text: string } } {
  const href = safeUrl(value);
  return href === null ? {} : { link: { href, text } };
}

function passageView(d: Dict): SpecimenView | null {
  const filer = str(d, "filer");
  const text = str(d, "text");
  const id = str(d, "chunk_id");
  if (filer === "" || text === "" || id === "") return null;
  const paragraph = id.split(":").pop() ?? "";
  const meta = [w.filed(formatDate(str(d, "filing_date"))), /^\d+$/.test(paragraph) ? w.paragraph(paragraphNumber(paragraph)) : ""];
  return {
    heading: w.passageHeading(filer, str(d, "form"), str(d, "section_title")),
    meta: meta.filter((part) => part !== "" && part !== w.filed("")).join(" \u00b7 "),
    quote: firstSentence(text),
    ...(d.is_current === false ? { note: w.olderFiling } : {}),
    ...link(d.source_url, w.openFiling),
  };
}

function numberView(d: Dict): SpecimenView | null {
  const year = fiscalYearLabel(str(d, "period_end"));
  const compact = compactMoney(d.value as number, str(d, "unit"));
  if (year === null || compact === "" || str(d, "company") === "") return null;
  return {
    heading: w.numberHeading(str(d, "company"), str(d, "metric"), year.long),
    figure: { compact, exact: w.exactly(exactMoney(d.value as number, str(d, "unit"))) },
    meta: w.reportedIn(str(d, "form"), formatDate(str(d, "filing_date"))),
    ...link(d.source_url, w.openFiling),
  };
}

function ruleView(d: Dict): SpecimenView | null {
  const title = str(d, "title");
  if (title === "") return null;
  return {
    label: w.ruleLabel,
    heading: title,
    meta: w.published(formatDate(str(d, "publication_date"))),
    note: w.ruleNote,
    ...link(d.url, w.openRule),
  };
}

const TYPES: Record<SpecimenKind, { type: string; view: (d: Dict) => SpecimenView | null }> = {
  passage: { type: "chunk", view: passageView },
  number: { type: "xbrl", view: numberView },
  rule: { type: "fr", view: ruleView },
};

/** The example a specimen stamp shows, or null when the payload is not the kind it should be or cannot be read. */
export function specimenView(kind: SpecimenKind, payload: unknown): SpecimenView | null {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return null;
  const d = payload as Dict;
  return d.type === TYPES[kind].type ? TYPES[kind].view(d) : null;
}
