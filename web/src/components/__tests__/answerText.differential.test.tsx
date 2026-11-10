import { readFileSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it } from "vitest";
import { AnswerText } from "@/components/Chips";
import { HOSTILE, generated } from "./answerCorpus";

// R21's transitional differential test (it goes when /legacy goes): every recorded answer, the hostile strings and a seeded
// corpus go through the legacy renderMarkdown (the page's own script, loaded by tests/ui/harness.mjs) and through AnswerText;
// both are reduced to one normalised tree (b is strong, the chip span and the chip button are one chip) and must be equal.
// The legacy output is parsed, never compared as a string: esc() writes &#39; where React writes &#x27;.

const ROOT = path.resolve(import.meta.dirname, "../../../..");
const SSE_DIR = path.join(ROOT, "tests", "data", "sse");
const GENERATED_COUNT = 2000;

interface LegacyApi {
  renderMarkdown: (text: string) => string;
}
let legacy: LegacyApi;

// The page's own script, loaded by the existing harness. It is required, not imported: the module runner of vitest cannot reach
// a file outside the project root, and Node loads an ES module without top-level await through require.
beforeAll(() => {
  const harness = createRequire(import.meta.url)(path.join(ROOT, "tests", "ui", "harness.mjs")) as {
    loadPage: () => { api: LegacyApi };
  };
  legacy = harness.loadPage().api;
});

const ENTITIES: Record<string, string> = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&#x27;": "'" };
const decode = (text: string) => text.replace(/&(?:amp|lt|gt|quot|#39|#x27);/g, (entity) => ENTITIES[entity]);

/**
 * Both renderers write only p, ul, li, h3, b or strong, and a chip (span or button with class cite), so the markup is read by a
 * small tokenizer instead of a DOM (a DOM per string is too slow and heavy for 2000 strings). Text is entity-decoded, b is
 * strong, and a chip becomes <chip ID></chip>: since RD11 (docs/specs/m5b-redesign.md) the chip's label is a redesigned surface
 * and no longer the legacy text, so the label and the icon are left out and the chip is compared by its id and its place. (This
 * also covers the pinned difference of a chunk id whose prefix is not 10..30 characters: tests/test_export_ids.py.)
 */
function normalise(markup: string): string {
  let out = "";
  let inChip = 0; // the depth of open elements inside the current chip (0: not in a chip)
  for (const piece of markup.split(/(<[^>]*>)/)) {
    if (piece === "") continue;
    if (!piece.startsWith("<")) {
      if (inChip === 0) out += JSON.stringify(decode(piece));
      continue;
    }
    const tag = /^<(\/?)([a-z0-9]+)/.exec(piece);
    if (tag === null) throw new Error(`unexpected markup ${piece}`);
    const [, closing, name] = tag;
    if (inChip > 0) {
      // RD11: a chip's content (its label and kind icon) is a redesigned surface, compared by its own tests (stamp.test.ts,
      // stampChips.test.tsx); here only the chip's id and its place in the tree count
      if (piece.endsWith("/>")) continue;
      inChip += closing ? -1 : 1;
      if (inChip === 0) out += "</chip>";
    } else if (closing) {
      out += `</${name === "b" ? "strong" : name}>`;
    } else if ((name === "span" || name === "button") && /class="[^"]*cite/.test(piece)) {
      const id = decode(/data-id="([^"]*)"/.exec(piece)?.[1] ?? "");
      out += `<chip ${id}>`;
      inChip = 1;
    } else {
      out += `<${name === "b" ? "strong" : name}>`;
    }
  }
  return out;
}

const shipped = (text: string) => normalise(renderToStaticMarkup(<AnswerText text={text} place="answer" gen={1} />));
const old = (text: string) => normalise(legacy.renderMarkdown(text));

/** Equal trees, or a SHORT description of the first difference (a diff of two 120 KB trees exhausts the heap). */
function difference(text: string): string | null {
  const a = shipped(text);
  const b = old(text);
  if (a === b) return null;
  let at = 0;
  while (at < a.length && a[at] === b[at]) at += 1;
  return `${JSON.stringify(text.slice(0, 60))}: trees differ at ${at}: shipped ${JSON.stringify(a.slice(Math.max(0, at - 40), at + 60))} legacy ${JSON.stringify(b.slice(Math.max(0, at - 40), at + 60))}`;
}

function recordedAnswers(): string[] {
  const answers: string[] = [];
  for (const name of readdirSync(SSE_DIR).filter((n) => n.endsWith(".expected.json"))) {
    const events = (JSON.parse(readFileSync(path.join(SSE_DIR, name), "utf-8")) as { events: { event: string; answer?: string }[] }).events;
    for (const e of events) if (e.event === "done" && typeof e.answer === "string") answers.push(e.answer);
  }
  return answers;
}

describe("AnswerText against the legacy renderMarkdown (R21)", () => {
  it("has a non-empty corpus: recorded answers, hostile strings, generated strings", () => {
    expect(recordedAnswers().length).toBeGreaterThanOrEqual(6);
    expect(HOSTILE.length).toBeGreaterThanOrEqual(40);
  });

  it("every recorded done.answer has the same structure", () => {
    expect(recordedAnswers().map(difference).filter(Boolean)).toEqual([]);
  });

  it("every hostile string has the same structure", () => {
    expect(HOSTILE.map(difference).filter(Boolean)).toEqual([]);
  });

  it("2000 generated strings have the same structure", () => {
    expect(generated(GENERATED_COUNT).map(difference).filter(Boolean).slice(0, 5)).toEqual([]);
  });

  it("the corpus actually reaches chips, strong, lists and headings (the comparison is not vacuous)", () => {
    const all = [...recordedAnswers(), ...HOSTILE, ...generated(GENERATED_COUNT)].map(old).join("");
    for (const marker of ["<chip ", "<strong>", "<ul>", "<li>", "<h3>", "<p>"]) expect(all).toContain(marker);
  });
});
