import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { EvidenceDrawerContext, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import type { Dossier, Edge, Rule } from "@/lib/company";
import { DossierBlocks } from "../CompanyBlocks";

// docs/specs/m5b-redesign.md RD23, RD24, D-R3, S2 and docs/decisions/001-r3-company-copy.md (2). Government rules: their own
// section with the notice (US export-control rules from the Federal Register, linked by keyword matching, actions by the
// government and not statements the company made); each rule its "Government rule" stamp, date and title; no passage chips; with
// none, the D-R3 sentence. Relationships: never a relation name; the other company, the quote "In the filing's own words" with the
// chip of the passage it came from, before the competitors, which are one list of names with chips.

const C = (n: number) => `0001045810-26-000021:I.1A:${String(n).padStart(4, "0")}`;
const rule = (id: string, title: string, date: string | null, chunk = C(9)): Rule => ({
  source: "Nvidia",
  relation: "AFFECTED_BY",
  target: title,
  status: "Active",
  quote: null,
  chunk_ids: [chunk],
  quoteChunk: chunk,
  rule_id: id,
  date,
  kind: "advanced_computing",
  cite: `fr:${id}`,
});
const edge = (source: string, relation: string, target: string, quote: string | null, chunkIds: string[], quoteChunk: string | null): Edge => ({
  source,
  relation,
  target,
  status: "Active",
  quote,
  chunk_ids: chunkIds,
  quoteChunk,
});

function dossier(over: Partial<Dossier> = {}): Dossier {
  return { company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 }, dataAsOf: null, filings: [], metrics: [], activeRisks: [], edges: [], rules: [], ...over };
}

function show(d: Dossier) {
  const open = vi.fn<EvidenceDrawerApi["open"]>();
  const api: EvidenceDrawerApi = { openId: null, openKey: null, open, close: vi.fn(), dropDocEvidence: vi.fn(), clearAll: vi.fn() };
  const view = render(
    <EvidenceDrawerContext.Provider value={api}>
      <MemoryRouter>
        <DossierBlocks dossier={d} />
      </MemoryRouter>
    </EvidenceDrawerContext.Provider>,
  );
  return { ...view, open };
}

const rules = () => screen.getByRole("region", { name: "Government rules" });
const relations = () => screen.getByRole("region", { name: "Relationships" });

describe("Government rules (RD23)", () => {
  it("states what the rules are, tied to the list, and shows each rule's stamp, date and title only", () => {
    show(dossier({ rules: [rule("2025-00711", "Advanced computing measures", "2025-01-16"), rule("2026-01234", "Manufacturing items", null)] }));
    const notice = within(rules()).getByText(/^These US export-control rules come from the Federal Register/);
    const list = within(rules()).getByRole("list");
    expect(list.getAttribute("aria-describedby")).toBe(notice.id);
    const items = within(list).getAllByRole("listitem");
    expect(within(items[0]).getByRole("button", { name: "Government rule 2025-00711" })).toHaveAttribute("data-id", "fr:2025-00711");
    expect(items[0]).toHaveTextContent("Jan 16, 2025");
    expect(items[0]).toHaveTextContent("Advanced computing measures");
    expect(within(items[0]).getAllByRole("button")).toHaveLength(1); // decision 2: no passage chips
    expect(rules().textContent).not.toMatch(/AFFECTED_BY|Nvidia AFFECTED|advanced_computing/);
  });

  it("keeps a rule whose id fails the grammar as its title and date with a text label and no button", () => {
    const { open } = show(dossier({ rules: [{ ...rule("x", "A title", "2025-01-16"), rule_id: "<img src=x>", cite: null }] }));
    const item = within(rules()).getByRole("listitem");
    expect(within(item).queryByRole("button")).toBeNull();
    expect(item).toHaveTextContent("Government rule");
    expect(item).toHaveTextContent("A title");
    expect(item.querySelector("img")).toBeNull();
    expect(open).not.toHaveBeenCalled();
  });

  it("with none linked, says so in the owner's words with the company's own name (D-R3)", () => {
    show(dossier({ company: { ticker: "ASML", name: "<b>server name</b>", cik: 937966 } }));
    expect(rules()).toHaveTextContent("No export-control rule in our data was linked to ASML by keyword matching. This is not a finding that none applies.");
  });

  it("never makes the rule's url a link", () => {
    show(dossier({ rules: [{ ...rule("2025-00711", "T", "2025-01-16"), url: "https://evil.example/x" } as Rule] }));
    expect(within(rules()).queryAllByRole("link")).toHaveLength(0);
    expect(rules().textContent).not.toContain("evil.example");
  });
});

