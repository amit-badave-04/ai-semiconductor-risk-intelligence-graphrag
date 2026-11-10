import { act, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { stampLabel } from "@/components/__tests__/stampLabel";
import { Company } from "../Company";
import * as copy from "../Company.copy";

// R25 (owner 2026-11-08: yes): the Company page also shows the recent risk changes of GET /api/company/{ticker}/risk-changes.
// The labels are the ones the server's own context layout and the page's reliability note pin (tests/test_answerer_blocks.py,
// test_answerer_context.py, test_static_ui.py); everything from the server is a text node (S2).

const CHUNK = "0001045810-26-000021:I.1:0320";
const HOSTILE = '<img src=x onerror="window.__x=1"><a href="https://evil.example/x">go</a>';
const DOSSIER = {
  company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 },
  data_as_of: "2026-09-30",
  filings: [],
  metrics: [],
  active_risks: [],
  edges: [],
  rules: [],
};
const OLD = { accession_no: "0001045810-25-000023", form: "10-K", filing_date: "2025-02-26" };
const NEW = { accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25" };
const CHANGES = {
  company: DOSSIER.company,
  pairs: [
    {
      older: OLD,
      newer: NEW,
      compared: true,
      not_compared_reason: null,
      items: [
        { kind: "new", headline: "Sovereign AI demand", passages: [] },
        { kind: "dropped", headline: "Crypto mining demand", passages: [] },
        { kind: "unsettled", headline: "Legacy gaming channel", passages: [] },
        {
          kind: "changed",
          headline: "Export licensing",
          passages: [
            { quote: "We may need a license.", chunk_id: CHUNK, side: "older" },
            { quote: "We need a license.", chunk_id: "not a chunk id", side: "newer" },
          ],
        },
      ],
    },
    { older: OLD, newer: NEW, compared: false, not_compared_reason: "low_text_yield", items: [] },
  ],
};

function json(body: unknown, status = 200, statusText = ""): Response {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "content-type": "application/json" } });
}

