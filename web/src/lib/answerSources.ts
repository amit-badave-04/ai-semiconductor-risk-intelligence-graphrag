// docs/specs/m5b-redesign.md RD15 and RD18's input. Both walk parseAnswer in the order AnswerText numbers its chips (blocks, list
// items, bold runs, then leaves), so a source is exactly a chip and a quote's key is exactly a chip's occurrence. Pure; the text is
// untrusted model output, scanned by hand (no RegExp built from it) and in time linear in its length.
import { parseAnswer, type Inline, type Leaf } from "./answerBlocks";

/** How far before a chip (in characters) a closing quote mark may end and still belong to it. */
export const QUOTE_NEAR_CHARS = 12;
const MAX_QUOTE_CHARS = 1000;
const CLOSE = new Set(['"', "”"]);
const OPEN = new Set(['"', "“"]);
const ANY_QUOTE = new Set(['"', "“", "”"]);

/** The leaves of each block (a paragraph, a heading, a list item), in document order, bold runs flattened in place. */
function blockLeaves(text: string): Leaf[][] {
  const flatten = (inline: Inline[]): Leaf[] => inline.flatMap((part) => (part.type === "strong" ? part.children : [part]));
  return parseAnswer(text).flatMap((block) => (block.type === "ul" ? block.items.map(flatten) : [flatten(block.inline)]));
}

/** The distinct cited ids of an answer, in first-occurrence order (RD15: "Sources (N)"). */
export function answerSources(text: string): string[] {
  const seen = new Set<string>();
  for (const leaves of blockLeaves(text)) for (const leaf of leaves) if (leaf.type === "cite") seen.add(leaf.id);
  return [...seen];
}

/** The double-quoted text that closes within QUOTE_NEAR_CHARS of the end of `before`, or null. */
function quoteBefore(before: string): string | null {
  let close = -1;
  for (let i = before.length - 1; i >= Math.max(0, before.length - 1 - QUOTE_NEAR_CHARS); i -= 1) {
    if (CLOSE.has(before[i])) {
      close = i;
      break;
    }
  }
  if (close === -1) return null;
  for (let i = close - 1; i >= Math.max(0, close - 1 - MAX_QUOTE_CHARS); i -= 1) {
    if (OPEN.has(before[i])) {
      const quote = before.slice(i + 1, close);
      return quote.trim() === "" ? null : quote;
    }
    if (ANY_QUOTE.has(before[i])) return null; // another mark first: not a pair this chip can use
  }
  return null;
}

/** The quotes before each chip, keyed `${id}:${nth}` as AnswerText numbers its chips; a chip with no quote has no entry. */
export function chipQuotes(text: string): Map<string, string[]> {
  const quotes = new Map<string, string[]>();
  const nth = new Map<string, number>();
  for (const leaves of blockLeaves(text)) {
    let before = "";
    for (const leaf of leaves) {
      if (leaf.type === "text") {
        before += leaf.text;
        continue;
      }
      const n = nth.get(leaf.id) ?? 0;
      nth.set(leaf.id, n + 1);
      const quote = quoteBefore(before.slice(-(MAX_QUOTE_CHARS + QUOTE_NEAR_CHARS + 2)));
      if (quote !== null) quotes.set(`${leaf.id}:${n}`, [quote]);
    }
  }
  return quotes;
}
