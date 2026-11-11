import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Company } from "../Company";
import { mountAsk } from "./askHarness";

// docs/specs/m5b-redesign.md RD19 (the "Ask about <company>" and "Compare filings" actions), RD25 (Filings and Active risks
// restyled, the D-R4 label, risk changes unchanged) and docs/decisions/001-r3-company-copy.md (4). "Ask about" opens Ask with the
// approved question in the box and sends nothing; the router state that carries the ticker is untrusted and is cleared once read.

const DOSSIER = {
  company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 },
  data_as_of: "2026-09-30",
  filings: [
    { accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25", is_current: true, status: "current", superseded_by: null },
    { accession_no: "0001045810-25-000023", form: "10-K", filing_date: "2025-02-26", is_current: false, status: "superseded", superseded_by: "0001045810-26-000021" },
  ],
  metrics: [{ metric: "revenue", value: 1, unit: "USD", period_start: null, period_end: "2026-01-25" }],
  active_risks: [{ risk_id: "r1", summary: "Supply of advanced packaging is concentrated.", category: "supply", last_evidenced_at: "2026-02-25" }],
  edges: [],
  rules: [],
};
const CHANGES = {
  company: DOSSIER.company,
  pairs: [
    {
      older: { accession_no: "a", form: "20-F", filing_date: "2025-04-17" },
      newer: { accession_no: "b", form: "20-F", filing_date: "2026-04-16" },
      compared: true,
      not_compared_reason: null,
      items: [{ kind: "new", headline: null, passages: [{ quote: "A new paragraph.", chunk_id: null, side: "newer" }] }],
    },
  ],
};

const where = { seen: "" };
function Where() {
  const location = useLocation();
  useEffect(() => {
    where.seen = `${location.pathname}|${JSON.stringify(location.state)}`;
  }, [location]);
  return null;
}

function mountCompany() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(String(input).endsWith("/dossier") ? DOSSIER : CHANGES), { status: 200 })),
  );
  return render(
    <MemoryRouter initialEntries={["/company/NVDA"]}>
      <Where />
      <Routes>
        <Route path="company/:ticker" element={<Company />} />
        <Route path="/" element={<p>the ask page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("Filings and Active risks (RD25)", () => {
  it("states each filing's date and status in words", async () => {
    mountCompany();
    const filings = await screen.findByRole("region", { name: "Filings" });
    const rows = within(filings).getAllByRole("row").slice(1).map((r) => r.textContent);
    expect(rows[0]).toBe("10-KFeb 25, 2026Current filing0001045810-26-000021");
    expect(rows[1]).toBe("10-KFeb 26, 2025Older filing0001045810-25-000023");
  });

  it("words a corrected filing, and shows an unknown status as the server wrote it (R3 review T11)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const body = String(input).endsWith("/dossier")
          ? { ...DOSSIER, filings: [{ ...DOSSIER.filings[0], status: "corrected" }, { ...DOSSIER.filings[1], status: "pending_review" }] }
          : CHANGES;
        return new Response(JSON.stringify(body), { status: 200 });
      }),
    );
    render(
      <MemoryRouter initialEntries={["/company/NVDA"]}>
        <Routes>
          <Route path="company/:ticker" element={<Company />} />
        </Routes>
      </MemoryRouter>,
    );
    const filings = await screen.findByRole("region", { name: "Filings" });
    const rows = within(filings).getAllByRole("row").slice(1).map((r) => r.textContent);
    expect(rows[0]).toContain("Corrected");
    expect(rows[1]).toContain("pending_review");
  });

  it("labels the active risks as summaries written by semigraph, and shows no date for them (D-R4)", async () => {
    mountCompany();
    const risks = await screen.findByRole("region", { name: "Active risks" });
    expect(risks).toHaveTextContent("Summaries written by semigraph, not quotes");
    expect(risks).toHaveTextContent("Supply of advanced packaging is concentrated.");
    expect(risks).not.toHaveTextContent("2026-02-25");
    expect(risks).not.toHaveTextContent("Feb 25, 2026");
  });

  it("renders a 20-F risk item with no headline as its passage alone, with no empty label", async () => {
    mountCompany();
    const changes = await screen.findByRole("region", { name: "Risk changes" });
    await waitFor(() => expect(changes).toHaveTextContent("A new paragraph."));
    const item = within(changes).getByRole("listitem");
    expect(item.firstElementChild?.tagName).toBe("P"); // the passage leads; no empty label before it
    expect([...item.children].every((child) => child.textContent !== "")).toBe(true);
    expect(item.textContent?.startsWith("20-F 2026-04-16: ")).toBe(true); // the pair's filing label, unchanged (R25)
  });
});

describe("the header actions (RD19)", () => {
  it("'Compare filings' goes to the risk changes and focuses their heading", async () => {
    const user = userEvent.setup();
    mountCompany();
    const link = await screen.findByRole("link", { name: "Compare filings" });
    expect(link).toHaveAttribute("href", "#risk-changes");
    await user.click(link);
    expect(screen.getByRole("heading", { level: 2, name: "Risk changes" })).toHaveFocus();
  });

  it("'Ask about Nvidia' opens Ask with the company in the router state", async () => {
    const user = userEvent.setup();
    mountCompany();
    await user.click(await screen.findByRole("link", { name: "Ask about Nvidia" }));
    await waitFor(() => expect(where.seen).toBe('/|{"askAbout":"NVDA"}'));
  });
});

describe("Ask, opened from a Company page", () => {
  it("puts the approved question in the box, sends nothing, keeps focus off the box and clears the state", async () => {
    const env = mountAsk({ entries: [{ pathname: "/", state: { askAbout: "NVDA" } }] });
    const box = screen.getByRole("textbox", { name: "Your question" });
    await waitFor(() => expect(box).toHaveValue("What risks does Nvidia describe in its latest annual report?"));
    expect(box).not.toHaveFocus();
    expect(env.posts()).toHaveLength(0);
  });

  it("still sends nothing with a workspace open", async () => {
    const env = mountAsk({ entries: [{ pathname: "/", state: { askAbout: "TSM" } }], workspace: { workspace: { id: "w", token: "t" } as never } });
    await waitFor(() => expect(screen.getByRole("textbox", { name: "Your question" })).toHaveValue("What risks does TSMC describe in its latest annual report?"));
    expect(env.posts()).toHaveLength(0);
  });

  it.each([
    ["a ticker that is not a filer", { askAbout: "<script>" }],
    ["an object", { askAbout: { ticker: "NVDA" } }],
    ["a huge string", { askAbout: "N".repeat(1_000_000) }],
    ["another shape", { other: "NVDA" }],
  ])("ignores a forged state: %s", async (_label, state) => {
    mountAsk({ entries: [{ pathname: "/", state }] });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByRole("textbox", { name: "Your question" })).toHaveValue("");
  });
});
