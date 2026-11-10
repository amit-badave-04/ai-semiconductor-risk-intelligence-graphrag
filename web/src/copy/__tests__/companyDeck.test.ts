import { describe, expect, it } from "vitest";
import { deck } from "@/copy/deck";

// docs/specs/m5b-redesign.md R3 (RD19-RD25), decisions D-R3 and D-R4, and docs/decisions/001-r3-company-copy.md: the Company
// page's words. The approved sentences are pinned exactly; server values arrive as parameters and are rendered as text.

const c = deck.company;

describe("the Company page's words", () => {
  it("states the reports in words (RD19)", () => {
    expect(c.header.annual({ form: "10-K", yearEnded: "Jan 25, 2026", filed: "Feb 25, 2026" })).toBe("10-K for the fiscal year ended Jan 25, 2026, filed Feb 25, 2026");
    expect(c.header.annual({ form: "10-K", yearEnded: null, filed: "Feb 25, 2026" })).toBe("10-K filed Feb 25, 2026");
    expect(c.header.quarterly({ form: "10-Q", filed: "Aug 27, 2026" })).toBe("10-Q filed Aug 27, 2026");
    expect(c.header.annualOnly).toBe("Files an annual report on Form 20-F; no quarterly 10-Q");
    expect(c.header.askAbout("Nvidia")).toBe("Ask about Nvidia");
    expect(c.header.compare).toBe("Compare filings");
    expect(c.header.askPrefill("Nvidia")).toBe("What risks does Nvidia describe in its latest annual report?");
  });

  it("labels the section navigation (RD21) and its sections", () => {
    expect(c.nav.label).toBe("On this page");
    expect([c.nav.keyNumbers, c.nav.rules, c.nav.relationships, c.nav.filings, c.nav.changes]).toEqual([
      "Key numbers",
      "Government rules",
      "Relationships",
      "Filings",
      "Risk changes",
    ]);
  });

  it("words the numbers, the years and what we do not hold (RD20, RD22, D-R4, decision 1)", () => {
    expect(c.numbers.notHeld("FY2012")).toBe("Not in our data for these years. The latest we hold is FY2012.");
    expect(c.numbers.absent).toBe("Not in our data for these years.");
    expect(c.numbers.fiscalNote).toBe("Fiscal years are the company's own. Each column shows the date its year ended.");
    expect(c.numbers.exact).toBe("Show exact values");
    expect(c.numbers.metric.revenue).toEqual({ name: "Revenue", meaning: "Money from sales" });
    expect(Object.keys(c.numbers.metric)).toEqual(["revenue", "net_income", "rnd", "capex"]);
    expect(c.glance.rulesCount(1)).toBe("1 government rule listed below");
    expect(c.glance.rulesCount(8)).toBe("8 government rules listed below");
  });

  it("frames the rules as the government's (RD23, D-R3)", () => {
    expect(c.rules.notice).toMatch(/Federal Register/);
    expect(c.rules.notice).toMatch(/keyword match/);
    expect(c.rules.notice).toMatch(/export-control/);
    expect(c.rules.notice).toMatch(/not statements the company made/);
    expect(c.rules.none("ASML")).toBe("No export-control rule in our data was linked to ASML by keyword matching. This is not a finding that none applies.");
  });

  it("words relationships without a relation name (RD24) and labels active risks as summaries (D-R4)", () => {
    expect(c.relationships.ownWords).toBe("In the filing's own words");
    expect(c.relationships.competitors).toBe("Competitors named in its filings");
    expect(JSON.stringify(c.relationships)).not.toMatch(/_OF|_TO|_ON|_WITH/);
    expect(c.risks.label).toBe("Summaries written by semigraph, not quotes");
  });

  it("names a figure stamp with its visible text first (WCAG 2.5.3)", () => {
    const name = c.numbers.stampName({ shown: "$215.9B", metric: "Revenue", year: "FY2026 (ended Jan 25, 2026)", exact: "$215,938,000,000" });
    expect(name).toBe("$215.9B, Revenue, FY2026 (ended Jan 25, 2026): $215,938,000,000. Open source");
    const exact = c.numbers.stampName({ shown: "$215,938,000,000", metric: "Revenue", year: "FY2026 (ended Jan 25, 2026)", exact: "$215,938,000,000" });
    expect(exact).toBe("$215,938,000,000, Revenue, FY2026 (ended Jan 25, 2026). Open source");
  });
});
