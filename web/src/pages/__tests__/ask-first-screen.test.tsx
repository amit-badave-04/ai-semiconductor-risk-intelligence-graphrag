import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fixtureBytes } from "@/lib/__tests__/fixtures";
import { responseOf } from "@/lib/__tests__/sse-helpers";
import { mountAsk } from "./askHarness";

// docs/specs/m5b-redesign.md RD4 to RD9: the Ask page before any question.

afterEach(() => vi.unstubAllGlobals());

const box = () => screen.getByRole("textbox", { name: "Your question" });
const askButton = () => screen.getByRole("button", { name: "Ask" });
const STARTER_PAYLOAD = {
  examples: [
    { id: "N2", type: "numeric", question: "What was Nvidia's total revenue for the fiscal year ended January 25, 2026?" },
    { id: "D3", type: "dependency", question: "Which foundries does AMD rely on to manufacture its chips?" },
    { id: "D1", type: "dependency", question: "Which suppliers does Nvidia depend on?" },
    { id: "R1", type: "regulatory", question: "Which BIS export-control rules is AMD affected by?" },
    { id: "T7", type: "temporal", question: "Did Nvidia remove any risk factors between its FY2024 and FY2025 annual reports?" },
    { id: "U1", type: "refusal", question: "Samsung?" },
  ],
};

