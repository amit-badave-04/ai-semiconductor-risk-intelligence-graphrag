import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fixtureBytes } from "@/lib/__tests__/fixtures";
import { responseOf } from "@/lib/__tests__/sse-helpers";
import { EXAMPLES, mountAsk } from "./askHarness";

// R22 / R23 / A3 / A4 / S12: the Ask page. Two titles of tests/ui/checks.test.mjs (the example panel) are ported here; since
// docs/specs/m5b-redesign.md RD5 the examples are six starters grouped by job and the rest by company under "Show all examples".

afterEach(() => vi.unstubAllGlobals());

const box = () => screen.getByRole("textbox", { name: "Your question" });
const askButton = () => screen.getByRole("button", { name: "Ask" });
const status = () => screen.getByRole("status");
const live = (name: string) => () => responseOf(fixtureBytes(name));
const startersRegion = () => screen.findByRole("region", { name: /Or try an example/ });
const questionButtons = (root: HTMLElement) => within(root).queryAllByRole("button").filter((b) => b.closest("li") !== null);

describe("tests/ui/checks.test.mjs: the example panel", () => {
  it("with no saved example the panel says so instead of listing questions that would cost money", async () => {
    mountAsk({ examples: { source: "s", examples: [] } });
    expect(await screen.findByText(/No saved example answers/)).toBeInTheDocument();
    expect(questionButtons(await startersRegion())).toHaveLength(0);
    expect(screen.queryByRole("button", { name: /Show all examples/ })).toBeNull();
  });

  it("examples are grouped (starters by job, the rest by company) and escaped", async () => {
    const env = mountAsk({
      examples: {
        examples: [
          { id: "N1", type: "numeric", question: "Nvidia revenue?" },
          { id: "N2", type: "numeric", question: '"><b>x</b>' },
          { id: "U1", type: "refusal", question: "Samsung?" },
        ],
      },
    });
    const starters = await startersRegion();
    await waitFor(() => expect(questionButtons(starters)).toHaveLength(1));
    expect(within(starters).getByRole("button", { name: '"><b>x</b>' })).toBeInTheDocument();
    await env.user.click(screen.getByRole("button", { name: "Show all examples, grouped by company" }));
    const all = document.getElementById("examples") as HTMLElement;
    expect(questionButtons(all)).toHaveLength(2);
    expect(within(all).getAllByRole("heading").map((h) => h.textContent)).toEqual(["Nvidia", "Other"]);
    expect(starters.querySelector("b")).toBeNull();
  });
});

