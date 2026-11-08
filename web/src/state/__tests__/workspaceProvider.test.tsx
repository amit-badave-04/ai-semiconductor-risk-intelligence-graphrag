import { act, cleanup, render, waitFor } from "@testing-library/react";
import { StrictMode, useEffect, type ReactNode } from "react";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WS_SESSION_KEY } from "@/lib/workspace";
import { INERT_TURNSTILE, TurnstileContext, type TurnstileContextValue } from "../useTurnstile";
import { useWorkspace, type WorkspaceContextValue } from "../useWorkspace";
import { useWorkspaceUploads, type WorkspaceUploadsValue } from "../useWorkspaceUploads";
import { WorkspaceProvider } from "../WorkspaceProvider";

// R32, R34, S8, S12 (docs/specs/m5b-frontend.md): the provider that owns the workspace session, the documents, the upload queue
// and the job watchers. Ported from tests/ui/upload_flow.test.mjs 1 to 6, 15 to 18, 21 and 24 (the as-of, the queue and the
// evidence-dropping signal); the rest of that file is lib/jobs.test.ts, lib/jobStream.test.ts and the drawer (worker B).

const ID = "0123456789abcdef0123456789abcdef";
const OTHER_ID = "fedcba9876543210fedcba9876543210";
const TOKEN = "tok-abc_123";
const EXPIRES = "2026-11-09T10:00:00+00:00";
const DOC = "abc123456789";
const V1 = "2026-09-01T00:00:00Z";
const V2 = "2026-09-15T00:00:00Z";

const jobId = (n: number) => n.toString(16).padStart(16, "0");
const docsBody = (id = ID) => ({
  workspace_id: id,
  expires_at: EXPIRES,
  documents: [
    {
      document_id: DOC,
      title: "memo.txt",
      latest_version: 2,
      versions: [
        { version: 1, created_at: V1, is_current: false, status: "superseded" },
        { version: 2, created_at: V2, is_current: true, status: "ready" },
      ],
    },
  ],
});

interface Server {
  calls: { method: string; url: string; headers: Record<string, string>; signal: AbortSignal | null | undefined }[];
  count: (method: string, pattern: RegExp) => number;
  streams: Map<string, { push: (state: string) => void; closed: boolean }>;
  open: () => number;
  maxOpen: () => number;
  status: { workspace: number };
  postStatus: { value: number };
}

function installServer(): Server {
  const calls: Server["calls"] = [];
  const streams: Server["streams"] = new Map();
  let posts = 0;
  let open = 0;
  let maxOpen = 0;
  const status = { workspace: 200 };
  const postStatus = { value: 202 };
  const encoder = new TextEncoder();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({ method, url, headers: (init?.headers ?? {}) as Record<string, string>, signal: init?.signal });
      if (method === "POST" && /\/documents$/.test(url)) {
        posts += 1;
        if (postStatus.value !== 202) return new Response(JSON.stringify({ detail: "refused" }), { status: postStatus.value });
        return new Response(JSON.stringify({ job_id: jobId(posts), document_id: DOC, version: posts }), { status: 202 });
      }
      if (method === "GET" && /\/jobs\/[0-9a-f]{16}$/.test(url)) {
        const id = url.slice(-16);
        open += 1;
        maxOpen = Math.max(maxOpen, open);
        let controller!: ReadableStreamDefaultController<Uint8Array>;
        const entry = { closed: false, push: (state: string) => {
          controller.enqueue(encoder.encode(`event: job\ndata: ${JSON.stringify({ job_id: id, state, document_id: DOC, version: 1 })}\n\n`));
        } };
        const body = new ReadableStream<Uint8Array>({
          start(c) {
            controller = c;
          },
          cancel() {
            entry.closed = true;
            open -= 1;
          },
        });
        streams.set(id, entry);
        init?.signal?.addEventListener("abort", () => {
          if (!entry.closed) {
            entry.closed = true;
            open -= 1;
          }
        });
        return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
      }
      if (method === "GET" && /\/api\/workspace\/[0-9a-f]{32}$/.test(url)) {
        if (status.workspace !== 200) return new Response(JSON.stringify({ detail: "x" }), { status: status.workspace });
        return new Response(JSON.stringify(docsBody(url.slice(-32))), { status: 200 });
      }
      if (method === "POST" && url === "/api/workspace") {
        return new Response(JSON.stringify({ workspace_id: OTHER_ID, token: "tok-new", expires_at: EXPIRES }), { status: 201 });
      }
      if (method === "DELETE") return new Response(null, { status: 204 });
      return new Response("{}", { status: 404 });
    }),
  );
  return {
    calls,
    count: (method, pattern) => calls.filter((c) => c.method === method && pattern.test(c.url)).length,
    streams,
    open: () => open,
    maxOpen: () => maxOpen,
    status,
    postStatus,
  };
}

