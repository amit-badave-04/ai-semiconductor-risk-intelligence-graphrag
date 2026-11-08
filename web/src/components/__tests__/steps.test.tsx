import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Steps } from "@/components/Steps";
import type { StepEvent } from "@/lib/events";
import { MAX_STEP_CHARS, MAX_STEPS_SHOWN, agentEnabled, stepText, tracingNote } from "@/lib/steps";

// tests/ui/agent.test.mjs, the step half (stepText, agentEnabled, tracingNote and the 4 timeline titles), ported; the strategy
// option, hint and privacy-line titles are ported with the Ask page (pages/__tests__/ask-agent.test.tsx). Each title is the
// legacy title, character for character.
const STEP = (over: Record<string, unknown> = {}): StepEvent =>
  ({ event: "step", n: 1, tool: "lookup_company", args: {}, summary: "Nvidia", ok: true, ...over }) as StepEvent;
const odd = (value: unknown) => value as StepEvent;

describe("tests/ui/agent.test.mjs: the plain-language line of one step", () => {
  it("a step reads as a short plain sentence built from the tool and the summary", () => {
    expect(stepText(STEP())).toBe("Looked up Nvidia");
    expect(stepText(STEP({ tool: "financial_metrics", summary: "revenue for FY2024-2026" }))).toBe("Fetched revenue for FY2024-2026");
    expect(stepText(STEP({ tool: "search_filings", summary: "6 excerpts" }))).toBe("Searched the filings: 6 excerpts");
    expect(stepText(STEP({ tool: "risk_changes", summary: "Nvidia FY2025 to FY2026" }))).toBe("Compared risk disclosures: Nvidia FY2025 to FY2026");
    expect(stepText(STEP({ tool: "relationships", summary: "12 links" }))).toBe("Checked relationships: 12 links");
    expect(stepText(STEP({ tool: "active_risks", summary: "9 items" }))).toBe("Listed active risks: 9 items");
    expect(stepText(STEP({ tool: "compute_change", summary: "revenue change FY2026 vs FY2025" }))).toBe("Computed revenue change FY2026 vs FY2025");
  });

  it("a failed step says so, a step without a summary is just the verb, an unknown tool gets a generic verb", () => {
    expect(stepText(STEP({ tool: "search_filings", summary: "no excerpts", ok: false }))).toBe("Searched the filings: no excerpts (failed)");
    expect(stepText(STEP({ summary: "" }))).toBe("Looked up");
    expect(stepText(STEP({ tool: "brand_new_tool", summary: "3 items" }))).toBe("Ran brand new tool: 3 items");
    expect(stepText(STEP({ tool: "<img src=x onerror=alert(1)>" }))).toBe("Ran a tool: Nvidia");
    expect(stepText(STEP({ tool: "constructor" }))).toBe("Ran constructor: Nvidia"); // an Object.prototype name is not a known tool
    expect(stepText(STEP({ tool: "__proto__" }))).toBe("Ran a tool: Nvidia");
  });

  it("a step is defensive about the shape of what the server sent", () => {
    expect(stepText(odd(null))).toBe("Ran a tool");
    expect(stepText(odd({}))).toBe("Ran a tool");
    expect(stepText(STEP({ summary: { evil: 1 } }))).toBe("Looked up");
    expect(stepText(STEP({ summary: "line one\n\n  line two\t!" }))).toBe("Looked up line one line two !");
    const long = stepText(STEP({ summary: "x".repeat(500) }));
    expect(long.length).toBeLessThan(200);
    expect(long.endsWith("\u2026")).toBe(true);
  });

  it("server text is returned as plain text for textContent, never escaped into markup and never interpreted", () => {
    const hostile = "<b>bold</b> & <script>alert(1)</script>";
    expect(stepText(STEP({ summary: hostile }))).toBe(`Looked up ${hostile}`);
  });
});

describe("tests/ui/agent.test.mjs: agentEnabled and the privacy line", () => {
  it("agentEnabled is true only for an explicit true", () => {
    expect(agentEnabled({ agent_enabled: true })).toBe(true);
    for (const s of [{}, { agent_enabled: false }, { agent_enabled: "true" }, { agent_enabled: 1 }, null, undefined]) expect(agentEnabled(s)).toBe(false);
  });

  it("the privacy line appears only when a sample of agent questions is really traced, and never claims the text is sent", () => {
    const note = tracingNote({ agent_enabled: true, tracing: true });
    // Exactly what the tracer sends (tests/test_tracing.py): lengths and a one-way hash, tool and model names, tokens, cost, timings,
    // the fallback reason; never the text, never an address. "Sampling rate", not "a small sample": the rate is the operator's.
    expect(note).toMatch(/sampling rate/);
    expect(note).not.toMatch(/small sample/);
    for (const part of [/length of the question/, /one-way hash/, /tools and models/, /token counts/, /cost/, /timings/, /fallback reason/]) expect(note).toMatch(part);
    expect(note).toMatch(/never the text of the question or the answer, and never your address/);
    expect(tracingNote({ agent_enabled: true, tracing: false })).toBe("");
    expect(tracingNote({ agent_enabled: false, tracing: true })).toBe("");
  });
});

describe("tests/ui/agent.test.mjs: the timeline", () => {
  const items = () => screen.queryAllByRole("listitem").map((li) => li.textContent);
  const list = () => document.querySelector("ol");

  it("each step is one list item set through textContent, and the list becomes visible with the first step", () => {
    const { rerender } = render(<Steps steps={[]} />);
    expect(list()).toHaveAttribute("hidden");
    rerender(<Steps steps={[STEP(), STEP({ n: 2, tool: "financial_metrics", summary: "revenue for FY2024-2026" })]} />);
    expect(list()).not.toHaveAttribute("hidden");
    expect(items()).toEqual(["Looked up Nvidia", "Fetched revenue for FY2024-2026"]);
  });

  it("a hostile summary lands in textContent, so it is displayed as text and cannot run", () => {
    render(<Steps steps={[STEP({ summary: "<img src=x onerror=alert(1)>" })]} />);
    expect(document.querySelector("img")).toBeNull();
    expect(items()).toEqual(["Looked up <img src=x onerror=alert(1)>"]);
  });

  it("clearing the timeline empties and hides it, and a new answer starts from step one", () => {
    const { rerender } = render(<Steps steps={[STEP()]} />);
    rerender(<Steps steps={[]} />);
    expect(list()).toHaveAttribute("hidden");
    expect(items()).toEqual([]);
    rerender(<Steps steps={[STEP({ summary: "AMD" })]} />);
    expect(items()).toEqual(["Looked up AMD"]);
  });

  it("the timeline is bounded whatever the server streams", () => {
    const many = Array.from({ length: 50 }, (_, i) => STEP({ n: i + 1 }));
    render(<Steps steps={many} />);
    expect(items().length).toBeLessThanOrEqual(12);
    expect(MAX_STEPS_SHOWN).toBe(12);
    expect(MAX_STEP_CHARS).toBe(140);
  });

  it("keeps the polite live region and the accessible name of the legacy timeline", () => {
    render(<Steps steps={[STEP()]} />);
    const region = document.querySelector("[aria-live]");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveAttribute("aria-relevant", "additions");
    expect(screen.getByRole("list", { name: "What the research agent did" })).toBeInTheDocument();
  });
});
