import { act, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { frame } from "@/lib/__tests__/sse-helpers";
import { mountAsk } from "./askHarness";

// I3 finding (medium): the answer boundary was keyed by the text length, so every streamed delta remounted the answer subtree and a
// keyboard user who had tabbed to a chip lost focus on the next delta. This goes through the real Ask page and AskProvider.

afterEach(() => vi.unstubAllGlobals());

const CHIP_ID = "0001045810-26-000021:I.1A:0361";
const delta = (text: string) => frame("delta", { text });

/** An /api/ask whose body the test feeds by hand, so a delta arrives exactly when the test says. */
function manualStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: () => new Response(body, { status: 200 }),
    push: async (bytes: Uint8Array) => {
      await act(async () => {
        controller.enqueue(bytes);
        await new Promise((resolve) => setTimeout(resolve, 20));
      });
    },
    close: () => controller.close(),
  };
}

describe("the answer while it streams", () => {
  it("keeps keyboard focus on a chip, the same DOM node, when the next delta arrives", async () => {
    const stream = manualStream();
    const env = mountAsk({ ask: stream.response });
    await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "What changed?");
    await env.user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(env.posts()).toHaveLength(1));
    await stream.push(delta(`Revenue grew [${CHIP_ID}] and `));
    const chip = await waitFor(() => {
      const found = document.querySelector(`#answer button[data-id="${CHIP_ID}"]`) as HTMLButtonElement | null;
      expect(found).not.toBeNull();
      return found as HTMLButtonElement;
    });
    act(() => chip.focus());
    expect(document.activeElement).toBe(chip);

    await stream.push(delta("margins improved."));

    await waitFor(() => expect(document.querySelector("#answer")?.textContent).toContain("margins improved."));
    const after = document.querySelector(`#answer button[data-id="${CHIP_ID}"]`);
    expect(after, "the chip is the same DOM node after the delta").toBe(chip);
    expect(document.activeElement).toBe(chip);
    stream.close();
  });
});
