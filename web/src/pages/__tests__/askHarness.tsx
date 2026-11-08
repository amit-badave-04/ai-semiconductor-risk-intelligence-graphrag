import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router";
import { vi } from "vitest";
import { EvidenceDrawerProvider } from "@/components/EvidenceDrawer";
import { resetChipEvidence } from "@/lib/chipEvidence";
import { resetStatsStore } from "@/lib/statsStore";
import { AskProvider } from "@/state/AskProvider";
import { INERT_TURNSTILE, TurnstileContext, type TurnstileContextValue } from "@/state/useTurnstile";
import { WorkspaceContext, type WorkspaceContextValue } from "@/state/useWorkspace";
import { Ask } from "../Ask";

// Test harness of the Ask page tests: the real Ask page, AskProvider and evidence drawer, over a scripted fetch, with a Turnstile
// gate and a workspace the test controls. Test code only.

export const EXAMPLES = {
  source: "s",
  examples: [
    { id: "N1", type: "numeric", question: "What was Nvidia's total revenue for the fiscal year ended January 25, 2026?" },
    { id: "N2", type: "numeric", question: "How much did AMD spend on research?" },
    { id: "R1", type: "refusal", question: "Samsung?" },
  ],
};

export interface Env {
  /** The /api/stats body; null makes the request fail. */
  stats?: Record<string, unknown> | null;
  examples?: unknown;
  /** The /api/ask response, made per request. */
  ask?: () => Response | Promise<Response>;
  workspace?: Partial<WorkspaceContextValue>;
  turnstile?: Partial<TurnstileContextValue>;
}

export interface Call {
  url: string;
  init: RequestInit | undefined;
}

export function workspaceValue(over: Partial<WorkspaceContextValue> = {}): WorkspaceContextValue {
  return {
    workspace: null,
    asOf: "",
    setAsOf: () => {},
    setWorkspace: () => {},
    generation: 0,
    docEvidenceEpoch: 0,
    invalidateDocEvidence: () => {},
    headers: () => ({}),
    ...over,
  };
}

export function mountAsk(env: Env = {}) {
  resetStatsStore();
  resetChipEvidence();
  const calls: Call[] = [];
  const stats = env.stats === undefined ? {} : env.stats;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    const path = String(url);
    if (path === "/api/ask") return env.ask ? env.ask() : new Response("{}", { status: 500 });
    if (path === "/api/stats") return stats === null ? new Response("{}", { status: 500 }) : new Response(JSON.stringify(stats), { status: 200 });
    if (path === "/api/examples") return new Response(JSON.stringify(env.examples ?? EXAMPLES), { status: 200 });
    return new Response("{}", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  const user = userEvent.setup();
  const tree = (ws: WorkspaceContextValue) => (
    <StrictMode>
      <TurnstileContext.Provider value={{ ...INERT_TURNSTILE, ...env.turnstile }}>
        <WorkspaceContext.Provider value={ws}>
          <MemoryRouter>
            <AskProvider>
              <EvidenceDrawerProvider>
                <Ask />
              </EvidenceDrawerProvider>
            </AskProvider>
          </MemoryRouter>
        </WorkspaceContext.Provider>
      </TurnstileContext.Provider>
    </StrictMode>
  );
  const view = render(tree(workspaceValue(env.workspace)));
  return {
    calls,
    user,
    fetchMock,
    posts: () => calls.filter((c) => c.url === "/api/ask"),
    statsCalls: () => calls.filter((c) => c.url === "/api/stats"),
    setWorkspace: (over: Partial<WorkspaceContextValue>) => view.rerender(tree(workspaceValue(over))),
    ...view,
  };
}