describe("Relationships (RD24)", () => {
  const edges = [
    edge("Nvidia", "COMPETES_WITH", "AMD", "q1", [C(6)], C(6)),
    edge("Nvidia", "DEPENDS_ON", "TSMC", "we depend on a foundry", [C(2), C(3)], C(2)),
    edge("Microsoft", "CUSTOMER_OF", "Nvidia", "a cloud provider buys", [C(4)], C(4)),
    edge("AMD", "COMPETES_WITH", "Nvidia", "q2", [C(7)], C(7)),
    edge("Nvidia", "DEPENDS_ON", "SK Hynix", "memory", [C(5)], null),
  ];

  it("never names a relation, and lists the other companies with the quote in the filing's own words first", () => {
    show(dossier({ edges }));
    expect(relations().textContent).not.toMatch(/SUPPLIES_TO|DEPENDS_ON|CUSTOMER_OF|COMPETES_WITH|_OF\b|\(Active\)/);
    const items = within(within(relations()).getAllByRole("list")[0]).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("TSMC");
    expect(items[0]).toHaveTextContent("In the filing's own words");
    const quote = within(items[0]).getByRole("blockquote");
    expect(quote).toHaveTextContent("we depend on a foundry");
    expect(within(items[0]).getAllByRole("button").map((b) => b.getAttribute("data-id"))).toEqual([C(2), C(3)]);
  });

  it("links a covered company to its page and keeps one without a page as text", () => {
    show(dossier({ edges }));
    expect(within(relations()).getByRole("link", { name: "TSMC" })).toHaveAttribute("href", "/company/TSM/");
    expect(within(relations()).getByRole("link", { name: "Microsoft" })).toHaveAttribute("href", "/company/MSFT/");
    expect(within(relations()).queryByRole("link", { name: "SK Hynix" })).toBeNull();
    expect(relations()).toHaveTextContent("SK Hynix");
  });

  it("shows a quote whose own passage is unknown with no chip beside it, and its other passages after", () => {
    show(dossier({ edges }));
    const item = within(within(relations()).getAllByRole("list")[0]).getAllByRole("listitem")[2];
    expect(within(item).getByRole("blockquote")).toHaveTextContent("memory");
    expect(within(item).getAllByRole("button").map((b) => b.getAttribute("data-id"))).toEqual([C(5)]);
  });

  it("keeps the chip of a relation with no quote, leading its other passages (R3 review of slice 5)", () => {
    show(dossier({ edges: [edge("Nvidia", "DEPENDS_ON", "TSMC", null, [C(2), C(3)], C(2))] }));
    const item = within(relations()).getByRole("listitem");
    expect(within(item).queryByRole("blockquote")).toBeNull();
    expect(item).not.toHaveTextContent("In the filing's own words");
    expect(within(item).getAllByRole("button").map((b) => b.getAttribute("data-id"))).toEqual([C(2), C(3)]);
  });

  it("lists each competitor once, after the others, with its chips merged", () => {
    show(dossier({ edges }));
    const heading = within(relations()).getByRole("heading", { level: 3, name: "Competitors named in its filings" });
    const list = heading.nextElementSibling as HTMLElement;
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(1);
    expect(items[0]).toHaveTextContent("AMD");
    expect(within(items[0]).getAllByRole("button").map((b) => b.getAttribute("data-id"))).toEqual([C(6), C(7)]);
  });

  it("renders hostile names and quotes as text, with no element, link or overflow from them", () => {
    const long = "x".repeat(5000);
    const { container } = show(dossier({ edges: [edge("Nvidia", "DEPENDS_ON", "<img src=x onerror=alert(1)>", "[x](javascript:alert(1)) " + long, [], null)] }));
    expect(container.querySelector("img")).toBeNull();
    expect(within(relations()).queryAllByRole("link")).toHaveLength(0);
    expect(relations()).toHaveTextContent("<img src=x onerror=alert(1)>");
    expect(within(relations()).getByRole("blockquote").className).toMatch(/break-words|\[overflow-wrap:anywhere\]/);
  });

  it("opens the evidence of a quote's chip", () => {
    const { open } = show(dossier({ edges }));
    const chip = within(relations()).getAllByRole("button")[0];
    fireEvent.click(chip);
    expect(open).toHaveBeenCalledWith(C(2), chip, "company", undefined);
  });
});
