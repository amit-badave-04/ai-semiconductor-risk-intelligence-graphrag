import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Company } from "../Company";

// docs/specs/m5b-redesign.md RD19: under the company's name and ticker, its latest annual and quarterly report in words; for a
// 20-F filer (TSMC, ASML), that it files no quarterly 10-Q; the data date in words (UTC). Server text stays text.

const filing = (form: string, date: string, status = "current") => ({
  accession_no: `x-${form}-${date}`,
  form,
  filing_date: date,
  is_current: status === "current",
  status,
  superseded_by: null,
});

function dossier(ticker: string, name: string, filings: unknown[], periodEnd: string, unit = "USD") {
  return {
    company: { ticker, name, cik: 1 },
    data_as_of: "2026-09-30",
    filings,
    metrics: [{ metric: "revenue", value: 1, unit, period_start: null, period_end: periodEnd }],
    active_risks: [],
    edges: [],
    rules: [],
  };
}

function mount(ticker: string, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith("/dossier") ? new Response(JSON.stringify(body), { status: 200 }) : new Response(JSON.stringify({ pairs: [] }), { status: 200 }),
    ),
  );
  render(
    <MemoryRouter initialEntries={[`/company/${ticker}`]}>
      <Routes>
        <Route path="company/:ticker" element={<Company />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("the Company header (RD19)", () => {
  it("states the latest annual report with its fiscal year, and the latest quarterly report", async () => {
    mount("NVDA", dossier("NVDA", "Nvidia", [filing("10-Q", "2026-08-27"), filing("10-K", "2026-02-25"), filing("10-K", "2025-02-26", "superseded")], "2026-01-25"));
    expect(await screen.findByText("10-K for the fiscal year ended Jan 25, 2026, filed Feb 25, 2026")).toBeInTheDocument();
    expect(screen.getByText("10-Q filed Aug 27, 2026")).toBeInTheDocument();
    expect(screen.getByText("Data as of Sep 30, 2026")).toBeInTheDocument();
  });

  it.each([
    ["TSM", "TSMC", "2026-04-16", "TWD"],
    ["ASML", "ASML", "2026-02-25", "EUR"],
  ])("says %s files a 20-F and no quarterly 10-Q", async (ticker, name, filed, unit) => {
    mount(ticker, dossier(ticker, name, [filing("20-F", filed)], "2025-12-31", unit));
    expect(await screen.findByText("Files an annual report on Form 20-F; no quarterly 10-Q")).toBeInTheDocument();
    expect(screen.getByText(/^20-F for the fiscal year ended Dec 31, 2025, filed /)).toBeInTheDocument();
    expect(screen.queryByText(/10-Q filed/)).toBeNull();
  });

  it("keeps the h1 to the generated name and renders a hostile company name from the server as nothing more than text", async () => {
    const evil = '<img src=x onerror="alert(1)">';
    mount("NVDA", { ...dossier("NVDA", evil, [filing("10-K", "2026-02-25")], "2026-01-25") });
    await screen.findByText(/^10-K for the fiscal year/);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Nvidia (NVDA)");
    expect(document.querySelector("img")).toBeNull();
  });
});
