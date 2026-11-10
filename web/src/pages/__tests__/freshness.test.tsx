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
