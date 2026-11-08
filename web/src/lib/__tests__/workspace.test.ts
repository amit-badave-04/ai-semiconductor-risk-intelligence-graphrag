import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  WS_SESSION_KEY,
  createWorkspace,
  deleteWorkspace,
  isWorkspaceId,
  isWorkspaceToken,
  loadWorkspace,
  parseDocuments,
  readStoredWorkspace,
  restoreWorkspace,
  staleClass,
  uploadTargetOptions,
  uploadTitleFor,
  versionAskAsOfOptions,
  versionTimeline,
  writeStoredWorkspace,
} from "../workspace";

// R28, R32, S8, S4 (docs/specs/m5b-frontend.md): the workspace's storage shape, its three calls, and the pure functions of the
// panel (tests/ui/workspace.test.mjs titles 6 and 11 to 12 and 25 to 29: staleClass, versionTimeline, uploadTargetOptions,
// uploadTitleFor, versionAskAsOfOptions; the rest of that file lives in citations, jobs, changes and freshness tests).

const ID = "0123456789abcdef0123456789abcdef";
const TOKEN = "tok_en-0123456789"; // a fake test token, not a credential - gitleaks:allow
const SESSION = { id: ID, token: TOKEN, expires_at: "2026-11-09T10:00:00+00:00" };
const DOC = "doc:0123456789ab:v2:0007";

function json(body: unknown, status = 200, statusText = ""): Response {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "content-type": "application/json" } });
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => handler(String(input), init));
  vi.stubGlobal("fetch", fn);
  return fn;
}

