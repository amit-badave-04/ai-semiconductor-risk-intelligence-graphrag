import { describe, expect, it } from "vitest";
import { highlight, MAX_PASSAGE_CHARS, MAX_QUOTE_CHARS, MIN_FRAGMENT_CHARS } from "../highlight";

// docs/specs/m5b-redesign.md RD18 (with the R2 plan's critique): a quote is split on an ellipsis ("..." or U+2026); a fragment
// under 20 characters is skipped; each fragment of 20 or more characters is marked where it first appears, matched exactly
// after whitespace normalisation (no case, quote or apostrophe folding); if any such fragment is not found whole (the quote
// straddles two passages, or the passage is cut short) the quote marks nothing. The segments always rejoin to the passage
// exactly. Untrusted text on both sides: plain strings out, matched with indexOf (never a RegExp built from data), bounded work.

const PASSAGE =
  "As of January 28, 2024, we had net outstanding a total of $9.7 billion in notes due by 2060. As each series of senior notes matures, unless redeemed or repurchased, we must repay or refinance the notes.";

const join = (r: ReturnType<typeof highlight>) => r.segments.map((s) => s.text).join("");
const marked = (r: ReturnType<typeof highlight>) => r.segments.filter((s) => s.marked).map((s) => s.text);

describe("highlight", () => {
  it("marks an exact quote of 20 or more characters, and the segments rejoin to the passage", () => {
    const r = highlight(PASSAGE, ["we had net outstanding a total of $9.7 billion"]);
    expect(r.marked).toBe(true);
    expect(marked(r)).toEqual(["we had net outstanding a total of $9.7 billion"]);
    expect(join(r)).toBe(PASSAGE);
  });

  it("matches across differences in whitespace and keeps the passage's own whitespace in the mark", () => {
    const passage = "Our   ability to\n design and\tintroduce new products in a timely manner.";
    const r = highlight(passage, ["Our ability to design and introduce new products"]);
    expect(marked(r)).toEqual(["Our   ability to\n design and\tintroduce new products"]);
    expect(join(r)).toBe(passage);
  });

  it("splits a quote on an ellipsis and marks each long fragment in its own place", () => {
    for (const ellipsis of ["...", "…", " … "]) {
      const r = highlight(PASSAGE, [`we had net outstanding a total of $9.7 billion${ellipsis}we must repay or refinance the notes`]);
      expect(marked(r)).toEqual(["we had net outstanding a total of $9.7 billion", "we must repay or refinance the notes"]);
      expect(join(r)).toBe(PASSAGE);
    }
  });

  it("skips a fragment under 20 characters and still marks the long ones", () => {
    expect(MIN_FRAGMENT_CHARS).toBe(20);
    const r = highlight(PASSAGE, ["As of January 28 … we had net outstanding a total of $9.7 billion"]);
    expect(marked(r)).toEqual(["we had net outstanding a total of $9.7 billion"]);
  });

  it("marks nothing for a quote with no fragment of 20 characters", () => {
    const r = highlight(PASSAGE, ["notes due by 2060"]);
    expect(r.marked).toBe(false);
    expect(r.segments).toEqual([{ text: PASSAGE, marked: false }]);
  });

  it("marks nothing for a quote one of whose long fragments is not in the passage (a straddle or a cut passage)", () => {
    const straddle = "we must repay or refinance the notes. If we decide to refinance, we may receive less favorable terms";
    const r = highlight(PASSAGE, [straddle, "we had net outstanding a total of $9.7 billion … a sentence that is not in this passage"]);
    expect(r.marked).toBe(false);
    expect(join(r)).toBe(PASSAGE);
  });

  it("is case-sensitive and folds no quote, apostrophe or dash", () => {
    expect(highlight(PASSAGE, ["WE HAD NET OUTSTANDING A TOTAL OF $9.7 BILLION"]).marked).toBe(false);
    expect(highlight("We can’t assure you that our products will succeed.", ["We can't assure you that our products"]).marked).toBe(false);
  });

  it("strips straight and curly double quote marks around a quote before matching", () => {
    for (const q of ['"we had net outstanding a total of $9.7 billion"', "“we had net outstanding a total of $9.7 billion”"]) {
      expect(marked(highlight(PASSAGE, [q]))).toEqual(["we had net outstanding a total of $9.7 billion"]);
    }
  });

  it("merges overlapping marks without repeating text, and a list of no quotes returns the passage unmarked", () => {
    const r = highlight(PASSAGE, ["we had net outstanding a total of $9.7 billion", "a total of $9.7 billion in notes due by 2060"]);
    expect(marked(r)).toEqual(["we had net outstanding a total of $9.7 billion in notes due by 2060"]);
    expect(join(r)).toBe(PASSAGE);
    expect(highlight(PASSAGE, [])).toEqual({ segments: [{ text: PASSAGE, marked: false }], marked: false });
  });

  it("keeps hostile strings as plain text and marks only an exact copy (SR2)", () => {
    const hostile = 'Revenue rose <img src=x onerror="alert(1)"> and javascript:alert(2) appeared in this risk factor text.';
    const r = highlight(hostile, ['<img src=x onerror="alert(1)"> and javascript:alert(2)']);
    expect(marked(r)).toEqual(['<img src=x onerror="alert(1)"> and javascript:alert(2)']);
    expect(join(r)).toBe(hostile);
    expect(highlight(hostile, ["<img src=x onerror=alert(1)> and javascript:alert(2)"]).marked).toBe(false);
  });

  it("treats regular-expression characters in a quote literally", () => {
    const passage = "The rate (as defined) was 5.25% per year [subject to change] and *more* text follows here.";
    expect(marked(highlight(passage, ["(as defined) was 5.25% per year [subject"]))).toEqual(["(as defined) was 5.25% per year [subject"]);
    expect(highlight(passage, [".* was 5.25% per year [subject to change]"]).marked).toBe(false);
  });

  it("bounds its work: an over-long passage or quote is left unmarked, quickly, without throwing", () => {
    const long = "word ".repeat(MAX_PASSAGE_CHARS / 5 + 10);
    expect(highlight(long, ["word word word word word word"]).marked).toBe(false);
    expect(highlight(PASSAGE, ["x".repeat(MAX_QUOTE_CHARS + 1)]).marked).toBe(false);
    const many = Array.from({ length: 500 }, () => "we had net outstanding").join(" … ");
    const started = performance.now();
    expect(() => highlight(PASSAGE, [many, ...Array.from({ length: 200 }, () => many)])).not.toThrow();
    expect(performance.now() - started).toBeLessThan(500);
  });

  it("always rejoins to the passage, whatever the whitespace (generated cases)", () => {
    const spaces = [" ", "  ", "\n", "\t", "\r\n", " ", " \n "];
    let seed = 7;
    const rand = (n: number) => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed % n;
    };
    for (let i = 0; i < 200; i += 1) {
      const words = "we had net outstanding a total of nine point seven billion in notes due by twenty sixty".split(" ");
      const passage = (rand(2) ? " " : "") + words.map((w) => w + spaces[rand(spaces.length)]).join("");
      const start = rand(6);
      const quote = words.slice(start, start + 6 + rand(6)).join(" ");
      const r = highlight(passage, [quote]);
      expect(join(r)).toBe(passage);
      if (quote.length >= MIN_FRAGMENT_CHARS) expect(r.marked).toBe(true);
    }
  });
});

describe("highlight caps (R2 review: exercised, not only declared)", () => {
  it("uses at most 8 fragments of a quote: a missing ninth fragment does not undo the first eight", () => {
    const passage = Array.from({ length: 9 }, (_, i) => `fragment number ${i} of the passage text here.`).join(" ");
    const fragments = Array.from({ length: 9 }, (_, i) => `fragment number ${i} of the passage text`);
    fragments[8] = "a ninth fragment that is nowhere in the passage";
    const r = highlight(passage, [fragments.join(" \u2026 ")]);
    expect(r.marked).toBe(true);
    expect(r.segments.filter((s) => s.marked)).toHaveLength(8);
  });

  it("reads at most 50 quotes: a 51st that would match marks nothing", () => {
    const misses = Array.from({ length: 50 }, (_, i) => `a quote that is not in the passage, number ${i}`);
    expect(highlight(PASSAGE, [...misses, "we had net outstanding a total of $9.7 billion"]).marked).toBe(false);
  });
});
