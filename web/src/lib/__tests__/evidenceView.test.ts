import { describe, expect, it } from "vitest";
import { evidenceView, formatNumber } from "../evidenceView";
import { safeUrl } from "../safeUrl";

// tests/ui/evidence.test.mjs, 13 of its 16 titles, ported (the other three, the escaping of every field in the drawer and the two
// dropDocEvidence titles, are ported in components/__tests__/evidenceDrawer.test.tsx). Each title is the legacy title, character for
// character. The legacy `evidenceView` returned HTML strings; the shipped one returns data, so each assertion reads the same fact
// from the data (title, note, freshness badges, fact rows, text, link).
const CHUNK = "0001045810-26-000021:I.1A:0361";
const XBRL = "xbrl:1045810:revenue:2026-01-25";
// built in pieces: the linter refuses the literal, and the test is precisely about what happens to such a value
const SCRIPT_URL = ["java", "script:alert(1)"].join("");
const FR = "fr:2026-19537";

// The shape GET /api/evidence/{chunk id} returns: {"type": "chunk", ...EVIDENCE_QUERY's columns} (serve/routes.py).
// `item_headlines` (a list, empty until the loader has produced RiskItem nodes) is the only headline field there is.
const CHUNK_PAYLOAD = {
  type: "chunk",
  chunk_id: CHUNK,
  text: "We are subject to laws on privacy <b>fines</b>.",
  source_url: "https://www.sec.gov/Archives/x.htm",
  section_key: "0001045810-26-000021:I.1A",
  section_title: "Risk Factors",
  accession_no: "0001045810-26-000021",
  form: "10-K",
  filing_date: "2026-02-25",
  filer: "NVIDIA Corp",
  mentions: ["TSMC", "Micron"],
  status: "current",
  is_current: true,
  retrievable: true,
  valid_to: null,
  superseded_by: null,
  corrected_by: null,
  item_headlines: [] as string[],
};
const facts = (v: ReturnType<typeof evidenceView>) => v.facts.map((f) => `${f.label}: ${f.value}`).join("\n");
const badges = (v: ReturnType<typeof evidenceView>) => v.fresh.badges.map((b) => `${b.cls}|${b.text}`);

