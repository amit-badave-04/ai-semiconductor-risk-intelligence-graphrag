import { act, render, waitFor } from "@testing-library/react";
import { StrictMode, useEffect, type ReactNode } from "react";
import { MemoryRouter, useNavigate } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fixtureBytes, fixtureRefusals } from "@/lib/__tests__/fixtures";
import { frame, responseOf } from "@/lib/__tests__/sse-helpers";
import { getStats, resetStatsStore } from "@/lib/statsStore";
import { PAGE_FAILED_STATUS, STREAM_CUT_STATUS } from "@/lib/sse";
import { AskProvider } from "../AskProvider";
import * as askState from "../askState";
import { useAsk, type AskContextValue } from "../useAsk";
import { INERT_TURNSTILE, TurnstileContext, type TurnstileContextValue } from "../useTurnstile";
import { WorkspaceContext, type WorkspaceContextValue } from "../useWorkspace";

// R31, R33(1), R12/S12: the ask state above the router. One POST from a handler, never retried, never held back for a token,
// a true status on every exit, and a stream that survives a route change.

const WS_ID = "a".repeat(32);
const workspaceValue = (over: Partial<WorkspaceContextValue> = {}): WorkspaceContextValue => ({
  workspace: null,
  asOf: "",
  setAsOf: () => {},
  setWorkspace: () => {},
  generation: 0,
  docEvidenceEpoch: 0,
  invalidateDocEvidence: () => {},
  headers: () => ({}),
  ...over,
});
const turnstileValue = (over: Partial<TurnstileContextValue> = {}): TurnstileContextValue => ({ ...INERT_TURNSTILE, ...over });

interface Call {
  url: string;
  init: RequestInit | undefined;
}
let calls: Call[];
let askResponse: () => Response | Promise<Response>;

beforeEach(() => {
  calls = [];
  resetStatsStore();
  askResponse = () => responseOf(fixtureBytes("live_hybrid"));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      if (String(url) === "/api/ask") return askResponse();
      if (String(url) === "/api/stats") return new Response(JSON.stringify({ agent_enabled: true, models: { llm: "anthropic/claude-sonnet-5" } }), { status: 200 });
      return new Response("{}", { status: 404 });
    }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const posts = () => calls.filter((c) => c.url === "/api/ask");
const body = (call: Call) => JSON.parse(String(call.init?.body)) as Record<string, unknown>;

/** A component that hands the ask context to the test (a factory: a component must not write to its props). */
function captureFor(into: { current: AskContextValue | null }) {
  return function Capture() {
    const value = useAsk();
    useEffect(() => {
      into.current = value;
    });
    return null;
  };
}

function mount(opts: { ws?: WorkspaceContextValue; turnstile?: TurnstileContextValue; extra?: ReactNode } = {}) {
  const ask: { current: AskContextValue | null } = { current: null };
  const Capture = captureFor(ask);
  const view = render(
    <StrictMode>
      <TurnstileContext.Provider value={opts.turnstile ?? turnstileValue()}>
        <WorkspaceContext.Provider value={opts.ws ?? workspaceValue()}>
          <MemoryRouter>
            <AskProvider>
              <Capture />
              {opts.extra}
            </AskProvider>
          </MemoryRouter>
        </WorkspaceContext.Provider>
      </TurnstileContext.Provider>
    </StrictMode>,
  );
  return { ask, ...view };
}
const settle = (ask: { current: AskContextValue | null }) => waitFor(() => expect(ask.current?.status).not.toBe("streaming"));

describe("AskProvider: one POST, from a handler only", () => {
  it("is idle after mounting and sends no request at all, under StrictMode (S12)", () => {
    const { ask } = mount();
    expect(ask.current?.status).toBe("idle");
    expect(ask.current?.seq).toBe(0);
    expect(calls).toHaveLength(0);
  });

  it("an ask sends exactly one POST /api/ask under StrictMode, with the question and the strategy", async () => {
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "  What did Nvidia say?  ", strategy: "hybrid" }));
    await settle(ask);
    expect(posts()).toHaveLength(1);
    expect(body(posts()[0])).toEqual({ question: "What did Nvidia say?", strategy: "hybrid" });
    expect(posts()[0].init?.method).toBe("POST");
  });

  it("a second ask while one streams is ignored: two quick calls make one POST", async () => {
    const { ask } = mount();
    act(() => {
      ask.current?.ask({ question: "one" });
      ask.current?.ask({ question: "two" });
    });
    await settle(ask);
    expect(posts()).toHaveLength(1);
    expect(body(posts()[0]).question).toBe("one");
  });

  it("an empty or blank question sends nothing", () => {
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "   " }));
    act(() => ask.current?.ask({ question: "" }));
    expect(calls).toHaveLength(0);
    expect(ask.current?.seq).toBe(0);
  });

  it("is never retried: a failing request is one POST", async () => {
    askResponse = () => {
      throw new TypeError("network down");
    };
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(posts()).toHaveLength(1);
  });

  it("the answer sequence moves on with every ask and never decreases, reset included", async () => {
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "one" }));
    await settle(ask);
    expect(ask.current?.seq).toBe(1);
    act(() => ask.current?.reset());
    expect(ask.current).toMatchObject({ status: "idle", seq: 1, text: "", done: null, question: "" });
    act(() => ask.current?.ask({ question: "two" }));
    await settle(ask);
    expect(ask.current?.seq).toBe(2);
  });
});

