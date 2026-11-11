import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode, useEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EvidenceDrawerProvider } from "@/components/EvidenceDrawer";
import { useEvidenceDrawer, type EvidenceDrawerApi } from "@/components/useEvidenceDrawer";
import { deck } from "@/copy/deck";
import { resetChipEvidence } from "@/lib/chipEvidence";
import { SPECIMENS } from "@/lib/specimens";
import { WorkspaceContext, type WorkspaceContextValue } from "@/state/useWorkspace";

// docs/specs/m5b-redesign.md RD17 and RD18: the evidence panel's body. Title; Section / Filed / Status; an "Older filing" band when
// the filing was replaced; the passage in serif with the words the answer quoted marked (exact match only, text nodes inside
// <mark>, a legend only when something is marked); the source link; "Details for analysts" with the ids.

const recorded = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../test/fixtures/specimens.recorded.json"), "utf-8")) as {
  payloads: Record<string, Record<string, unknown>>;
};

function ws(): WorkspaceContextValue {
  return { workspace: null, asOf: "", setAsOf: () => {}, setWorkspace: () => {}, generation: 0, docEvidenceEpoch: 0, invalidateDocEvidence: () => {}, headers: () => ({}) };
}

const api: { current: EvidenceDrawerApi | null } = { current: null };
function Probe() {
  const drawer = useEvidenceDrawer();
  useEffect(() => {
    api.current = drawer;
  });
  return null;
}

function mount() {
  render(
    <StrictMode>
      <WorkspaceContext.Provider value={ws()}>
        <EvidenceDrawerProvider>
          <Probe />
        </EvidenceDrawerProvider>
      </WorkspaceContext.Provider>
    </StrictMode>,
  );
}

const panel = () => screen.getByRole("complementary", { name: "Evidence" });

beforeEach(() => {
  resetChipEvidence();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const id = decodeURIComponent(String(url).replace("/api/evidence/", ""));
      const payload = recorded.payloads[id];
      return payload === undefined ? new Response(JSON.stringify({ detail: "not found" }), { status: 404 }) : new Response(JSON.stringify(payload), { status: 200 });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("the evidence panel body", () => {
  it("shows the title, Section / Filed / Status, the older-filing band, the passage, the link and the ids for analysts", async () => {
    mount();
    api.current?.open(SPECIMENS.passage, null, "answer");
    const body = await screen.findByRole("complementary", { name: "Evidence" });
    await waitFor(() => expect(within(body).getByRole("heading", { name: "Nvidia 10-K, Item 1A. Risk Factors" })).toBeInTheDocument());
    const terms = within(body).getAllByRole("term").map((t) => t.textContent);
    expect(terms).toEqual(["Section", "Filed", "Status"]);
    expect(body).toHaveTextContent("Feb 21, 2024");
    expect(body).toHaveTextContent("Older filing: a newer filing has replaced it. It was current until Feb 26, 2025.");
    expect(within(body).getByRole("blockquote")).toHaveTextContent(/^As of January 28, 2024/);
    expect(within(body).getByRole("link", { name: /Open the filing on SEC\.gov/ })).toHaveAttribute("rel", "noopener noreferrer");
    const details = body.querySelector("details");
    expect(details?.querySelector("summary")).toHaveTextContent("Details for analysts");
    expect(details).toHaveTextContent(`id: ${SPECIMENS.passage}`);
  });

  it("marks the words the answer quoted, exactly, with the legend; and nothing, with no legend, when no quote matches", async () => {
    mount();
    api.current?.open(SPECIMENS.passage, null, "answer", ["we had net outstanding a total of $9.7 billion in notes due by 2060"]);
    await screen.findByRole("complementary", { name: "Evidence" });
    await waitFor(() => expect(panel().querySelector("mark")).not.toBeNull());
    const mark = panel().querySelector("mark") as HTMLElement;
    expect(mark.textContent).toBe("we had net outstanding a total of $9.7 billion in notes due by 2060");
    expect(mark.children).toHaveLength(0); // text only
    expect(mark.attributes).toHaveLength(0); // nothing from data on the element
    expect(panel()).toHaveTextContent(deck.evidence.legend);
    expect(within(panel()).getByRole("blockquote").textContent).toBe(recorded.payloads[SPECIMENS.passage].text);

    api.current?.open(SPECIMENS.passage, null, "answer", ["a sentence that this passage does not hold at all"]);
    await waitFor(() => expect(panel().querySelector("mark")).toBeNull());
    expect(panel()).not.toHaveTextContent(deck.evidence.legend);
  });

  it("never marks a reported number or a rule's abstract, and frames a rule as the government's", async () => {
    mount();
    api.current?.open(SPECIMENS.rule, null, "answer", ["revising the Export Administration Regulations (EAR) in response"]);
    await screen.findByRole("complementary", { name: "Evidence" });
    await waitFor(() => expect(panel()).toHaveTextContent("Government rule"));
    expect(panel().querySelector("mark")).toBeNull();
    expect(panel()).toHaveTextContent(/not statements the company made/);
    expect(within(panel()).getByRole("link", { name: /Open the rule on FederalRegister\.gov/ })).toBeInTheDocument();
    api.current?.open(SPECIMENS.number, null, "answer");
    await waitFor(() => expect(panel()).toHaveTextContent("$215.9B"));
    expect(panel()).toHaveTextContent("Exactly $215,938,000,000");
    expect(panel()).toHaveTextContent("Jan 27, 2025 to Jan 25, 2026");
  });

  it("says plainly while loading and when the source cannot be loaded", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    mount();
    api.current?.open(SPECIMENS.passage, null, "answer");
    await screen.findByRole("complementary", { name: "Evidence" });
    await waitFor(() => expect(panel()).toHaveTextContent("Loading the source…"));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "not found" }), { status: 404 })));
    api.current?.open("0001045810-24-000029:I.1A:0999", null, "answer");
    await waitFor(() => expect(panel()).toHaveTextContent("Could not load this source: not found"));
  });

  it("announces the loading and failed states politely (R2 review)", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    mount();
    api.current?.open(SPECIMENS.passage, null, "answer");
    const body = await screen.findByRole("complementary", { name: "Evidence" });
    await waitFor(() => expect(within(body).getByRole("status")).toHaveTextContent("Loading the source…"));
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "not found" }), { status: 404 })));
    api.current?.open("0001045810-24-000029:I.1A:0998", null, "answer");
    await waitFor(() => expect(within(body).getByRole("status")).toHaveTextContent("Could not load this source: not found"));
  });
});
