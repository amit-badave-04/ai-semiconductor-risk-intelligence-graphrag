import { describe, expect, it } from "vitest";
import type { Edge, Filing, Metric } from "@/lib/company";
import { fiscalColumns, glance, numberRows, relationshipsView, reportLine } from "@/lib/companyView";

// docs/specs/m5b-redesign.md R3 (RD19, RD20, RD22, RD24) and docs/decisions/001-r3-company-copy.md: the Company page's views,
// pure functions over the parsed dossier. Server text is untrusted and is only passed through as strings.

const m = (metric: string, periodEnd: string, value: number, unit = "USD"): Metric => ({
  metric,
  value,
  unit,
  period_start: null,
  period_end: periodEnd,
  cite: `xbrl:1045810:${metric}:${periodEnd}`,
});

const NVDA: Metric[] = [
  m("capex", "2012-01-29", 138e6),
  m("capex", "2011-01-30", 97e6),
  m("capex", "2010-01-31", 78e6),
  ...["2026-01-25", "2025-01-26", "2024-01-28", "2023-01-29"].flatMap((d, i) => [
    m("revenue", d, 100 - i),
    m("net_income", d, 50 - i),
    m("rnd", d, 10 - i),
  ]),
];

describe("fiscalColumns", () => {
  it("takes at most four of the company's newest fiscal years, newest first, and a stale metric never makes a column", () => {
    const columns = fiscalColumns(NVDA);
    expect(columns.map((c) => c.periodEnd)).toEqual(["2026-01-25", "2025-01-26", "2024-01-28", "2023-01-29"]);
    expect(columns[0]).toEqual({ periodEnd: "2026-01-25", short: "FY2026", long: "FY2026 (ended Jan 25, 2026)" });
  });

  it("drops unreadable dates and keeps two year ends in one calendar year apart", () => {
    const columns = fiscalColumns([m("revenue", "not a date", 1), m("revenue", "2025-12-31", 1), m("revenue", "2025-01-26", 1)]);
    expect(columns.map((c) => c.long)).toEqual(["FY2025 (ended Dec 31, 2025)", "FY2025 (ended Jan 26, 2025)"]);
  });

  it("is empty with no metrics", () => {
    expect(fiscalColumns([])).toEqual([]);
  });
});

describe("numberRows", () => {
  it("gives the four metrics in a fixed order, one cell per column", () => {
    const rows = numberRows(NVDA, fiscalColumns(NVDA));
    expect(rows.map((r) => r.metric)).toEqual(["revenue", "net_income", "rnd", "capex"]);
    expect(rows[0].cells.map((c) => c?.value)).toEqual([100, 99, 98, 97]);
    expect(rows[0].cells[0]).toEqual({ value: 100, unit: "USD", cite: "xbrl:1045810:revenue:2026-01-25" });
  });

  it("says the latest year held for a metric with nothing in these years, and marks one we hold nothing of", () => {
    const rows = numberRows(NVDA, fiscalColumns(NVDA));
    const capex = rows[3];
    expect(capex.cells).toEqual([null, null, null, null]);
    expect(capex.latestHeld).toBe("FY2012");
    expect(capex.absent).toBe(false);
    const asml = [m("revenue", "2025-12-31", 1, "EUR")];
    const rnd = numberRows(asml, fiscalColumns(asml)).find((r) => r.metric === "rnd");
    expect(rnd).toMatchObject({ absent: true, latestHeld: null, cells: [null] });
  });

  it("keeps a metric it does not know after the four, by its raw name, and never lets a hostile name reach a lookup", () => {
    const data = [m("revenue", "2025-12-31", 1), m("__proto__", "2025-12-31", 2), m("<img src=x onerror=1>", "2025-12-31", 3)];
    const rows = numberRows(data, fiscalColumns(data));
    expect(rows.map((r) => r.metric)).toEqual(["revenue", "net_income", "rnd", "capex", "__proto__", "<img src=x onerror=1>"]);
    expect(rows.slice(4).map((r) => r.known)).toEqual([false, false]);
  });
});

describe("glance", () => {
  it("takes the latest and prior figures from the columns, never from a metric's own second row", () => {
    const columns = fiscalColumns(NVDA);
    const figures = glance(NVDA, columns);
    expect(figures.map((f) => f.metric)).toEqual(["revenue", "net_income", "rnd"]);
    expect(figures[0].latest?.value).toBe(100);
    expect(figures[0].prior?.value).toBe(99);
  });

  it("has no figure for a metric missing from the latest year, and no prior for a gap year", () => {
    const data = [m("revenue", "2025-12-31", 1), m("revenue", "2023-12-31", 3), m("net_income", "2024-12-31", 9), m("net_income", "2025-12-31", 8)];
    const figures = glance(data, fiscalColumns(data));
    expect(figures[0].latest?.value).toBe(1);
    expect(figures[0].prior).toBeNull(); // 2024 is the prior column; revenue has no 2024 row
    expect(figures[2]).toMatchObject({ metric: "rnd", latest: null, prior: null, absent: true });
  });
});

const filing = (form: string, filingDate: string, status = "current"): Filing => ({
  accession_no: `x-${form}-${filingDate}`,
  form,
  filing_date: filingDate,
  is_current: status === "current",
  status,
  superseded_by: null,
});

