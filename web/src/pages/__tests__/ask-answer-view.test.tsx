import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fixtureBytes } from "@/lib/__tests__/fixtures";
import { frame, responseOf } from "@/lib/__tests__/sse-helpers";
import { mountAsk } from "./askHarness";

// docs/specs/m5b-redesign.md RD10: once a question is asked, the question is the answer's heading (the focus target A5 falls back
// to), the status speaks plain words (deck status), and the static sample gives way to the real answer. The status line stays in
// the form, next to the Ask button (A10).

afterEach(() => vi.unstubAllGlobals());

const box = () => screen.getByRole("textbox", { name: "Your question" });
const askButton = () => screen.getByRole("button", { name: "Ask" });

describe("the answer view", () => {
  it("before any question the form's heading is the focus target; after one, the question is the heading and the target", async () => {
    const env = mountAsk({ ask: () => responseOf(fixtureBytes("cached_done")) });
    expect(document.getElementById("ask-heading")).toHaveTextContent("Your question");
    await env.user.type(box(), "Did Nvidia remove any risk factors?");
    await env.user.click(askButton());
    const heading = await screen.findByRole("heading", { level: 2, name: "Did Nvidia remove any risk factors?" });
    expect(heading).toHaveAttribute("id", "ask-heading");
    expect(heading).toHaveAttribute("tabindex", "-1");
    expect(document.querySelectorAll("#ask-heading")).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Your question" })).not.toHaveAttribute("id");
  });

  it("says in plain words that an answer given earlier came from the cache", async () => {
    const env = mountAsk({ ask: () => responseOf(fixtureBytes("cached_done")) });
    await env.user.type(box(), "q");
    await env.user.click(askButton());
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Answered earlier, shown instantly."));
  });

  it("says Done. when a live answer is complete", async () => {
    const env = mountAsk({ ask: () => responseOf(fixtureBytes("live_hybrid")) });
    await env.user.type(box(), "q");
    await env.user.click(askButton());
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Done."));
  });

  it("names a saved benchmark answer as one", async () => {
    const done = { answer: "Revenue was $1.", citations: [], hallucinated: [], cached: true, source: "benchmark" };
    const env = mountAsk({ ask: () => responseOf(frame("done", done)) });
    await env.user.type(box(), "q");
    await env.user.click(askButton());
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Saved answer, shown instantly."));
  });

  it("shows the question as text only, however hostile, and keeps the status beside the Ask button (A10)", async () => {
    const evil = '<img src=x onerror="alert(1)">';
    const env = mountAsk({ ask: () => responseOf(fixtureBytes("cached_done")) });
    await env.user.type(box(), evil);
    await env.user.click(askButton());
    const heading = await screen.findByRole("heading", { level: 2, name: evil });
    expect(heading.querySelector("img")).toBeNull();
    expect(screen.getByRole("status").closest("section")).toBe(askButton().closest("section"));
  });

  it("puts the real answer where the sample was: the sample is not shown once a question is asked", async () => {
    const env = mountAsk({ ask: () => responseOf(fixtureBytes("cached_done")) });
    expect(screen.getByRole("region", { name: "What an answer looks like" })).toBeInTheDocument();
    await env.user.type(box(), "q");
    await env.user.click(askButton());
    await screen.findByRole("heading", { level: 2, name: "q" });
    expect(screen.queryByRole("region", { name: "What an answer looks like" })).toBeNull();
  });
});
