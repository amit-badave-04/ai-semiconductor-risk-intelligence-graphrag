import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { stampLabel } from "@/components/__tests__/stampLabel";
import { frame, responseOf } from "@/lib/__tests__/sse-helpers";
import { mountAsk } from "./askHarness";

// docs/specs/m5b-redesign.md RD13 and RD15. "Read with care": under a finished answer that holds one of the pinned change-claim
// wordings (pages/Method.copy.ts HEDGES), one note per wording with that wording's own hand-check counts, and a link to how
// reliable change claims are. "Sources (N)": under a finished answer, its distinct cited sources, the same parse as the chips,
// each a chip that opens the evidence panel; no section when the answer cites nothing.

afterEach(() => vi.unstubAllGlobals());

const A = "0001045810-24-000029:I.1A:0162";
const B = "xbrl:1045810:revenue:2026-01-25";

function done(answer: string, citations: string[] = []): Uint8Array {
  return frame("done", { answer, citations, hallucinated: [], cached: false, checks: { numbers_checked: 0, unmatched_numbers: [] } });
}

async function ask(bytes: Uint8Array) {
  const env = mountAsk({ ask: () => responseOf(bytes) });
  await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "q");
  await env.user.click(screen.getByRole("button", { name: "Ask" }));
  return env;
}

const care = () => screen.queryByRole("region", { name: "Read with care" });
const sources = () => screen.queryByRole("region", { name: /^Sources \(\d+\)$/ });

describe("Read with care (RD13)", () => {
  it("adds the hand-check counts of a removal claim, with a link to how reliable change claims are", async () => {
    await ask(done(`Risk X no longer appears as a separate risk factor [${A}].`, [A]));
    await waitFor(() => expect(care()).not.toBeNull());
    const note = care() as HTMLElement;
    expect(note).toHaveTextContent("4 of 4");
    expect(note).toHaveTextContent("2 of 4 had been folded into another risk factor");
    expect(within(note).getByRole("link", { name: "How reliable change claims are" })).toHaveAttribute("href", "/method/");
    expect(within(note).getAllByRole("listitem")).toHaveLength(1);
  });

  it("gives each wording its own counts, in a fixed order, and matches across line breaks and case", async () => {
    const text = "Whose wording was\nnot found in the newer filing. Also: no matching risk factor found in the earlier filing.";
    await ask(done(text));
    await waitFor(() => expect(care()).not.toBeNull());
    const items = within(care() as HTMLElement).getAllByRole("listitem").map((li) => li.textContent ?? "");
    expect(items).toHaveLength(2);
    expect(items[0]).toMatch(/6 of 12/);
    expect(items[1]).toMatch(/6 of 51/);
  });

  it("is not shown for an answer with no change-claim wording", async () => {
    await ask(done(`Revenue rose [${B}].`, [B]));
    await waitFor(() => expect(sources()).not.toBeNull());
    expect(care()).toBeNull();
  });

  it("waits for the finished answer", async () => {
    const env = mountAsk({
      ask: () => responseOf(frame("delta", { text: "It no longer appears as a separate risk factor" })),
    });
    await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "q");
    await env.user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(document.getElementById("answer")).toHaveTextContent("no longer appears"));
    expect(care()).toBeNull();
    expect(sources()).toBeNull();
  });
});

describe("Sources (N) (RD15)", () => {
  it("lists each cited source once, as many as the answer's distinct chips, each a chip with the chip's label", async () => {
    await ask(done(`One [${A}], two [${B}], one again [${A}].`, [A, B]));
    await waitFor(() => expect(sources()).not.toBeNull());
    const list = sources() as HTMLElement;
    expect(within(list).getByRole("heading", { name: "Sources (2)" })).toBeInTheDocument();
    const answerIds = new Set(Array.from(document.querySelectorAll("#answer button[data-id]"), (b) => b.getAttribute("data-id")));
    const listed = within(list).getAllByRole("button");
    expect(listed.map((b) => b.getAttribute("data-id"))).toEqual([A, B]);
    expect(new Set(listed.map((b) => b.getAttribute("data-id")))).toEqual(answerIds);
    expect(listed.map((b) => b.textContent)).toEqual([stampLabel(A), stampLabel(B)]);
  });

  it("gives every chip on the page its own key, and only the opened one is expanded", async () => {
    const env = await ask(done(`One [${A}] and [${B}].`, [A, B]));
    await waitFor(() => expect(sources()).not.toBeNull());
    const keys = Array.from(document.querySelectorAll("button[data-chip-key]"), (b) => b.getAttribute("data-chip-key"));
    expect(new Set(keys).size).toBe(keys.length);
    const listed = within(sources() as HTMLElement).getAllByRole("button")[0];
    await env.user.click(listed);
    await waitFor(() => expect(listed).toHaveAttribute("aria-expanded", "true"));
    expect(document.querySelector(`#answer button[data-id="${A}"]`)).toHaveAttribute("aria-expanded", "false");
  });

  it("is not shown when the answer cites nothing", async () => {
    await ask(done("I cannot answer that from the filings."));
    await waitFor(() => expect(screen.getByRole("region", { name: "What was checked" })).toBeInTheDocument());
    expect(sources()).toBeNull();
  });
});
