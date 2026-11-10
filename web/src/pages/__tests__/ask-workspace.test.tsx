import { screen, waitFor, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fixtureBytes } from "@/lib/__tests__/fixtures";
import { frame, responseOf } from "@/lib/__tests__/sse-helpers";
import { EXAMPLES, mountAsk } from "./askHarness";

// R23: while a workspace is open every example is disabled and Depth offers only Standard (`hybrid`; another strategy is a
// guaranteed 400); asks as of a version carry `as_of`. docs/specs/m5b-redesign.md RD5 and RD7 moved the examples into the
// starters and "Show all examples", and the strategy select into the Depth radio group of "More options".
afterEach(() => vi.unstubAllGlobals());

const WS = { workspace: { id: "a".repeat(32), token: "secret-token", expires_at: null }, headers: () => ({ "X-Workspace-Token": "secret-token" }) };

async function depthValues(user: UserEvent): Promise<string[]> {
  const toggle = screen.getByRole("button", { name: /More options/ });
  if (toggle.getAttribute("aria-expanded") !== "true") await user.click(toggle);
  return (within(screen.getByRole("group", { name: "Depth" })).getAllByRole("radio") as HTMLInputElement[]).map((r) => r.value);
}
const chosen = () => (within(screen.getByRole("group", { name: "Depth" })).getAllByRole("radio") as HTMLInputElement[]).find((r) => r.checked)?.value;

describe("the Ask page with a workspace open (R23)", () => {
  it("disables the examples panel: no example can become a paid live call", async () => {
    const env = mountAsk({ workspace: WS, ask: () => responseOf(fixtureBytes("workspace_done")) });
    await env.user.click(await screen.findByRole("button", { name: "Show all examples, grouped by company" }));
    const starters = screen.getByRole("region", { name: /Or try an example/ });
    const buttons = within(starters).getAllByRole("button").filter((b) => b.closest("li") !== null);
    expect(buttons).toHaveLength(EXAMPLES.examples.length);
    for (const b of buttons) expect(b).toBeDisabled();
    await env.user.click(buttons[0]);
    expect(env.posts()).toHaveLength(0);
  });

  it("the strategy select offers only hybrid, even when the agent is enabled", async () => {
    const env = mountAsk({ workspace: WS, stats: { agent_enabled: true } });
    await new Promise((r) => setTimeout(r, 30));
    expect(await depthValues(env.user)).toEqual(["hybrid"]);
    expect(screen.getByText("With a workspace open, questions use Standard.")).toBeInTheDocument();
  });

  it("a workspace that was left on agent or vector asks as hybrid", async () => {
    const env = mountAsk({ stats: { agent_enabled: true } });
    await waitFor(async () => expect(await depthValues(env.user)).toContain("agent"));
    await env.user.click(screen.getByRole("radio", { name: "Basic text search" }));
    env.setWorkspace({ ...WS });
    await waitFor(async () => expect(await depthValues(env.user)).toEqual(["hybrid"]));
    expect(chosen()).toBe("hybrid");
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
