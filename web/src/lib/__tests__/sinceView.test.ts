import { describe, expect, it } from "vitest";
import type { Freshness, SinceData, SinceFiling } from "@/pages/Freshness.lib";
import { filingText, filingsFor, filingsPart, partOf, ruleText, sinceState } from "../sinceView";

// docs/specs/m2a-answer-integrity.md P4: what the "Since our data" panel shows, as a pure function of the payload.

const data = (over: Partial<SinceData> = {}): SinceData => ({
  since: "2026-09-24", rules_since: "2026-09-24", filings: [], filing_totals: {}, rules: [], rules_more: false, dropped: { filings: 0, rules: 0 }, ...over,
});
const freshness = (since_data: Freshness["since_data"], status = "ok"): Freshness => ({
  status, checked_at: null, snapshot_as_of: null, last_error_at: null, next_check_at: null, pending_count: null, pending_filings: [],
  federal_register: null, unresolved: [], since_data,
});
const filing = (over: Partial<SinceFiling> = {}): SinceFiling => ({
  ticker: "NVDA", form: "8-K", filing_date: "2026-11-19", accession_no: "0001045810-26-000020", items: ["2.02", "9.01"], href: null, ...over,
});

describe("sinceState", () => {
  it.each([
    ["invalid", "ok", "hidden"],
    ["invalid", "error", "hidden"],
    [null, "ok", "notChecked"],
    [null, "disabled", "notChecked"],
    [null, "error", "notChecked"],
    [data(), "ok", "data"],
    [data(), "error", "data"],
  ] as const)("since_data %j with status %s is %s", (since, status, kind) => {
    expect(sinceState(freshness(since, status)).kind).toBe(kind);
  });
});

describe("partOf", () => {
  it.each([
    [[1], 0, "rows"],
    [[1], 4, "rows"],
    [[], 0, "empty"],
    [[], 2, "omitted"],
  ] as const)("rows %j with %i dropped is %s", (rows, dropped, kind) => {
    expect(partOf([...rows], dropped).kind).toBe(kind);
  });
});

describe("filingsPart (a company the server did not check is never reported as having nothing new)", () => {
  const nvda = filing();
  const tsm = filing({ ticker: "TSM", form: "6-K", items: [], accession_no: "0001046179-26-000001" });
  it("is the rows when there are rows, even if some company is unresolved", () => {
    expect(filingsPart(data({ filings: [nvda] }), ["ASML"])).toEqual({ kind: "rows", rows: [nvda] });
  });
  it("is empty when the list is empty, nothing was dropped and every company was checked", () => {
    expect(filingsPart(data(), []).kind).toBe("empty");
    expect(filingsPart(data(), [], "NVDA").kind).toBe("empty");
  });
  it("is omitted when the list is empty and any company is unresolved", () => {
    expect(filingsPart(data(), ["ASML"]).kind).toBe("omitted");
  });
  it("is omitted when rows were dropped", () => {
    expect(filingsPart(data({ dropped: { filings: 1, rules: 0 } }), []).kind).toBe("omitted");
  });
  it("for one company: its rows; omitted when that company is unresolved; empty when only another one is", () => {
    const d = data({ filings: [nvda, tsm] });
    expect(filingsPart(d, [], "TSM")).toEqual({ kind: "rows", rows: [tsm] });
    expect(filingsPart(d, ["TSM"], "AMD").kind).toBe("empty");
    expect(filingsPart(d, ["AMD"], "AMD").kind).toBe("omitted");
    expect(filingsPart(d, ["AMD"], "NVDA")).toEqual({ kind: "rows", rows: [nvda] });
  });
  it("for one company with no rows: omitted when the list lost rows", () => {
    expect(filingsPart(data({ filings: [nvda], dropped: { filings: 2, rules: 0 } }), [], "AMD").kind).toBe("omitted");
  });
});

describe("filingsFor", () => {
  it("returns only that company's rows", () => {
    const d = data({ filings: [filing(), filing({ ticker: "TSM", form: "6-K", items: [] })] });
    expect(filingsFor(d, "NVDA")).toEqual([d.filings[0]]);
    expect(filingsFor(d, "TSM")).toEqual([d.filings[1]]);
    expect(filingsFor(d, "AMD")).toEqual([]);
  });
});

describe("the row sentences", () => {
  it("names the company and the items, with the date in words", () => {
    expect(filingText(filing())).toBe(
      "Nvidia filed an 8-K on Nov 19, 2026: Results of Operations and Financial Condition \u00b7 Financial Statements and Exhibits",
    );
    expect(filingText(filing({ ticker: "TSM", form: "6-K", items: [] }))).toBe("TSMC filed a 6-K on Nov 19, 2026");
  });
  it("is one sentence for a rule, with its title as text", () => {
    expect(ruleText({ title: "<b>Entity List</b>", publication_date: "2026-11-18", document_number: "2026-23001", href: null })).toBe("Nov 18, 2026 <b>Entity List</b>");
  });
});
