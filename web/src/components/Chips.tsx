// The citation chip and the text that holds chips (R21, A1, S2, S11). The props are the frozen contract of I3 main-first.
// A chip is a real <button type="button"> (A1): reachable by Tab, operable with Enter and Space, a visible focus indicator, an
// accessible name (its label). Model text is data: it is rendered as text nodes only, never as markup, and a chip id is untrusted
// (it already matched the generated grammar in tokenize, and the drawer checks it again before any request, S4).
import { ChartNoAxesColumn, FileQuestionMark, FileText, FileUser, Landmark, TriangleAlert, type LucideIcon } from "lucide-react";
import { Fragment, useMemo, type ReactNode } from "react";
import { deck } from "@/copy/deck";
import { parseAnswer, type Inline, type Leaf } from "@/lib/answerBlocks";
import { chipQuotes } from "@/lib/answerSources";
import { useChipEvidence } from "@/lib/chipEvidence";
import { chipKey, type ChipPlace } from "@/lib/citations";
import { stampView, type StampView } from "@/lib/stamp";
import { EVIDENCE_PANEL_ID, useEvidenceDrawer } from "./useEvidenceDrawer";

export interface ChipProps {
  /** The citation id. Untrusted: it must already have matched the generated grammar (tokenize guarantees it). */
  id: string;
  /** "answer", "changes" or "company": which view the chip is in (focus return, A2). */
  place: ChipPlace;
  /** The answer sequence the chip belongs to (AskState.seq); the what-changed view passes its own sequence; the Company page passes its block group (CITE_GROUP). */
  gen: number;
  /** The occurrence of this id in its text, from 0. With place, gen and id it forms the chip's key (chipKey). */
  nth: number;
  /** The id is listed in the workspace answer's `stale_citations`: the chip says so. */
  stale?: boolean;
  /** The words the answer quoted right before this chip (lib/answerSources.ts chipQuotes), for the panel's highlighter (RD18). */
  quotes?: readonly string[];
}

// RD11: an icon and a shape per kind (the icon is hidden from assistive technology: the label says the same). A filing passage and
// an uploaded document sit on the neutral surface (the document with a dashed edge), a reported number is a pill, a government rule
// is square in the rule colours. A stale chip takes the caution colours and a warning icon, and says so in visible words.
const KINDS: Record<StampView["kind"], { icon: LucideIcon; look: string }> = {
  chunk: { icon: FileText, look: "rounded-sm border-border-strong bg-card text-foreground" },
  xbrl: { icon: ChartNoAxesColumn, look: "rounded-full border-border-strong bg-card text-foreground" },
  fr: { icon: Landmark, look: "rounded-none border-rule-line bg-rule-fill text-rule" },
  doc: { icon: FileUser, look: "rounded-sm border-dashed border-border-strong bg-card text-foreground" },
  other: { icon: FileQuestionMark, look: "rounded-sm border-border-strong bg-card text-foreground" },
};
const STALE_LOOK = "stale rounded-sm border-caution-line bg-caution-fill text-caution";

export function Chip({ id, place, gen, nth, stale = false, quotes }: ChipProps) {
  const drawer = useEvidenceDrawer();
  const evidence = useChipEvidence(id); // a chunk chip names its filer, form and year once the drawer has loaded it
  const view = stampView(id, evidence);
  const { icon: KindIcon, look } = KINDS[view.kind];
  const Icon = stale ? TriangleAlert : KindIcon;
  const title = `open evidence ${id}${stale ? " \u2014 a newer version exists" : ""}`;
  return (
    <button
      type="button"
      data-id={id}
      data-chip-key={chipKey(place, gen, id, nth)}
      aria-expanded={drawer.openKey !== null && drawer.openKey === chipKey(place, gen, id, nth)}
      aria-controls={EVIDENCE_PANEL_ID}
      data-stale={stale ? "" : undefined}
      title={title}
      className={`cite mx-0.5 inline min-h-[24px] min-w-[24px] max-w-full cursor-pointer border px-1 text-left text-[0.9em] font-semibold hover:underline ${stale ? STALE_LOOK : look}`}
      onClick={(event) => drawer.open(id, event.currentTarget, place, quotes)}
    >
      <Icon aria-hidden="true" className="mr-1 inline size-[1em] align-[-0.125em]" />
      {`${deck.chips.label(view)}${stale ? deck.chips.stale : ""}`}
    </button>
  );
}

export interface AnswerTextProps {
  /** Model or server text. Never HTML. */
  text: string;
  place: ChipPlace;
  /** The answer sequence (AskState.seq). */
  gen: number;
  /** Ids to mark as stale (workspace answers). */
  staleIds?: readonly string[];
}

/** Counts the occurrences of each id in document order, so a chip's key survives streaming appends (A2). */
class Occurrences {
  private readonly seen = new Map<string, number>();
  next(id: string): number {
    const nth = this.seen.get(id) ?? 0;
    this.seen.set(id, nth + 1);
    return nth;
  }
}

interface Context {
  place: ChipPlace;
  gen: number;
  stale: ReadonlySet<string>;
  counts: Occurrences;
  quotes: ReadonlyMap<string, readonly string[]>;
}

function leaf(part: Leaf, index: number, c: Context): ReactNode {
  if (part.type === "text") return <Fragment key={index}>{part.text}</Fragment>;
  const nth = c.counts.next(part.id);
  return <Chip key={index} id={part.id} place={c.place} gen={c.gen} nth={nth} stale={c.stale.has(part.id)} quotes={c.quotes.get(`${part.id}:${nth}`)} />;
}

function inline(parts: Inline[], c: Context): ReactNode[] {
  return parts.map((part, index) =>
    part.type === "strong" ? <strong key={index}>{part.children.map((child, i) => leaf(child, i, c))}</strong> : leaf(part, index, c),
  );
}

/** Text with its blocks, bold runs and `[id]` citation tokens as elements; everything else is a text node. No wrapper element. */
export function AnswerText({ text, place, gen, staleIds = [] }: AnswerTextProps) {
  const blocks = useMemo(() => parseAnswer(text), [text]);
  const stale = useMemo(() => new Set(staleIds), [staleIds]);
  const quotes = useMemo(() => chipQuotes(text), [text]); // RD18: the words quoted right before each chip (markup unchanged, R21)
  const c: Context = { place, gen, stale, counts: new Occurrences(), quotes };
  return (
    <>
      {blocks.map((block, index) => {
        if (block.type === "ul") {
          return (
            <ul key={index}>
              {block.items.map((item, i) => (
                <li key={i}>
                  {inline(item, c)}
                </li>
              ))}
            </ul>
          );
        }
        if (block.type === "h3") {
          return (
            <h3 key={index}>
              {inline(block.inline, c)}
            </h3>
          );
        }
        return <p key={index}>{inline(block.inline, c)}</p>;
      })}
    </>
  );
}