describe("AskProvider: the state of an answer", () => {
  it("a live answer ends clean: the events are in the state, the status text is empty", async () => {
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current?.status).toBe("ended");
    expect(ask.current?.retrieval).not.toBeNull();
    expect(ask.current?.done?.answer).toBe(ask.current?.text);
    expect(ask.current?.statusText).toBeNull();
    expect(ask.current?.refusal).toBeNull();
  });

  it("the agent's steps and an escalation reach the state", async () => {
    askResponse = () => responseOf(fixtureBytes("agent_steps"));
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q", strategy: "agent" }));
    await settle(ask);
    expect(ask.current?.steps.length).toBeGreaterThan(0);
    askResponse = () => responseOf(fixtureBytes("escalated"));
    act(() => ask.current?.ask({ question: "q2" }));
    await settle(ask);
    expect(ask.current?.escalated).not.toBeNull();
    expect(ask.current?.steps).toHaveLength(0); // a new answer starts clean
  });

  it("a cached example with no token sends exactly one POST and ends with its answer", async () => {
    askResponse = () => responseOf(fixtureBytes("cached_done"));
    const { ask } = mount({ turnstile: turnstileValue({ siteKeySet: true, consume: () => null }) });
    act(() => ask.current?.ask({ question: "q", strategy: "hybrid" }));
    await settle(ask);
    expect(posts()).toHaveLength(1);
    expect(body(posts()[0]).turnstile_token).toBeUndefined();
    expect(ask.current?.done).toMatchObject({ cached: true });
  });

  it("a server error event is kept as the status text", async () => {
    askResponse = () => responseOf(fixtureBytes("error_before_delta"));
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current?.status).toBe("ended");
    expect(ask.current?.statusText).toBeTruthy();
    expect(ask.current?.done).toBeNull();
  });

  it("an error after some text keeps the text that arrived", async () => {
    askResponse = () => responseOf(fixtureBytes("error_after_delta"));
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current?.text.length).toBeGreaterThan(0);
    expect(ask.current?.statusText).toBeTruthy();
  });

  it("a stream cut mid-answer fails with the connection-lost text and keeps the text", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(frame("delta", { text: "Nvidia reported " }));
      },
      pull(c) {
        c.error(new TypeError("network error"));
      },
    });
    askResponse = () => new Response(stream, { status: 200 });
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current).toMatchObject({ status: "failed", statusText: STREAM_CUT_STATUS, text: "Nvidia reported ", done: null });
  });

  it("a request that never reaches the server says the network failed", async () => {
    askResponse = () => {
      throw new TypeError("Failed to fetch");
    };
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current).toMatchObject({ status: "failed", statusText: "network error \u2014 is the service online?" });
  });

  it("a page failure while taking an event in is reported, honestly and out loud", async () => {
    const report = vi.fn();
    vi.stubGlobal("reportError", report);
    vi.spyOn(askState, "applyEvent").mockImplementation(() => {
      throw new Error("boom");
    });
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current).toMatchObject({ status: "failed", statusText: PAGE_FAILED_STATUS });
    expect(report).toHaveBeenCalledTimes(1);
    expect(String((report.mock.calls[0][0] as Error).message)).toBe("boom");
  });

  it("a response without a body is a page failure and ends the ask", async () => {
    askResponse = () => ({ ok: true, status: 200, body: null }) as unknown as Response;
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current).toMatchObject({ status: "failed", statusText: PAGE_FAILED_STATUS });
  });
});

