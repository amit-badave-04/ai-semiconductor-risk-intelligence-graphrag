import { describe, expect, it } from "vitest";
import { NEVER_STARTERS, STARTERS, splitExamples } from "../starters";

// docs/specs/m5b-redesign.md RD5 and decision D-R6: six starters from a curated id list (N2, D3, Q2, R1, T7, NG15) intersected
// with GET /api/examples, grouped by job; D1 and X4 are never starters; a curated id the server does not list is dropped; the
// rest are listed by company (the first covered company the question names, in the order the question names them).

const ex = (id: string, question: string, type = "t") => ({ id, type, question });
const PAYLOAD = {
  examples: [
    ex("N1", "What was Nvidia's revenue in 2024?"),
    ex("N2", "What was Nvidia's total revenue for the fiscal year ended January 25, 2026?", "numeric"),
    ex("D1", "Which suppliers does Nvidia depend on?", "dependency"),
    ex("D3", "Which foundries does AMD rely on to manufacture its chips?", "dependency"),
    ex("Q2", "What geopolitical risks does ASML disclose?", "risk"),
    ex("R1", "Which BIS export-control rules is AMD affected by?", "regulatory"),
    ex("T7", "Did Nvidia remove any risk factors between its FY2024 and FY2025 annual reports?", "temporal"),
    ex("NG15", "What were Micron's total revenue and capital expenditures for the fiscal year ended August 28, 2025?", "numeric"),
    ex("X4", "Compare the competition risks disclosed by AMD and Nvidia.", "compare"),
    ex("U1", "Which semiconductor equipment makers are mentioned?", "other"),
  ],
};

describe("splitExamples", () => {
  it("takes the six curated starters, grouped by job in the order understand, check, track, see what changed", () => {
    const { groups } = splitExamples(PAYLOAD);
    expect(groups.map((g) => [g.job, g.examples.map((e) => e.id)])).toEqual([
      ["company", ["D3", "Q2"]],
      ["number", ["N2", "NG15"]],
      ["rules", ["R1"]],
      ["changes", ["T7"]],
    ]);
  });

  it("never makes D1 or X4 a starter, and the curated list names neither", () => {
    expect(NEVER_STARTERS).toEqual(["D1", "X4"]);
    expect(STARTERS.map((s) => s.id)).toEqual(["N2", "D3", "Q2", "R1", "T7", "NG15"]);
    const ids = splitExamples(PAYLOAD).groups.flatMap((g) => g.examples.map((e) => e.id));
    expect(ids).not.toContain("D1");
    expect(ids).not.toContain("X4");
  });

  it("drops a curated id the server does not list, and a job left with none", () => {
    const { groups } = splitExamples({ examples: PAYLOAD.examples.filter((e) => e.id !== "R1") });
    expect(groups.map((g) => g.job)).toEqual(["company", "number", "changes"]);
  });

  it("lists every other example by the first company its question names, in the covered companies' order, the rest last", () => {
    const { byCompany } = splitExamples(PAYLOAD);
    expect(byCompany.map((c) => [c.company, c.examples.map((e) => e.id)])).toEqual([
      ["Nvidia", ["N1", "D1"]],
      ["AMD", ["X4"]],
      ["Other", ["U1"]],
    ]);
  });

  it("drops an entry without a string id or question, and reads a malformed payload as no examples", () => {
    const { groups, byCompany } = splitExamples({ examples: [{ id: 3, question: "x" }, { id: "N2" }, null, "N2"] });
    expect(groups).toEqual([]);
    expect(byCompany).toEqual([]);
    expect(splitExamples(null)).toEqual({ groups: [], byCompany: [] });
    expect(splitExamples({ examples: "nope" })).toEqual({ groups: [], byCompany: [] });
  });

  it("matches a company name as a whole word, so 'Metadata' is not Meta", () => {
    const { byCompany } = splitExamples({ examples: [ex("Z1", "How is metadata about Meta's chips stored?")] });
    expect(byCompany[0].company).toBe("Meta");
    const { byCompany: none } = splitExamples({ examples: [ex("Z2", "How is metadata stored?")] });
    expect(none[0].company).toBe("Other");
  });
});