beforeEach(() => {
  window.sessionStorage.clear();
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the storage shape (R32, S8)", () => {
  it("writes exactly {id, token, expires_at} under semigraph:workspace in sessionStorage, and nowhere else", () => {
    writeStoredWorkspace(SESSION);
    expect(window.sessionStorage.length).toBe(1);
    const raw = window.sessionStorage.getItem(WS_SESSION_KEY);
    expect(WS_SESSION_KEY).toBe("semigraph:workspace");
    expect(JSON.parse(raw ?? "null")).toEqual(SESSION);
    expect(Object.keys(JSON.parse(raw ?? "{}"))).toEqual(["id", "token", "expires_at"]);
    expect(window.localStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  it("drops a field that is not part of the shape, so nothing else is persisted", () => {
    writeStoredWorkspace({ ...SESSION, extra: "x" } as typeof SESSION);
    expect(Object.keys(JSON.parse(window.sessionStorage.getItem(WS_SESSION_KEY) ?? "{}"))).toEqual(["id", "token", "expires_at"]);
  });

  it("reads back what the legacy page wrote, and a null expiry", () => {
    window.sessionStorage.setItem(WS_SESSION_KEY, JSON.stringify({ id: ID, token: TOKEN, expires_at: null }));
    expect(readStoredWorkspace()).toEqual({ id: ID, token: TOKEN, expires_at: null });
    window.sessionStorage.setItem(WS_SESSION_KEY, JSON.stringify({ id: ID, token: TOKEN }));
    expect(readStoredWorkspace()).toEqual({ id: ID, token: TOKEN, expires_at: null });
  });

  it("removes the key when told to close the workspace", () => {
    writeStoredWorkspace(SESSION);
    writeStoredWorkspace(null);
    expect(window.sessionStorage.getItem(WS_SESSION_KEY)).toBeNull();
  });

  it.each([
    ["not JSON", "{nope"],
    ["an array", JSON.stringify([ID, TOKEN])],
    ["a string", JSON.stringify("x")],
    ["an upper-case id", JSON.stringify({ id: ID.toUpperCase(), token: TOKEN, expires_at: null })],
    ["an id with a trailing newline", JSON.stringify({ id: `${ID}\n`, token: TOKEN, expires_at: null })],
    ["a path-like id", JSON.stringify({ id: "../../api/admin", token: TOKEN, expires_at: null })],
    ["an empty token", JSON.stringify({ id: ID, token: "", expires_at: null })],
    ["a token with a line break", JSON.stringify({ id: ID, token: "a\r\nX-Evil: 1", expires_at: null })],
    ["a numeric token", JSON.stringify({ id: ID, token: 12345, expires_at: null })],
    ["a numeric expiry", JSON.stringify({ id: ID, token: TOKEN, expires_at: 5 })],
  ])("treats %s in storage as no workspace", (_name, raw) => {
    window.sessionStorage.setItem(WS_SESSION_KEY, raw);
    expect(readStoredWorkspace()).toBeNull();
  });

  it("never throws when sessionStorage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("full", "QuotaExceededError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(readStoredWorkspace()).toBeNull();
    expect(() => writeStoredWorkspace(SESSION)).not.toThrow();
    expect(() => writeStoredWorkspace(null)).not.toThrow();
  });

  it("never throws when the sessionStorage accessor itself throws", () => {
    vi.spyOn(window, "sessionStorage", "get").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(readStoredWorkspace()).toBeNull();
    expect(() => writeStoredWorkspace(SESSION)).not.toThrow();
  });
});

describe("the id and token grammar (S4, S8)", () => {
  it("accepts a server id and refuses everything else", () => {
    expect(isWorkspaceId(ID)).toBe(true);
    for (const bad of ["", ID.slice(1), `${ID}0`, ID.toUpperCase(), `${ID}\n`, "..", "../x", "%2e%2e", 5, null, undefined]) {
      expect(isWorkspaceId(bad)).toBe(false);
    }
  });

  it("accepts a header-safe token and refuses control characters, spaces and an empty string", () => {
    expect(isWorkspaceToken(TOKEN)).toBe(true);
    for (const bad of ["", "a b", "a\nb", "a\rb", "a\tb", "caf\u00e9", "x".repeat(513), 7, null]) {
      expect(isWorkspaceToken(bad)).toBe(false);
    }
  });
});

describe("createWorkspace (POST /api/workspace)", () => {
  it("posts the token (or null) in the body, at once, and returns the session", async () => {
    const fn = stubFetch(() => json({ workspace_id: ID, token: TOKEN, expires_at: SESSION.expires_at, limits: {} }, 201));
    const result = await createWorkspace("cf-token");
    expect(result).toEqual({ kind: "ok", session: SESSION });
    const [url, init] = fn.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/workspace");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ turnstile_token: "cf-token" });
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    await createWorkspace(null);
    expect(JSON.parse((fn.mock.calls[1] as [string, RequestInit])[1].body as string)).toEqual({ turnstile_token: null });
  });

  it("shows the server's detail for a refusal, unchanged", async () => {
    stubFetch(() => json({ detail: "Bot check failed \u2014 reload the page and try again." }, 403, "Forbidden"));
    expect(await createWorkspace(null)).toEqual({
      kind: "error",
      detail: "Bot check failed \u2014 reload the page and try again.",
    });
  });

  it("falls back to the status text when the refusal is not JSON", async () => {
    stubFetch(() => new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }));
    expect(await createWorkspace(null)).toEqual({ kind: "error", detail: "Bad Gateway" });
  });

  it("reports a network failure in the legacy words", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    expect(await createWorkspace(null)).toEqual({ kind: "error", detail: "network error creating a workspace" });
  });

  it.each([
    ["an id outside the grammar", { workspace_id: "../x", token: TOKEN, expires_at: null }],
    ["a token with a line break", { workspace_id: ID, token: "a\nb", expires_at: null }],
    ["no token", { workspace_id: ID }],
    ["not an object", [1, 2]],
  ])("refuses a 201 with %s instead of opening a workspace", async (_name, body) => {
    stubFetch(() => json(body, 201));
    expect(await createWorkspace(null)).toEqual({ kind: "error", detail: "could not create a workspace" });
  });

  it("refuses a 201 whose body is not JSON", async () => {
    stubFetch(() => new Response("nope", { status: 201 }));
    expect(await createWorkspace(null)).toEqual({ kind: "error", detail: "could not create a workspace" });
  });
});

const DOCS_BODY = {
  workspace_id: ID,
  expires_at: SESSION.expires_at,
  documents: [
    {
      document_id: "abc123456789",
      title: "Q2 board memo.pdf",
      latest_version: 2,
      versions: [
        { version: 1, created_at: "2026-09-01T00:00:00Z", is_current: false, status: "superseded" },
        { version: 2, created_at: "2026-09-15T00:00:00Z", is_current: true, status: "ready" },
      ],
    },
  ],
};