describe("the page", () => {
  it("loading the page makes no POST, no upload and no workspace create (S12), under StrictMode", async () => {
    const env = mountAsk({ stats: { agent_enabled: false } });
    await screen.findByRole("button", { name: EXAMPLES.examples[0].question });
    expect(env.calls.filter((c) => c.init?.method === "POST")).toHaveLength(0);
  });

  it("every control has a programmatic label (A4) and the status is a polite live region (A3)", async () => {
    mountAsk();
    expect(box()).toHaveAttribute("maxlength", "500");
    expect(screen.getByRole("button", { name: /More options/ })).toBeInTheDocument(); // Depth's radios are labelled (ask-first-screen.test.tsx)
    expect(askButton()).toBeInTheDocument();
    expect(status()).toHaveAttribute("aria-live", "polite");
    expect(screen.getByRole("heading", { level: 1, name: "Ask about 13 chip and AI companies." })).toBeInTheDocument();
    const heading = screen.getByRole("heading", { name: "Your question" });
    expect(heading).toHaveAttribute("id", "ask-heading");
    expect(heading).toHaveAttribute("tabindex", "-1");
  });

  it("the streamed answer text is not a live region", async () => {
    const env = mountAsk({ ask: live("live_hybrid") });
    await env.user.type(box(), "What changed?");
    await env.user.click(askButton());
    await waitFor(() => expect(status()).toHaveTextContent("Done."));
    expect(document.querySelector("#answer")?.closest("[aria-live]")).toBeNull();
    expect(document.querySelector("#answer")).not.toHaveAttribute("aria-live");
  });

  it("clicking Ask sends one POST with the hybrid strategy and shows the answer, the checks and the done status", async () => {
    const env = mountAsk({ ask: live("live_hybrid"), stats: { models: { llm: "anthropic/claude-sonnet-5" } } });
    await env.user.type(box(), "What changed in Nvidia's risk factors?");
    await env.user.click(askButton());
    await waitFor(() => expect(status()).toHaveTextContent("Done."));
    expect(env.posts()).toHaveLength(1);
    expect(JSON.parse(String(env.posts()[0].init?.body))).toMatchObject({ question: "What changed in Nvidia's risk factors?", strategy: "hybrid" });
    expect(document.querySelector("#answer")?.textContent?.length).toBeGreaterThan(20);
    expect(screen.getAllByRole("button").filter((b) => b.hasAttribute("data-chip-key")).length).toBeGreaterThan(0);
    expect(document.querySelector("#meta")).not.toBeNull();
    expect(askButton()).toBeEnabled();
  });

  it("Ctrl+Enter and Cmd+Enter ask, and a plain Enter does not", async () => {
    const env = mountAsk({ ask: live("cached_done") });
    await env.user.type(box(), "q1");
    await env.user.keyboard("{Enter}");
    expect(env.posts()).toHaveLength(0);
    await env.user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(env.posts()).toHaveLength(1));
    await waitFor(() => expect(askButton()).toBeEnabled());
    await env.user.keyboard("{Meta>}{Enter}{/Meta}");
    await waitFor(() => expect(env.posts()).toHaveLength(2));
  });

  it("Ask is disabled while an answer streams and a second press makes no second POST", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const env = mountAsk({
      ask: async () => {
        await gate;
        return responseOf(fixtureBytes("cached_done"));
      },
    });
    await env.user.type(box(), "q");
    await env.user.click(askButton());
    await waitFor(() => expect(askButton()).toHaveAttribute("aria-disabled", "true")); // focusable while disabled (it keeps focus)
    await env.user.keyboard("{Control>}{Enter}{/Control}");
    release();
    await waitFor(() => expect(askButton()).toBeEnabled());
    expect(env.posts()).toHaveLength(1);
  });

  it("a blank question sends nothing", async () => {
    const env = mountAsk();
    await env.user.type(box(), "   ");
    await env.user.click(askButton());
    expect(env.posts()).toHaveLength(0);
  });

  it("clicking an example asks it on the hybrid path, at once, exactly once", async () => {
    const env = mountAsk({ ask: live("cached_done") });
    await env.user.click(await screen.findByRole("button", { name: EXAMPLES.examples[0].question }));
    await waitFor(() => expect(status()).toHaveTextContent("Answered earlier, shown instantly."));
    expect(env.posts()).toHaveLength(1);
    expect(JSON.parse(String(env.posts()[0].init?.body))).toMatchObject({ question: EXAMPLES.examples[0].question, strategy: "hybrid" });
    expect(box()).toHaveValue(EXAMPLES.examples[0].question);
  });

  it("the progress status follows the retrieval, then an escalation, then the end", async () => {
    let step: () => void = () => {};
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const enc = new TextEncoder();
        const send = (e: string, d: unknown) => controller.enqueue(enc.encode(`event: ${e}\ndata: ${JSON.stringify(d)}\n\n`));
        send("retrieval", { anchors: { Nvidia: 1 }, counts: { chunks: 8, edges: 2 } });
        let n = 0;
        step = () => {
          n += 1;
          if (n === 1) send("escalated", { reasons: ["ungrounded_number"], from: "a", to: "b" });
          if (n === 2) {
            send("done", { answer: "Done.", citations: [], hallucinated: [], cached: false });
            controller.close();
          }
        };
      },
    });
    const env = mountAsk({ ask: () => new Response(stream, { status: 200 }) });
    await env.user.type(box(), "q");
    await env.user.click(askButton());
    await waitFor(() => expect(status()).toHaveTextContent("Found 8 passages and 2 relationships about Nvidia. Writing the answer\u2026"));
    step();
    await waitFor(() => expect(status()).toHaveTextContent(/did not pass the automatic checks, so a stronger model is answering again/));
    step();
    await waitFor(() => expect(status()).toHaveTextContent("Done."));
  });

  it("a refusal lands in the status region, in the same card as the Ask button (A10)", async () => {
    const env = mountAsk({ ask: () => new Response(JSON.stringify({ detail: "Live questions are paused right now." }), { status: 503 }) });
    await env.user.type(box(), "q");
    await env.user.click(askButton());
    await waitFor(() => expect(status()).toHaveTextContent("Live questions are paused right now."));
    expect(status().closest("section")).toBe(askButton().closest("section"));
    expect(status().className).toMatch(/destructive/);
  });

  it("the stats are reloaded after an ask, and the cost line and the cost note come from them", async () => {
    const env = mountAsk({
      ask: live("cached_done"),
      stats: { limits: { per_ip: "4 per 10 min", per_ip_per_day: 9 }, models: { llm: "anthropic/claude-sonnet-5", escalation: "openai/gpt-6-luna" } },
    });
    await screen.findByText("Free to use, up to 4 live questions every 10 minutes (9 a day).");
    expect(document.body.textContent).toContain("one claude-sonnet-5 call each, plus a second gpt-6-luna call when the draft fails its checks");
    const before = env.statsCalls().length;
    await env.user.type(box(), "q");
    await env.user.click(askButton());
    await waitFor(() => expect(env.statsCalls().length).toBeGreaterThan(before));
  });

  it("shows the withdrawn-accuracy notice, the reliability note and the footer with their links opened safely", async () => {
    mountAsk();
    expect(screen.getByText(/Accuracy figures shown on earlier versions of this page are withdrawn/)).toBeInTheDocument();
    expect(screen.getByText(/How reliable are change claims\?/)).toBeInTheDocument();
    // every link that opens a new tab is opened safely (the in-app links, such as a company page, stay in the tab)
    const external = Array.from(document.querySelectorAll("a")).filter((a) => a.getAttribute("target") === "_blank");
    expect(external.length).toBeGreaterThan(0);
    for (const a of external) expect(a.getAttribute("rel")).toBe("noopener noreferrer");
    expect(screen.getByRole("link", { name: "GitHub" })).toHaveAttribute("href", "https://github.com/amit-badave-04/ai-semiconductor-risk-intelligence-graphrag");
  });
});