describe("RD4: the first screen, in order", () => {
  it("leads with the headline naming the 13 covered companies, and the tab title stays short", () => {
    mountAsk();
    expect(screen.getByRole("heading", { level: 1, name: "Ask about 13 chip and AI companies." })).toBeInTheDocument();
    expect(document.title).toBe("Ask · semigraph");
  });

  it("says answers cite three kinds of source, then the labelled box and the Ask button, then the cost line", () => {
    mountAsk();
    const grounding = screen.getByText(/^Answers cite their sources:/);
    for (const stamp of ["filing passage", "reported number", "government rule"]) expect(grounding).toHaveTextContent(stamp);
    const cost = screen.getByText(/^Free to use, up to/);
    const order = [grounding, box(), askButton(), cost];
    for (let i = 1; i < order.length; i += 1) {
      expect(order[i - 1].compareDocumentPosition(order[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(screen.getByText(/Not financial advice\./)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "How answers are made" })).toHaveAttribute("href", "/method/");
  });

  it("the question form's heading is the focus target after an answer (A5), and only the box is named 'Your question'", () => {
    mountAsk();
    const heading = screen.getByRole("heading", { name: "Your question" });
    expect(heading).toHaveAttribute("id", "ask-heading");
    expect(heading).toHaveAttribute("tabindex", "-1");
    expect(heading.closest("section")).toContainElement(askButton());
    expect(screen.getAllByLabelText("Your question")).toEqual([box()]);
  });
});

describe("RD4a: the cost line", () => {
  it("shows the approved defaults until /api/stats loads, then the numbers it reports", async () => {
    mountAsk({ stats: { limits: { per_ip: "3 per 15 min", per_ip_per_day: 12 } } });
    expect(await screen.findByText("Free to use, up to 3 live questions every 15 minutes (12 a day).")).toBeInTheDocument();
    expect(screen.getByText(/Example questions answer instantly and are free\./)).toBeInTheDocument();
  });

  it("keeps the approved defaults when /api/stats fails", async () => {
    mountAsk({ stats: null });
    expect(await screen.findByText("Free to use, up to 5 live questions every 10 minutes (20 a day).")).toBeInTheDocument();
  });
});

describe("RD8: Ask with an empty box", () => {
  it("says so next to the box and in the status region, and sends nothing", async () => {
    const env = mountAsk();
    await env.user.type(box(), "   ");
    await env.user.click(askButton());
    const message = "Type a question first, or pick an example below.";
    expect(screen.getByRole("status")).toHaveTextContent(message);
    const note = document.getElementById(box().getAttribute("aria-describedby") ?? "");
    expect(note).toHaveTextContent(message);
    expect(env.posts()).toHaveLength(0);
  });

  it("clears the message as soon as the reader types", async () => {
    const env = mountAsk();
    await env.user.click(askButton());
    await env.user.type(box(), "q");
    expect(screen.getByRole("status")).not.toHaveTextContent("Type a question first");
    expect(box()).not.toHaveAttribute("aria-describedby");
  });
});

describe("RD7: More options holds Depth as a labelled radio group", () => {
  it("is closed at first, says the current depth, and offers Standard, Deep research and Basic text search", async () => {
    const env = mountAsk({ stats: { agent_enabled: true } });
    const toggle = screen.getByRole("button", { name: /More options/ });
    expect(toggle).toHaveTextContent("Depth: Standard");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await env.user.click(toggle);
    const group = screen.getByRole("group", { name: "Depth" });
    await waitFor(() => expect(within(group).getAllByRole("radio")).toHaveLength(3));
    expect(within(group).getByRole("radio", { name: "Standard (recommended)" })).toBeChecked();
    expect(within(group).getByRole("radio", { name: "Deep research" })).toBeInTheDocument();
    expect(within(group).getByRole("radio", { name: "Basic text search" })).toHaveAccessibleDescription(
      "Comparison only; cannot track changes over time.",
    );
  });

  it("offers Deep research only when /api/stats says the agent is on", async () => {
    const env = mountAsk({ stats: { agent_enabled: false } });
    await env.user.click(screen.getByRole("button", { name: /More options/ }));
    const group = screen.getByRole("group", { name: "Depth" });
    expect(within(group).queryByRole("radio", { name: "Deep research" })).toBeNull();
    expect(within(group).getAllByRole("radio")).toHaveLength(2);
  });

  it("sends the chosen depth's strategy", async () => {
    const env = mountAsk({ stats: { agent_enabled: false }, ask: () => responseOf(fixtureBytes("cached_done")) });
    await env.user.click(screen.getByRole("button", { name: /More options/ }));
    await env.user.click(screen.getByRole("radio", { name: "Basic text search" }));
    expect(screen.getByRole("button", { name: /More options/ })).toHaveTextContent("Depth: Basic text search");
    await env.user.type(box(), "q");
    await env.user.click(askButton());
    await waitFor(() => expect(env.posts()).toHaveLength(1));
    expect(JSON.parse(String(env.posts()[0].init?.body))).toMatchObject({ strategy: "vector" });
  });
});

describe("RD5: six starters grouped by job, and the rest in a disclosure", () => {
  it("shows the curated starters under their jobs, each job saying what proof it gives, and never D1", async () => {
    mountAsk({ examples: STARTER_PAYLOAD });
    const starters = await screen.findByRole("region", { name: /Or try an example/ });
    expect(within(starters).getByRole("button", { name: STARTER_PAYLOAD.examples[0].question })).toBeInTheDocument();
    expect(within(starters).getByText("Check a number")).toBeInTheDocument();
    expect(within(starters).getByText(/^You get the figure as the company reported it/)).toBeInTheDocument();
    expect(within(starters).queryByRole("button", { name: "Which suppliers does Nvidia depend on?" })).toBeNull();
    expect(within(starters).getAllByRole("listitem")).toHaveLength(4);
  });

  it("lists every other example by company once 'Show all examples' is opened", async () => {
    const env = mountAsk({ examples: STARTER_PAYLOAD });
    const toggle = await screen.findByRole("button", { name: "Show all examples, grouped by company" });
    await env.user.click(toggle);
    const all = document.getElementById("examples") as HTMLElement;
    expect(within(all).getByRole("heading", { name: "Nvidia" })).toBeInTheDocument();
    expect(within(all).getByRole("button", { name: "Which suppliers does Nvidia depend on?" })).toBeInTheDocument();
    expect(within(all).getByRole("heading", { name: "Other" })).toBeInTheDocument();
  });

  it("a starter is asked on the Standard path at once, exactly once", async () => {
    const env = mountAsk({ examples: STARTER_PAYLOAD, ask: () => responseOf(fixtureBytes("cached_done")) });
    await env.user.click(await screen.findByRole("button", { name: STARTER_PAYLOAD.examples[1].question }));
    await waitFor(() => expect(env.posts()).toHaveLength(1));
    expect(JSON.parse(String(env.posts()[0].init?.body))).toMatchObject({ question: STARTER_PAYLOAD.examples[1].question, strategy: "hybrid" });
  });

  // SR4 rules 1 and 2: whatever depth was chosen, a starter sends a Standard (hybrid) ask and resets the control to Standard.
  it.each([
    ["Basic text search", false],
    ["Deep research", true],
  ])("a starter picked after choosing %s still asks on Standard and resets the depth to Standard", async (depth, agent) => {
    const env = mountAsk({ examples: STARTER_PAYLOAD, stats: { agent_enabled: agent }, ask: () => responseOf(fixtureBytes("cached_done")) });
    await env.user.click(screen.getByRole("button", { name: /More options/ }));
    await env.user.click(screen.getByRole("radio", { name: depth }));
    expect(screen.getByRole("button", { name: /More options/ })).toHaveTextContent(`Depth: ${depth}`);
    await env.user.click(await screen.findByRole("button", { name: STARTER_PAYLOAD.examples[1].question }));
    await waitFor(() => expect(env.posts()).toHaveLength(1));
    expect(JSON.parse(String(env.posts()[0].init?.body))).toMatchObject({ strategy: "hybrid" });
    expect(screen.getByRole("button", { name: /More options/ })).toHaveTextContent("Depth: Standard");
  });
});

describe("RD9: the covered companies, after the starters", () => {
  it("links each of the 13 companies to its page, and names the rules as the government's, matched by keyword", async () => {
    mountAsk({ examples: STARTER_PAYLOAD });
    const companies = screen.getByRole("region", { name: "Companies covered" });
    expect(within(companies).getAllByRole("link")).toHaveLength(13);
    expect(within(companies).getByRole("link", { name: /Nvidia/ })).toHaveAttribute("href", "/company/NVDA/");
    expect(companies).toHaveTextContent(/keyword matching/);
    const starters = await screen.findByRole("region", { name: /Or try an example/ });
    expect(starters.compareDocumentPosition(companies) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

// Reviews of baba14d (accessibility, Opus; React, Sonnet): each finding as a failing test first.
describe("review fixes", () => {
  it("the Ask button keeps keyboard focus while an answer streams (it is aria-disabled, not removed from the tab order)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const env = mountAsk({
      ask: async () => {
        await gate;
        return responseOf(fixtureBytes("cached_done"));
      },
    });
    await env.user.type(box(), "q");
    askButton().focus();
    await env.user.keyboard("{Enter}");
    await waitFor(() => expect(askButton()).toHaveAttribute("aria-disabled", "true"));
    expect(askButton()).toHaveFocus();
    await env.user.keyboard("{Enter}");
    release();
    await waitFor(() => expect(askButton()).not.toHaveAttribute("aria-disabled", "true"));
    expect(env.posts()).toHaveLength(1);
  });

  it("a starter keeps keyboard focus when it is pressed (the starters are aria-disabled while the answer streams)", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const env = mountAsk({
      examples: STARTER_PAYLOAD,
      ask: async () => {
        await gate;
        return responseOf(fixtureBytes("cached_done"));
      },
    });
    const starter = await screen.findByRole("button", { name: STARTER_PAYLOAD.examples[1].question });
    starter.focus();
    await env.user.keyboard("{Enter}");
    await waitFor(() => expect(starter).toHaveAttribute("aria-disabled", "true"));
    expect(starter).toHaveFocus();
    await env.user.keyboard("{Enter}");
    release();
    await waitFor(() => expect(starter).not.toHaveAttribute("aria-disabled"));
    expect(env.posts()).toHaveLength(1);
  });

  it("Ctrl+Enter asks only from the question box, and says so to assistive technology", async () => {
    const env = mountAsk({ ask: () => responseOf(fixtureBytes("cached_done")) });
    await env.user.type(box(), "q");
    expect(box()).toHaveAttribute("aria-keyshortcuts", "Control+Enter Meta+Enter");
    screen.getByRole("link", { name: "How answers are made" }).focus();
    await env.user.keyboard("{Control>}{Enter}{/Control}");
    expect(env.posts()).toHaveLength(0);
    box().focus();
    await env.user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(env.posts()).toHaveLength(1));
  });

  it("an empty Ask marks the box invalid, and a second empty Ask puts the note into the status region anew", async () => {
    const env = mountAsk();
    await env.user.click(askButton());
    expect(box()).toHaveAttribute("aria-invalid", "true");
    const status = screen.getByRole("status");
    const first = status.firstElementChild;
    expect(status).toHaveTextContent("Type a question first, or pick an example below.");
    await env.user.click(askButton());
    const second = status.firstElementChild;
    expect(status).toHaveTextContent("Type a question first, or pick an example below.");
    expect(second).not.toBeNull();
    expect(second).not.toBe(first); // a new node in the live region: an addition screen readers announce again
  });

  it("with a workspace open, the starters say why they are off", async () => {
    mountAsk({ examples: STARTER_PAYLOAD, workspace: { workspace: { id: "a".repeat(32), token: "t", expires_at: null } } });
    const starters = await screen.findByRole("region", { name: "Or try an example" });
    const note = within(starters).getByText(/With a workspace open, the examples are off/);
    const button = within(starters).getByRole("button", { name: STARTER_PAYLOAD.examples[0].question });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-describedby", note.id);
  });

  it("names the starters' region by its heading alone (the note is a paragraph of its own)", async () => {
    mountAsk({ examples: STARTER_PAYLOAD });
    expect(await screen.findByRole("region", { name: "Or try an example" })).toBeInTheDocument();
  });

  it("says when the examples could not be loaded, which is not the same as having none", async () => {
    mountAsk({ examplesStatus: 503 });
    expect(await screen.findByText("The examples could not be loaded right now. You can still ask your own question.")).toBeInTheDocument();
    expect(screen.queryByText(/No saved example answers/)).toBeNull();
  });
});

describe("RD6: what an answer looks like", () => {
  it("comes after the question form and before the starters, so a phone shows it below the box", async () => {
    mountAsk({ examples: STARTER_PAYLOAD });
    const sample = screen.getByRole("region", { name: "What an answer looks like" });
    const starters = await screen.findByRole("region", { name: "Or try an example" });
    expect(askButton().compareDocumentPosition(sample) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(sample.compareDocumentPosition(starters) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