describe("tests/ui/evidence.test.mjs", () => {
  it("a current filing excerpt shows a current badge, its provenance and a safe link", () => {
    const v = evidenceView(CHUNK, CHUNK_PAYLOAD);
    expect(v.kind).toBe("chunk");
    expect(v.title).toMatch(/10-K filed 2026-02-25 \u00b7 Item 1A \u00b60361/);
    expect(v.note).toMatch(/NVIDIA Corp/);
    expect(v.note).toMatch(/Risk Factors/);
    expect(v.note).toMatch(/mentions: TSMC, Micron/);
    expect(badges(v)).toContain("ok|current");
    expect(v.text).toBe(CHUNK_PAYLOAD.text); // the excerpt is returned as raw text and shown as a text node
    expect(v.link?.href).toBe("https://www.sec.gov/Archives/x.htm");
  });

  it("a superseded excerpt names the replacing filing and the end of validity", () => {
    const v = evidenceView(CHUNK, { ...CHUNK_PAYLOAD, status: "superseded", is_current: false, retrievable: false, valid_to: "2026-02-24", superseded_by: "0001045810-27-000010" });
    const all = badges(v).join("\n");
    expect(all).toMatch(/superseded by 0001045810-27-000010/);
    expect(all).toMatch(/valid until 2026-02-24/);
    expect(all).toMatch(/not returned by default search/);
    expect(badges(v)).not.toContain("ok|current");
  });

  it("a corrected excerpt names the amending filing", () => {
    const v = evidenceView("0000002488-26-000010:II.7:0003", { ...CHUNK_PAYLOAD, status: "corrected", is_current: false, retrievable: false, corrected_by: "0000002488-26-000021" });
    expect(badges(v).join("\n")).toMatch(/bad\|corrected by 0000002488-26-000021/);
    expect(v.fresh.hint).toMatch(/later amendment restated this passage/);
  });

  it("the risk items a chunk belongs to (item_headlines, the field /api/evidence returns) are shown", () => {
    const v = evidenceView(CHUNK, { ...CHUNK_PAYLOAD, item_headlines: ["We may be subject to fines under privacy laws", "Privacy rules differ by country"] });
    expect(v.facts).toEqual([{ label: "Risk item", value: "We may be subject to fines under privacy laws; Privacy rules differ by country" }]);
  });

  it("a chunk that belongs to no risk item shows no risk-item row", () => {
    expect(evidenceView(CHUNK, CHUNK_PAYLOAD).facts).toEqual([]);
  });

  it("a field /api/evidence never returns (a singular `headline`) is not rendered", () => {
    // routes.py EVIDENCE_QUERY returns item_headlines only; the page used to also read `headline`, which no response carries.
    const v = evidenceView(CHUNK, { ...CHUNK_PAYLOAD, headline: "NOT A FIELD OF THE API" });
    expect(v.facts).toEqual([]);
    expect(JSON.stringify(v)).not.toMatch(/NOT A FIELD OF THE API/);
  });

  it("an XBRL id shows the metric payload", () => {
    const v = evidenceView(XBRL, { value: 215938000000, unit: "USD", period: "2025-01-27 to 2026-01-25", concept: "us-gaap:Revenues", accession: "0001045810-26-000021", company: "NVIDIA Corp" });
    expect(v.kind).toBe("xbrl");
    expect(v.title).toMatch(/XBRL revenue FY ended 2026-01-25/);
    const f = facts(v);
    expect(f).toMatch(/NVIDIA Corp/);
    expect(f).toMatch(/215,938,000,000 USD/);
    expect(f).toMatch(/us-gaap:Revenues/);
    expect(f).toMatch(/2025-01-27 to 2026-01-25/);
    expect(f).toMatch(/0001045810-26-000021/);
    expect(v.text).toBe("");
  });

  it("the payload shapes /api/evidence returns are rendered (routes.py XBRL, Federal Register and chunk queries)", () => {
    const xbrl = evidenceView(XBRL, {
      type: "xbrl", metric_id: "1045810:revenue:2026-01-25", metric: "revenue", concept: "us-gaap:Revenues", value: 215938000000, unit: "USD",
      period_start: "2025-01-27", period_end: "2026-01-25", company: "NVIDIA Corp", cik: 1045810, accession_no: "0001045810-26-000021", form: "10-K",
      filing_date: "2026-02-25", source_url: "https://www.sec.gov/Archives/edgar/data/1045810/x.htm",
    });
    expect(facts(xbrl)).toMatch(/215,938,000,000 USD/);
    expect(facts(xbrl)).toMatch(/2025-01-27 to 2026-01-25/);
    expect(facts(xbrl)).toMatch(/10-K \u00b7 filed 2026-02-25 \u00b7 0001045810-26-000021/);
    expect(xbrl.link?.href).toMatch(/sec\.gov/);
    const fr = evidenceView(FR, {
      type: "fr", source: "federal_register", external: true, note: "A Federal Register rule linked to companies by keyword match; not a company disclosure.",
      document_number: "2026-19537", title: "Rule title", publication_date: "2026-09-10", url: "https://www.federalregister.gov/d/2026-19537",
      kind: "entity_list_additions", topics: ["china", "hbm"], relevant: false, abstract: "The abstract of the rule.",
    });
    expect(facts(fr)).toMatch(/china, hbm/);
    expect(facts(fr)).toMatch(/Kind flagged relevant: no/);
    expect(fr.text).toBe("The abstract of the rule.");
    expect(fr.note).toMatch(/external event/i);
    const chunk = evidenceView(CHUNK, { ...CHUNK_PAYLOAD, item_headlines: ["We may face privacy fines"] });
    expect(facts(chunk)).toMatch(/We may face privacy fines/);
  });

  it("an XBRL payload with a period object still renders", () => {
    const v = evidenceView(XBRL, { value: 1, unit: "USD", period: { start: "2025-01-27", end: "2026-01-25" } });
    expect(facts(v)).toMatch(/2025-01-27 to 2026-01-25/);
  });

  it("a Federal Register id shows the rule and says it is an external keyword match", () => {
    const v = evidenceView(FR, { title: "Revision to license exceptions", publication_date: "2026-09-10", document_number: "2026-19537", url: "https://www.federalregister.gov/d/2026-19537", kind: "licensing_policy", relevant: true });
    expect(v.kind).toBe("fr");
    expect(v.title).toMatch(/BIS rule 2026-19537/);
    expect(facts(v)).toMatch(/Revision to license exceptions/);
    expect(facts(v)).toMatch(/2026-09-10/);
    expect(facts(v)).toMatch(/licensing_policy/);
    expect(v.note).toMatch(/external event/i);
    expect(v.note).toMatch(/keyword/i);
    expect(v.note).toMatch(/not a statement (made )?by the company/i);
    expect(v.link?.href).toBe("https://www.federalregister.gov/d/2026-19537");
  });

  it("only https links are ever rendered", () => {
    expect(safeUrl("https://www.sec.gov/a?b=1&c=2")).toBe("https://www.sec.gov/a?b=1&c=2");
    expect(safeUrl(SCRIPT_URL)).toBeNull();
    expect(safeUrl("http://example.com")).toBeNull();
    expect(safeUrl("data:text/html,<script>")).toBeNull();
    expect(safeUrl(undefined)).toBeNull();
    expect(evidenceView(FR, { title: "x", url: SCRIPT_URL }).link).toBeNull();
  });

  it("an empty or partial payload never throws", () => {
    expect(() => evidenceView(CHUNK, {})).not.toThrow();
    expect(() => evidenceView(XBRL, {})).not.toThrow();
    expect(() => evidenceView(FR, {})).not.toThrow();
    expect(() => evidenceView("junk", null)).not.toThrow();
    expect(evidenceView(CHUNK, {}).text).toBe("");
  });

  it("formatNumber groups digits and leaves non-numbers alone", () => {
    expect(formatNumber(215938000000)).toBe("215,938,000,000");
    expect(formatNumber("1234.5")).toBe("1,234.5");
    expect(formatNumber("abc")).toBe("abc");
    expect(formatNumber(null)).toBe("");
  });
});