describe("loadWorkspace (GET /api/workspace/{id})", () => {
  it("sends the token in the X-Workspace-Token header only, never in the URL", async () => {
    const fn = stubFetch(() => json(DOCS_BODY));
    const result = await loadWorkspace({ id: ID, token: TOKEN, expires_at: null });
    const [url, init] = fn.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/workspace/${ID}`);
    expect(url).not.toContain(TOKEN);
    expect(init.headers).toEqual({ "X-Workspace-Token": TOKEN });
    expect(result.kind).toBe("ok");
    if (result.kind === "ok") {
      expect(result.expires_at).toBe(SESSION.expires_at);
      expect(result.documents.map((d) => d.document_id)).toEqual(["abc123456789"]);
    }
  });

  it("answers gone for a 404, and error (keep the last state) for everything else", async () => {
    stubFetch(() => json({ detail: "x" }, 404));
    expect(await loadWorkspace(SESSION)).toEqual({ kind: "gone" });
    for (const status of [401, 429, 500, 503]) {
      stubFetch(() => json({ detail: "x" }, status));
      expect(await loadWorkspace(SESSION)).toEqual({ kind: "error" });
    }
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    expect(await loadWorkspace(SESSION)).toEqual({ kind: "error" });
  });

  it("is an error, not data, for a 200 that is not JSON", async () => {
    stubFetch(() => new Response("<html>", { status: 200 }));
    expect(await loadWorkspace(SESSION)).toEqual({ kind: "error" });
  });

  it("makes no request for an id outside the grammar", async () => {
    const fn = stubFetch(() => json(DOCS_BODY));
    for (const id of ["..", "../x", ID.toUpperCase(), `${ID}\n`, "%2e%2e%2fadmin"]) {
      expect(await loadWorkspace({ id, token: TOKEN, expires_at: null })).toEqual({ kind: "error" });
    }
    expect(fn).not.toHaveBeenCalled();
  });

  it("reports a missing expiry as null", async () => {
    stubFetch(() => json({ documents: [] }));
    const result = await loadWorkspace(SESSION);
    expect(result).toEqual({ kind: "ok", expires_at: null, documents: [] });
  });

  it("passes the abort signal to fetch", async () => {
    const fn = stubFetch(() => json(DOCS_BODY));
    const controller = new AbortController();
    await loadWorkspace(SESSION, controller.signal);
    expect((fn.mock.calls[0] as [string, RequestInit])[1].signal).toBe(controller.signal);
  });
});

describe("restoreWorkspace (the Restore button)", () => {
  it("opens the workspace the server confirms, with the expiry it reports", async () => {
    stubFetch(() => json(DOCS_BODY));
    const result = await restoreWorkspace(ID, TOKEN);
    expect(result.kind).toBe("ok");
    if (result.kind === "ok") {
      expect(result.session).toEqual({ id: ID, token: TOKEN, expires_at: SESSION.expires_at });
      expect(result.documents).toHaveLength(1);
    }
  });

  it("says 'workspace not found' for a wrong token, a network failure and a 404 alike", async () => {
    stubFetch(() => json({ detail: "x" }, 404));
    expect(await restoreWorkspace(ID, TOKEN)).toEqual({ kind: "notFound" });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    expect(await restoreWorkspace(ID, TOKEN)).toEqual({ kind: "notFound" });
  });

  it("makes no request for an id or a token outside the grammar", async () => {
    const fn = stubFetch(() => json(DOCS_BODY));
    expect(await restoreWorkspace("../x", TOKEN)).toEqual({ kind: "notFound" });
    expect(await restoreWorkspace(`${ID}\n`, TOKEN)).toEqual({ kind: "notFound" });
    expect(await restoreWorkspace(ID, "a\nb")).toEqual({ kind: "notFound" });
    expect(await restoreWorkspace(ID, "")).toEqual({ kind: "notFound" });
    expect(fn).not.toHaveBeenCalled();
  });
});

describe("deleteWorkspace (DELETE /api/workspace/{id})", () => {
  it("sends DELETE with the token header and swallows a failure", async () => {
    const fn = stubFetch(() => new Response(null, { status: 204 }));
    await deleteWorkspace(SESSION);
    const [url, init] = fn.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/workspace/${ID}`);
    expect(init.method).toBe("DELETE");
    expect(init.headers).toEqual({ "X-Workspace-Token": TOKEN });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await expect(deleteWorkspace(SESSION)).resolves.toBeUndefined();
  });

  it("makes no request for an id outside the grammar", async () => {
    const fn = stubFetch(() => new Response(null, { status: 204 }));
    await deleteWorkspace({ id: "../x", token: TOKEN, expires_at: null });
    expect(fn).not.toHaveBeenCalled();
  });
});

