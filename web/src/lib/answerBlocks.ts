// R21 (docs/specs/m5b-frontend.md): the grammar of the old renderMarkdown (index.html, the function of that name) as DATA, never as
// markup. A line starting with `-` or `*` and a space is a list item, `#` to `####` and a space is a heading (h3), any other
// non-empty line is a paragraph; `**x**` is bold; a citation token from lib/citations.ts is a chip. Everything else is text.
// Order matters and is the legacy order: bold is found first (on the line), citations second (inside each run), so a chip can sit
// inside bold and `**[id**]` is not a chip. Pure functions, linear in the length of the text (trimEnd, never a trailing-space regex, which is quadratic on a whitespace run).
import { tokenize } from "./citations";

export type Leaf = { type: "text"; text: string } | { type: "cite"; id: string };
export type Inline = Leaf | { type: "strong"; children: Leaf[] };
export type Block =
  | { type: "p"; inline: Inline[] }
  | { type: "h3"; inline: Inline[] }
  | { type: "ul"; items: Inline[][] };

const BOLD = /\*\*(.+?)\*\*/g;
const LIST_ITEM = /^\s*[-*]\s+(.*)/;
const HEADING = /^#{1,4}\s+(.*)/;

const leaves = (text: string): Leaf[] => tokenize(text);

/** One line's inline content: bold runs first, then the citation tokens of every run. */
export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const match of text.matchAll(BOLD)) {
    if (match.index > last) out.push(...leaves(text.slice(last, match.index)));
    out.push({ type: "strong", children: leaves(match[1]) });
    last = match.index + match[0].length;
  }
  if (last < text.length) out.push(...leaves(text.slice(last)));
  return out;
}

/** The blocks of an answer, in order. Consecutive list lines form one list. */
export function parseAnswer(markdown: unknown): Block[] {
  const lines = String(markdown ?? "").split("\n");
  const blocks: Block[] = [];
  let list: Inline[][] | null = null;
  for (const raw of lines) {
    const line = raw.trimEnd(); // not a regex: /s+$/ is quadratic on a long whitespace run followed by a non-space
    const item = LIST_ITEM.exec(line);
    if (item) {
      if (list === null) {
        list = [];
        blocks.push({ type: "ul", items: list });
      }
      list.push(parseInline(item[1]));
      continue;
    }
    list = null;
    const heading = HEADING.exec(line);
    if (heading) {
      blocks.push({ type: "h3", inline: parseInline(heading[1]) });
    } else if (line.trim()) {
      blocks.push({ type: "p", inline: parseInline(line) });
    }
  }
  return blocks;
}
