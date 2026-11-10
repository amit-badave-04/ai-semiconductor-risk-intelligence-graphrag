import { act, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { stampLabel } from "@/components/__tests__/stampLabel";
import { FILERS } from "@/lib/pages.generated";
import { Company } from "../Company";
import * as copy from "../Company.copy";

// built from pieces so no script URL literal sits in the source
const SCRIPT_URL = ["java", "script:alert(1)"].join("");

// R25 (docs/specs/m5b-frontend.md): the company page. A GET (not a paid call), text-only rendering of everything the server
// sends, a switcher of the 13 filers, and no request at all for a ticker that is not one of them.

const CHUNK = "0001045810-26-000021:I.1:0320";
const RULE_LABEL = "keyword matches to external Federal Register rules";

const DOSSIER = {
  company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 },
  data_as_of: "2026-09-30",
  filings: [
    { accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25", is_current: true, status: "current", superseded_by: null },
  ],
  metrics: [{ cik: 1045810, metric: "revenue", value: 130497000000, unit: "USD", period_start: "2025-01-27", period_end: "2026-01-25" }],
  active_risks: [{ risk_id: "r1", summary: "Supply of advanced packaging is concentrated.", category: "supply", last_evidenced_at: "2026-02-25" }],
  edges: [{ source: "Nvidia", relation: "DEPENDS_ON", target: "TSMC", status: "Active", quote: "We depend on TSMC.", chunk_ids: [CHUNK] }],
  rules: [
    { source: "Nvidia", relation: "AFFECTED_BY", target: "Export rule title", status: "Active", quote: null, chunk_ids: null, rule_id: "fr:2026-01234", date: "2026-03-01", url: "https://evil.example/x", kind: "Rule" },
  ],
};

function json(body: unknown, status = 200, statusText = ""): Response {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "content-type": "application/json" } });
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => handler(String(input), init));
  vi.stubGlobal("fetch", fn);
  return fn;
}

