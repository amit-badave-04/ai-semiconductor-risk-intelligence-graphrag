import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AnswerText } from "@/components/Chips";
import { generated, HOSTILE } from "@/components/__tests__/answerCorpus";
import { answerSources, chipQuotes, QUOTE_NEAR_CHARS } from "../answerSources";

// docs/specs/m5b-redesign.md RD15 (Sources (N), from the same parsed citations as the chips) and RD18's input (the words an
// answer quotes right before a chip). Both walk parseAnswer, in the order AnswerText numbers its chips.

const A = "0001045810-24-000029:I.1A:0162";
const B = "0001045810-25-000023:I.1A:0040";
const X = "xbrl:1045810:revenue:2026-01-25";
const R = "fr:2025-00711";

const renderedIds = (text: string): string[] =>
  [...renderToStaticMarkup(<AnswerText text={text} place="answer" gen={1} />).matchAll(/data-id="([^"]+)"/g)].map((m) => m[1]);

describe("answerSources", () => {
  it("lists the distinct cited ids in first-occurrence order, inside bold and list items too", () => {
    const text = `Revenue was $215.9B [${X}].\n\n- **Notes [${A}]** and a rule [${R}]\n- again [${X}] and [${A}]`;
    expect(answerSources(text)).toEqual([X, A, R]);
  });

  it("counts an id cited twice once, and never a forged token or an id outside the grammar", () => {
    expect(answerSources(`a [${A}] b [${A}] c [not-an-id] d [${A}x] e **[${B}**]`)).toEqual([A]);
    expect(answerSources("")).toEqual([]);
  });

  it("names exactly the ids AnswerText renders as chips, on recorded answers, generated answers and hostile strings", () => {
    const dir = path.resolve(import.meta.dirname, "../../../../tests/data/sse");
    const recorded = readdirSync(dir)
      .filter((f) => f.endsWith(".expected.json"))
      .flatMap((f) => (JSON.parse(readFileSync(path.join(dir, f), "utf-8")) as { events: { answer?: unknown }[] }).events)
      .map((e) => e.answer)
      .filter((a): a is string => typeof a === "string");
    expect(recorded.length).toBeGreaterThan(3);
    for (const text of [...recorded, ...generated(300), ...HOSTILE]) {
      expect(answerSources(text)).toEqual([...new Set(renderedIds(text))]);
    }
  });
});

describe("chipQuotes", () => {
  it("pairs a quote that ends right before a chip with that chip's occurrence", () => {
    const text = `It said “we had net outstanding a total of $9.7 billion in notes” [${A}] in 2024.`;
    expect(chipQuotes(text).get(`${A}:0`)).toEqual(["we had net outstanding a total of $9.7 billion in notes"]);
  });

  it("accepts straight quotes, and a period or comma between the quote and the chip", () => {
    expect(chipQuotes(`The filing: "we must repay or refinance the notes." [${A}]`).get(`${A}:0`)).toEqual(["we must repay or refinance the notes."]);
    expect(chipQuotes(`"we must repay or refinance the notes", [${A}]`).get(`${A}:0`)).toEqual(["we must repay or refinance the notes"]);
  });

  it(`takes only a quote that ends within ${QUOTE_NEAR_CHARS} characters of the chip`, () => {
    const quote = '"we must repay or refinance the notes"';
    const at = (gap: number) => chipQuotes(`${quote}${"x".repeat(gap)}[${A}]`).get(`${A}:0`);
    expect(QUOTE_NEAR_CHARS).toBe(12);
    expect(at(12)).toEqual(["we must repay or refinance the notes"]);
    expect(at(13)).toBeUndefined();
  });

  it("pairs each quote with its own chip, numbers repeated ids as the chips do, and never reaches across a block", () => {
    const text = `"first quoted words of the filing here" [${A}] then "second quoted words of the other filing" [${B}] and "third quote for the same first filing" [${A}]\n\n[${B}] alone`;
    const q = chipQuotes(text);
    expect(q.get(`${A}:0`)).toEqual(["first quoted words of the filing here"]);
    expect(q.get(`${B}:0`)).toEqual(["second quoted words of the other filing"]);
    expect(q.get(`${A}:1`)).toEqual(["third quote for the same first filing"]);
    expect(q.get(`${B}:1`)).toBeUndefined();
  });

  it("finds a quote inside bold and a list item, and ignores a lone double quote", () => {
    const text = `- **"we had net outstanding a total of $9.7 billion"** [${A}]\n- a lone " mark [${B}]`;
    const q = chipQuotes(text);
    expect(q.get(`${A}:0`)).toEqual(["we had net outstanding a total of $9.7 billion"]);
    expect(q.get(`${B}:0`)).toBeUndefined();
  });

  it("stays linear on an answer full of quote marks", () => {
    const text = `${'"a" '.repeat(20000)}[${A}]`;
    const started = performance.now();
    chipQuotes(text);
    expect(performance.now() - started).toBeLessThan(500);
  });
});

describe("chipQuotes: straight quotes are paired, never guessed (R2 review)", () => {
  it("does not read an opening straight quote as a closing one, so unquoted prose is never passed off as a quote", () => {
    const text = `"x" the firm told investors that revenue rose, "up [${A}]`;
    expect(chipQuotes(text).get(`${A}:0`)).toBeUndefined();
  });

  it("still pairs a balanced straight quote after an earlier balanced one", () => {
    const text = `"x" then "we must repay or refinance the notes" [${A}]`;
    expect(chipQuotes(text).get(`${A}:0`)).toEqual(["we must repay or refinance the notes"]);
  });
});
