// The citation chip and the text that holds chips (R21, A1, S2, S11). The props are the frozen contract of I3 main-first.
// A chip is a real <button type="button"> (A1): reachable by Tab, operable with Enter and Space, a visible focus indicator, an
// accessible name (its label). Model text is data: it is rendered as text nodes only, never as markup, and a chip id is untrusted
// (it already matched the generated grammar in tokenize, and the drawer checks it again before any request, S4).
import { Fragment, useMemo, type ReactNode } from "react";
import { parseAnswer, type Inline, type Leaf } from "@/lib/answerBlocks";
import { useChipEvidence } from "@/lib/chipEvidence";
import { chipKey, chipLabel, type ChipPlace } from "@/lib/citations";
import { useEvidenceDrawer } from "./useEvidenceDrawer";

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
}

export function Chip({ id, place, gen, nth, stale = false }: ChipProps) {
  const drawer = useEvidenceDrawer();
  const evidence = useChipEvidence(id); // a chunk chip gains its form and period once the drawer has loaded it
  const title = `open evidence ${id}${stale ? " \u2014 a newer version exists" : ""}`;
  return (
    <button
      type="button"
      data-id={id}
      data-chip-key={chipKey(place, gen, id, nth)}
      data-stale={stale ? "" : undefined}
      title={title}
      className={`cite mx-0.5 inline cursor-pointer rounded-sm border bg-muted px-1 font-mono text-[0.85em] focus-visible:ring-2 focus-visible:ring-ring ${stale ? "stale border-amber-600 text-amber-700" : "border-border text-primary"}`}
      onClick={(event) => drawer.open(id, event.currentTarget, place)}
    >
      {chipLabel(id, evidence)}
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
}

function leaf(part: Leaf, index: number, c: Context): ReactNode {
  if (part.type === "text") return <Fragment key={index}>{part.text}</Fragment>;
  return <Chip key={index} id={part.id} place={c.place} gen={c.gen} nth={c.counts.next(part.id)} stale={c.stale.has(part.id)} />;
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
  const c: Context = { place, gen, stale, counts: new Occurrences() };
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
