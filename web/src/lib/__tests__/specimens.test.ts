import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { classifyId } from "../citations";
import { SPECIMENS, specimenView } from "../specimens";

// docs/specs/m5b-redesign.md RD4 and SR3: the three specimen stamps of the Ask page open a real example, pinned here to evidence
// recorded from the live service (web/src/test/fixtures/specimens.recorded.json). The view is a pure function over the
// untrusted payload: plain text, a link only through safeUrl, and null for anything it cannot read.

const recorded = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../test/fixtures/specimens.recorded.json"), "utf-8")) as {
  payloads: Record<string, unknown>;
};
const payload = (id: string): unknown => recorded.payloads[id];

describe("the specimen ids", () => {
  it("are the ids the recorded fixture holds, and each passes classifyId as a public kind (SR3)", () => {
    expect(Object.keys(recorded.payloads).sort()).toEqual(Object.values(SPECIMENS).sort());
    expect(classifyId(SPECIMENS.passage)).toBe("chunk");
    expect(classifyId(SPECIMENS.number)).toBe("xbrl");
    expect(classifyId(SPECIMENS.rule)).toBe("fr");
  });
});

describe("specimenView", () => {
  it("shows a filing passage by its filer, form and section, the filing date, the paragraph and the first sentence", () => {
    const view = specimenView("passage", payload(SPECIMENS.passage));
    expect(view?.heading).toBe("Nvidia 10-K, Item 1A. Risk Factors");
    expect(view?.meta).toBe("Filed Feb 21, 2024 · paragraph 162");
    expect(view?.quote).toBe("As of January 28, 2024, we had net outstanding a total of $9.7 billion in notes due by 2060.");
    expect(view?.note).toBe("From an older filing; a newer one has replaced it.");
    expect(view?.link).toEqual({ href: "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000029/nvda-20240128.htm", text: "Open the filing on SEC.gov" });
  });

  it("shows a reported number compactly, with its exact value, fiscal year and filing", () => {
    const view = specimenView("number", payload(SPECIMENS.number));
    expect(view?.heading).toBe("Nvidia revenue, FY2026 (ended Jan 25, 2026)");
    expect(view?.figure).toEqual({ compact: "$215.9B", exact: "Exactly $215,938,000,000" });
    expect(view?.meta).toBe("Reported in the 10-K filed Feb 25, 2026");
  });

  it("shows a government rule as the government's, never as something a company said", () => {
    const view = specimenView("rule", payload(SPECIMENS.rule));
    expect(view?.label).toBe("Government rule");
    expect(view?.heading).toMatch(/^Implementation of Additional Due Diligence Measures/);
    expect(view?.meta).toBe("Published in the Federal Register on Jan 16, 2025");
    expect(view?.note).toMatch(/keyword matching/);
    expect(view?.note).toMatch(/not statements the company made/);
    expect(view?.link?.href).toMatch(/^https:\/\/www\.federalregister\.gov\//);
  });

  it("cuts a long first sentence at a word with an ellipsis, and never shows more than one sentence", () => {
    const long = { ...(payload(SPECIMENS.passage) as object), text: `${"word ".repeat(80)}end. Second sentence.` };
    const quote = specimenView("passage", long)?.quote ?? "";
    expect(quote.length).toBeLessThanOrEqual(241);
    expect(quote.endsWith("…")).toBe(true);
    expect(quote).not.toContain("Second");
  });

  it("returns null for a payload of the wrong type or shape, and drops a link that is not https", () => {
    expect(specimenView("passage", payload(SPECIMENS.number))).toBeNull();
    expect(specimenView("number", { type: "xbrl" })).toBeNull();
    expect(specimenView("rule", null)).toBeNull();
    const unsafe = { ...(payload(SPECIMENS.rule) as object), url: `${"java"}script:alert(1)` }; // a hostile scheme, built so lint allows it
    expect(specimenView("rule", unsafe)?.link).toBeUndefined();
  });
});