function mount(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="company/:ticker" element={<Company />} />
        <Route path="*" element={<p>elsewhere</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Company: an untrusted :ticker", () => {
  it.each(["..%2F..%2Fapi%2Fadmin%2Fstate", "XXXX", "nvda", "constructor"])(
    "/company/%s shows the not-found view and makes no request",
    async (ticker) => {
      const fn = stubFetch(() => json({}));
      mount(`/company/${ticker}`);
      expect(await screen.findByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
      expect(fn).not.toHaveBeenCalled();
    },
  );
});

describe("Company: the dossier", () => {
  it("keeps the h1, says it is loading, then renders every block", async () => {
    let release: (r: Response) => void = () => undefined;
    const fn = stubFetch(() => new Promise<Response>((resolve) => (release = resolve)));
    mount("/company/NVDA");
    expect(screen.getByRole("heading", { level: 1, name: "Nvidia (NVDA)" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("loading\u2026");
    expect(fn.mock.calls[0]?.[0]).toBe("/api/company/NVDA/dossier");

    await act(async () => release(json(DOSSIER)));
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.getByText("Data as of Sep 30, 2026")).toBeInTheDocument(); // RD19: in the header, in words
    const filings = screen.getByRole("region", { name: "Filings" });
    expect(within(filings).getByText("10-K")).toBeInTheDocument();
    expect(within(filings).getByText("0001045810-26-000021")).toBeInTheDocument();
    const metrics = screen.getByRole("region", { name: "Key numbers" });
    expect(within(metrics).getByText("revenue")).toBeInTheDocument();
    expect(within(metrics).getByRole("button", { name: stampLabel("xbrl:1045810:revenue:2026-01-25") })).toHaveAttribute("data-id", "xbrl:1045810:revenue:2026-01-25");
    const risks = screen.getByRole("region", { name: "Active risks" });
    expect(within(risks).getByText("Supply of advanced packaging is concentrated.")).toBeInTheDocument();
    const edges = screen.getByRole("region", { name: "Relationships" });
    expect(within(edges).getByText("We depend on TSMC.")).toBeInTheDocument();
    expect(within(edges).getByRole("button", { name: stampLabel(CHUNK) })).toHaveAttribute("data-id", CHUNK);
  });

  it("labels the rules as keyword matches to external Federal Register rules", async () => {
    stubFetch(() => json(DOSSIER));
    mount("/company/NVDA");
    const rules = await screen.findByRole("region", { name: "Government rules" });
    expect(rules).toHaveTextContent(RULE_LABEL);
    expect(within(rules).getByText("Export rule title")).toBeInTheDocument();
    expect(within(rules).getByRole("button", { name: stampLabel("fr:2026-01234") })).toHaveAttribute("data-id", "fr:2026-01234");
  });

  it("says none for an empty block", async () => {
    stubFetch(() => json({ ...DOSSIER, filings: [], metrics: [], active_risks: [], edges: [], rules: [] }));
    mount("/company/NVDA");
    const filings = await screen.findByRole("region", { name: "Filings" });
    expect(filings).toHaveTextContent("none");
    expect(screen.getByRole("region", { name: "Government rules" })).toHaveTextContent("none");
  });

  it("renders server text as text: no element, no handler, no link comes from the data", async () => {
    const hostile = '<img src=x onerror="window.__x=1"><a href=SCRIPT_URL>go</a>';
    stubFetch(() =>
      json({
        ...DOSSIER,
        active_risks: [{ risk_id: "r1", summary: hostile, category: hostile, last_evidenced_at: hostile }],
        edges: [{ source: hostile, relation: hostile, target: hostile, status: hostile, quote: hostile, chunk_ids: [hostile, CHUNK] }],
        rules: [{ ...DOSSIER.rules[0], target: hostile, rule_id: hostile, url: SCRIPT_URL }],
      }),
    );
    const { container } = mount("/company/NVDA");
    await screen.findByRole("region", { name: "Active risks" });
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    expect(container.querySelector("[src]")).toBeNull();
    expect(container.querySelector("[style]")).toBeNull();
    const hrefs = [...container.querySelectorAll("a")].map((a) => a.getAttribute("href"));
    // the switcher's company pages and RD21's five fixed section links: constants, never from data
    const SECTIONS = ["#key-numbers", "#government-rules", "#relationships", "#filings", "#risk-changes"];
    expect(hrefs.filter((h) => !/^\/company\/[A-Z]+\/$/.test(h ?? "") && !SECTIONS.includes(h ?? ""))).toEqual([]);
    expect(container.querySelector('[href*="evil"]')).toBeNull();
    expect(container.textContent).not.toContain("evil.example");
    // the metric's xbrl id and the one valid chunk id are chips; the hostile rule_id and chunk id are text only, never a chip
    expect([...container.querySelectorAll("button[data-id]")].map((b) => b.getAttribute("data-id"))).toEqual(["xbrl:1045810:revenue:2026-01-25", CHUNK]);
  });

  it("a 404 from the dossier renders the not-found view and moves focus to its h1", async () => {
    let release: (r: Response) => void = () => undefined;
    stubFetch(() => new Promise<Response>((resolve) => (release = resolve)));
    mount("/company/AMD");
    const old = screen.getByRole("heading", { level: 1, name: "AMD (AMD)" });
    old.focus();
    expect(old).toHaveFocus();
    await act(async () => release(json({ detail: "unknown ticker" }, 404, "Not Found")));
    const heading = screen.getByRole("heading", { level: 1, name: "Page not found" });
    expect(heading).toHaveFocus();
  });

  it("does not take focus on a 404 when focus is somewhere the user put it", async () => {
    let release: (r: Response) => void = () => undefined;
    stubFetch(() => new Promise<Response>((resolve) => (release = resolve)));
    render(
      <MemoryRouter initialEntries={["/company/AMD"]}>
        <button type="button">elsewhere on the page</button>
        <Routes>
          <Route path="company/:ticker" element={<Company />} />
        </Routes>
      </MemoryRouter>,
    );
    const other = screen.getByRole("button", { name: "elsewhere on the page" });
    other.focus();
    await act(async () => release(json({ detail: "unknown ticker" }, 404, "Not Found")));
    expect(other).toHaveFocus();
  });

  it("shows the server's detail in a local status next to the heading on an error", async () => {
    stubFetch(() => json({ detail: "Too many requests from your address \u2014 please slow down." }, 429, "Too Many Requests"));
    mount("/company/NVDA");
    expect(await screen.findByText("Too many requests from your address \u2014 please slow down.")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Too many requests");
    expect(screen.getByRole("heading", { level: 1, name: "Nvidia (NVDA)" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Filings" })).toBeNull();
  });

  it("says the company data could not be loaded for a 200 it cannot read (owner 2026-11-08), not the status text", async () => {
    expect(copy.MSG_COMPANY_UNREADABLE).toBe("could not load the company data");
    stubFetch(() => new Response(JSON.stringify({ filings: [] }), { status: 200, statusText: "OK" }));
    const first = mount("/company/NVDA");
    expect(await screen.findByText("could not load the company data")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("could not load the company data");
    expect(screen.getByRole("status")).not.toHaveTextContent("OK");
    expect(screen.queryByRole("region", { name: "Filings" })).toBeNull();
    first.unmount();
    stubFetch(() => new Response("<html>not json</html>", { status: 200, statusText: "OK" }));
    mount("/company/NVDA");
    expect(await screen.findByText("could not load the company data")).toBeInTheDocument();
  });

  it("aborts the request on unmount and renders nothing from it", async () => {
    let signal: AbortSignal | undefined;
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    stubFetch((_url, init) => {
      signal = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    });
    const view = mount("/company/NVDA");
    view.unmount();
    await act(async () => undefined);
    expect(signal?.aborted).toBe(true);
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("never fills the page with a stale response when the ticker changed", async () => {
    const pending: Record<string, (r: Response) => void> = {};
    stubFetch(
      (url) =>
        new Promise<Response>((resolve) => {
          pending[url.split("/")[3] ?? ""] = resolve;
        }),
    );
    mount("/company/NVDA");
    await act(async () => screen.getByRole("link", { name: "AMD (AMD)" }).click());
    expect(screen.getByRole("heading", { level: 1, name: "AMD (AMD)" })).toBeInTheDocument();
    await act(async () => pending.AMD?.(json({ ...DOSSIER, company: { ticker: "AMD", name: "AMD", cik: 2488 }, active_risks: [] })));
    await act(async () => pending.NVDA?.(json(DOSSIER)));
    expect(screen.queryByText("Supply of advanced packaging is concentrated.")).toBeNull();
  });
});

describe("Company: the switcher", () => {
  it("lists the 13 filers as links in a navigation named Companies and marks the current one", async () => {
    stubFetch(() => json(DOSSIER));
    mount("/company/NVDA");
    const nav = screen.getByRole("navigation", { name: "Companies" });
    const links = within(nav).getAllByRole("link");
    expect(links).toHaveLength(FILERS.length);
    expect(FILERS).toHaveLength(13);
    expect(within(nav).getByRole("link", { name: "Nvidia (NVDA)" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "AMD (AMD)" })).not.toHaveAttribute("aria-current");
    expect(within(nav).getByRole("link", { name: "AMD (AMD)" })).toHaveAttribute("href", "/company/AMD/");
    await screen.findByRole("region", { name: "Filings" });
  });
});