describe("AskProvider: refusals", () => {
  it.each(fixtureRefusals().map((r) => [r.name, r] as const))("the %s refusal is shown with its own words", async (_name, refusal) => {
    askResponse = () => new Response(JSON.stringify({ detail: refusal.detail }), { status: refusal.status, headers: refusal.retry_after ? { "Retry-After": refusal.retry_after } : {} });
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current?.status).toBe("ended");
    expect(ask.current?.refusal).toEqual({ status: refusal.status, detail: refusal.detail });
    expect(ask.current?.statusText).toBe(refusal.detail);
    expect(posts()).toHaveLength(1);
  });

  const botRefusal = () => new Response(JSON.stringify({ detail: "Bot check failed." }), { status: 403 });

  it("a 403 with a site key and no token adds the clause that says the check has not completed", async () => {
    askResponse = botRefusal;
    const { ask } = mount({ turnstile: turnstileValue({ siteKeySet: true, consume: () => null }) });
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current?.statusText).toBe("Bot check failed. (the bot check below has not completed yet \u2014 wait a moment and click Ask again)");
  });

  it("a 403 with a site key and a token that was sent adds the clause that says the check expired", async () => {
    askResponse = botRefusal;
    const { ask } = mount({ turnstile: turnstileValue({ siteKeySet: true, consume: () => "tok" }) });
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current?.statusText).toBe("Bot check failed. (the check expired \u2014 please click Ask again)");
  });

  it("a 403 with no site key adds no clause", async () => {
    askResponse = botRefusal;
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(ask.current?.statusText).toBe("Bot check failed.");
  });
});

describe("AskProvider: R33(3) the widget is reset at every legacy reset point (index.html:681, 712)", () => {
  const botRefusal = () => new Response(JSON.stringify({ detail: "Bot check failed." }), { status: 403 });

  it("a tokenless ask that gets the 403 resets the widget once (a site key is set)", async () => {
    askResponse = botRefusal;
    const clear = vi.fn();
    const { ask } = mount({ turnstile: turnstileValue({ siteKeySet: true, consume: () => null, clear }) });
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(clear).toHaveBeenCalledTimes(1);
  });

  it("a tokenless ask that is answered (a cached example) also resets the widget once", async () => {
    askResponse = () => responseOf(fixtureBytes("cached_done"));
    const clear = vi.fn();
    const { ask } = mount({ turnstile: turnstileValue({ siteKeySet: true, consume: () => null, clear }) });
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(clear).toHaveBeenCalledTimes(1);
  });

  it("an ask that sent a token resets through consume only: clear is not called a second time", async () => {
    askResponse = botRefusal;
    const clear = vi.fn();
    const consume = vi.fn(() => "tok");
    const { ask } = mount({ turnstile: turnstileValue({ siteKeySet: true, consume, clear }) });
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(consume).toHaveBeenCalledTimes(1);
    expect(clear).not.toHaveBeenCalled();
  });

  it("with no site key there is no widget: nothing is cleared", async () => {
    const clear = vi.fn();
    const { ask } = mount({ turnstile: turnstileValue({ siteKeySet: false, clear }) });
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(clear).not.toHaveBeenCalled();
  });
});

describe("AskProvider: the bot check and the workspace", () => {
  it("the token is taken once, at send time, and travels in the body", async () => {
    const consume = vi.fn(() => "tok-1");
    const { ask } = mount({ turnstile: turnstileValue({ siteKeySet: true, consume }) });
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(consume).toHaveBeenCalledTimes(1);
    expect(body(posts()[0]).turnstile_token).toBe("tok-1");
  });

  it("an ask with no workspace carries no workspace id, no as_of and no token header", async () => {
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(body(posts()[0])).not.toHaveProperty("workspace_id");
    expect(body(posts()[0])).not.toHaveProperty("as_of");
    expect(posts()[0].init?.headers).not.toHaveProperty("X-Workspace-Token");
  });

  it("a workspace ask carries the workspace id, the as-of value as it is, and the token only in the header", async () => {
    const ws = workspaceValue({ workspace: { id: WS_ID, token: "secret-token", expires_at: null }, asOf: "2026-02-01T00:00:00Z", headers: () => ({ "X-Workspace-Token": "secret-token" }) });
    const { ask } = mount({ ws });
    act(() => ask.current?.ask({ question: "q", strategy: "hybrid" }));
    await settle(ask);
    const call = posts()[0];
    expect(body(call)).toMatchObject({ workspace_id: WS_ID, as_of: "2026-02-01T00:00:00Z", strategy: "hybrid" });
    expect(JSON.stringify(body(call))).not.toContain("secret-token");
    expect(call.init?.headers).toMatchObject({ "X-Workspace-Token": "secret-token", "Content-Type": "application/json" });
  });

  it("a workspace ask with an empty as-of sends no as_of", async () => {
    const ws = workspaceValue({ workspace: { id: WS_ID, token: "t", expires_at: null }, asOf: "", headers: () => ({ "X-Workspace-Token": "t" }) });
    const { ask } = mount({ ws });
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    expect(body(posts()[0])).not.toHaveProperty("as_of");
  });
});

