import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Method } from "../Method";
import * as copy from "../Method.copy";

// R27 / D7 (docs/specs/m5b-frontend.md): the Method & Evidence page. Only copy already pinned by a test of the legacy page; the
// verbatim match against index.html is checked outside vitest (it cannot read the Python tree in the off-OneDrive mirror).

// S15: the 13 withdrawn claims of tests/test_static_ui.py::test_page_never_carries_the_withdrawn_claims plus the user-count
// claims, built from pieces here so this file does not itself trip tests/test_web_source_scan.py.
const BANNED = [
  ["draft", "verified"],
  ["all citations", "verified"],
  ["95%", "correct"],
  ["75%", "correct"],
  ["100%", "correct"],
  ["one", "Claude", "call"],
  ["dropped risk", "lineages"],
  ["deleted", "risk", "lineages"],
  ["text-verified", "removed"],
  ["text verified", "absent"],
  ["verified", "absent"],
  ["removed lists", "do not support"],
  ["stop disclosing", "in its latest"],
  ["1,000", "users"],
  ["1000", "concurrent"],
].map((parts) => (parts[0] === "deleted" ? parts.join("_") : parts.join(" ")));

function mount() {
  return render(
    <MemoryRouter>
      <Method />
    </MemoryRouter>,
  );
}

function joined(segments: readonly copy.Segment[]): string {
  return segments.map((s) => s.text).join("");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Method & Evidence page", () => {
  it("keeps the h1 and makes no request", () => {
    const fn = vi.fn();
    vi.stubGlobal("fetch", fn);
    mount();
    expect(screen.getByRole("heading", { level: 1, name: "Method & Evidence" })).toBeInTheDocument();
    expect(fn).not.toHaveBeenCalled();
  });

  it("carries the pinned phrases of the legacy tests", () => {
    const { container } = mount();
    const text = container.textContent ?? "";
    for (const phrase of [
      "verbatim filing excerpt",
      "keyword matches to external Federal Register rules",
      "SEC filings of 13 keystone companies",
      "withdrawn",
      "re-measurement",
      "How reliable are change claims?",
      "no longer appears as a separate risk factor",
      "4 of 4",
      "folded into another risk factor",
      "Not matched",
      "could not settle",
      "never shown as removed",
      "No matching risk factor found in the earlier filing",
      "6 of 12",
      "already disclosed earlier",
      "wording was not found",
      "6 of the 51",
      "1 of 48",
      "different words",
      "small",
    ]) {
      expect(text, phrase).toContain(phrase);
    }
  });

  it("opens with a plain lead for a newcomer, before the technical explanation (RD27)", () => {
    const { container } = mount();
    const text = (container.textContent ?? "").replace(/\s+/g, " ");
    const lead = "Answers are written from passages of the companies' SEC filings, their reported figures and US government rules, and cite them, so you can open each source.";
    expect(text).toContain(lead);
    expect(text.indexOf(lead)).toBeLessThan(text.indexOf(joined(copy.INTRO)));
  });

  it("renders each pinned passage whole, in its own words", () => {
    const { container } = mount();
    const text = (container.textContent ?? "").replace(/\s+/g, " ");
    expect(text).toContain(joined(copy.INTRO));
    expect(text).toContain(joined(copy.WITHDRAWN));
    expect(text).toContain(copy.CHECKS_HINT);
    expect(text).toContain(joined(copy.RELIABILITY));
  });

  it("states the reliability counts as counts, never as percentages", () => {
    mount();
    const section = screen.getByRole("region", { name: "How reliable are change claims?" });
    expect(section.textContent).not.toContain("%");
  });

  it("makes no withdrawn or user-count claim (S15)", () => {
    expect(BANNED).toHaveLength(15);
    const { container } = mount();
    const text = (container.textContent ?? "").toLowerCase();
    const source = JSON.stringify(copy).toLowerCase();
    for (const phrase of BANNED) {
      expect(text, phrase).not.toContain(phrase.toLowerCase());
      expect(source, phrase).not.toContain(phrase.toLowerCase());
    }
  });

  it("puts the reliability note last, as the checks hint says (at the bottom of the page)", () => {
    mount();
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings.at(-1)).toBe("How reliable are change claims?");
    const sections = screen.getAllByRole("region");
    expect(sections.at(-1)).toBe(screen.getByRole("region", { name: "How reliable are change claims?" }));
  });

  it("has the two static links, each safe, and one internal link to the Freshness page", () => {
    const { container } = mount();
    const links = [...container.querySelectorAll("a")];
    const external = links.filter((a) => a.getAttribute("target") === "_blank");
    expect(external.map((a) => a.getAttribute("href"))).toEqual([
      "https://github.com/amit-badave-04/ai-semiconductor-risk-intelligence-graphrag/blob/v2/docs/v2/M1B_PLAN.md",
      "https://github.com/amit-badave-04/ai-semiconductor-risk-intelligence-graphrag/blob/v2/docs/v2/REVIEW_2026-09-26.md",
    ]);
    for (const a of external) {
      expect(a.getAttribute("rel")?.split(" ").sort()).toEqual(["noopener", "noreferrer"]);
    }
    expect(links).toHaveLength(3);
    expect(container.querySelector("[src], [style], [onclick]")).toBeNull();
  });

  it("links to the Freshness page by its navigation label, as a route link (R27)", () => {
    mount();
    const link = screen.getByRole("link", { name: "Freshness" });
    expect(link).toHaveAttribute("href", "/freshness/");
    expect(link).not.toHaveAttribute("target");
  });

  it("has no heading 'Earlier accuracy figures': the withdrawn notice is an unheaded note", () => {
    const { container } = mount();
    expect(screen.queryByRole("heading", { name: "Earlier accuracy figures" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Earlier accuracy figures" })).toBeNull();
    const note = [...container.querySelectorAll("p")].find((p) => p.textContent?.includes("withdrawn"));
    expect(note).toBeDefined();
    expect(note?.closest("section")).toBeNull();
    expect(note?.textContent).toBe(joined(copy.WITHDRAWN));
  });

  it("shows bold runs as bold", () => {
    const { container } = mount();
    const bold = [...container.querySelectorAll("b")].map((b) => b.textContent);
    expect(bold).toContain("bitemporal knowledge graph");
    expect(bold).toContain("keyword matches to external Federal Register rules");
  });
});
