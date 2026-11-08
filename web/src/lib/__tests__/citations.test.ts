import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { chipKey, chipLabel, classifyId, docChipLabel, tokenize, type TextPart } from "../citations";

// R5's vitest leg: the SHIPPED tokenizer and classifier against tests/data/citation_corpus.json, whose labels are hand-written.
// tests/test_export_ids.py proves Python and the bare generated constants against the same file; this proves the module the
// Ask page uses. S2: the tokenizer is linear on adversarial input. S4: classification is anchored (it gates every fetch).
const CORPUS_PATH = path.resolve(import.meta.dirname, "..", "..", "..", "..", "tests", "data", "citation_corpus.json");

interface Corpus {
  classify: { id: string; kind: string | null; note: string }[];
  text: { text: string; ids: string[]; note: string }[];
}
const corpus = JSON.parse(readFileSync(CORPUS_PATH, "utf8")) as Corpus;

const CHUNK = "0001045810-26-000021:I.1A:0361";
const XBRL = "xbrl:1045810:revenue:2026-01-25";
const FR = "fr:2026-19537";
const DOC = "doc:0123456789ab:v2:0007";

const citedIds = (parts: TextPart[]) => parts.flatMap((p) => (p.type === "cite" ? [p.id] : []));

describe("the corpus (R5, JavaScript leg over the shipped module)", () => {
  it("is not empty", () => {
    expect(corpus.classify.length).toBeGreaterThan(30);
    expect(corpus.text.length).toBeGreaterThan(20);
  });

  it.each(corpus.classify.map((e) => [e.note, e] as const))("classifies %s as labelled", (_note, entry) => {
    expect(classifyId(entry.id)).toBe(entry.kind);
  });

  it.each(corpus.text.map((e) => [e.note, e] as const))("tokenizes %s to the labelled ids", (_note, entry) => {
    expect(citedIds(tokenize(entry.text))).toEqual(entry.ids);
  });
});

describe("classifyId", () => {
  it("knows the four forms", () => {
    expect(classifyId(CHUNK)).toBe("chunk");
    expect(classifyId(XBRL)).toBe("xbrl");
    expect(classifyId(FR)).toBe("fr");
    expect(classifyId("fr:C1-2026-16628")).toBe("fr");
    expect(classifyId(DOC)).toBe("doc");
  });

  it("is anchored: nothing before, after or inside the id", () => {
    for (const bad of [`${CHUNK}\n`, ` ${CHUNK}`, `${CHUNK} `, `../${CHUNK}`, `${CHUNK}?x=`, "%2f", `${DOC}%2f`, "doc:0123456789AB:v2:0007", "0361", ""]) {
      expect(classifyId(bad)).toBeNull();
    }
  });

  it("is null for anything that is not a string", () => {
    for (const bad of [undefined, null, 5, {}, [CHUNK], Symbol("x")]) expect(classifyId(bad)).toBeNull();
  });
});

describe("tokenize", () => {
  it("splits text around chips and keeps every other character", () => {
    expect(tokenize(`a [${CHUNK}] b [${FR}]`)).toEqual([
      { type: "text", text: "a " },
      { type: "cite", id: CHUNK },
      { type: "text", text: " b " },
      { type: "cite", id: FR },
    ]);
  });

  it("returns no parts for an empty string and one text part for plain text", () => {
    expect(tokenize("")).toEqual([]);
    expect(tokenize("plain [text]")).toEqual([{ type: "text", text: "plain [text]" }]);
  });

  it("never matches a forged or malformed token", () => {
    for (const forged of [`[${CHUNK}`, `${CHUNK}]`, `[ ${CHUNK}]`, `[${CHUNK}x]`, `["${CHUNK}"]`, "[fr:2026-1]", "[../../etc]"]) {
      expect(citedIds(tokenize(forged))).toEqual([]);
    }
  });

  it("does not treat a repeated or nested bracket as a token start inside an id", () => {
    expect(citedIds(tokenize(`[[${CHUNK}]]`))).toEqual([CHUNK]);
  });

  it("runs in time linear in the length on adversarial input (S2)", () => {
    const shapes: Record<string, (n: number) => string> = {
      brackets: (n) => "[".repeat(n),
      digits: (n) => `[${"1".repeat(n)}`,
      repeatedOpeners: (n) => `[1111`.repeat(n / 5),
      colons: (n) => `[1:`.repeat(n / 3),
      nearIds: (n) => `[${CHUNK.slice(0, -1)}`.repeat(n / 29),
    };
    for (const [name, make] of Object.entries(shapes)) {
      const time = (n: number) => {
        const text = make(n);
        const start = performance.now();
        tokenize(text);
        return performance.now() - start;
      };
      time(20_000); // warm up
      const small = Math.max(time(100_000), 0.5);
      const large = time(400_000);
      // 4x the input: linear is about 4x; quadratic would be about 16x. Generous bound for noisy CI.
      expect(large / small, name).toBeLessThan(10);
    }
  });
});