interface Gate extends TurnstileContextValue {
  held: { token: string | null };
  clears: { count: number };
}

function makeGate(siteKeySet = false, token: string | null = null): Gate {
  const held = { token };
  const clears = { count: 0 };
  return {
    ...INERT_TURNSTILE,
    siteKeySet,
    siteKeyState: siteKeySet ? { kind: "key", siteKey: "k" } : { kind: "none" },
    consume: () => {
      const used = held.token;
      held.token = null;
      return used;
    },
    waitForToken: () => Promise.resolve(null),
    clear: () => {
      clears.count += 1;
      held.token = null;
    },
    held,
    clears,
  };
}

const seen: { ws: WorkspaceContextValue | null; up: WorkspaceUploadsValue | null; navigate: ((to: string) => void) | null } = {
  ws: null,
  up: null,
  navigate: null,
};

function Probe() {
  const ws = useWorkspace();
  const up = useWorkspaceUploads();
  const navigate = useNavigate();
  useEffect(() => {
    seen.ws = ws;
    seen.up = up;
    seen.navigate = navigate;
  });
  return null;
}

function tree(gate: TurnstileContextValue): ReactNode {
  return (
    <TurnstileContext.Provider value={gate}>
      <WorkspaceProvider>
        <MemoryRouter initialEntries={["/workspace/"]}>
          <Probe />
          <Routes>
            <Route path="workspace/" element={<p>workspace page</p>} />
            <Route path="freshness/" element={<p>freshness page</p>} />
          </Routes>
        </MemoryRouter>
      </WorkspaceProvider>
    </TurnstileContext.Provider>
  );
}

function mount(gate: TurnstileContextValue = makeGate(), strict = true) {
  return render(strict ? <StrictMode>{tree(gate)}</StrictMode> : tree(gate));
}

const ws = () => seen.ws as WorkspaceContextValue;
const up = () => seen.up as WorkspaceUploadsValue;
const file = (name: string) => new File(["hello"], name, { type: "text/plain" });
const store = (expires: string | null = null) =>
  window.sessionStorage.setItem(WS_SESSION_KEY, JSON.stringify({ id: ID, token: TOKEN, expires_at: expires }));
const settle = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

beforeEach(() => {
  window.sessionStorage.clear();
  seen.ws = seen.up = seen.navigate = null;
});

afterEach(() => vi.unstubAllGlobals());

describe("on load (R32, S12)", () => {
  it("makes no request at all without a stored session", async () => {
    const server = installServer();
    mount();
    await settle();
    expect(ws().workspace).toBeNull();
    expect(server.calls).toEqual([]);
  });

  it("restores a stored session at once, then reads the workspace with a GET only: no POST, no watcher, no bump", async () => {
    store(null);
    const server = installServer();
    mount();
    expect(ws().workspace).toEqual({ id: ID, token: TOKEN, expires_at: null });
    expect(ws().generation).toBe(0);
    await waitFor(() => expect(up().documents).toHaveLength(1));
    expect(server.calls.every((c) => c.method === "GET" && c.url === `/api/workspace/${ID}`)).toBe(true);
    expect(server.calls[0].headers).toEqual({ "X-Workspace-Token": TOKEN });
    expect(ws().workspace?.expires_at).toBe(EXPIRES); // filled in by the read, not a new identity
    expect(ws().generation).toBe(0);
    expect(JSON.parse(window.sessionStorage.getItem(WS_SESSION_KEY) ?? "{}")).toEqual({ id: ID, token: TOKEN, expires_at: EXPIRES });
  });

  it("closes the workspace and forgets the stored session when the server says it is gone (404)", async () => {
    store(null);
    const server = installServer();
    server.status.workspace = 404;
    mount();
    await waitFor(() => expect(ws().workspace).toBeNull());
    expect(window.sessionStorage.getItem(WS_SESSION_KEY)).toBeNull();
  });

  it.each([429, 500, 503])("keeps the workspace and the stored session on a %i (it may be back in a moment)", async (code) => {
    store(null);
    const server = installServer();
    server.status.workspace = code;
    mount();
    await settle();
    await settle();
    expect(ws().workspace?.id).toBe(ID);
    expect(window.sessionStorage.getItem(WS_SESSION_KEY)).not.toBeNull();
  });

  it("ignores a stored value that is not a session, with no request", async () => {
    window.sessionStorage.setItem(WS_SESSION_KEY, JSON.stringify({ id: "../x", token: TOKEN, expires_at: null }));
    const server = installServer();
    mount();
    await settle();
    expect(ws().workspace).toBeNull();
    expect(server.calls).toEqual([]);
  });
});

