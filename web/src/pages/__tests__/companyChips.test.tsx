import { fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { EvidenceDrawerContext, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import { stampLabel } from "@/components/__tests__/stampLabel";
import { numberOccurrences } from "@/lib/citations";
import type { Dossier, RiskChangesResult } from "@/lib/company";
import { DossierBlocks } from "../CompanyBlocks";
import { RiskChangesBlock } from "../CompanyChanges";

// R25 (docs/specs/m5b-frontend.md:373): "filing-backed items open evidence chips". Every citation id the Company page shows is the
// shared evidence chip of the Ask page (A1: a real button with its readable label) and opens the one evidence drawer with the place
// "company"; the `rules` stay labelled as keyword matches. An id outside the corpus grammar (or a `doc:` id, which belongs to a
// workspace) is never a chip: it stays text and can never cause a request (S4).

const CHUNK = "0001045810-26-000021:I.1:0320";
const XBRL = "xbrl:1045810:revenue:2026-01-25";
const FR = "fr:2026-01234";
const DOC = "doc:0123456789ab:v1:0001";

const DOSSIER: Dossier = {
  company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 },
  dataAsOf: "2026-09-30",
  filings: [],
  metrics: [{ metric: "revenue", value: 1, unit: "USD", period_start: null, period_end: "2026-01-25", cite: XBRL }],
  activeRisks: [],
  edges: [{ source: "Nvidia", relation: "DEPENDS_ON", target: "TSMC", status: null, quote: null, chunk_ids: [CHUNK], quoteChunk: CHUNK }],
  rules: [{ source: "Nvidia", relation: "MATCHES", target: "Export rule", status: null, quote: null, chunk_ids: [CHUNK], quoteChunk: CHUNK, rule_id: FR, date: null, kind: null, cite: FR }],
};

function drawerSpy() {
  const open = vi.fn<EvidenceDrawerApi["open"]>();
  const api: EvidenceDrawerApi = { openId: null, openKey: null, open, close: vi.fn(), dropDocEvidence: vi.fn(), clearAll: vi.fn() };
  return { ...api, open };
}

function showDossier(dossier: Dossier, drawer = drawerSpy()) {
  const view = render(
    <EvidenceDrawerContext.Provider value={drawer}>
      <MemoryRouter>
        <DossierBlocks dossier={dossier} />
      </MemoryRouter>
    </EvidenceDrawerContext.Provider>,
  );
  return { ...view, drawer };
}

const chips = (root: ParentNode) => [...root.querySelectorAll<HTMLButtonElement>("button[data-id]")];

describe("numberOccurrences: each id's occurrence across groups, in order (a chip key part, A2)", () => {
  it("counts each id across all groups and leaves other ids at 0", () => {
    expect(numberOccurrences([[CHUNK], [CHUNK, XBRL], [], [CHUNK]])).toEqual([[0], [1, 0], [], [2]]);
  });

  it("returns nothing for no groups", () => {
    expect(numberOccurrences([])).toEqual([]);
  });
});

describe("R25: the dossier's citation ids are evidence chips", () => {
  it("renders the metric, the edge chunk, the rule chunk and the rule as chip buttons with the Ask page's labels", () => {
    const { container } = showDossier(DOSSIER);
    const found = chips(container);
    // RD20/RD21 order: at a glance, key numbers, rules, relationships; a figure shows its value (RD22), a passage or rule its stamp
    expect(found.map((b) => b.dataset.id)).toEqual([XBRL, XBRL, CHUNK, FR, CHUNK]);
    expect(found.slice(2).map((b) => b.textContent)).toEqual([CHUNK, FR, CHUNK].map((id) => stampLabel(id)));
    expect(found.slice(0, 2).map((b) => b.textContent)).toEqual(["$1", "$1"]);
    for (const button of found) expect(button).toHaveAttribute("type", "button");
    expect(container.querySelectorAll("[data-cite]")).toHaveLength(0);
  });

  it("gives every chip on the page its own key, under the place company", () => {
    const { container } = showDossier(DOSSIER);
    const keys = chips(container).map((b) => b.dataset.chipKey ?? "");
    expect(keys).toHaveLength(5);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys.every((k) => k.startsWith("company:"))).toBe(true);
  });

  it("opens the drawer for the clicked chip, with the chip as the opener and the place company", () => {
    const { drawer } = showDossier(DOSSIER);
    const metrics = screen.getByRole("region", { name: "Key numbers" });
    const chip = within(metrics).getByRole("button", { name: /^\$1, Revenue, FY2026/ });
    fireEvent.click(chip);
    expect(drawer.open).toHaveBeenCalledWith(XBRL, chip, "company", undefined); // no quote before it (RD18)
  });

  it("keeps the rules labelled as keyword matches next to their chips", () => {
    showDossier(DOSSIER);
    const rules = screen.getByRole("region", { name: "Government rules" });
    expect(rules).toHaveTextContent("keyword matches to external Federal Register rules");
    expect(within(rules).getByRole("button", { name: stampLabel(FR) })).toBeInTheDocument();
  });

  it("keeps both chips of an id an edge lists twice: distinct chip keys and no duplicate React key", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { container } = showDossier({ ...DOSSIER, metrics: [], rules: [], edges: [{ ...DOSSIER.edges[0], chunk_ids: [CHUNK, CHUNK] }] });
    const found = chips(container);
    expect(found).toHaveLength(2);
    expect(new Set(found.map((b) => b.dataset.chipKey)).size).toBe(2);
    expect(errors.mock.calls.filter((call) => String(call[0]).includes("same key"))).toEqual([]);
    errors.mockRestore();
  });

  it("never makes a chip of a doc: id: it stays text, and a click on it opens nothing", () => {
    const { container, drawer } = showDossier({ ...DOSSIER, metrics: [{ ...DOSSIER.metrics[0], cite: DOC }], edges: [], rules: [] });
    expect(chips(container)).toHaveLength(0);
    expect(container.textContent).not.toContain(DOC); // a figure shows its value; its id is never a stamp (SR3)
    const text = within(screen.getByRole("region", { name: "Key numbers" })).getByText("$1");
    fireEvent.click(text);
    expect(drawer.open).not.toHaveBeenCalled();
  });
});