describe("chipLabel (index.html:152-178)", () => {
  it("shows the item and paragraph of a chunk, not a bare number", () => {
    expect(chipLabel(CHUNK)).toBe("Item 1A \u00b60361");
    expect(chipLabel("0000002488-26-000021:II.7:0012")).toBe("Item 7 \u00b60012");
  });

  it("adds form and period once evidence is known", () => {
    expect(chipLabel(CHUNK, { form: "10-K", filing_date: "2026-02-25" })).toBe("10-K filed 2026-02-25 \u00b7 Item 1A \u00b60361");
    expect(chipLabel(CHUNK, { form: "10-K", fiscal_year: 2026, filing_date: "2026-02-25" })).toBe("10-K FY2026 \u00b7 Item 1A \u00b60361");
    expect(chipLabel(CHUNK, {})).toBe("Item 1A \u00b60361");
    expect(chipLabel(CHUNK, null)).toBe("Item 1A \u00b60361");
    expect(chipLabel(CHUNK, "not an object")).toBe("Item 1A \u00b60361");
  });

  it("labels the other forms", () => {
    expect(chipLabel(XBRL)).toBe("XBRL revenue FY ended 2026-01-25");
    expect(chipLabel("xbrl:1045810:data_center_revenue:2026-01-25")).toBe("XBRL data center revenue FY ended 2026-01-25");
    expect(chipLabel(FR)).toBe("BIS rule 2026-19537");
    expect(chipLabel(DOC)).toBe("your document v2 \u00b60007");
  });

  it("falls back to the text itself for an unknown id and to an empty string for nothing", () => {
    expect(chipLabel("Reported Metrics")).toBe("Reported Metrics");
    expect(chipLabel(undefined)).toBe("");
    expect(chipLabel(null)).toBe("");
  });
});

describe("docChipLabel (index.html:454): plain text, the distinct from-your-document label", () => {
  it("uses the uploaded title when evidence has one", () => {
    expect(docChipLabel(DOC, { title: "Board memo" })).toBe("Board memo \u00b7 v2 \u00b60007");
  });

  it("falls back to the bare label while the evidence is unknown", () => {
    expect(docChipLabel(DOC)).toBe("your document \u00b7 v2 \u00b60007");
    expect(docChipLabel(DOC, {})).toBe("your document \u00b7 v2 \u00b60007");
    expect(docChipLabel(DOC, null)).toBe("your document \u00b7 v2 \u00b60007");
  });

  it("keeps a title with markup as text (the caller renders text nodes)", () => {
    expect(docChipLabel(DOC, { title: "<img onerror=x>" })).toBe("<img onerror=x> \u00b7 v2 \u00b60007");
  });

  it("copes with a malformed id", () => {
    expect(docChipLabel("doc:x")).toBe("your document \u00b7 v? \u00b6?");
  });
});

describe("chipKey (A2)", () => {
  it("joins place, answer sequence, id and occurrence", () => {
    expect(chipKey("answer", 3, CHUNK, 1)).toBe(`answer:3:${CHUNK}:1`);
    expect(chipKey("changes", 0, DOC, 0)).toBe(`changes:0:${DOC}:0`);
  });

  it("differs for another sequence, occurrence or place (a new answer never reuses a key)", () => {
    const keys = new Set([chipKey("answer", 1, CHUNK, 0), chipKey("answer", 2, CHUNK, 0), chipKey("answer", 1, CHUNK, 1), chipKey("changes", 1, CHUNK, 0)]);
    expect(keys.size).toBe(4);
  });
});
