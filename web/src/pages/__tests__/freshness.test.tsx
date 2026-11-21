import { readFileSync } from "node:fs";
import path from "node:path";
import { act, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { resetFreshnessStore } from "@/lib/freshnessStore";
import { Freshness } from "../Freshness";

// R26 (docs/specs/m5b-frontend.md) and docs/specs/m5b-redesign.md RD27: the Freshness page, from GET /api/freshness, in plain
// words through the deck. Every field of the payload is rendered or listed as omitted (see Freshness.tsx); dates are in words,
// in UTC; the summary is the header's own line (RD3); a failed request says so in the RD3 form with the server's detail.

const DISABLED = {
  checked_at: null, snapshot_as_of: null, last_error_at: null, next_check_at: null, pending_count: 0, pending_filings: [],
  federal_register: null, unresolved: [], duration_s: null, configured: false, enabled: false, status: "disabled",
};
const FULL = {
  configured: true, enabled: true, status: "ok", checked_at: "2026-09-29T11:45:00Z", snapshot_as_of: "2026-09-24",
  last_error_at: "2026-09-28T01:00:00Z", next_check_at: "2026-09-29T17:45:00Z", pending_count: 2,
  pending_filings: [
    { ticker: "NVDA", cik: 1, form: "10-Q", filing_date: "2026-09-20", accession_no: "0001-26-1", period_of_report: "2026-07-27" },
    { ticker: "AMD", cik: 2, form: "10-K", filing_date: "2026-09-21", accession_no: "0002-26-2", period_of_report: null },
  ],
  federal_register: { graph_count: 10, live_count: 12, new_since: 2 }, unresolved: ["TSM"], duration_s: 4.2,
};

function stubFetch(body: unknown, status = 200, statusText = "") {
  const fn = vi.fn(async (_u: RequestInfo | URL, _i?: RequestInit) => new Response(JSON.stringify(body), { status, statusText }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  resetFreshnessStore();
});

function mount() {
  return render(
    <MemoryRouter>
      <Freshness />
    </MemoryRouter>,
  );
}

const fields = () => screen.getByRole("list", { name: "Freshness details" });
const row = (label: string) => within(fields()).getByText(label).closest("li") as HTMLElement;
// the page's own status line (this harness renders no evidence panel, so it is the only status, R2 review C4)
const pageStatus = () => screen.getByRole("status");

describe("Freshness page (RD27)", () => {
  it("keeps the h1, says what the page is, and says it is loading until the payload arrives", async () => {
    let release: (r: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (release = resolve))));
    mount();
    expect(screen.getByRole("heading", { level: 1, name: "Freshness" })).toBeInTheDocument();
    expect(screen.getByText("When the filings and the government rules were last checked for anything newer than our data.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading…");
    await act(async () => release(new Response(JSON.stringify(DISABLED), { status: 200 })));
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("words a disabled check plainly and invents nothing", async () => {
    const fn = stubFetch(DISABLED);
    mount();
    expect(await screen.findByText("Data status unavailable")).toBeInTheDocument();
    expect(fn.mock.calls[0]?.[0]).toBe("/api/freshness");
    expect(within(row("Checks")).getByText("Off")).toBeInTheDocument();
    expect(within(row("Newer filings being added")).getByText("0")).toBeInTheDocument();
    expect(within(fields()).queryByText("Data as of")).toBeNull();
    expect(within(fields()).queryByText("Next check")).toBeNull();
  });

  it("states every shown field of a full payload in words, with dates in UTC", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: Date.parse("2026-09-29T12:00:00Z") });
    stubFetch(FULL);
    mount();
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText("Data as of Sep 24, 2026, 2 newer filings being added")).toBeInTheDocument();
    expect(within(row("Checks")).getByText("Working")).toBeInTheDocument();
    expect(row("Data as of")).toHaveTextContent("Sep 24, 2026");
    expect(row("Last checked")).toHaveTextContent("15 min ago (Sep 29, 2026)");
    expect(row("Last failed check")).toHaveTextContent("1 d ago (Sep 28, 2026)");
    expect(row("Next check")).toHaveTextContent("in 6 h (Sep 29, 2026)");
    expect(row("Government rules")).toHaveTextContent("10 in our data, 12 published, 2 not yet in our data");
    expect(row("Companies the check could not look up")).toHaveTextContent("TSM");
    const pending = within(fields()).getByRole("list", { name: "Newer filings" });
    expect(within(pending).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "NVDA 10-Q filed Sep 20, 2026 · 0001-26-1",
      "AMD 10-K filed Sep 21, 2026 · 0002-26-2",
    ]);
    // no ISO time stamp reaches the page
    expect(fields().textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T/);
  });

  it("reads several tickers the check could not look up apart, not run together (review of N3)", async () => {
    stubFetch({ ...FULL, unresolved: ["TSM", "ASML"] });
    mount();
    await act(async () => {});
    await screen.findByRole("list", { name: "Freshness details" });
    expect(row("Companies the check could not look up").textContent).toContain("TSM, ASML");
  });

  it("words each check status, and shows one it does not know as the server wrote it", async () => {
    for (const [status, words] of [["error", "The last check failed"], ["stale", "Overdue"], ["unconfigured", "Not set up"], ["paused_by_admin", "paused_by_admin"], ["constructor", "constructor"]]) {
      stubFetch({ ...DISABLED, status });
      const view = mount();
      expect(await within(view.container).findByText(words)).toBeInTheDocument();
      view.unmount();
      resetFreshnessStore();
    }
  });

  it("uses the header's own words for a failed check", async () => {
    stubFetch({ ...DISABLED, status: "error", last_error_at: "2026-09-29T11:45:00Z" });
    mount();
    expect(await screen.findByText("The check for newer filings failed")).toBeInTheDocument();
  });

  it("renders server text as text", async () => {
    stubFetch({ ...FULL, status: "<b>s</b>", unresolved: ['<img src=x onerror="1">'], pending_filings: [{ ticker: "<b>x</b>", form: "<i>", filing_date: "d", accession_no: "a" }] });
    const { container } = mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(container.querySelector("img, b, i")).toBeNull();
    expect(container.querySelector("[onerror], [src], [style]")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="1">');
  });

  it("says the status is unavailable, with the server's detail, on an error and shows no details", async () => {
    stubFetch({ detail: "Too many requests from your address — please slow down." }, 429, "Too Many Requests");
    mount();
    expect(await screen.findByText("Data status unavailable: Too many requests from your address — please slow down.")).toBeInTheDocument();
    expect(pageStatus()).toHaveTextContent("Data status unavailable");
    expect(screen.queryByRole("list", { name: "Freshness details" })).toBeNull();
  });

  it("aborts the request on unmount without logging and without a raw code", async () => {
    let signal: AbortSignal | undefined;
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn((_u: RequestInfo | URL, init?: RequestInit) => {
        signal = init?.signal ?? undefined;
        return new Promise<Response>((_res, rej) => signal?.addEventListener("abort", () => rej(new DOMException("a", "AbortError"))));
      }),
    );
    mount().unmount();
    await act(async () => undefined);
    expect(signal?.aborted).toBe(true);
    expect(errors).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain("request cancelled");
    errors.mockRestore();
  });
});