describe("parseDocuments (a type guard for the documents of GET /api/workspace)", () => {
  it("keeps the fields the panel shows", () => {
    const docs = parseDocuments(DOCS_BODY);
    expect(docs).toHaveLength(1);
    expect(docs[0]).toEqual({
      document_id: "abc123456789",
      title: "Q2 board memo.pdf",
      latest_version: 2,
      versions: [
        { version: 1, created_at: "2026-09-01T00:00:00Z", is_current: false, status: "superseded" },
        { version: 2, created_at: "2026-09-15T00:00:00Z", is_current: true, status: "ready" },
      ],
    });
  });

  it("skips what is not a document and tolerates mistyped fields", () => {
    expect(parseDocuments(null)).toEqual([]);
    expect(parseDocuments({ documents: "x" })).toEqual([]);
    const docs = parseDocuments({
      documents: [
        null,
        { title: "no id" },
        { document_id: "d1", title: 5, latest_version: "2", versions: [null, { version: "x" }, { version: 3, is_current: "yes" }] },
      ],
    });
    expect(docs).toEqual([
      { document_id: "d1", title: null, latest_version: null, versions: [{ version: 3, created_at: null, is_current: false, status: null }] },
    ]);
  });
});

describe("staleClass (workspace.test.mjs 6)", () => {
  it("flags exactly the ids named in stale_citations", () => {
    expect(staleClass(DOC, [DOC])).toBe("stale");
    expect(staleClass(DOC, ["doc:0123456789ab:v1:0001"])).toBe("");
    expect(staleClass(DOC, [])).toBe("");
    expect(staleClass(DOC, undefined)).toBe("");
    expect(staleClass(DOC, null)).toBe("");
  });
});

describe("versionTimeline (workspace.test.mjs 11 and 12)", () => {
  it("marks exactly the current version", () => {
    const versions = [
      { version: 1, is_current: false, status: "superseded" },
      { version: 2, is_current: true, status: "current" },
    ];
    expect(versionTimeline(versions)).toBe("v1 (superseded) \u2192 v2 (current)");
  });

  it("handles a single version, an empty list and a missing one", () => {
    expect(versionTimeline([{ version: 1, is_current: true, status: "current" }])).toBe("v1 (current)");
    expect(versionTimeline([])).toBe("");
    expect(versionTimeline(undefined)).toBe("");
    expect(versionTimeline([{ version: 1, is_current: false, status: null }])).toBe("v1 (superseded)");
  });
});

describe("uploadTargetOptions and uploadTitleFor (workspace.test.mjs 25 to 27)", () => {
  it("always offers a new document first", () => {
    expect(uploadTargetOptions([])).toEqual([{ value: "", label: "Upload a new document" }]);
    expect(uploadTargetOptions(undefined)).toEqual([{ value: "", label: "Upload a new document" }]);
  });

  it("offers a 'new version of' entry per existing document, titled or not", () => {
    const docs = [
      { document_id: "abc123456789", title: "Q2 board memo.pdf" },
      { document_id: "def123456789", title: null },
    ];
    expect(uploadTargetOptions(docs)).toEqual([
      { value: "", label: "Upload a new document" },
      { value: "abc123456789", label: "New version of Q2 board memo.pdf" },
      { value: "def123456789", label: "New version of def123456789" },
    ]);
  });

  it("truncates a long file name to the server's own title cap", () => {
    const long = `${"a".repeat(200)}.pdf`;
    expect(uploadTitleFor(long, 120)).toHaveLength(120);
    expect(uploadTitleFor("short.txt", 120)).toBe("short.txt");
    expect(uploadTitleFor(null, 120)).toBe("");
  });
});

describe("versionAskAsOfOptions (workspace.test.mjs 28 and 29)", () => {
  it("offers 'current' plus one 'ask as of vN' per version, keyed by that version's created_at", () => {
    const versions = [
      { version: 1, created_at: "2026-09-01T00:00:00Z" },
      { version: 2, created_at: "2026-09-15T00:00:00Z" },
    ];
    expect(versionAskAsOfOptions(versions)).toEqual([
      { value: "", label: "ask as of: current" },
      { value: "2026-09-01T00:00:00Z", label: "ask as of v1" },
      { value: "2026-09-15T00:00:00Z", label: "ask as of v2" },
    ]);
  });

  it("tolerates an empty or missing version list", () => {
    expect(versionAskAsOfOptions([])).toEqual([{ value: "", label: "ask as of: current" }]);
    expect(versionAskAsOfOptions(undefined)).toEqual([{ value: "", label: "ask as of: current" }]);
  });
});
