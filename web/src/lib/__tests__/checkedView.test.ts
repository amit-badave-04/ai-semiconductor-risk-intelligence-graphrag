import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { deck } from "@/copy/deck";
import { checkedView, type CheckedLine } from "../answerMeta";

// docs/specs/m5b-redesign.md RD14 and section 4: "What was checked" as codes the UI words from the deck. Three cases, read from
// the done event: a saved benchmark answer (cached, source "benchmark"), a repeated live answer from the cache (cached, any other
// source: no checks are stored), and a live answer (its checks, line by line, never claiming more than they do). The legacy
// badges (checksSummary) stay as they are; this is a sibling view over the same rules.

const codes = (lines: CheckedLine[]) => lines.map((l) => l.code);
const live = (checks: Record<string, unknown> | undefined, extra: Record<string, unknown> = {}) =>
  checkedView({ event: "done", answer: "a", citations: ["x"], hallucinated: [], checks, ...extra });

describe("checkedView: the three cases", () => {
  it("a saved benchmark answer says only that it was checked when saved", () => {
    const view = checkedView({ event: "done", cached: true, source: "benchmark", answer: "a", citations: ["x"], hallucinated: [] });
    expect(view.kind).toBe("saved");
    expect(codes(view.lines)).toEqual(["saved"]);
  });

  it("a repeated live answer from the cache makes no claim either way, whatever its source says", () => {
    for (const source of ["live", undefined, "other"]) {
      const view = checkedView({ event: "done", cached: true, source, answer: "a", citations: ["x"], hallucinated: [] });
      expect(view.kind).toBe("earlier");
      expect(codes(view.lines)).toEqual(["earlier"]);
    }
  });

  it("a live answer reads its checks", () => {
    expect(live({ citations_retrieved: true, has_citation: true, numbers_checked: 3, numbers_grounded: true }).kind).toBe("live");
  });
});

describe("checkedView: a live answer's lines", () => {
  it("all sources retrieved and all figures matched", () => {
    const view = live({ citations_retrieved: true, has_citation: true, numbers_checked: 3, numbers_grounded: true });
    expect(view.lines).toEqual([
      { code: "sourcesOk", tone: "ok" },
      { code: "figuresMatched", tone: "ok" },
    ]);
  });

  it("no figures to check, and figures not checked when the count is missing", () => {
    expect(codes(live({ has_citation: true, numbers_checked: 0 }).lines)).toContain("figuresNone");
    expect(codes(live({ has_citation: true }).lines)).toContain("figuresUnchecked");
    expect(codes(live(undefined).lines)).toEqual(["sourcesOk", "figuresUnchecked"]);
  });

  it("unmatched and echoed figures, with the figures as parameters", () => {
    expect(live({ unmatched_numbers: ["$3.1B"], numbers_checked: 2 }).lines).toContainEqual({ code: "figuresUnmatched", tone: "bad", params: { figures: "$3.1B" } });
    expect(live({ numbers_grounded: false, numbers_checked: 2 }).lines).toContainEqual({ code: "figuresUnmatched", tone: "bad", params: { figures: "" } });
    expect(live({ echoed_numbers: ["42%"], numbers_checked: 1 }).lines).toContainEqual({ code: "figuresEchoed", tone: "warn", params: { figures: "42%" } });
  });

  it("sources missing, no citation, a refusal, a pseudo-citation, a removal claim and a cut-short answer", () => {
    expect(live({}, { hallucinated: ["a", "b"] }).lines[0]).toEqual({ code: "sourcesMissing", tone: "bad", params: { count: 2 } });
    expect(live({ citations_retrieved: false }).lines[0]).toEqual({ code: "sourcesMissing", tone: "bad", params: { count: 0 } });
    expect(live({ has_citation: false }).lines[0]).toEqual({ code: "noCitation", tone: "warn" });
    expect(live({ has_citation: false, is_refusal: true }).lines[0]).toEqual({ code: "declines", tone: "plain" });
    expect(live({ pseudo_citations: ["[source 3]"] }).lines).toContainEqual({ code: "pseudoCitation", tone: "warn", params: { items: "[source 3]" } });
    expect(codes(live({ unsupported_removal_claim: true }).lines)).toContain("removalClaim");
    expect(codes(live({}, { finish_reason: "length" }).lines)).toContain("truncated");
  });

  it("never throws on a malformed event, and an empty one reads as a live answer with nothing checked", () => {
    for (const bad of [null, undefined, 3, "x", [], { checks: "nope" }]) expect(() => checkedView(bad)).not.toThrow();
    expect(codes(checkedView({}).lines)).toEqual(["figuresUnchecked"]);
  });
});

describe("the deck words every code, plainly", () => {
  const all: CheckedLine[] = [
    { code: "saved", tone: "plain" },
    { code: "earlier", tone: "plain" },
    { code: "sourcesOk", tone: "ok" },
    { code: "sourcesMissing", tone: "bad", params: { count: 2 } },
    { code: "sourcesMissing", tone: "bad", params: { count: 0 } },
    { code: "noCitation", tone: "warn" },
    { code: "declines", tone: "plain" },
    { code: "figuresMatched", tone: "ok" },
    { code: "figuresUnmatched", tone: "bad", params: { figures: "$3.1B" } },
    { code: "figuresUnmatched", tone: "bad", params: { figures: "" } },
    { code: "figuresNone", tone: "plain" },
    { code: "figuresEchoed", tone: "warn", params: { figures: "42" } },
    { code: "figuresUnchecked", tone: "plain" },
    { code: "pseudoCitation", tone: "warn", params: { items: "[x]" } },
    { code: "removalClaim", tone: "warn" },
    { code: "truncated", tone: "warn" },
  ];

  it("has a sentence for every code, never 'verified', never a percentage claim", () => {
    for (const line of all) {
      const text = deck.checked.line(line);
      expect(text.length, line.code).toBeGreaterThan(10);
      expect(text).not.toMatch(/verified|guarantee|accurate/i);
      expect(text.replace(String(line.params?.figures ?? ""), "")).not.toMatch(/%/);
    }
  });

  it("words the three cases as the spec does", () => {
    expect(deck.checked.line({ code: "saved", tone: "plain" })).toBe("Checked when the answer was saved. Open a source to read it.");
    expect(deck.checked.line({ code: "earlier", tone: "plain" })).toBe(
      "Answered earlier. The checks are shown only when an answer is first written; open a source to read it.",
    );
    expect(deck.checked.line({ code: "figuresMatched", tone: "ok" })).toBe("All figures matched the filings.");
    expect(deck.checked.line({ code: "figuresUnmatched", tone: "bad", params: { figures: "" } })).toBe("Some figures could not be matched.");
    expect(deck.checked.line({ code: "figuresNone", tone: "plain" })).toBe("No dollar amounts or percentages in this answer.");
  });

  it("reads the recorded live answers without a gap: every line of each has a sentence", () => {
    const dir = path.resolve(import.meta.dirname, "../../../../tests/data/sse");
    for (const name of ["live_hybrid", "escalated", "cached_done"]) {
      const events = (JSON.parse(readFileSync(path.join(dir, `${name}.expected.json`), "utf-8")) as { events: { event: string }[] }).events;
      const done = events.find((e) => e.event === "done");
      for (const line of checkedView(done).lines) expect(deck.checked.line(line).length).toBeGreaterThan(10);
    }
  });
});
