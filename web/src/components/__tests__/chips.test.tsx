import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerText } from "@/components/Chips";
import { chipLabel, classifyId } from "@/lib/citations";
import { CHUNK, DOC, FR, XBRL } from "./answerCorpus";

// tests/ui/chips.test.mjs (12 titles), ported: the grammar and the labels run through the shipped lib/citations.ts, the renderer
// through AnswerText. Each title is the legacy title, character for character.

function html(text: string): string {
  const { container } = render(<AnswerText text={text} place="answer" gen={1} />);
  return new XMLSerializer().serializeToString(container); // the serialized markup (the S1 scan forbids the HTML-string properties)
}

describe("tests/ui/chips.test.mjs", () => {
  it("citation ids are classified with the three-form grammar", () => {
    expect(classifyId(CHUNK)).toBe("chunk");
    expect(classifyId(XBRL)).toBe("xbrl");
    expect(classifyId(FR)).toBe("fr");
    expect(classifyId("fr:C1-2026-16628")).toBe("fr");
    expect(classifyId("Reported Metrics")).toBeNull();
    expect(classifyId("0361")).toBeNull();
    expect(classifyId(undefined)).toBeNull();
  });

  it("an uploaded-document id is the fourth form and gets a readable chip", () => {
    expect(classifyId(DOC)).toBe("doc");
    expect(classifyId("doc:0123456789AB:v2:0007")).toBeNull();
    expect(chipLabel(DOC)).toBe("your document v2 \u00b60007");
    render(<AnswerText text={`It says so [${DOC}].`} place="answer" gen={1} />);
    const chip = screen.getByRole("button", { name: "Your document, version 2 \u00b7 paragraph 7" });
    expect(chip).toHaveAttribute("data-id", DOC);
  });

  it("a chunk chip shows the item and paragraph, not a bare number", () => {
    expect(chipLabel(CHUNK)).toBe("Item 1A \u00b60361");
    expect(chipLabel("0000002488-26-000021:II.7:0012")).toBe("Item 7 \u00b60012");
  });

  it("once evidence has loaded a chunk chip also shows the form and period", () => {
    expect(chipLabel(CHUNK, { form: "10-K", filing_date: "2026-02-25" })).toBe("10-K filed 2026-02-25 \u00b7 Item 1A \u00b60361");
    expect(chipLabel(CHUNK, { form: "10-K", fiscal_year: 2026, filing_date: "2026-02-25" })).toBe("10-K FY2026 \u00b7 Item 1A \u00b60361");
    expect(chipLabel(CHUNK, {})).toBe("Item 1A \u00b60361");
    expect(chipLabel(CHUNK, null)).toBe("Item 1A \u00b60361");
  });

  it("XBRL and Federal Register chips are readable", () => {
    expect(chipLabel(XBRL)).toBe("XBRL revenue FY ended 2026-01-25");
    expect(chipLabel("xbrl:1045810:research_and_development:2026-01-25")).toBe("XBRL research and development FY ended 2026-01-25");
    expect(chipLabel(FR)).toBe("BIS rule 2026-19537");
    expect(chipLabel("fr:C1-2026-16628")).toBe("BIS rule C1-2026-16628");
  });

  it("an id outside the grammar is shown as it is", () => {
    expect(chipLabel("weird")).toBe("weird");
    expect(chipLabel(null)).toBe("");
  });

  it("renderMarkdown turns each of the three id forms into a labelled chip", () => {
    render(<AnswerText text={`Revenue was $215.9B [${XBRL}], see risk [${CHUNK}] and the rule [${FR}].`} place="answer" gen={1} />);
    expect(screen.getByRole("button", { name: "Filing 2026 \u00b7 paragraph 361" })).toHaveAttribute("data-id", CHUNK);
    expect(screen.getByRole("button", { name: "Reported revenue, period ended Jan 25, 2026" })).toHaveAttribute("data-id", XBRL);
    expect(screen.getByRole("button", { name: "Government rule 2026-19537" })).toHaveAttribute("data-id", FR);
  });

  it("bracketed text that is not a citation stays plain text", () => {
    render(<AnswerText text="Growth [Reported Metrics] and [0001045810-26-000021 lineage data]." place="answer" gen={1} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.getByText(/\[Reported Metrics\]/)).toBeInTheDocument();
  });

  it("two ids in one bracket are not a citation (one per bracket)", () => {
    render(<AnswerText text={`Both [${CHUNK}, ${XBRL}].`} place="answer" gen={1} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("model text cannot inject markup through the answer renderer", () => {
    const out = html(`<script>alert(1)</script> <img src=x onerror="alert(2)"> [${CHUNK}]`);
    expect(out).not.toMatch(/<script/i);
    expect(out).not.toMatch(/<img/i);
    expect(out).toContain("&lt;script&gt;");
    expect(document.querySelectorAll("button")).toHaveLength(1);
  });

  it("bold, bullet lists and headings still render", () => {
    const out = html("## Title\n\n- one **bold** item\n- two\n\nPlain.");
    expect(out).toContain("<h3>Title</h3>");
    expect(out).toContain("<ul><li>one <strong>bold</strong> item</li><li>two</li></ul>");
    expect(out).toContain("<p>Plain.</p>");
  });

  it("esc handles non-strings and quotes", () => {
    // React has no esc(): the same guarantee is that text is a text node, whatever it holds.
    render(<AnswerText text={`<a href="x">'&'</a>`} place="answer" gen={1} />);
    expect(document.querySelector("a")).toBeNull();
    expect(screen.getByText(`<a href="x">'&'</a>`)).toBeInTheDocument();
  });
});