describe("AskProvider: the stats reload and the stop", () => {
  it("reloads /api/stats after an ask, whatever way it ended", async () => {
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await settle(ask);
    await waitFor(() => expect(calls.filter((c) => c.url === "/api/stats")).toHaveLength(1));
    await waitFor(() => expect(getStats()).not.toBeNull());
    askResponse = () => new Response(JSON.stringify({ detail: "paused" }), { status: 503 });
    act(() => ask.current?.ask({ question: "q2" }));
    await settle(ask);
    await waitFor(() => expect(calls.filter((c) => c.url === "/api/stats")).toHaveLength(2));
  });

  it("abort() stops the open stream: the status is aborted and says nothing about a lost connection", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(frame("delta", { text: "Partial " }));
      },
      pull: () => new Promise(() => {}),
    });
    askResponse = () => new Response(stream, { status: 200 });
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await waitFor(() => expect(ask.current?.text).toBe("Partial "));
    act(() => ask.current?.abort());
    await settle(ask);
    expect(ask.current).toMatchObject({ status: "aborted", statusText: null, text: "Partial " });
  });

  it("the page unloading (pagehide) stops the open stream", async () => {
    askResponse = () => new Response(new ReadableStream<Uint8Array>({ pull: () => new Promise(() => {}) }), { status: 200 });
    const { ask } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await waitFor(() => expect(ask.current?.status).toBe("streaming"));
    act(() => void window.dispatchEvent(new Event("pagehide")));
    await settle(ask);
    expect(ask.current?.status).toBe("aborted");
  });

  it("abort() with nothing open does nothing", () => {
    const { ask } = mount();
    expect(() => act(() => ask.current?.abort())).not.toThrow();
    expect(ask.current?.status).toBe("idle");
  });

  it("the provider unmounting aborts the open stream (its request signal is aborted)", async () => {
    askResponse = () => new Response(new ReadableStream<Uint8Array>({ pull: () => new Promise(() => {}) }), { status: 200 });
    const { ask, unmount } = mount();
    act(() => ask.current?.ask({ question: "q" }));
    await waitFor(() => expect(ask.current?.status).toBe("streaming"));
    const signal = posts()[0].init?.signal;
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});

describe("AskProvider: R31 a route change neither aborts the stream nor loses the answer", () => {
  it("navigating while the answer streams lets it finish, with one POST", async () => {
    let finish: () => void = () => {};
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(frame("delta", { text: "Nvidia " }));
        finish = () => {
          c.enqueue(frame("done", { answer: "Nvidia reported growth.", citations: [], hallucinated: [], cached: true }));
          c.close();
        };
      },
    });
    askResponse = () => new Response(stream, { status: 200 });
    const nav: { current: ReturnType<typeof useNavigate> | null } = { current: null };
    function Nav() {
      const navigate = useNavigate();
      useEffect(() => {
        nav.current = navigate;
      });
      return null;
    }
    const { ask } = mount({ extra: <Nav /> });
    act(() => ask.current?.ask({ question: "q" }));
    await waitFor(() => expect(ask.current?.text).toBe("Nvidia "));
    act(() => void nav.current?.("/method/"));
    expect(ask.current?.status).toBe("streaming");
    act(() => finish());
    await settle(ask);
    expect(ask.current).toMatchObject({ status: "ended", text: "Nvidia reported growth." });
    expect(posts()).toHaveLength(1);
  });
});