describe("R25: a compared passage's chunk id is an evidence chip", () => {
  const OLD = { accession_no: "0001045810-25-000023", form: "10-K", filing_date: "2025-02-26" };
  const NEW = { accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25" };
  const STATE: RiskChangesResult = {
    kind: "ok",
    changes: {
      pairs: [
        {
          older: OLD,
          newer: NEW,
          compared: true,
          reason: null,
          items: [
            { kind: "changed", headline: "Export licensing", passages: [{ quote: "a", chunkId: CHUNK, side: "older" }, { quote: "b", chunkId: CHUNK, side: "newer" }] },
            { kind: "new", headline: "Sovereign AI", passages: [{ quote: "c", chunkId: CHUNK, side: "newer" }] },
          ],
        },
      ],
    },
  };

  it("renders each passage's chunk id as a chip with its own key, and a click opens the drawer with the place company", () => {
    const drawer = drawerSpy();
    const { container } = render(
      <EvidenceDrawerContext.Provider value={drawer}>
        <RiskChangesBlock state={STATE} />
      </EvidenceDrawerContext.Provider>,
    );
    const found = chips(container);
    expect(found.map((b) => b.dataset.id)).toEqual([CHUNK, CHUNK, CHUNK]);
    const keys = found.map((b) => b.dataset.chipKey);
    expect(new Set(keys).size).toBe(3);
    fireEvent.click(found[0]);
    expect(drawer.open).toHaveBeenCalledWith(CHUNK, found[0], "company", undefined);
  });
});
