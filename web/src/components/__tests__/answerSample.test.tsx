import { readFileSync } from "node:fs";
import path from "node:path";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { deck } from "@/copy/deck";
import { SPECIMENS } from "@/lib/specimens";
import { AnswerSample } from "../AnswerSample";

// docs/specs/m5b-redesign.md RD6 as docs/decisions/003-evidence-highlight.md settled it: "What an answer looks like" is a static
// sample with the cited passage and its source line, and NO highlight or legend (real panels mark words only when an answer
// copies them exactly, which most answers do not); the passage comes from the recorded evidence fixture, word for word.

const recorded = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "../../test/fixtures/specimens.recorded.json"), "utf-8")) as {
  payloads: Record<string, { text?: string; filing_date?: string; section_title?: string }>;
};
const passage = recorded.payloads[SPECIMENS.passage];

describe("AnswerSample", () => {
  it("quotes the recorded passage word for word: the sentence and the words after it are in the fixture's text", () => {
    const { quote, quoteAfter } = deck.sample;
    expect(passage.text).toContain(`${quote} ${quoteAfter.trim()}`);
  });

  it("is a labelled region with the sample answer, the cited passage and the source line, and no highlight or legend", () => {
    render(<AnswerSample />);
    const region = screen.getByRole("region", { name: "What an answer looks like" });
    expect(region).toHaveTextContent(deck.sample.answer);
    expect(region.querySelector("blockquote")?.textContent).toBe(`${deck.sample.quote}${deck.sample.quoteAfter}`);
    expect(region.querySelector("mark")).toBeNull();
    expect(region).toHaveTextContent("Nvidia 10-K filed Feb 21, 2024 · Item 1A. Risk Factors · paragraph 162");
    expect(region).not.toHaveTextContent("Highlighted: the words copied exactly from the source");
  });

  it("names the same filing date and section as the fixture", () => {
    expect(passage.filing_date).toBe("2024-02-21");
    expect(passage.section_title).toBe("Item 1A. Risk Factors");
  });

  it("is not interactive: a sample, with no button or link a reader could mistake for a live answer", () => {
    render(<AnswerSample />);
    const region = screen.getByRole("region", { name: "What an answer looks like" });
    expect(region.querySelectorAll("button, a")).toHaveLength(0);
  });
});
