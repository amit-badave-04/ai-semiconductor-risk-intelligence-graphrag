import { describe, expect, it } from "vitest";
import { deck } from "@/copy/deck";
import { statusView } from "../askStatus";
import { stepView } from "../steps";

// docs/specs/m5b-redesign.md RD10 and section 4: the status line and the agent's steps as codes the deck words plainly. The legacy
// statusLine, retrievalStatus and stepText stay as they are (their differential tests are untouched); these are sibling views.

const base = { status: "idle", strategy: "hybrid", retrieval: null, escalated: null, done: null, statusText: null } as const;
const line = (ask: Parameters<typeof statusView>[0]) => deck.status.line(statusView(ask));

describe("statusView", () => {
  it("is empty before an ask, and an error carries the server's own words", () => {
    expect(statusView(base)).toEqual({ code: "idle", isError: false });
    expect(line(base)).toBe("");
    const error = statusView({ ...base, statusText: "Live questions are paused right now." });
    expect(error).toEqual({ code: "error", isError: true, params: { text: "Live questions are paused right now." } });
    expect(line({ ...base, statusText: "Live questions are paused right now." })).toBe("Live questions are paused right now.");
  });

  it("while streaming: looking, the agent choosing, what was found, a stronger model answering again", () => {
    expect(line({ ...base, status: "streaming" })).toBe("Looking in the filings…");
    expect(line({ ...base, status: "streaming", strategy: "agent" })).toBe("The research agent is choosing where to look…");
    const retrieval = { event: "retrieval" as const, anchors: { Nvidia: 1, TSMC: 2 }, counts: { chunks: 8, edges: 1, metrics: 0 } };
    expect(statusView({ ...base, status: "streaming", retrieval })).toEqual({
      code: "retrieval",
      isError: false,
      params: { anchors: ["Nvidia", "TSMC"], defaulted: false, passages: 8, figures: 0, relationships: 1, risks: 0, changes: 0 },
    });
    expect(line({ ...base, status: "streaming", retrieval })).toBe("Found 8 passages and 1 relationship about Nvidia and TSMC. Writing the answer…");
    expect(line({ ...base, status: "streaming", escalated: { event: "escalated", reasons: ["ungrounded_number"], from: "a", to: "b" } })).toBe(
      "The first draft did not pass the automatic checks, so a stronger model is answering again…",
    );
  });

  it("words what was found in plain English, with plurals, lists and the defaulted company", () => {
    const at = (counts: Record<string, number>, anchors: Record<string, number> = { AMD: 1 }, defaulted = false) =>
      line({ ...base, status: "streaming", retrieval: { event: "retrieval", anchors, counts, anchor_defaulted: defaulted } });
    expect(at({ chunks: 1 })).toBe("Found 1 passage about AMD. Writing the answer…");
    expect(at({ chunks: 3, metrics: 2, edges: 4, risks: 5, temporal: 6 })).toBe(
      "Found 3 passages, 2 reported figures, 4 relationships, 5 risk disclosures and 6 risk changes about AMD. Writing the answer…",
    );
    expect(at({}, {})).toBe("Writing the answer…");
    expect(at({ chunks: 2 }, { Nvidia: 1 }, true)).toBe("No company was named, so this looks at Nvidia. Found 2 passages about Nvidia. Writing the answer…");
    expect(at({ chunks: 2 }, { Nvidia: 1, AMD: 2, TSMC: 3 })).toBe("Found 2 passages about Nvidia, AMD and TSMC. Writing the answer…");
  });

  it("at the end: a saved answer, an answer given earlier, or done", () => {
    const done = (extra: Record<string, unknown>) => ({ ...base, status: "ended" as const, done: { event: "done" as const, answer: "a", citations: [], hallucinated: [], ...extra } });
    expect(line(done({ cached: true, source: "benchmark" }))).toBe("Saved answer, shown instantly.");
    expect(line(done({ cached: true, source: "live" }))).toBe("Answered earlier, shown instantly.");
    expect(line(done({ cached: true }))).toBe("Answered earlier, shown instantly.");
    expect(line(done({}))).toBe("Done.");
  });

  it("never reads a count that is not a number", () => {
    const view = statusView({ ...base, status: "streaming", retrieval: { event: "retrieval", anchors: { AMD: 1 }, counts: { chunks: "8" as unknown as number } } });
    expect(view.params !== undefined && "passages" in view.params ? view.params.passages : null).toBe(0);
  });
});

describe("stepView", () => {
  it("words each known tool as the legacy sentence does, with the bounded summary and a failure", () => {
    expect(deck.steps.line(stepView({ event: "step", n: 1, tool: "search_filings", summary: "8 passages", ok: true }))).toBe("Searched the filings: 8 passages");
    expect(deck.steps.line(stepView({ event: "step", n: 2, tool: "lookup_company", summary: "Nvidia", ok: true }))).toBe("Looked up Nvidia");
    expect(deck.steps.line(stepView({ event: "step", n: 3, tool: "risk_changes", summary: "", ok: false }))).toBe("Compared risk disclosures (failed)");
  });

  it("names an unknown tool plainly and never trusts a tool name outside the grammar", () => {
    expect(deck.steps.line(stepView({ event: "step", n: 1, tool: "new_tool", summary: "x", ok: true }))).toBe("Ran new tool: x");
    expect(deck.steps.line(stepView({ event: "step", n: 1, tool: "<b>bad</b>", summary: "", ok: true }))).toBe("Ran a tool");
    expect(stepView(null)).toEqual({ code: "unknown", params: { tool: "", summary: "", failed: false } });
  });
});
