import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { EvidenceDrawerContext, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import type { Dossier, Metric } from "@/lib/company";
import { DossierBlocks } from "../CompanyBlocks";

// docs/specs/m5b-redesign.md RD20, RD22, SR3 and docs/decisions/001-r3-company-copy.md (1, 3): "At a glance" first (revenue, net
// income and R&D for the latest fiscal year with the prior year beneath, and the rules listed below), then "Key numbers": the
// company's four latest fiscal years as columns, readable metric names with a plain meaning, compact values in the reported
// currency with the exact value in the accessible name and a "Show exact values" switch, the fiscal-year sentence, and the
// "not in our data" wording. Every figure is a source stamp opening its XBRL fact; an id outside the grammar is text only.

const CIK = 1045810;
const m = (metric: string, periodEnd: string, value: number, unit = "USD"): Metric => ({
  metric,
  value,
  unit,
  period_start: null,
  period_end: periodEnd,
  cite: `xbrl:${CIK}:${metric}:${periodEnd}`,
});
const YEARS = ["2026-01-25", "2025-01-26", "2024-01-28", "2023-01-29"];
const NVDA_METRICS: Metric[] = [
  m("capex", "2012-01-29", 138e6),
  ...YEARS.flatMap((d, i) => [m("revenue", d, [215938e6, 130497e6, 60922e6, 26974e6][i]), m("net_income", d, [120067e6, 72880e6, 29760e6, 4368e6][i]), m("rnd", d, [18497e6, 12914e6, 8675e6, 7339e6][i])]),
];

function dossier(over: Partial<Dossier> = {}): Dossier {
  return {
    company: { ticker: "NVDA", name: "Nvidia", cik: CIK },
    dataAsOf: "2026-09-30",
    filings: [],
    metrics: NVDA_METRICS,
    activeRisks: [],
    edges: [],
    rules: [],
    ...over,
  };
}

function show(d: Dossier) {
  const open = vi.fn<EvidenceDrawerApi["open"]>();
  const api: EvidenceDrawerApi = { openId: null, openKey: null, open, close: vi.fn(), dropDocEvidence: vi.fn(), clearAll: vi.fn() };
  const view = render(
    <EvidenceDrawerContext.Provider value={api}>
      <MemoryRouter>
        <DossierBlocks dossier={d} />
      </MemoryRouter>
    </EvidenceDrawerContext.Provider>,
  );
  return { ...view, open };
}

const numbers = () => screen.getByRole("region", { name: "Key numbers" });
const glanceRegion = () => screen.getByRole("region", { name: "At a glance" });

describe("Key numbers (RD22)", () => {
  it("is a table named by its heading: the fiscal years newest first as column headers, the metrics as row headers", () => {
    show(dossier());
    const table = within(numbers()).getByRole("table", { name: "Key numbers" });
    const columns = within(table).getAllByRole("columnheader").slice(1).map((h) => h.textContent);
    expect(columns).toEqual(["FY2026 (ended Jan 25, 2026)", "FY2025 (ended Jan 26, 2025)", "FY2024 (ended Jan 28, 2024)", "FY2023 (ended Jan 29, 2023)"]);
    for (const h of within(table).getAllByRole("columnheader")) expect(h).toHaveAttribute("scope", "col");
    const rows = within(table).getAllByRole("rowheader");
    expect(rows.map((h) => h.textContent)).toEqual([
      // N3 (the owner's NVDA pass): the name ends in a no-break space, so it is not read run together with its meaning
      "Revenue\u00a0Money from sales",
      "Net income\u00a0Profit after all costs and taxes",
      "Research and development\u00a0Money spent on developing new products",
      "Capital spending\u00a0Money spent on buildings and equipment",
    ]);
    for (const h of rows) expect(h).toHaveAttribute("scope", "row");
    expect(numbers()).toHaveTextContent("Fiscal years are the company's own. Each column shows the date its year ended.");
  });

  it("scrolls inside its own named, focusable region", () => {
    show(dossier());
    const scroller = within(numbers()).getByRole("region", { name: "Key numbers table" });
    expect(scroller).toHaveAttribute("tabindex", "0");
    expect(scroller.querySelector("table")).not.toBeNull();
  });

  it("shows compact values whose stamp names start with the visible text and hold the exact value", () => {
    show(dossier());
    const stamp = within(numbers()).getByRole("button", { name: "$215.9B, Revenue, FY2026 (ended Jan 25, 2026): $215,938,000,000. Open source" });
    expect(stamp).toHaveTextContent("$215.9B");
    expect(stamp).toHaveAttribute("data-id", "xbrl:1045810:revenue:2026-01-25");
    expect(stamp).toHaveAttribute("data-chip-key");
  });

  it("switches the table, and only the table, to exact values; the stamps and their order stay", async () => {
    const user = userEvent.setup();
    show(dossier());
    const ids = () => within(numbers()).getAllByRole("button").map((b) => b.getAttribute("data-id"));
    const before = ids();
    const switchControl = within(numbers()).getByRole("switch", { name: "Show exact values" });
    expect(switchControl).not.toBeChecked();
    await user.click(switchControl);
    expect(switchControl).toBeChecked();
    const stamp = within(numbers()).getByRole("button", { name: "$215,938,000,000, Revenue, FY2026 (ended Jan 25, 2026). Open source" });
    expect(stamp).toHaveTextContent("$215,938,000,000");
    expect(ids()).toEqual(before);
    expect(glanceRegion()).toHaveTextContent("$215.9B");
  });

  it("says the latest year held for a metric with nothing in these years, in one cell and with no stamp", () => {
    show(dossier());
    const capex = within(numbers()).getByRole("rowheader", { name: /^Capital spending/ }).closest("tr") as HTMLElement;
    const cells = within(capex).getAllByRole("cell");
    expect(cells).toHaveLength(1);
    expect(cells[0]).toHaveAttribute("colspan", "4");
    expect(cells[0]).toHaveTextContent("Not in our data for these years. The latest we hold is FY2012.");
    expect(within(capex).queryByRole("button")).toBeNull();
  });

  it("says a metric we hold nothing of is not in our data (decision 1)", () => {
    show(dossier({ metrics: NVDA_METRICS.filter((r) => r.metric !== "rnd") }));
    const rnd = within(numbers()).getByRole("rowheader", { name: /^Research and development/ }).closest("tr") as HTMLElement;
    expect(within(rnd).getByRole("cell")).toHaveTextContent(/^Not in our data for these years\.$/);
  });

  it("shows each company's own currency and never a default dollar sign", () => {
    show(dossier({ metrics: [m("revenue", "2025-12-31", 3809054e6, "TWD")] }));
    expect(numbers()).toHaveTextContent("NT$3.8T");
    show(dossier({ metrics: [m("revenue", "2025-12-31", 32667e6, "EUR")] }));
    expect(screen.getAllByRole("region", { name: "Key numbers" })[1]).toHaveTextContent("€32.7B");
    expect(screen.getAllByRole("region", { name: "Key numbers" })[1].textContent).not.toContain("$");
    show(dossier({ metrics: [m("revenue", "2025-12-31", 1.2e9, "XYZ")] }));
    expect(screen.getAllByRole("region", { name: "Key numbers" })[2]).toHaveTextContent("1.2B XYZ");
  });

  it("keeps a figure whose id fails the grammar as text, with no button and no request (SR3)", () => {
    const bad: Metric[] = [
      { metric: "revenue", value: 5, unit: "USD", period_start: null, period_end: "2026-01-25", cite: null },
      { metric: "net_income", value: 6, unit: "USD", period_start: null, period_end: "2026-01-25", cite: "xbrl:../x" },
    ];
    const { open } = show(dossier({ metrics: bad }));
    expect(within(numbers()).queryAllByRole("button")).toHaveLength(0);
    expect(within(glanceRegion()).queryAllByRole("button")).toHaveLength(0);
    expect(numbers()).toHaveTextContent("$5");
    expect(open).not.toHaveBeenCalled();
  });
});

describe("At a glance (RD20)", () => {
  it("comes first, with revenue, net income and R&D for the latest year and the prior year beneath, each a stamp", () => {
    show(dossier());
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings[0]).toBe("At a glance");
    const stamps = within(glanceRegion()).getAllByRole("button");
    expect(stamps.map((b) => b.getAttribute("data-id"))).toEqual([
      "xbrl:1045810:revenue:2026-01-25",
      "xbrl:1045810:revenue:2025-01-26",
      "xbrl:1045810:net_income:2026-01-25",
      "xbrl:1045810:net_income:2025-01-26",
      "xbrl:1045810:rnd:2026-01-25",
      "xbrl:1045810:rnd:2025-01-26",
    ]);
    expect(stamps[0]).toHaveAccessibleName("$215.9B, Revenue, FY2026 (ended Jan 25, 2026): $215,938,000,000. Open source");
    expect(stamps[1]).toHaveAccessibleName("$130.5B, Revenue, FY2025 (ended Jan 26, 2025): $130,497,000,000. Open source");
  });

  it("gives a figure shown in both blocks two distinct keys, and opens the clicked one", () => {
    const { open } = show(dossier());
    const keys = Array.from(document.querySelectorAll("button[data-chip-key]"), (b) => b.getAttribute("data-chip-key"));
    expect(new Set(keys).size).toBe(keys.length);
    const stamp = within(glanceRegion()).getAllByRole("button")[0];
    fireEvent.click(stamp);
    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith("xbrl:1045810:revenue:2026-01-25", stamp, "company", undefined);
  });

  it("counts the rules the page lists as a link to them (decision 3)", () => {
    const rule = { source: "Nvidia", relation: "AFFECTED_BY", target: "A rule", status: null, quote: null, chunk_ids: [], quoteChunk: null, rule_id: "2026-01234", date: null, kind: null, cite: "fr:2026-01234" };
    show(dossier({ rules: Array.from({ length: 8 }, (_, i) => ({ ...rule, rule_id: `2026-0123${i}`, cite: `fr:2026-0123${i}` })) }));
    const link = within(glanceRegion()).getByRole("link", { name: "8 government rules listed below" });
    expect(link).toHaveAttribute("href", "#government-rules");
    fireEvent.click(link);
    expect(screen.getByRole("heading", { level: 2, name: "Government rules" })).toHaveFocus();
  });

  it("shows the latest figures' fiscal year as visible text, as it does the prior year's (R3 review A1)", () => {
    show(dossier());
    expect(glanceRegion()).toHaveTextContent("FY2026 (ended Jan 25, 2026)");
    expect(within(glanceRegion()).getAllByRole("listitem")[0]).toHaveTextContent(/FY2025: \$130\.5B/);
  });

  it("makes no stamp of a figure that has no value (R3 review A6)", () => {
    const blank: Metric[] = [
      { ...m("revenue", "2026-01-25", 1), value: null },
      { ...m("net_income", "2026-01-25", 1), value: "n/a" },
      m("rnd", "2026-01-25", 5e9),
    ];
    show(dossier({ metrics: blank }));
    const ids = [...document.querySelectorAll("button[data-id]")].map((b) => b.getAttribute("data-id"));
    expect(ids.every((id) => id?.includes(":rnd:"))).toBe(true);
    for (const b of document.querySelectorAll("button[data-id]")) expect(b.getAttribute("aria-label")?.startsWith(",")).toBe(false);
  });

  it("says a figure we hold nothing of is not in our data, and shows no prior for it", () => {
    show(dossier({ metrics: NVDA_METRICS.filter((r) => r.metric !== "rnd") }));
    expect(glanceRegion()).toHaveTextContent("Not in our data for these years.");
    expect(within(glanceRegion()).getAllByRole("button")).toHaveLength(4);
  });
});
