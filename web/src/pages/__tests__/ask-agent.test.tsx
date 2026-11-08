import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fixtureBytes } from "@/lib/__tests__/fixtures";
import { responseOf } from "@/lib/__tests__/sse-helpers";
import { refreshStats } from "@/lib/statsStore";
import { act } from "react";
import { EXAMPLES, mountAsk } from "./askHarness";

// tests/ui/agent.test.mjs, the 10 titles about the strategy option, the hint and the privacy line, ported (the step half is ported in
// components/__tests__/steps.test.tsx). Each title is the legacy title, character for character. Nothing agent-related may show while
// the service reports `agent_enabled: false`.
afterEach(() => vi.unstubAllGlobals());

const select = () => screen.getByRole("combobox", { name: "Retrieval strategy" }) as HTMLSelectElement;
const optionValues = () => Array.from(select().options).map((o) => o.value);
const HINT = /planning model/;
const hint = () => screen.queryByText(HINT);
const PRIVACY = /sampling rate/;
const loaded = () => waitFor(() => expect(screen.getByRole("button", { name: EXAMPLES.examples[0].question })).toBeInTheDocument());

describe("tests/ui/agent.test.mjs: the option, the hint and the privacy line", () => {
  it("with the agent off nothing agent-related is shown", async () => {
    mountAsk({ stats: { agent_enabled: false, tracing: true } });
    await loaded();
    await waitFor(() => expect(optionValues()).toEqual(["hybrid", "vector"]));
    expect(hint()).toBeNull();
    expect(screen.queryByText(PRIVACY)).toBeNull();
    expect(document.querySelectorAll("ol li")).toHaveLength(0);
  });

  it("with the agent on the option appears once, however often the stats are reloaded, without a privacy line", async () => {
    mountAsk({ stats: { agent_enabled: true, tracing: false } });
    await waitFor(() => expect(optionValues()).toContain("agent"));
    for (let i = 0; i < 3; i += 1) await act(() => refreshStats()); // loadStats() runs after every answer
    expect(optionValues().filter((v) => v === "agent")).toHaveLength(1);
    expect(screen.getByRole("option", { name: /^Deep research \(agent\)/ })).toBeInTheDocument();
    expect(screen.queryByText(PRIVACY)).toBeNull();
  });

  it("the hint stays hidden for the default hybrid strategy even while the agent is enabled", async () => {
    mountAsk({ stats: { agent_enabled: true } });
    await waitFor(() => expect(optionValues()).toContain("agent"));
    expect(select().value).toBe("hybrid");
    expect(hint()).toBeNull();
  });

  it("picking the agent strategy shows the hint; switching back to hybrid hides it again", async () => {
    const env = mountAsk({ stats: { agent_enabled: true } });
    await waitFor(() => expect(optionValues()).toContain("agent"));
    await env.user.selectOptions(select(), "agent");
    expect(hint()).toBeInTheDocument();
    await env.user.selectOptions(select(), "hybrid");
    expect(hint()).toBeNull();
  });

  it("clicking a benchmark example (which forces hybrid) hides the hint at once, not only after the answer", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    const env = mountAsk({
      stats: { agent_enabled: true },
      ask: async () => {
        await gate;
        return responseOf(fixtureBytes("cached_done"));
      },
    });
    await waitFor(() => expect(optionValues()).toContain("agent"));
    await env.user.selectOptions(select(), "agent");
    expect(hint()).toBeInTheDocument();
    await env.user.click(screen.getByRole("button", { name: EXAMPLES.examples[0].question }));
    expect(select().value).toBe("hybrid");
    expect(hint()).toBeNull(); // the answer has not come back yet
    release();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("served from cache"));
  });

  it("syncAgentUi stays idempotent when re-run (as loadStats does after every answer) with the agent strategy selected", async () => {
    const env = mountAsk({ stats: { agent_enabled: true } });
    await waitFor(() => expect(optionValues()).toContain("agent"));
    await env.user.selectOptions(select(), "agent");
    for (let i = 0; i < 3; i += 1) await act(() => refreshStats());
    expect(hint()).toBeInTheDocument();
    expect(optionValues().filter((v) => v === "agent")).toHaveLength(1);
  });

  it("the hint is hidden the moment the service reports the agent off, even with agent selected", async () => {
    const env = mountAsk({ stats: { agent_enabled: true } });
    await waitFor(() => expect(optionValues()).toContain("agent"));
    await env.user.selectOptions(select(), "agent");
    expect(hint()).toBeInTheDocument();
    env.fetchMock.mockImplementation(async (url: string) =>
      String(url) === "/api/stats" ? new Response(JSON.stringify({ agent_enabled: false }), { status: 200 }) : new Response("{}", { status: 404 }),
    );
    await act(() => refreshStats());
    await waitFor(() => expect(optionValues()).toEqual(["hybrid", "vector"]));
    expect(hint()).toBeNull();
  });

  it("the privacy line appears only when a sample of agent questions is really traced, and never claims the text is sent", async () => {
    mountAsk({ stats: { agent_enabled: true, tracing: true } });
    const note = await screen.findByText(PRIVACY);
    expect(note.textContent).not.toMatch(/small sample/);
    expect(note.textContent).toMatch(/never the text of the question or the answer, and never your address/);
  });

  it("turning the agent off again removes the option, resets a selected agent to hybrid and clears the timeline", async () => {
    const env = mountAsk({ stats: { agent_enabled: true }, ask: () => responseOf(fixtureBytes("agent_steps")) });
    await waitFor(() => expect(optionValues()).toContain("agent"));
    await env.user.selectOptions(select(), "agent");
    await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "q");
    await env.user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(screen.getAllByRole("listitem").length).toBeGreaterThan(0));
    env.fetchMock.mockImplementation(async (url: string) =>
      String(url) === "/api/stats" ? new Response(JSON.stringify({ agent_enabled: false }), { status: 200 }) : new Response("{}", { status: 404 }),
    );
    await act(() => refreshStats());
    await waitFor(() => expect(optionValues()).toEqual(["hybrid", "vector"]));
    expect(select().value).toBe("hybrid");
    expect(document.querySelectorAll("ol li")).toHaveLength(0);
  });

  it("the option keeps a chosen strategy that is not the agent", async () => {
    const env = mountAsk({ stats: { agent_enabled: false } });
    await env.user.selectOptions(select(), "vector");
    await env.user.click(document.body);
    env.fetchMock.mockImplementation(async (url: string) =>
      String(url) === "/api/stats" ? new Response(JSON.stringify({ agent_enabled: true }), { status: 200 }) : new Response("{}", { status: 404 }),
    );
    await act(() => refreshStats());
    await act(() => refreshStats());
    await waitFor(() => expect(optionValues()).toContain("agent"));
    expect(select().value).toBe("vector");
  });
});

describe("the agent path of an ask", () => {
  it("an agent ask sends strategy agent, shows the steps timeline and the agent status first", async () => {
    const env = mountAsk({ stats: { agent_enabled: true }, ask: () => responseOf(fixtureBytes("agent_steps")) });
    await waitFor(() => expect(optionValues()).toContain("agent"));
    await env.user.selectOptions(select(), "agent");
    await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "q");
    await env.user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("done"));
    expect(JSON.parse(String(env.posts()[0].init?.body)).strategy).toBe("agent");
    expect(screen.getByRole("list", { name: "What the research agent did" })).not.toHaveAttribute("hidden");
  });
});
