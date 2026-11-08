import { act, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Freshness } from "../Freshness";

// R26 (docs/specs/m5b-frontend.md): the Freshness page, from GET /api/freshness. Every field of the payload is rendered or
// listed as omitted (see FRESHNESS_FIELDS in Freshness.tsx and the I4 classification).

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
});

function mount() {
  return render(
    <MemoryRouter>
      <Freshness />
    </MemoryRouter>,
  );
}

describe("Freshness page", () => {
  it("keeps the h1 and says it is loading until the payload arrives", async () => {
    let release: (r: Response) => void = () => undefined;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => (release = resolve))));
    mount();
    expect(screen.getByRole("heading", { level: 1, name: "Freshness" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("loading\u2026");
    await act(async () => release(new Response(JSON.stringify(DISABLED), { status: 200 })));
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("renders the disabled payload: the neutral line, the status and a zero count, nothing invented", async () => {
    const fn = stubFetch(DISABLED);
    mount();
    expect(await screen.findByText("freshness check unavailable")).toBeInTheDocument();
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn.mock.calls[0]?.[0]).toBe("/api/freshness");
    const list = screen.getByRole("list", { name: "Freshness fields" });
    expect(within(list).getByText("disabled")).toBeInTheDocument();
    expect(within(list).getByText("0")).toBeInTheDocument();
    expect(screen.queryByText("Data as of")).toBeNull();
    expect(screen.queryByText("Next check")).toBeNull();
  });

  it("renders every shown field of a full payload", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: Date.parse("2026-09-29T12:00:00Z") });
    stubFetch(FULL);
    mount();
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText("Data as of 2026-09-24 \u00b7 checked 15 min ago \u00b7 2 filings pending")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Freshness fields" });
    expect(within(list).getByText("ok")).toBeInTheDocument();
    expect(within(list).getByText("2026-09-24")).toBeInTheDocument();
    expect(within(list).getByText(/2026-09-29T11:45:00Z/)).toBeInTheDocument();
    expect(within(list).getByText(/2026-09-28T01:00:00Z/)).toBeInTheDocument();
    expect(within(list).getByText(/2026-09-29T17:45:00Z/)).toBeInTheDocument();
    expect(within(list).getByText("in graph 10 \u00b7 live 12 \u00b7 new 2")).toBeInTheDocument();
    expect(within(list).getByText("TSM")).toBeInTheDocument();
    expect(within(list).getByText("NVDA 10-Q filed 2026-09-20 \u00b7 0001-26-1")).toBeInTheDocument();
    expect(within(list).getByText("AMD 10-K filed 2026-09-21 \u00b7 0002-26-2")).toBeInTheDocument();
  });

  it("shows the next check as ahead of now, not as just now", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: Date.parse("2026-09-29T12:00:00Z") });
    stubFetch({ ...FULL, next_check_at: "2026-09-29T12:20:00Z" });
    mount();
    await act(async () => vi.advanceTimersByTimeAsync(0));
    const list = screen.getByRole("list", { name: "Freshness fields" });
    expect(within(list).getByText("in 20 min (2026-09-29T12:20:00Z)")).toBeInTheDocument();
  });

  it("says the check failed for an error status, from last_error_at", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: Date.parse("2026-09-29T12:00:00Z") });
    stubFetch({ ...DISABLED, status: "error", last_error_at: "2026-09-29T11:45:00Z" });
    mount();
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText("freshness check failed 15 min ago")).toBeInTheDocument();
  });

  it("renders server text as text", async () => {
    stubFetch({ ...FULL, unresolved: ['<img src=x onerror="1">'], pending_filings: [{ ticker: "<b>x</b>", form: "<i>", filing_date: "d", accession_no: "a" }] });
    const { container } = mount();
    await screen.findByRole("list", { name: "Freshness fields" });
    expect(container.querySelector("img, b, i")).toBeNull();
    expect(container.querySelector("[onerror], [src], [style]")).toBeNull();
  });

  it("shows the server's detail in a local status on an error and no field list", async () => {
    stubFetch({ detail: "Too many requests from your address \u2014 please slow down." }, 429, "Too Many Requests");
    mount();
    expect(await screen.findByText("Too many requests from your address \u2014 please slow down.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Too many requests");
    expect(screen.queryByRole("list", { name: "Freshness fields" })).toBeNull();
  });

  it("aborts the request on unmount without logging", async () => {
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
    errors.mockRestore();
  });
});
