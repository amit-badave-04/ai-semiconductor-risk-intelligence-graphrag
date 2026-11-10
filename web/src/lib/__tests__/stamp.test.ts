import { describe, expect, it } from "vitest";
import { deck } from "@/copy/deck";
import { stampView } from "@/lib/stamp";

// docs/specs/m5b-redesign.md RD11: a chip's label names the source in plain words ("paragraph 162", no pilcrow, no leading
// zeros). Until the evidence loads a filing chip shows only the filing year (from an accession-shaped id, when the year is
// sane), never a company guessed from the accession prefix; once loaded it names the filer, the form and the filing year.

const CHUNK = "0001045810-24-000029:I.1A:0162";
const label = (id: unknown, evidence?: unknown) => deck.chips.label(stampView(id, evidence));

describe("stampView and the chip label (RD11)", () => {
  it("names a filing passage by year and paragraph before its evidence loads, with no pilcrow and no leading zeros", () => {
    expect(stampView(CHUNK)).toEqual({ kind: "chunk", loaded: false, filer: "", form: "", year: "2024", paragraph: "162" });
    expect(label(CHUNK)).toBe("Filing 2024 \u00b7 paragraph 162");
    expect(label(CHUNK)).not.toMatch(/\u00b6|Nvidia|1045810/);
  });

  it("names the filer, the form and the filing year once the evidence has loaded", () => {
    const evidence = { filer: "Nvidia", form: "10-K", filing_date: "2024-02-21" };
    expect(label(CHUNK, evidence)).toBe("Nvidia 10-K, filed 2024 \u00b7 paragraph 162");
    expect(label(CHUNK, { form: "10-K", filing_date: "2024-02-21" })).toBe("10-K, filed 2024 \u00b7 paragraph 162");
    expect(label(CHUNK, { filer: "Nvidia", form: "10-K" })).toBe("Nvidia 10-K \u00b7 paragraph 162");
  });

  it("never prints a fake year: an id that only passes the loose chunk grammar, or a year out of range, shows the paragraph only", () => {
    expect(label("1234567890-1:I.1A:0007")).toBe("Paragraph 7");
    expect(label("0000000000-99-000001:I.1A:0001")).toBe("Paragraph 1"); // 2099 is not a filing year
    expect(label("00000000001-24-000001:I.1A:0001")).toBe("Paragraph 1"); // eleven digits: not accession-shaped
    expect(label(CHUNK, { filing_date: "not a date" })).toBe("Paragraph 162");
    // a prefix outside 10..30 characters is a token to the tokenizer but no id to classifyId: the chip shows the bare id
    // (the pinned difference of tests/test_export_ids.py)
    expect(label("12-34:I.1A:0007")).toBe("12-34:I.1A:0007");
  });

  it("bounds the filer and form from the payload and never takes the company from anywhere else", () => {
    const long = "N".repeat(500);
    expect(label(CHUNK, { filer: long, form: "10-K", filing_date: "2024-02-21" }).length).toBeLessThan(140);
    expect(label(CHUNK, { filer: 42, form: { x: 1 }, filing_date: "2024-02-21" })).toBe("Filed 2024 \u00b7 paragraph 162");
  });

  it("names a reported number by its metric and the end of its period", () => {
    expect(label("xbrl:1045810:revenue:2026-01-25")).toBe("Reported revenue, period ended Jan 25, 2026");
    expect(label("xbrl:1045810:research_and_development:2026-01-25")).toBe("Reported research and development, period ended Jan 25, 2026");
  });

  it("calls a Federal Register document a government rule", () => {
    expect(label("fr:2025-00711")).toBe("Government rule 2025-00711");
    expect(label("fr:C1-2026-16628")).toBe("Government rule C1-2026-16628");
  });

  it("names an uploaded document by its version and paragraph", () => {
    expect(label("doc:0123456789ab:v2:0007")).toBe("Your document, version 2 \u00b7 paragraph 7");
  });

  it("shows an id outside the grammar as it is, and nothing for a missing one", () => {
    expect(label("weird")).toBe("weird");
    expect(label(null)).toBe("");
  });
});