function route(changes: () => Response | Promise<Response>, dossier: () => Response | Promise<Response> = () => json(DOSSIER)) {
  const fn = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    return url.endsWith("/risk-changes") ? changes() : dossier();
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function urls(fn: ReturnType<typeof route>): string[] {
  return fn.mock.calls.map(([input]) => String(input));
}

function mount(path = "/company/NVDA") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="company/:ticker" element={<Company />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Company: the risk changes block", () => {
  it("asks the risk-changes route once, after the dossier is ok, and not before", async () => {
    let release: (r: Response) => void = () => undefined;
    const fn = route(
      () => json(CHANGES),
      () => new Promise<Response>((resolve) => (release = resolve)),
    );
    mount();
    expect(urls(fn)).toEqual(["/api/company/NVDA/dossier"]);
    await act(async () => release(json(DOSSIER)));
    expect(urls(fn)).toEqual(["/api/company/NVDA/dossier", "/api/company/NVDA/risk-changes"]);
  });

  it("never asks for the risk changes of a company whose dossier is a 404 or an error", async () => {
    const fn = route(
      () => json(CHANGES),
      () => json({ detail: "unknown ticker" }, 404, "Not Found"),
    );
    mount("/company/AMD");
    await act(async () => undefined);
    expect(urls(fn)).toEqual(["/api/company/AMD/dossier"]);
    const again = route(
      () => json(CHANGES),
      () => json({ detail: "slow down" }, 429),
    );
    mount();
    await act(async () => undefined);
    expect(urls(again)).toEqual(["/api/company/NVDA/dossier"]);
  });

  it("says loading while the report is pending", async () => {
    route(() => new Promise<Response>(() => undefined));
    mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    expect(block).toHaveTextContent("loading\u2026");
  });

  it("shows each pair, each kind under its pinned label, and each headline", async () => {
    route(() => json(CHANGES));
    mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    expect(await within(block).findByRole("heading", { level: 3, name: "10-K 2025-02-26 \u2192 10-K 2026-02-25" })).toBeInTheDocument();
    for (const label of [copy.KIND_NEW, copy.KIND_DROPPED, copy.KIND_UNSETTLED, copy.KIND_CHANGED]) {
      expect(within(block).getByText(label)).toBeInTheDocument();
    }
    for (const headline of ["Sovereign AI demand", "Crypto mining demand", "Legacy gaming channel", "Export licensing"]) {
      expect(within(block).getByText(headline)).toBeInTheDocument();
    }
    expect(block).not.toHaveTextContent(/\bremoved\b/i);
  });

  it("uses the pinned wording of the server for the four kinds", () => {
    expect(copy.KIND_NEW).toBe("No matching risk factor found in the earlier filing");
    expect(copy.KIND_DROPPED).toBe("No longer appears as a separate risk factor");
    expect(copy.KIND_UNSETTLED).toBe("Not matched");
    expect(copy.KIND_CHANGED).toBe("Reworded");
    expect(copy.RISK_CHANGES_TITLE).toBe("Compared risk disclosures");
  });

  it("quotes passages, and only a chunk id of the grammar becomes an evidence chip (R25)", async () => {
    route(() => json(CHANGES));
    mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    await within(block).findByText("Export licensing");
    expect(block).toHaveTextContent("10-K 2025-02-26: \u201cWe may need a license.\u201d");
    expect(block).toHaveTextContent("10-K 2026-02-25: \u201cWe need a license.\u201d");
    const cites = [...block.querySelectorAll("button[data-id]")];
    expect(cites.map((c) => c.getAttribute("data-id"))).toEqual([CHUNK]);
    expect(block.querySelectorAll("[data-cite]")).toHaveLength(0);
    expect(block).not.toHaveTextContent("not a chunk id");
  });

  it("tells the older and the newer passage apart, each by its own filing label (owner 2026-11-08)", async () => {
    route(() => json(CHANGES));
    mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    await within(block).findByText("Export licensing");
    const older = block.querySelector("li p:nth-of-type(1)");
    const newer = block.querySelector("li p:nth-of-type(2)");
    expect(older?.textContent).toBe("10-K 2025-02-26: \u201cWe may need a license.\u201d " + stampLabel(CHUNK));
    expect(newer?.textContent).toBe("10-K 2026-02-25: \u201cWe need a license.\u201d");
    expect(older?.textContent).not.toBe(newer?.textContent);
  });

  it("falls back to the words of the 'new' label when a filing has no label, and prefixes nothing without a side", async () => {
    const bare = { accession_no: "x", form: null, filing_date: null };
    const pairs = [
      {
        older: bare,
        newer: bare,
        compared: true,
        items: [
          {
            kind: "changed",
            headline: "H",
            passages: [
              { quote: "old text", side: "older" },
              { quote: "new text", side: "newer" },
              { quote: "loose text" },
            ],
          },
        ],
      },
    ];
    route(() => json({ company: DOSSIER.company, pairs }));
    mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    await within(block).findByText("H");
    const text = [...block.querySelectorAll("li p")].map((p) => p.textContent);
    expect(text).toEqual(["earlier filing: \u201cold text\u201d", "later filing: \u201cnew text\u201d", "\u201cloose text\u201d"]);
  });

  it("says a pair could not be compared, with the server's reason, in the legacy template", async () => {
    route(() => json(CHANGES));
    mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    await within(block).findByText("Export licensing");
    expect(block).toHaveTextContent("10-K 2025-02-26 \u2192 10-K 2026-02-25: comparison not available (low_text_yield).");
  });

  it("says none for a company with no pairs", async () => {
    route(() => json({ company: DOSSIER.company, pairs: [] }));
    mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    await within(block).findByText("none");
  });

  it("shows the server's refusal in the block and keeps the dossier", async () => {
    route(() => json({ detail: "Too many requests from your address \u2014 please slow down." }, 429, "Too Many Requests"));
    mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    await within(block).findByText("Too many requests from your address \u2014 please slow down.");
    expect(screen.getByRole("region", { name: "Filings" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("keeps the status-text fallback for a 200 that is no report", async () => {
    route(() => new Response("{}", { status: 200, statusText: "OK" }));
    mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    await within(block).findByText("OK");
  });

  it("renders hostile server text as text: no element, link, handler or cite comes from it (S2)", async () => {
    const hostile = {
      company: DOSSIER.company,
      pairs: [
        {
          older: { ...OLD, form: HOSTILE, filing_date: HOSTILE },
          newer: NEW,
          compared: true,
          items: [{ kind: "changed", headline: HOSTILE, passages: [{ quote: HOSTILE, chunk_id: HOSTILE, side: "older" }] }],
        },
        { older: OLD, newer: NEW, compared: false, not_compared_reason: HOSTILE, items: [] },
      ],
    };
    route(() => json(hostile));
    const { container } = mount();
    const block = await screen.findByRole("region", { name: copy.RISK_CHANGES_TITLE });
    await within(block).findAllByText(new RegExp("<img src=x"));
    expect(container.querySelector("img, [onerror], [src], [style]")).toBeNull();
    expect(block.querySelector("a")).toBeNull();
    expect(block.querySelectorAll("[data-cite], button[data-id]")).toHaveLength(0);
  });

  it("aborts the risk-changes request on unmount and renders nothing from it", async () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const fn = route(() => new Promise<Response>(() => undefined));
    const view = mount();
    await act(async () => undefined);
    const signal = fn.mock.calls.find(([u]) => String(u).endsWith("/risk-changes"))?.[1]?.signal ?? undefined;
    expect(signal).toBeDefined();
    view.unmount();
    expect(signal?.aborted).toBe(true);
    expect(errors).not.toHaveBeenCalled();
    errors.mockRestore();
  });

  it("never fills the page with the changes of the company it came from after a switch", async () => {
    const pending: Record<string, (r: Response) => void> = {};
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (input: RequestInfo | URL) =>
          new Promise<Response>((resolve) => {
            pending[String(input)] = resolve;
          }),
      ),
    );
    mount();
    await act(async () => pending["/api/company/NVDA/dossier"]?.(json(DOSSIER)));
    await act(async () => screen.getByRole("link", { name: "AMD (AMD)" }).click());
    await act(async () => pending["/api/company/NVDA/risk-changes"]?.(json(CHANGES)));
    expect(screen.queryByText("Sovereign AI demand")).toBeNull();
  });
});

describe("Company.copy helpers", () => {
  it("names a filing by whichever of form and date the server gave", () => {
    expect(copy.filingLabel({ form: "10-K", filing_date: "2026-02-25" })).toBe("10-K 2026-02-25");
    expect(copy.filingLabel({ form: null, filing_date: "2026-02-25" })).toBe("2026-02-25");
    expect(copy.filingLabel({ form: "10-K", filing_date: "" })).toBe("10-K");
    expect(copy.filingLabel({ form: null, filing_date: null })).toBe("");
  });

  it("says 'unknown reason' for a pair with no reason, as the legacy page does", () => {
    expect(copy.notComparedLine("a \u2192 b", null)).toBe("a \u2192 b: comparison not available (unknown reason).");
    expect(copy.notComparedLine("a \u2192 b", "low_text_yield")).toBe("a \u2192 b: comparison not available (low_text_yield).");
  });
});