describe("create, restore and delete", () => {
  it("create sends the token it holds, opens the new workspace and writes exactly the stored shape", async () => {
    const gate = makeGate(true, "cf-token");
    const server = installServer();
    mount(gate);
    let text = "unset";
    await act(async () => {
      text = await up().create();
    });
    expect(text).toBe("");
    const post = server.calls.find((c) => c.method === "POST");
    expect(post?.url).toBe("/api/workspace");
    expect(gate.held.token).toBeNull();
    expect(ws().workspace).toEqual({ id: OTHER_ID, token: "tok-new", expires_at: EXPIRES });
    expect(ws().generation).toBe(1);
    expect(JSON.parse(window.sessionStorage.getItem(WS_SESSION_KEY) ?? "{}")).toEqual(ws().workspace);
    expect(up().documents).toEqual([]);
  });

  it("create without a token still goes out at once and resets the widget afterwards", async () => {
    const gate = makeGate(true, null);
    installServer();
    mount(gate);
    await act(async () => {
      await up().create();
    });
    expect(gate.clears.count).toBe(1);
    expect(ws().workspace?.id).toBe(OTHER_ID);
  });

  it("create shows the server's text for a refusal and opens nothing", async () => {
    installServer();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Bot check failed" }), { status: 403 })));
    mount();
    let text = "";
    await act(async () => {
      text = await up().create();
    });
    expect(text).toBe("Bot check failed");
    expect(ws().workspace).toBeNull();
    expect(window.sessionStorage.length).toBe(0);
  });

  it("restore opens the workspace the server confirms, with its documents and expiry", async () => {
    const server = installServer();
    mount();
    let text = "unset";
    await act(async () => {
      text = await up().restore(ID, TOKEN);
    });
    expect(text).toBe("");
    expect(ws().workspace).toEqual({ id: ID, token: TOKEN, expires_at: EXPIRES });
    expect(up().documents).toHaveLength(1);
    expect(server.calls).toHaveLength(1);
  });

  it("restore says 'workspace not found' and keeps the open workspace when the server does not confirm", async () => {
    store(EXPIRES);
    const server = installServer();
    mount();
    await waitFor(() => expect(up().documents).toHaveLength(1));
    server.status.workspace = 404;
    let text = "";
    await act(async () => {
      text = await up().restore(OTHER_ID, "wrong-token");
    });
    expect(text).toBe("workspace not found");
    expect(ws().workspace?.id).toBe(ID);
    expect(ws().generation).toBe(0);
  });

  it("restore does nothing, with no request, when a field is empty or malformed", async () => {
    const server = installServer();
    mount();
    for (const [id, token] of [["", TOKEN], [ID, ""], ["   ", "  "]]) {
      await act(async () => {
        expect(await up().restore(id, token)).toBe("");
      });
    }
    expect(server.calls).toEqual([]);
    await act(async () => {
      expect(await up().restore("../x", TOKEN)).toBe("workspace not found");
    });
    expect(server.calls).toEqual([]);
  });

  it("restore trims the typed id and token", async () => {
    installServer();
    mount();
    await act(async () => {
      await up().restore(`  ${ID}\n`, ` ${TOKEN} `);
    });
    expect(ws().workspace).toMatchObject({ id: ID, token: TOKEN });
  });

  it("delete closes the workspace whatever the server answers, and forgets the stored session", async () => {
    store(EXPIRES);
    const server = installServer();
    mount();
    await waitFor(() => expect(up().documents).toHaveLength(1));
    await act(async () => {
      await up().remove();
    });
    expect(server.count("DELETE", /\/api\/workspace\//)).toBe(1);
    expect(ws().workspace).toBeNull();
    expect(ws().generation).toBe(1);
    expect(up().documents).toEqual([]);
    expect(window.sessionStorage.getItem(WS_SESSION_KEY)).toBeNull();
  });
});

describe("the as-of value (upload_flow 1 to 4)", () => {
  async function open() {
    store(EXPIRES);
    const server = installServer();
    mount();
    await waitFor(() => expect(up().documents).toHaveLength(1));
    return server;
  }

  it("keeps an instant that a document still offers when the documents are read again", async () => {
    await open();
    act(() => ws().setAsOf(V1));
    await act(async () => {
      await up().restore(ID, TOKEN); // the same workspace read again
    });
    expect(ws().asOf).toBe(V1);
  });

  it("drops an instant that no document offers any more, so a later ask falls back to current (2)", async () => {
    await open();
    act(() => ws().setAsOf("2020-01-01T00:00:00Z"));
    await act(async () => {
      await up().restore(ID, TOKEN);
    });
    expect(ws().asOf).toBe("");
  });

  it("keeps a date the visitor typed", async () => {
    await open();
    act(() => ws().setAsOf("2026-09-10"));
    await act(async () => {
      await up().restore(ID, TOKEN);
    });
    expect(ws().asOf).toBe("2026-09-10");
  });

  it("resets on a change of workspace, so a new workspace never inherits the old instant (4)", async () => {
    await open();
    act(() => ws().setAsOf(V1));
    await act(async () => {
      await up().create();
    });
    expect(ws().workspace?.id).toBe(OTHER_ID);
    expect(ws().asOf).toBe("");
  });
});

describe("the upload queue and its watchers (R34, S12) under StrictMode, with a route change in the middle", () => {
  async function openAndUpload(names: string[]) {
    store(EXPIRES);
    const server = installServer();
    mount();
    await waitFor(() => expect(up().documents).toHaveLength(1));
    const reads = server.count("GET", new RegExp(`/api/workspace/${ID}$`));
    act(() => up().upload(names.map(file), ""));
    return { server, reads };
  }

  it("sends exactly one POST per file, keeps one watcher per job and continues the queue across the route change", async () => {
    const { server } = await openAndUpload(["a.txt", "b.txt"]);
    await waitFor(() => expect(server.streams.has(jobId(1))).toBe(true));
    act(() => seen.navigate?.("/freshness/")); // the route changes while the first job runs
    await settle();
    expect(server.count("POST", /documents$/)).toBe(1); // the second file waits for the first job
    act(() => server.streams.get(jobId(1))?.push("ready"));
    await waitFor(() => expect(server.streams.has(jobId(2))).toBe(true));
    expect(server.count("POST", /documents$/)).toBe(2);
    act(() => server.streams.get(jobId(2))?.push("ready"));
    await waitFor(() => expect(up().rows.map((r) => r.text)).toEqual(["ready", "ready"]));
    expect(server.count("GET", /\/jobs\//)).toBe(2); // one request per job: never a second watcher
    expect(server.maxOpen()).toBe(1);
    expect(up().rows.map((r) => r.name)).toEqual(["a.txt", "b.txt"]);
  });

  it("signals the evidence drawer once per terminal job, reloads the documents, and announces the state", async () => {
    const { server, reads } = await openAndUpload(["a.txt"]);
    const epoch = ws().docEvidenceEpoch;
    await waitFor(() => expect(server.streams.has(jobId(1))).toBe(true));
    act(() => server.streams.get(jobId(1))?.push("parsing"));
    await settle();
    expect(ws().docEvidenceEpoch).toBe(epoch);
    act(() => server.streams.get(jobId(1))?.push("ready"));
    await waitFor(() => expect(ws().docEvidenceEpoch).toBe(epoch + 1));
    await waitFor(() => expect(server.count("GET", new RegExp(`/api/workspace/${ID}$`))).toBeGreaterThan(reads));
    expect(up().announcement).toBe("a.txt: ready");
    expect(ws().generation).toBe(0); // the identity did not change
  });

  it("signals for a failed job too", async () => {
    const { server } = await openAndUpload(["a.txt"]);
    const epoch = ws().docEvidenceEpoch;
    await waitFor(() => expect(server.streams.has(jobId(1))).toBe(true));
    act(() => server.streams.get(jobId(1))?.push("failed"));
    await waitFor(() => expect(ws().docEvidenceEpoch).toBe(epoch + 1));
    expect(up().rows[0]).toMatchObject({ tone: "err", terminal: true });
  });

  it("shows a refusal's own text and posts the next file", async () => {
    store(EXPIRES);
    const server = installServer();
    server.postStatus.value = 429;
    mount();
    await waitFor(() => expect(up().documents).toHaveLength(1));
    act(() => up().upload([file("a.txt"), file("b.txt")], ""));
    await waitFor(() => expect(up().rows).toHaveLength(2));
    expect(up().rows.map((r) => r.text)).toEqual(["refused", "refused"]);
    expect(server.count("POST", /documents$/)).toBe(2);
    expect(server.count("GET", /\/jobs\//)).toBe(0);
  });

  // Owner 2026-11-08 (R8): the gate's config state reaches the queue. Failed says so at once; loading is waited for.
  const configGate = (siteKeyState: TurnstileContextValue["siteKeyState"], siteKeySet = false, token: string | null = null) => ({
    ...makeGate(siteKeySet, token),
    siteKeyState,
  });

  it("says the bot check could not be loaded, and posts nothing, when /api/config failed", async () => {
    store(EXPIRES);
    const server = installServer();
    mount(configGate({ kind: "unknown", failed: true }), false);
    await waitFor(() => expect(up().documents).toHaveLength(1));
    act(() => up().upload([file("a.txt")], ""));
    await waitFor(() => expect(up().rows.map((r) => r.text)).toEqual(["could not load the bot check \u2014 reload the page and try again"]));
    expect(server.count("POST", /documents$/)).toBe(0);
  });

  it("waits for a /api/config that is still loading and sends the file once it answers with a key", async () => {
    store(EXPIRES);
    const server = installServer();
    const view = mount(configGate({ kind: "unknown", failed: false }), false);
    await waitFor(() => expect(up().documents).toHaveLength(1));
    act(() => up().upload([file("a.txt")], ""));
    await settle();
    expect(server.count("POST", /documents$/)).toBe(0);
    expect(up().rows.map((r) => r.text)).toEqual(["uploading\u2026"]);
    view.rerender(tree(configGate({ kind: "key", siteKey: "k" }, true, "cf-token")));
    await waitFor(() => expect(server.count("POST", /documents$/)).toBe(1));
    const post = server.calls.find((c) => c.method === "POST" && /documents$/.test(c.url));
    expect(post?.headers["X-Turnstile-Token"]).toBe("cf-token");
  });

  it("ends a wait for /api/config with the config-failure text when the config then fails", async () => {
    store(EXPIRES);
    const server = installServer();
    const view = mount(configGate({ kind: "unknown", failed: false }), false);
    await waitFor(() => expect(up().documents).toHaveLength(1));
    act(() => up().upload([file("a.txt")], ""));
    await settle();
    view.rerender(tree(configGate({ kind: "unknown", failed: true })));
    await waitFor(() => expect(up().rows.map((r) => r.text)).toEqual(["could not load the bot check \u2014 reload the page and try again"]));
    expect(server.count("POST", /documents$/)).toBe(0);
  });

  it("aborts the live watcher and posts nothing more when the workspace is deleted mid-queue", async () => {
    const { server } = await openAndUpload(["a.txt", "b.txt"]);
    await waitFor(() => expect(server.streams.has(jobId(1))).toBe(true));
    const watcher = server.calls.find((c) => /\/jobs\//.test(c.url));
    await act(async () => {
      await up().remove();
    });
    expect(watcher?.signal?.aborted).toBe(true);
    await settle();
    expect(server.count("POST", /documents$/)).toBe(1);
    expect(up().rows).toEqual([]);
  });

  it("aborts the live watcher when the provider unmounts", async () => {
    const { server } = await openAndUpload(["a.txt"]);
    await waitFor(() => expect(server.streams.has(jobId(1))).toBe(true));
    const watcher = server.calls.find((c) => /\/jobs\//.test(c.url));
    expect(watcher?.signal?.aborted).toBe(false);
    cleanup();
    expect(watcher?.signal?.aborted).toBe(true);
  });

  it("uploads nothing when no workspace is open", async () => {
    const server = installServer();
    mount();
    act(() => up().upload([file("a.txt")], ""));
    await settle();
    expect(server.calls).toEqual([]);
  });
});

