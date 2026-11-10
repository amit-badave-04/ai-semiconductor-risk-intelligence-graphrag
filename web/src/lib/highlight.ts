// docs/specs/m5b-redesign.md RD18: which words of a passage an answer copied exactly. A quote is split on an ellipsis ("..." or
// U+2026); fragments under MIN_FRAGMENT_CHARS are skipped; every longer fragment must be found whole after whitespace
// normalisation (no case, quote or apostrophe folding), or the quote marks nothing (it straddles two passages, or the passage
// is cut short). Both sides are untrusted: plain strings in and out, matched with indexOf (never a RegExp built from data), and
// the work is bounded. A caller renders each segment as a text node, marked ones inside <mark> (never HTML from data).

export const MIN_FRAGMENT_CHARS = 20;
export const MAX_PASSAGE_CHARS = 50_000;
export const MAX_QUOTE_CHARS = 1_000;
const MAX_QUOTES = 50;
const MAX_FRAGMENTS = 8;

export interface Segment {
  text: string;
  marked: boolean;
}

export interface Highlighted {
  segments: Segment[];
  /** true when at least one mark was made (the legend is shown only then) */
  marked: boolean;
}

const WHITESPACE = /\s/;
const ELLIPSIS = /\s*(?:\.\.\.|…)\s*/;
const OUTER_QUOTES = /^["“”]+|["“”]+$/g;

const collapse = (text: string): string => text.replace(/\s+/g, " ").trim();

/** The passage with each run of whitespace as one space, and where each of its characters starts and ends in the original. */
function normalise(passage: string): { text: string; start: number[]; end: number[] } {
  let text = "";
  const start: number[] = [];
  const end: number[] = [];
  let i = 0;
  while (i < passage.length) {
    if (WHITESPACE.test(passage[i])) {
      const from = i;
      while (i < passage.length && WHITESPACE.test(passage[i])) i += 1;
      text += " ";
      start.push(from);
      end.push(i);
    } else {
      text += passage[i];
      start.push(i);
      end.push(i + 1);
      i += 1;
    }
  }
  return { text, start, end };
}

/** The ranges of the original passage one quote covers, or null when it marks nothing. */
function rangesOf(quote: string, passage: ReturnType<typeof normalise>): [number, number][] | null {
  if (quote.length > MAX_QUOTE_CHARS) return null;
  const fragments = collapse(quote).replace(OUTER_QUOTES, "").split(ELLIPSIS).slice(0, MAX_FRAGMENTS).map(collapse);
  const long = fragments.filter((f) => f.length >= MIN_FRAGMENT_CHARS);
  if (long.length === 0) return null;
  const ranges: [number, number][] = [];
  for (const fragment of long) {
    const at = passage.text.indexOf(fragment);
    if (at === -1) return null;
    ranges.push([passage.start[at], passage.end[at + fragment.length - 1]]);
  }
  return ranges;
}

export function highlight(passage: string, quotes: readonly string[]): Highlighted {
  const plain: Highlighted = { segments: [{ text: passage, marked: false }], marked: false };
  if (passage.length === 0 || passage.length > MAX_PASSAGE_CHARS || quotes.length === 0) return plain;
  const normalised = normalise(passage);
  const ranges = quotes.slice(0, MAX_QUOTES).flatMap((q) => rangesOf(q, normalised) ?? []);
  if (ranges.length === 0) return plain;

  ranges.sort((a, b) => a[0] - b[0]);
  const merged: [number, number][] = [];
  for (const [from, to] of ranges) {
    const last = merged[merged.length - 1];
    if (last !== undefined && from <= last[1]) last[1] = Math.max(last[1], to);
    else merged.push([from, to]);
  }
  const segments: Segment[] = [];
  let at = 0;
  for (const [from, to] of merged) {
    if (from > at) segments.push({ text: passage.slice(at, from), marked: false });
    segments.push({ text: passage.slice(from, to), marked: true });
    at = to;
  }
  if (at < passage.length) segments.push({ text: passage.slice(at), marked: false });
  return { segments, marked: true };
}