// docs/specs/m2a-answer-integrity.md P4: the "Since our data" panel under the details list.
describe("Since our data on the Freshness page", () => {
  const sample = JSON.parse(
    readFileSync(path.resolve(import.meta.dirname, "../../../../tests/data/freshness/since_data.sample.json"), "utf-8"),
  ) as Record<string, unknown> & { filings: Record<string, unknown>[]; rules: Record<string, unknown>[] };
  const panel = () => screen.getByRole("region", { name: "Since our data" });

  it("lists the filings and the rules under Since our data, after the details list", async () => {
    stubFetch({ ...FULL, since_data: sample });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(within(panel()).getByRole("heading", { level: 2, name: "Since our data" })).toBeInTheDocument();
    const filings = within(panel()).getByRole("list", { name: "New company filings since Sep 24, 2026" });
    expect(within(filings).getAllByRole("listitem")).toHaveLength(7); // six rows and "showing 5 of 7"
    expect(within(filings).getByText("TSMC filed a 6-K on Nov 20, 2026")).toBeInTheDocument();
    expect(
      within(filings).getByText("Nvidia filed an 8-K on Nov 19, 2026: Results of Operations and Financial Condition \u00b7 Financial Statements and Exhibits"),
    ).toBeInTheDocument();
    const rules = within(panel()).getByRole("list", { name: "New BIS rules since Sep 24, 2026" });
    expect(within(rules).getAllByRole("listitem")).toHaveLength(2);
    expect(within(rules).getByText("Nov 18, 2026 Export Administration Regulations: Advanced Computing Items")).toBeInTheDocument();
    expect(within(panel()).getByText("2 new BIS rules, not linked to companies yet")).toBeInTheDocument();
    expect(fields().compareDocumentPosition(panel()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("gives each link the safe href, a new tab, the rel, and a name that starts with its words and carries its row", async () => {
    stubFetch({ ...FULL, since_data: sample });
    mount();
    const first = (await within(await screen.findByRole("region", { name: "Since our data" })).findAllByRole("link", { name: /^Open the filing on SEC.gov/ }))[0];
    expect(first).toHaveAttribute("href", "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000020/nvda-8k-results.htm");
    expect(first).toHaveAttribute("target", "_blank");
    expect(first).toHaveAttribute("rel", "noopener noreferrer");
    const rule = within(panel()).getAllByRole("link", { name: /^Open the rule on FederalRegister\.gov/ })[0];
    expect(rule).toHaveAttribute("href", expect.stringMatching(/^https:\/\/www\.federalregister\.gov\//));
  });

  it("names each link by its visible words and the row's accession or document number (after a no-break space), and describes it by the row's sentence", async () => {
    stubFetch({ ...FULL, since_data: sample });
    mount();
    const links = await within(await screen.findByRole("region", { name: "Since our data" })).findAllByRole("link");
    const first = links[0];
    expect(first).toHaveAccessibleName(/^Open the filing on SEC\.gov\s?0001045810-26-000020$/) // jsdom's name algorithm trims the no-break space; Chrome keeps it (e2e)
    expect(first.textContent).toBe("Open the filing on SEC.gov 0001045810-26-000020");
    expect(first).toHaveAccessibleDescription(
      "Nvidia filed an 8-K on Nov 19, 2026: Results of Operations and Financial Condition · Financial Statements and Exhibits",
    );
    expect(first).not.toHaveAttribute("aria-labelledby");
    expect(first.querySelector(".sr-only")?.textContent).toBe(" 0001045810-26-000020");
    const rule = within(panel()).getAllByRole("link", { name: /^Open the rule on FederalRegister\.gov/ })[0];
    expect(rule).toHaveAccessibleName(/^Open the rule on FederalRegister\.gov\s?2026-23001$/)
    expect(rule.textContent).toBe("Open the rule on FederalRegister.gov 2026-23001");
    expect(rule).toHaveAccessibleDescription("Nov 18, 2026 Export Administration Regulations: Advanced Computing Items");
    const names = links.map((l) => l.textContent);
    expect(new Set(names).size).toBe(links.length); // two filings on one day still have different names
  });

  it("gives every element a unique id, even when two rules repeat a date and a title", async () => {
    const twice = [sample.rules[0], { ...sample.rules[0], document_number: "2026-23002" }];
    stubFetch({ ...FULL, since_data: { ...sample, rules: twice } });
    const { container } = mount();
    await screen.findByRole("region", { name: "Since our data" });
    const ids = [...container.querySelectorAll("[id]")].map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("shows one row for an accession number the server sent twice, with unique ids", async () => {
    stubFetch({ ...FULL, since_data: { ...sample, filings: [sample.filings[0], sample.filings[0]], filing_totals: {} } });
    const { container } = mount();
    await screen.findByRole("region", { name: "Since our data" });
    expect(within(panel()).getAllByRole("link", { name: /^Open the filing/ })).toHaveLength(1);
    const ids = [...container.querySelectorAll("[id]")].map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("writes the sentence as one text run, with no markup inside it", async () => {
    stubFetch({ ...FULL, since_data: sample });
    mount();
    const row = (await screen.findByText("TSMC filed a 6-K on Nov 20, 2026")).closest("li") as HTMLElement;
    expect(row.firstElementChild?.childNodes).toHaveLength(1);
  });

  it("says Not checked yet when the monitor has no lists", async () => {
    stubFetch({ ...DISABLED, since_data: null });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(within(panel()).getByText("Not checked yet")).toBeInTheDocument();
  });

  it("says Nothing new under a heading whose list is empty and lost no row", async () => {
    stubFetch({ ...FULL, unresolved: [], since_data: { ...sample, filings: [], filing_totals: {}, rules: [] } });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(within(panel()).getByRole("heading", { level: 3, name: "New company filings since Sep 24, 2026" })).toBeInTheDocument();
    expect(within(panel()).getAllByText("Nothing new since our data")).toHaveLength(2);
    expect(within(panel()).queryByText(/not linked to companies yet/)).toBeNull();
  });

  it("leaves out a subsection whose rows were all dropped, instead of saying nothing new", async () => {
    stubFetch({ ...FULL, since_data: { ...sample, filings: [], rules: [], dropped: { filings: 2, rules: 0 } } });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(within(panel()).queryByRole("heading", { level: 3, name: /New company filings/ })).toBeNull();
    expect(within(panel()).getAllByText("Nothing new since our data")).toHaveLength(1);
  });

  it("shows no panel for a malformed since_data", async () => {
    stubFetch({ ...FULL, since_data: { since: "never" } });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(screen.queryByRole("region", { name: "Since our data" })).toBeNull();
    expect(screen.queryByText("Not checked yet")).toBeNull();
  });

  it("writes 20+ and the singular, and shows how many of a company's filings are shown", async () => {
    const one = [sample.rules[0]];
    stubFetch({ ...FULL, since_data: { ...sample, rules: one, rules_more: true } });
    mount();
    expect(await screen.findByText("1+ new BIS rules, not linked to companies yet")).toBeInTheDocument();
    expect(within(panel()).getByText("showing 5 of 7")).toBeInTheDocument();
  });

  it("renders a title with markup as text", async () => {
    stubFetch({ ...FULL, since_data: { ...sample, rules: [{ ...sample.rules[0], title: '<img src=x onerror="1"> Entity' }] } });
    const { container } = mount();
    await screen.findByRole("region", { name: "Since our data" });
    expect(container.querySelector("img, [onerror]")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="1"> Entity');
  });

  it("does not say Nothing new for the filings when a company was not checked, but still shows the rules", async () => {
    stubFetch({ ...FULL, unresolved: ["ASML"], since_data: { ...sample, filings: [], filing_totals: {} } });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(within(panel()).queryByRole("heading", { level: 3, name: /New company filings/ })).toBeNull();
    expect(within(panel()).queryByText("Nothing new since our data")).toBeNull();
    expect(within(panel()).getByRole("list", { name: "New BIS rules since Sep 24, 2026" })).toBeInTheDocument();
  });

  it("still lists the filings when a company was not checked", async () => {
    stubFetch({ ...FULL, unresolved: ["ASML"], since_data: sample });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(within(panel()).getByRole("list", { name: "New company filings since Sep 24, 2026" })).toBeInTheDocument();
  });

  it("renders no section, not a bare heading, when both subsections are left out", async () => {
    stubFetch({ ...FULL, unresolved: ["ASML"], since_data: { ...sample, filings: [], filing_totals: {}, rules: [], dropped: { filings: 0, rules: 2 } } });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(screen.queryByRole("region", { name: "Since our data" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Since our data" })).toBeNull();
  });

  it("does not say Nothing new for the rules when none are left but some were dropped", async () => {
    stubFetch({ ...FULL, since_data: { ...sample, rules: [], dropped: { filings: 0, rules: 3 } } });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(within(panel()).queryByRole("heading", { level: 3, name: /New BIS rules/ })).toBeNull();
    expect(within(panel()).queryByText("Nothing new since our data")).toBeNull();
    expect(within(panel()).getByRole("list", { name: "New company filings since Sep 24, 2026" })).toBeInTheDocument();
  });

  it("keeps the last lists when the last check failed", async () => {
    stubFetch({ ...FULL, status: "error", since_data: sample });
    mount();
    await screen.findByRole("list", { name: "Freshness details" });
    expect(within(panel()).getAllByRole("listitem").length).toBeGreaterThan(5);
  });
});
