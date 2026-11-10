import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerText } from "@/components/Chips";
import { parseAnswer } from "@/lib/answerBlocks";
import { HOSTILE, generated } from "./answerCorpus";

// S2 (docs/specs/m5b-frontend.md): the answer text contains only p, ul, li, h3, strong, the chip button and text; no on*, src or
// style attribute anywhere and no href; the chip carries nothing but the documented attributes.
const ALLOWED_TAGS = new Set(["p", "ul", "li", "h3", "strong", "button"]);
// aria-expanded and aria-controls: docs/specs/m5b-redesign.md RD11 (constant values, never from data)
const ALLOWED_CHIP_ATTRS = new Set(["type", "class", "data-id", "data-chip-key", "data-stale", "title", "aria-expanded", "aria-controls"]);

function violations(text: string): string[] {
  const { container, unmount } = render(<AnswerText text={text} place="answer" gen={1} />);
  const found: string[] = [];
  for (const el of Array.from(container.querySelectorAll("*"))) {
    const tag = el.tagName.toLowerCase();
    if (!ALLOWED_TAGS.has(tag)) found.push(`tag <${tag}>`);
    for (const attr of Array.from(el.attributes)) {
      if (/^on/i.test(attr.name) || attr.name === "src" || attr.name === "style" || attr.name === "href") found.push(`attr ${attr.name}`);
      if (tag === "button" && !ALLOWED_CHIP_ATTRS.has(attr.name)) found.push(`chip attr ${attr.name}`);
      if (tag !== "button" && attr.name !== "class") found.push(`attr ${attr.name} on <${tag}>`);
    }
  }
  unmount();
  return found;
}

describe("S2 allow-list of the answer text", () => {
  it.each(HOSTILE.map((text, i) => [i, text.slice(0, 40)] as const))("hostile string %i (%s) renders only allowed elements", (i) => {
    expect(violations(HOSTILE[i])).toEqual([]);
  });

  it("300 generated markdown-shaped strings render only allowed elements", () => {
    for (const text of generated(300)) expect(violations(text)).toEqual([]);
  });

  it("a very long single line renders in bounded time and stays one text node", () => {
    const started = performance.now();
    const text = "x".repeat(200_000);
    const { container } = render(<AnswerText text={text} place="answer" gen={1} />);
    expect(performance.now() - started).toBeLessThan(2000);
    expect(container.querySelectorAll("p")).toHaveLength(1);
    expect(container.textContent).toBe(text);
  });

  it("a forged token with a quote in its id is no chip and carries no event attribute", () => {
    const { container } = render(<AnswerText text={`[xbrl:1:a:2026-01-01"onclick="x] [0001045810-26-000021:I.1A:0361]`} place="answer" gen={1} />);
    expect(container.querySelectorAll("button")).toHaveLength(1);
    for (const el of Array.from(container.querySelectorAll("*"))) expect(el.hasAttribute("onclick")).toBe(false);
  });
});

describe("S2 linear time on whitespace runs (I3 finding)", () => {
  const best = (n: number): number => {
    const text = `a${" ".repeat(n)}b`;
    let fastest = Infinity;
    for (let run = 0; run < 3; run++) {
      const started = performance.now();
      parseAnswer(text);
      fastest = Math.min(fastest, performance.now() - started);
    }
    return fastest;
  };

  it("a long whitespace run followed by a non-space does not grow faster than linear (ratio at 4n under 10)", () => {
    const n = 20_000;
    best(n); // warm up
    const ratio = best(4 * n) / Math.max(best(n), 0.05);
    expect(ratio).toBeLessThan(10);
  });

  it("AnswerText renders the whitespace-run text with the run kept inside the paragraph", () => {
    const text = `a${" ".repeat(50_000)}b`;
    const { container } = render(<AnswerText text={text} place="answer" gen={1} />);
    expect(container.querySelectorAll("p")).toHaveLength(1);
    expect(container.textContent).toBe(text);
  });
});
