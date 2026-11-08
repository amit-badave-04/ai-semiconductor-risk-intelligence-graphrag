import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fixtureBytes } from "@/lib/__tests__/fixtures";
import { frame, responseOf } from "@/lib/__tests__/sse-helpers";
import { EXAMPLES, mountAsk } from "./askHarness";

// R23: while a workspace is open the examples panel is disabled and the strategy select offers only `hybrid` (a guaranteed 400
// otherwise); asks as of a version carry `as_of`.
afterEach(() => vi.unstubAllGlobals());

const WS = { workspace: { id: "a".repeat(32), token: "secret-token", expires_at: null }, headers: () => ({ "X-Workspace-Token": "secret-token" }) };
const select = () => screen.getByRole("combobox", { name: "Retrieval strategy" }) as HTMLSelectElement;

describe("the Ask page with a workspace open (R23)", () => {
  it("disables the examples panel: no example can become a paid live call", async () => {
    const env = mountAsk({ workspace: WS, ask: () => responseOf(fixtureBytes("workspace_done")) });
    const panel = document.querySelector("#examples") as HTMLElement;
    await waitFor(() => expect(within(panel).getAllByRole("button")).toHaveLength(EXAMPLES.examples.length));
    for (const b of within(panel).getAllByRole("button")) expect(b).toBeDisabled();
    await env.user.click(within(panel).getAllByRole("button")[0]);
    expect(env.posts()).toHaveLength(0);
  });

  it("the strategy select offers only hybrid, even when the agent is enabled", async () => {
    mountAsk({ workspace: WS, stats: { agent_enabled: true } });
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Retrieval strategy" })).toBeInTheDocument());
    await new Promise((r) => setTimeout(r, 30));
    expect(Array.from(select().options).map((o) => o.value)).toEqual(["hybrid"]);
  });

  it("a workspace that was left on agent or vector asks as hybrid", async () => {
    const env = mountAsk({ stats: { agent_enabled: true } });
    await waitFor(() => expect(Array.from(select().options).map((o) => o.value)).toContain("agent"));
    await env.user.selectOptions(select(), "vector");
    env.setWorkspace({ ...WS });
    await waitFor(() => expect(Array.from(select().options).map((o) => o.value)).toEqual(["hybrid"]));
    expect(select().value).toBe("hybrid");
  });

  it("an ask carries the workspace id, the as-of value and the token header", async () => {
    const env = mountAsk({ workspace: { ...WS, asOf: "2026-02-01T00:00:00Z" }, ask: () => responseOf(fixtureBytes("workspace_done")) });
    await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "What does my document say?");
    await env.user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("done"));
    const call = env.posts()[0];
    expect(JSON.parse(String(call.init?.body))).toMatchObject({ workspace_id: WS.workspace.id, as_of: "2026-02-01T00:00:00Z", strategy: "hybrid" });
    expect(call.init?.headers).toMatchObject({ "X-Workspace-Token": "secret-token" });
  });

  it("a workspace answer marks the chips of its stale citations, and only those", async () => {
    const OLD = "doc:0123456789ab:v1:0007";
    const NEW = "doc:0123456789ab:v2:0007";
    const event = {
      answer: `Old [${OLD}] and new [${NEW}].`,
      citations: [OLD, NEW],
      hallucinated: [],
      cached: false,
      workspace: { id_hash: "x", doc_chunks: 2, stale_citations: [OLD], suspicious: false },
    };
    const env = mountAsk({ workspace: WS, ask: () => responseOf(frame("done", event)) });
    await env.user.type(screen.getByRole("textbox", { name: "Your question" }), "q");
    await env.user.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("done"));
    const marked = Array.from(document.querySelectorAll("[data-stale]"));
    expect(marked.map((c) => c.getAttribute("data-id"))).toEqual([OLD]);
    expect(marked[0].getAttribute("title")).toMatch(/a newer version exists/);
  });
});