describe("reportLine (RD19)", () => {
  const filer = { annualForm: "10-K", quarterly: true };

  it("names the annual report's fiscal year end from the figures it reported, and the latest 10-Q", () => {
    const line = reportLine([filing("10-Q", "2026-08-27"), filing("10-K", "2026-02-25"), filing("10-K", "2025-02-26", "superseded")], NVDA, filer);
    expect(line).toEqual({
      annual: { form: "10-K", yearEnded: "Jan 25, 2026", filed: "Feb 25, 2026" },
      quarterly: { form: "10-Q", filed: "Aug 27, 2026" },
      annualOnly: false,
    });
  });

  it("says filed only when no figure ended within 180 days before the annual report", () => {
    const stale = [m("revenue", "2025-01-26", 1)];
    expect(reportLine([filing("10-K", "2026-02-25")], stale, filer).annual).toEqual({ form: "10-K", yearEnded: null, filed: "Feb 25, 2026" });
  });

  it("does not take an amendment for the annual report, and shows both reports when the 10-K is newer than the 10-Q", () => {
    const line = reportLine([filing("10-K", "2026-07-29"), filing("10-K/A", "2026-08-01"), filing("10-Q", "2026-04-29")], [m("revenue", "2026-06-30", 1)], filer);
    expect(line.annual).toEqual({ form: "10-K", yearEnded: "Jun 30, 2026", filed: "Jul 29, 2026" });
    expect(line.quarterly).toEqual({ form: "10-Q", filed: "Apr 29, 2026" });
  });

  it("for a 20-F filer: the annual report and no quarterly line", () => {
    const line = reportLine([filing("20-F", "2026-04-16")], [m("revenue", "2025-12-31", 1, "TWD")], { annualForm: "20-F", quarterly: false });
    expect(line).toEqual({ annual: { form: "20-F", yearEnded: "Dec 31, 2025", filed: "Apr 16, 2026" }, quarterly: null, annualOnly: true });
  });

  it("has no annual line without a current annual report", () => {
    expect(reportLine([filing("10-K", "2025-02-26", "superseded")], [], filer).annual).toBeNull();
  });
});

const edge = (source: string, relation: string, target: string, quote: string | null, chunkIds: string[], quoteChunk: string | null): Edge => ({
  source,
  relation,
  target,
  status: "Active",
  quote,
  chunk_ids: chunkIds,
  quoteChunk,
});

const C = (n: number) => `0001045810-26-000021:I.1A:${String(n).padStart(4, "0")}`;

describe("relationshipsView (RD24)", () => {
  const edges = [
    edge("Nvidia", "COMPETES_WITH", "AMD", "q1", [C(6)], C(6)),
    edge("Nvidia", "DEPENDS_ON", "TSMC", "we depend on a foundry", [C(2), C(3)], C(2)),
    edge("Microsoft", "CUSTOMER_OF", "Nvidia", "a cloud provider buys", [C(4)], C(4)),
    edge("AMD", "COMPETES_WITH", "Nvidia", "q2", [C(7)], C(7)),
    edge("Nvidia", "DEPENDS_ON", "SK Hynix", "memory", [C(5)], null),
    edge("Nvidia", "SOMETHING_NEW", "Acme", "new kind", [], null),
  ];

  it("lists the relations that have a direction first: the other company, the quote and the quote's own chip", () => {
    const view = relationshipsView(edges, "Nvidia");
    expect(view.directional).toEqual([
      { other: ["TSMC"], quote: "we depend on a foundry", quoteChunk: C(2), chips: [C(3)] },
      { other: ["Microsoft"], quote: "a cloud provider buys", quoteChunk: C(4), chips: [] },
      { other: ["SK Hynix"], quote: "memory", quoteChunk: null, chips: [C(5)] },
      { other: ["Acme"], quote: "new kind", quoteChunk: null, chips: [] },
    ]);
  });

  it("lists each competitor once, its chips merged", () => {
    expect(relationshipsView(edges, "Nvidia").competitors).toEqual([{ name: "AMD", chips: [C(6), C(7)] }]);
  });

  it("names both sides when neither is the company, and never returns a relation name", () => {
    const view = relationshipsView([edge("TSMC", "SUPPLIES_TO", "Apple", "q", [], null)], "Nvidia");
    expect(view.directional[0].other).toEqual(["TSMC", "Apple"]);
    expect(JSON.stringify(view)).not.toMatch(/SUPPLIES_TO|DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH|SOMETHING_NEW/);
  });

  it("passes hostile names and quotes through as plain strings", () => {
    const view = relationshipsView([edge("Nvidia", "DEPENDS_ON", "<img src=x onerror=1>", "[x](javascript:alert(1))", [], null)], "Nvidia");
    expect(view.directional[0]).toMatchObject({ other: ["<img src=x onerror=1>"], quote: "[x](javascript:alert(1))" });
    const competitors = relationshipsView([edge("Nvidia", "COMPETES_WITH", "__proto__", null, [C(1)], C(1))], "Nvidia").competitors;
    expect(competitors).toEqual([{ name: "__proto__", chips: [C(1)] }]);
  });
});
