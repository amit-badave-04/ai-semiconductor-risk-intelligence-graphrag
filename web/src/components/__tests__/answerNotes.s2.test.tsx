import { act, render } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it } from "vitest";
import { ReadWithCare, SourcesList } from "@/components/AnswerNotes";
import { AnswerText } from "@/components/Chips";
import { WhatWasChecked } from "@/components/WhatWasChecked";
import { rememberChunkEvidence, resetChipEvidence } from "@/lib/chipEvidence";
import type { DoneEvent } from "@/lib/events";
import { HOSTILE } from "./answerCorpus";

// S2 (docs/specs/m5b-frontend.md, "each component has its own allow-list"), for the surfaces R2 added (R2 review S1): "What was
// checked", "Read with care", "Sources (N)" and a chip's label from an evidence payload. Model text and server values reach them
// only as text nodes: the markup holds only the elements each component writes, no event, source or style attribute, and the
// one link is the constant Method link.

const ALLOWED = new Set(["section", "h3", "ul", "li", "span", "p", "a", "button", "svg", "path", "circle", "rect", "line", "polyline"]);
const CHUNK = "0001045810-24-000029:I.1A:0162";

function violations(container: HTMLElement): string[] {
  const found: string[] = [];
  for (const el of Array.from(container.querySelectorAll("*"))) {
    const tag = el.tagName.toLowerCase();
    if (!ALLOWED.has(tag)) found.push(`tag <${tag}>`);
    for (const attr of Array.from(el.attributes)) {
      if (/^on/i.test(attr.name) || attr.name === "src" || attr.name === "style") found.push(`attr ${attr.name} on <${tag}>`);
      if (attr.name === "href" && attr.value !== "/method/") found.push(`href ${attr.value}`);
    }
  }
  return found;
}

afterEach(() => resetChipEvidence());

describe("S2 allow-lists of the R2 surfaces", () => {
  it.each(HOSTILE.map((text, i) => [i, text.slice(0, 40)] as const))("What was checked holds hostile check values %i (%s) as text", (i) => {
    const evil = HOSTILE[i];
    const done = {
      event: "done",
      answer: "a",
      citations: [],
      hallucinated: [evil],
      cached: false,
      checks: { numbers_checked: 2, unmatched_numbers: [evil], echoed_numbers: [evil], pseudo_citations: [evil], unsupported_removal_claim: true },
    } as unknown as DoneEvent;
    const { container, unmount } = render(<WhatWasChecked done={done} />);
    expect(violations(container)).toEqual([]);
    unmount();
  });

  it.each(HOSTILE.map((text, i) => [i, text.slice(0, 40)] as const))("Read with care and Sources hold hostile answer %i (%s) as text", (i) => {
    const text = `${HOSTILE[i]} It no longer appears as a separate risk factor [${CHUNK}].`;
    const { container, unmount } = render(
      <MemoryRouter>
        <ReadWithCare text={text} />
        <SourcesList text={text} gen={1} staleIds={[]} />
      </MemoryRouter>,
    );
    expect(violations(container)).toEqual([]);
    unmount();
  });

  it("a chip shows a hostile filer and form from its evidence as text", () => {
    const { container } = render(<AnswerText text={`See [${CHUNK}].`} place="answer" gen={1} />);
    act(() => rememberChunkEvidence(CHUNK, { filer: '<img src=x onerror="alert(1)">', form: "<script>1</script>", filing_date: "2024-02-21" }));
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("button")?.textContent).toContain('<img src=x onerror="alert(1)">');
  });
});
