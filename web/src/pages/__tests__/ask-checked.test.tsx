import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fixtureBytes } from "@/lib/__tests__/fixtures";
import { frame, responseOf } from "@/lib/__tests__/sse-helpers";
import { mountAsk } from "./askHarness";

// docs/specs/m5b-redesign.md RD14: under every finished answer, "What was checked" in plain sentences (lib/answerMeta.ts
// checkedView worded by the deck), each with an icon whose shape, not only its colour, says ok, caution or problem; the legacy
// badges (tokens, cost, the model, the checks hint) stay for analysts in a "Technical details" disclosure.

afterEach(() => vi.unstubAllGlobals());

async function ask(response: () => Response) {
  const env = mountAsk({ ask: response });
  await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "q");
  await env.user.click(screen.getByRole("button", { name: "Ask" }));
  return env;
}

const checked = () => screen.getByRole("region", { name: "What was checked" });

describe("What was checked", () => {
  it("lists a live answer's checks as plain sentences", async () => {
    await ask(() => responseOf(fixtureBytes("live_hybrid")));
    await waitFor(() => expect(checked()).toBeInTheDocument());
    const items = within(checked()).getAllByRole("listitem").map((li) => li.textContent);
    expect(items.length).toBeGreaterThan(0);
    for (const item of items) expect(item).toMatch(/^[A-Z0-9].*\.$/);
    expect(checked()).not.toHaveTextContent(/verified|%/);
  });

  it("says a repeated answer from the cache makes no claim, and a saved one was checked when saved", async () => {
    await ask(() => responseOf(fixtureBytes("cached_done")));
    await waitFor(() =>
      expect(checked()).toHaveTextContent("Answered earlier. The checks are shown only when an answer is first written; open a source to read it."),
    );
  });

  it("names a saved benchmark answer as checked when it was saved, and nothing more", async () => {
    await ask(() => responseOf(frame("done", { answer: "a", citations: [], hallucinated: [], cached: true, source: "benchmark" })));
    await waitFor(() => expect(checked()).toHaveTextContent("Checked when the answer was saved. Open a source to read it."));
    expect(within(checked()).getAllByRole("listitem")).toHaveLength(1);
  });

  it("marks each line by shape for the eye, hidden from assistive technology (the words say it)", async () => {
    await ask(() =>
      responseOf(frame("done", { answer: "a [x]", citations: ["x"], hallucinated: ["y"], cached: false, checks: { unmatched_numbers: ["$3B"], numbers_checked: 2 } })),
    );
    await waitFor(() => expect(checked()).toBeInTheDocument());
    for (const icon of checked().querySelectorAll("svg")) expect(icon).toHaveAttribute("aria-hidden", "true");
    expect(checked()).toHaveTextContent("Some figures could not be matched: $3B.");
  });

  it("keeps the analysts' badges and the checks hint in a Technical details disclosure", async () => {
    await ask(() => responseOf(fixtureBytes("live_hybrid")));
    await waitFor(() => expect(document.querySelector("#meta details")).not.toBeNull());
    const details = document.querySelector("#meta details") as HTMLElement;
    expect(within(details).getByText("Technical details")).toBeInTheDocument();
    expect(details).toHaveTextContent(/\d+ citations?/);
    expect(details).toHaveTextContent("do not prove");
  });
});
