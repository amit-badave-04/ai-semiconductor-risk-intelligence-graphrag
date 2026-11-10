import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { checkedView } from "../answerMeta";
import { evidenceBody } from "../evidenceView";
import { SPECIMENS } from "../specimens";

// The R2 review workflow's confirmed findings on lib code (wf_87d19659-645): a link names a host only when it goes there; a thin
// number payload keeps what its id says; server text in a "What was checked" line is bounded.

const recorded = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../test/fixtures/specimens.recorded.json"), "utf-8")) as {
  payloads: Record<string, Record<string, unknown>>;
};
const passage = recorded.payloads[SPECIMENS.passage];
const rule = recorded.payloads[SPECIMENS.rule];

describe("evidenceBody links", () => {
  it("names SEC.gov or FederalRegister.gov only when the link really goes there", () => {
    expect(evidenceBody(SPECIMENS.passage, passage).link).toEqual({ href: passage.source_url, code: "filing" });
    expect(evidenceBody(SPECIMENS.passage, { ...passage, source_url: "https://example.com/x.htm" }).link).toEqual({
      href: "https://example.com/x.htm",
      code: "source",
    });
    expect(evidenceBody(SPECIMENS.rule, { ...rule, url: "https://federalregister.gov.evil.example/x" }).link?.code).toBe("source");
    expect(evidenceBody(SPECIMENS.rule, rule).link?.code).toBe("rule");
  });
});

describe("evidenceBody for a thin number payload", () => {
  it("still names the metric and the period end its id gives", () => {
    const body = evidenceBody("xbrl:1045810:total_revenue:2026-01-25", { type: "xbrl", company: "Nvidia", value: 5, unit: "USD" });
    expect(body.heading).toBe("Nvidia total revenue, FY2026 (ended Jan 25, 2026)");
    expect(body.facts).toContainEqual({ code: "period", value: "Jan 25, 2026" });
  });
});

describe("checkedView bounds the server text a line names", () => {
  it("cuts the figures and items to at most 160 characters", () => {
    const many = Array.from({ length: 200 }, (_, i) => `$${i}.0B`);
    const view = checkedView({
      event: "done",
      answer: "a",
      citations: ["x"],
      hallucinated: [],
      checks: { unmatched_numbers: many, pseudo_citations: many, numbers_checked: 9 },
    });
    const values = view.lines.flatMap((line) => Object.values(line.params ?? {})).filter((v): v is string => typeof v === "string");
    expect(values.length).toBeGreaterThan(0);
    for (const value of values) expect(value.length).toBeLessThanOrEqual(161);
  });
});
