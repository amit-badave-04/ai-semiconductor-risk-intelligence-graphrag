import { describe, expect, it } from "vitest";
import { deck } from "@/copy/deck";

// docs/specs/m2a-answer-integrity.md P4 and OD7 (the owner's wording records of 2026-11-21): the "Since our data" words, word for word.

const s = deck.since;

describe("the Since our data words", () => {
  it("holds the approved strings word for word", () => {
    expect(s.heading).toBe("Since our data");
    expect(s.filingsHeading("Nov 5, 2026")).toBe("New company filings since Nov 5, 2026");
    expect(s.rulesHeading("Nov 5, 2026")).toBe("New BIS rules since Nov 5, 2026");
    expect(s.ruleRow("Nov 18, 2026", "Entity List Additions")).toBe("Nov 18, 2026 Entity List Additions");
    expect(s.nothingNew).toBe("Nothing new since our data");
    expect(s.notChecked).toBe("Not checked yet");
    expect(s.companyHeading).toBe("New filings since our data");
    expect(s.showingOf(5, 7)).toBe("showing 5 of 7");
    expect(s.notLinked(3, false)).toBe("3 new BIS rules, not linked to companies yet");
    expect(s.notLinked(1, false)).toBe("1 new BIS rule, not linked to companies yet");
    expect(s.notLinked(20, true)).toBe("20+ new BIS rules, not linked to companies yet");
    expect(s.companyRules(2, false)).toBe("2 new BIS rules since our data \u2014 see Freshness");
    expect(s.companyRules(1, false)).toBe("1 new BIS rule since our data \u2014 see Freshness");
    expect(s.companyRules(20, true)).toBe("20+ new BIS rules since our data \u2014 see Freshness");
  });

  it("words a filing row with the right article and the labels joined by a middle dot", () => {
    const row = (form: string, labels: string[]) => s.filingRow({ company: "Nvidia", form, date: "Nov 19, 2026", labels });
    expect(row("8-K", ["Results of Operations and Financial Condition", "Financial Statements and Exhibits"])).toBe(
      "Nvidia filed an 8-K on Nov 19, 2026: Results of Operations and Financial Condition \u00b7 Financial Statements and Exhibits",
    );
    expect(row("8-K/A", ["Other Events"])).toBe("Nvidia filed an 8-K/A on Nov 19, 2026: Other Events");
    expect(row("6-K", [])).toBe("Nvidia filed a 6-K on Nov 19, 2026");
  });

  it("names every item code from 1.01 to 9.01", () => {
    const codes = ["1.01", "1.02", "1.03", "1.04", "1.05", "2.01", "2.02", "2.03", "2.04", "2.05", "2.06", "3.01", "3.02", "3.03", "4.01", "4.02",
      "5.01", "5.02", "5.03", "5.04", "5.05", "5.06", "5.07", "5.08", "6.01", "6.02", "6.03", "6.04", "6.05", "6.06", "7.01", "8.01", "9.01"];
    expect(codes).toHaveLength(33);
    for (const code of codes) expect(s.itemLabel(code)).not.toBe(code);
    expect(s.itemLabel("1.04")).toBe("Mine Safety \u2013 Reporting of Shutdowns and Patterns of Violations");
    expect(s.itemLabel("4.01")).toBe("Changes in Registrant\u2019s Certifying Accountant");
    expect(s.itemLabel("5.05")).toBe("Amendments to the Registrant\u2019s Code of Ethics, or Waiver of a Provision of the Code of Ethics");
    expect(s.itemLabel("9.01")).toBe("Financial Statements and Exhibits");
  });

  it("an unknown code, and constructor, is shown as written", () => {
    expect(s.itemLabel("9.99")).toBe("9.99");
    expect(s.itemLabel("constructor")).toBe("constructor");
    expect(s.itemLabel("__proto__")).toBe("__proto__");
  });
});
