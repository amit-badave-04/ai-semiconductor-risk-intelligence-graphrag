import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { stampLabel } from "@/components/__tests__/stampLabel";
import type { Dossier, Edge, Rule } from "@/lib/company";
import { CompanySwitcher, DossierBlocks } from "../CompanyBlocks";

// R25 (docs/specs/m5b-frontend.md): the blocks of the dossier, rendered directly over typed data so that every branch of a
// nullable field (null shown as nothing, present shown as text) and every empty block is exercised.

const CHUNK = "0001045810-26-000021:I.1:0320";

const EMPTY: Dossier = {
  company: { ticker: "NVDA", name: "Nvidia", cik: null },
  dataAsOf: null,
  filings: [],
  metrics: [],
  activeRisks: [],
  edges: [],
  rules: [],
};

const NULL_EDGE: Edge = { source: null, relation: null, target: null, status: null, quote: null, chunk_ids: [], quoteChunk: null };
const FULL_EDGE: Edge = {
  source: "Nvidia",
  relation: "DEPENDS_ON",
  target: "TSMC",
  status: "Active",
  quote: "We depend on TSMC.",
  chunk_ids: [CHUNK],
  quoteChunk: CHUNK,
};
const NULL_RULE: Rule = { ...NULL_EDGE, rule_id: null, date: null, kind: null, cite: null };
const FULL_RULE: Rule = { ...FULL_EDGE, rule_id: "fr:2026-01234", date: "2026-03-01", kind: "Rule", cite: "fr:2026-01234" };

function show(dossier: Dossier) {
  return render(
    <MemoryRouter>
      <DossierBlocks dossier={dossier} />
    </MemoryRouter>,
  );
}

function section(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

describe("DossierBlocks: empty dossier", () => {
  it("shows no data-as-of line and the word none in every empty block", () => {
    show(EMPTY);
    expect(screen.queryByText(/Data as of/)).toBeNull();
    for (const name of ["Filings", "Metrics", "Active risks", "Relationships", "Federal Register rules"]) {
      expect(within(section(name)).getByText("none"), name).toBeTruthy();
    }
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("still states what the rules are, in the empty rules block", () => {
    show(EMPTY);
    expect(within(section("Federal Register rules")).getByText("keyword matches to external Federal Register rules")).toBeTruthy();
  });
});

describe("DossierBlocks: rows with null fields", () => {
  const nulls: Dossier = {
    ...EMPTY,
    filings: [{ accession_no: null, form: null, filing_date: null, is_current: null, status: null, superseded_by: null }],
    metrics: [{ metric: null, value: null, unit: null, period_start: null, period_end: null, cite: null }],
    activeRisks: [{ risk_id: null, summary: null, category: null, last_evidenced_at: null }],
    edges: [NULL_EDGE],
    rules: [NULL_RULE],
  };

  it("renders empty cells for null fields without a citation, a date suffix or a status", () => {
    const { container } = show(nulls);
    expect(within(section("Filings")).getAllByRole("cell").map((c) => c.textContent)).toEqual(["", "", "", ""]);
    expect(within(section("Metrics")).getAllByRole("cell").map((c) => c.textContent)).toEqual(["", "", "", "", ""]);
    expect(container.querySelectorAll("[data-cite], button[data-id]")).toHaveLength(0);
    expect(section("Active risks").textContent).not.toContain("\u00b7");
    expect(section("Relationships").textContent).not.toContain("(");
  });
});

describe("DossierBlocks: rows with every field present", () => {
  const full: Dossier = {
    ...EMPTY,
    dataAsOf: "2026-09-30",
    filings: [
      { accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25", is_current: true, status: "current", superseded_by: null },
    ],
    metrics: [
      { metric: "revenue", value: 130497000000, unit: "USD", period_start: "2025-01-27", period_end: "2026-01-25", cite: "xbrl:1045810:revenue:2026-01-25" },
    ],
    activeRisks: [{ risk_id: "r1", summary: "Packaging supply is concentrated.", category: "supply", last_evidenced_at: "2026-02-25" }],
    edges: [FULL_EDGE],
    rules: [FULL_RULE],
  };

  it("shows the data-as-of line and each value as text", () => {
    show(full);
    expect(screen.getByText("Data as of 2026-09-30")).toBeTruthy();
    expect(within(section("Filings")).getByText("10-K")).toBeTruthy();
    expect(within(section("Metrics")).getByText("130497000000")).toBeTruthy();
    expect(section("Active risks").textContent).toContain("supply \u00b7 2026-02-25");
  });

  it("shows the citation ids as evidence chips (R25): the metric, the edge chunk, the rule chunk and the rule", () => {
    const { container } = show(full);
    const cites = Array.from(container.querySelectorAll("button[data-id]")).map((el) => el.getAttribute("data-id"));
    expect(cites).toEqual(["xbrl:1045810:revenue:2026-01-25", CHUNK, CHUNK, "fr:2026-01234"]);
    expect(container.querySelector(`button[data-id="${CHUNK}"]`)?.textContent).toBe(stampLabel(CHUNK));
  });

  it("shows an edge's status and quote, and a rule's date and kind", () => {
    show(full);
    const edges = section("Relationships");
    expect(edges.textContent).toContain("Nvidia DEPENDS_ON TSMC (Active)");
    expect(within(edges).getByText("We depend on TSMC.")).toBeTruthy();
    const rules = section("Federal Register rules");
    expect(within(rules).getByText("2026-03-01")).toBeTruthy();
    expect(within(rules).getByText("Rule")).toBeTruthy();
  });
});

describe("DossierBlocks: a rule without a citation id falls back to its rule id as text", () => {
  it("shows the rule id when cite is null, and neither date nor kind when they are null", () => {
    const { container } = show({ ...EMPTY, rules: [{ ...FULL_RULE, cite: null, date: null, kind: null, chunk_ids: [] }] });
    const rules = section("Federal Register rules");
    expect(within(rules).getByText("fr:2026-01234")).toBeTruthy();
    expect(container.querySelectorAll("[data-cite], button[data-id]")).toHaveLength(0);
    expect(within(rules).queryByText("2026-03-01")).toBeNull();
    expect(within(rules).queryByText("Rule")).toBeNull();
  });

  it("keeps an edge with a quote but no chunk ids free of citation chips", () => {
    const { container } = show({ ...EMPTY, edges: [{ ...FULL_EDGE, chunk_ids: [] }] });
    expect(container.querySelectorAll("[data-cite], button[data-id]")).toHaveLength(0);
    expect(screen.getByText("We depend on TSMC.")).toBeTruthy();
  });
});

describe("CompanySwitcher", () => {
  it("lists the filers and marks only the current one", () => {
    render(
      <MemoryRouter>
        <CompanySwitcher current="NVDA" />
      </MemoryRouter>,
    );
    const nav = screen.getByRole("navigation", { name: "Companies" });
    const current = within(nav).getAllByRole("link").filter((a) => a.getAttribute("aria-current") === "page");
    expect(current.map((a) => a.textContent)).toEqual(["Nvidia (NVDA)"]);
  });

  it("marks none when the current ticker is not a filer", () => {
    render(
      <MemoryRouter>
        <CompanySwitcher current="XXXX" />
      </MemoryRouter>,
    );
    expect(document.querySelector("[aria-current]")).toBeNull();
  });
});
