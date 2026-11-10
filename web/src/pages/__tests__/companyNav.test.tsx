import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Company } from "../Company";

// docs/specs/m5b-redesign.md RD21 and SR10: one row of in-page links (Key numbers, Government rules, Relationships, Filings, Risk
// changes); following one focuses the target heading; a hash is matched against the fixed list of section ids with
// getElementById, never as a selector, and anything else is ignored; every section stays in the DOM (not ARIA tabs). With focus
// in the evidence panel a hash change moves nothing.

const DOSSIER = {
  company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 },
  data_as_of: "2026-09-30",
  filings: [{ accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25", is_current: true, status: "current", superseded_by: null }],
  metrics: [{ metric: "revenue", value: 1, unit: "USD", period_start: "2025-01-27", period_end: "2026-01-25" }],
  active_risks: [],
  edges: [],
  rules: [],
};

function stub() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith("/dossier")
        ? new Response(JSON.stringify(DOSSIER), { status: 200 })
        : new Response(JSON.stringify({ company: DOSSIER.company, pairs: [] }), { status: 200 }),
    ),
  );
}

const go: { to: ((hash: string) => void) | null } = { to: null };
function Probe() {
  const navigate = useNavigate();
  useEffect(() => {
    go.to = (hash) => navigate({ pathname: "/company/NVDA", hash });
  }, [navigate]);
  return null;
}

function mount(path = "/company/NVDA") {
  stub();
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Probe />
      <div id="evidence-panel">
        <button type="button">inside the panel</button>
      </div>
      <Routes>
        <Route path="company/:ticker" element={<Company />} />
      </Routes>
    </MemoryRouter>,
  );
}

const ready = () => screen.findByRole("heading", { level: 2, name: "Key numbers" });
const nav = () => screen.getByRole("navigation", { name: "On this page" });
const heading = (name: string) => screen.getByRole("heading", { level: 2, name });

afterEach(() => vi.unstubAllGlobals());

describe("section navigation (RD21)", () => {
  it("is one named list of five in-page links, in order", async () => {
    mount();
    await ready();
    const links = within(nav()).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(["Key numbers", "Government rules", "Relationships", "Filings", "Risk changes"]);
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["#key-numbers", "#government-rules", "#relationships", "#filings", "#risk-changes"]);
  });

  it("puts every section in the page, in a fixed order, each headed by a focusable h2 with its id; no tabs", async () => {
    mount();
    await ready();
    await screen.findByRole("heading", { level: 2, name: "Compared risk disclosures" });
    const ids = Array.from(document.querySelectorAll("main h2[id], h2[id]"), (h) => h.id);
    expect(ids).toEqual(["key-numbers", "government-rules", "relationships", "filings", "active-risks", "risk-changes"]);
    for (const id of ids) expect(document.getElementById(id)).toHaveAttribute("tabindex", "-1");
    expect(screen.queryAllByRole("tab")).toHaveLength(0);
    expect(screen.queryAllByRole("tabpanel")).toHaveLength(0);
  });

  it("focuses the target heading when a link is followed, and again when it is followed twice", async () => {
    const user = userEvent.setup();
    mount();
    await ready();
    await user.click(within(nav()).getByRole("link", { name: "Filings" }));
    expect(heading("Filings")).toHaveFocus();
    await user.click(within(nav()).getByRole("link", { name: "Key numbers" }));
    await user.click(within(nav()).getByRole("link", { name: "Filings" }));
    expect(heading("Filings")).toHaveFocus();
  });

  it("focuses the section a page is opened at, and the one a later hash change names", async () => {
    mount("/company/NVDA#government-rules");
    await ready();
    await waitFor(() => expect(heading("Government rules")).toHaveFocus());
    act(() => go.to?.("#relationships"));
    await waitFor(() => expect(heading("Relationships")).toHaveFocus());
  });

  const HOSTILE_HASHES: readonly (readonly [string, string])[] = [
    ["a list", "#a,b"],
    ["a wildcard", "#*"],
    ["another landmark's id", "#main"],
    ["a selector", "#:has(*)"],
    ["a quote", '#"]'],
    ["a broken escape", "#%E0%A4%A"],
    ["a section id with more", "#key-numbers%20x"],
    ["10,000 characters", `#${"x".repeat(10_000)}`],
    ["an unknown id", "#unknown"],
    ["an empty hash", "#"],
    ["no hash", ""],
  ];

  it.each(HOSTILE_HASHES)("ignores %s: no focus moves, nothing throws, no selector is built from it", async (_label, hash) => {
      const spies = [
        vi.spyOn(document, "querySelector"),
        vi.spyOn(document, "querySelectorAll"),
        vi.spyOn(Element.prototype, "querySelector"),
        vi.spyOn(Element.prototype, "querySelectorAll"),
      ];
      mount(`/company/NVDA${hash}`);
      await ready();
      act(() => go.to?.(hash));
      expect(document.activeElement).toBe(document.body);
      // a selector built from the hash would hold it whole, "#" included (shorter pieces such as "*" are in ordinary selectors)
      if (hash.length > 1) {
        for (const spy of spies) for (const call of spy.mock.calls) expect(String(call[0])).not.toContain(hash);
      }
      for (const spy of spies) spy.mockRestore();
  });

  it("moves nothing on a hash change while focus is in the evidence panel; a link still moves it", async () => {
    const user = userEvent.setup();
    mount();
    await ready();
    const inside = screen.getByRole("button", { name: "inside the panel" });
    inside.focus();
    act(() => go.to?.("#filings"));
    expect(inside).toHaveFocus();
    await user.click(within(nav()).getByRole("link", { name: "Relationships" }));
    expect(heading("Relationships")).toHaveFocus();
  });
});
