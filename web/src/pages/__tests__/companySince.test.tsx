import { readFileSync } from "node:fs";
import path from "node:path";
import { act, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { loadShellFreshness, resetFreshnessStore } from "@/lib/freshnessStore";
import { CompanySince } from "../CompanySince";

// docs/specs/m2a-answer-integrity.md P4: the Company page's "New filings since our data" and its one line about BIS rules.

const sample = JSON.parse(
  readFileSync(path.resolve(import.meta.dirname, "../../../../tests/data/freshness/since_data.sample.json"), "utf-8"),
) as Record<string, unknown> & { rules: unknown[] };
const BASE = { configured: true, enabled: true, status: "ok", checked_at: "2026-11-21T00:00:00Z", snapshot_as_of: "2026-09-24", pending_count: 0, pending_filings: [], unresolved: [] };

function fetchStub(body: unknown, status = 200) {
  const fn = vi.fn(async (_u: RequestInfo | URL) => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

async function arrive(since: unknown, extra: Record<string, unknown> = {}) {
  const fn = fetchStub({ ...BASE, ...extra, since_data: since });
  await act(async () => loadShellFreshness());
  return fn;
}

function show(ticker = "NVDA") {
  return render(
    <MemoryRouter>
      <CompanySince ticker={ticker} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  resetFreshnessStore();
});

describe("CompanySince", () => {
  it("shows nothing until the shell's freshness has arrived, and makes no request of its own", () => {
    const fn = fetchStub({});
    const { container } = show();
    expect(container).toBeEmptyDOMElement();
    expect(fn).not.toHaveBeenCalled();
  });

  it("shows nothing after a failed request or for a malformed since_data", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 500 })));
    await act(async () => loadShellFreshness());
    expect(show().container).toBeEmptyDOMElement();
    resetFreshnessStore();
    await arrive({ since: "never" });
    expect(show().container).toBeEmptyDOMElement();
  });

  it("lists only this company's filings, with how many of its filings are shown, and the BIS line", async () => {
    await arrive(sample);
    show();
    const block = screen.getByRole("region", { name: "New filings since our data" });
    expect(within(block).getByRole("heading", { level: 2 })).toHaveAttribute("id", "since-our-data");
    expect(within(block).getAllByText(/^Nvidia filed an? 8-K/)).toHaveLength(5);
    expect(within(block).queryByText(/TSMC/)).toBeNull();
    expect(within(block).getByText("showing 5 of 7")).toBeInTheDocument();
    expect(within(block).getAllByRole("link", { name: /^Open the filing on SEC\.gov\s?[0-9]{10}-[0-9]{2}-[0-9]{6}$/ })).toHaveLength(5); // a row with no primary document links to its EDGAR folder
  });

  it("links the whole BIS line to the Freshness page", async () => {
    await arrive(sample);
    show();
    const link = screen.getByRole("link", { name: "2 new BIS rules since our data \u2014 see Freshness" });
    expect(link).toHaveAttribute("href", "/freshness/");
    expect(link).not.toHaveAttribute("target");
  });

  it("writes 20+ for more rules than shown", async () => {
    await arrive({ ...sample, rules_more: true });
    show();
    expect(screen.getByRole("link", { name: "2+ new BIS rules since our data \u2014 see Freshness" })).toBeInTheDocument();
  });

  it("has no BIS line when there are no rules, and says nothing new when the company has no filings", async () => {
    await arrive({ ...sample, rules: [], filings: [], filing_totals: {} });
    show();
    expect(screen.getByText("Nothing new since our data")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("says Not checked yet when the monitor has no lists", async () => {
    await arrive(null);
    show();
    expect(screen.getByText("Not checked yet")).toBeInTheDocument();
  });

  it("shows no block at all, not the BIS line alone, when the filings part is left out", async () => {
    await arrive({ ...sample, filings: [], filing_totals: {}, dropped: { filings: 3, rules: 0 } });
    expect(show().container).toBeEmptyDOMElement();
  });

  it("shows no block when its filings were all dropped and there are no rules", async () => {
    await arrive({ ...sample, filings: [], filing_totals: {}, rules: [], dropped: { filings: 3, rules: 0 } });
    expect(show().container).toBeEmptyDOMElement();
  });

  it("shows no filings part for a ticker the server did not check, and never says Nothing new", async () => {
    await arrive({ ...sample, filings: [], filing_totals: {} }, { unresolved: ["AMD"] });
    const { container } = show("AMD");
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("Nothing new since our data")).toBeNull();
  });

  it("still says Nothing new for a checked company when another one was not checked", async () => {
    await arrive({ ...sample, filings: [], filing_totals: {}, rules: [] }, { unresolved: ["AMD"] });
    show("NVDA");
    expect(screen.getByText("Nothing new since our data")).toBeInTheDocument();
  });

  it("does not repeat the section's name on its list", async () => {
    await arrive(sample);
    show();
    const block = screen.getByRole("region", { name: "New filings since our data" });
    expect(within(block).queryByRole("list", { name: "New filings since our data" })).toBeNull();
    expect(within(block).getByRole("list")).not.toHaveAttribute("aria-labelledby");
  });
});
