import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { evidenceBody } from "../evidenceView";
import { SPECIMENS } from "../specimens";

// docs/specs/m5b-redesign.md RD17: the parts of the evidence panel as plain data (a sibling of evidenceView, whose legacy fields
// and differential test are unchanged): a heading; Section / Filed / Status; an "Older filing" band when the filing was replaced;
// the passage (highlighted only for a filing passage, RD18); the source link (through safeUrl); and the ids for analysts. The
// payload is untrusted: plain strings out, a missing field an empty part, never a throw.

const recorded = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../test/fixtures/specimens.recorded.json"), "utf-8")) as {
  payloads: Record<string, Record<string, unknown>>;
};
const passage = recorded.payloads[SPECIMENS.passage];
const number = recorded.payloads[SPECIMENS.number];
const rule = recorded.payloads[SPECIMENS.rule];

describe("evidenceBody: a filing passage", () => {
  it("names the filer, form and section, lists Section, Filed and Status, and links the filing", () => {
    const body = evidenceBody(SPECIMENS.passage, passage);
    expect(body.heading).toBe("Nvidia 10-K, Item 1A. Risk Factors");
    expect(body.facts).toEqual([
      { code: "section", value: "Item 1A. Risk Factors" },
      { code: "filed", value: "Feb 21, 2024" },
      { code: "status", value: "superseded" },
    ]);
    expect(body.link).toEqual({ href: passage.source_url, code: "filing" });
    expect(body.passage).toBe(passage.text);
    expect(body.highlightable).toBe(true);
  });

  it("says when the filing was replaced, with the date it stopped being current, and names the newer filing only for analysts", () => {
    const body = evidenceBody(SPECIMENS.passage, passage);
    expect(body.superseded).toEqual({ until: "Feb 26, 2025" });
    expect(body.details).toContainEqual({ code: "replacedBy", value: "0001045810-25-000023" });
    expect(body.details).toContainEqual({ code: "id", value: SPECIMENS.passage });
  });

  it("shows a replacement accession only when it has the accession form, and a current filing has no band", () => {
    const forged = evidenceBody(SPECIMENS.passage, { ...passage, superseded_by: "see https://evil.example" });
    expect(forged.details.find((d) => d.code === "replacedBy")).toBeUndefined();
    const current = evidenceBody(SPECIMENS.passage, { ...passage, status: "current", is_current: true, superseded_by: null, valid_to: null });
    expect(current.superseded).toBeNull();
    expect(current.facts.find((f) => f.code === "status")?.value).toBe("current");
  });
});

describe("evidenceBody: a reported number and a government rule", () => {
  it("a number: company, metric and fiscal year, the figure compact and exact, the period and filing", () => {
    const body = evidenceBody(SPECIMENS.number, number);
    expect(body.heading).toBe("Nvidia revenue, FY2026 (ended Jan 25, 2026)");
    expect(body.figure).toEqual({ compact: "$215.9B", exact: "$215,938,000,000" });
    expect(body.facts).toEqual([
      { code: "period", value: "Jan 27, 2025 to Jan 25, 2026" },
      { code: "form", value: "10-K" },
      { code: "filed", value: "Feb 25, 2026" },
    ]);
    expect(body.highlightable).toBe(false);
  });

  it("a rule: the government's, never the company's, with its publication date, abstract and link", () => {
    const body = evidenceBody(SPECIMENS.rule, rule);
    expect(body.rule).toBe(true);
    expect(body.heading).toMatch(/^Implementation of Additional Due Diligence/);
    expect(body.facts).toEqual([{ code: "published", value: "Jan 16, 2025" }]);
    expect(body.passage).toBe(rule.abstract);
    expect(body.link?.code).toBe("rule");
    expect(body.highlightable).toBe(false);
  });
});

describe("evidenceBody: untrusted payloads", () => {
  it("keeps hostile text as plain strings, drops a link that is not https, and never throws", () => {
    const evil = '<img src=x onerror="alert(1)">';
    const body = evidenceBody(SPECIMENS.passage, { ...passage, filer: evil, section_title: evil, text: evil, source_url: `${"java"}script:alert(1)` });
    expect(body.heading).toContain(evil);
    expect(body.passage).toBe(evil);
    expect(body.link).toBeNull();
    for (const bad of [null, undefined, 3, "x", [], {}]) expect(() => evidenceBody(SPECIMENS.passage, bad)).not.toThrow();
    expect(evidenceBody(SPECIMENS.number, {}).figure).toBeNull();
  });
});
