import { afterEach, describe, expect, it, vi } from "vitest";
import type { JobEvent } from "../events";
import type { WatchOptions, WatchResult } from "../jobStream";
import {
  BOT_TIMEOUT_TEXT,
  CONFIG_FAILED_TEXT,
  TOKEN_WAIT_MS,
  UploadQueue,
  jobStateText,
  type JobRow,
  type TokenSource,
} from "../jobs";

// R28, R33(2), R34, S12 (docs/specs/m5b-frontend.md): the upload queue. Ported from tests/ui/upload_flow.test.mjs (titles 5, 6,
// 7 to 11 and the evidence-dropping half of 15 to 18 and 21) and tests/ui/workspace.test.mjs (7 to 10: jobStateText).

const WS = "0123456789abcdef0123456789abcdef";
const OTHER_WS = "fedcba9876543210fedcba9876543210";
const TOKEN = "tok-abc";
const SESSION = { id: WS, token: TOKEN, expires_at: null };
const OTHER = { id: OTHER_WS, token: "tok-other", expires_at: null };

afterEach(() => vi.unstubAllGlobals());

describe("jobStateText (workspace.test.mjs 7 to 10)", () => {
  it("names each pipeline stage in plain words", () => {
    const text = (state: string) => jobStateText({ state } as unknown as JobEvent);
    expect(text("received")).toBe("queued");
    expect(text("validating")).toBe("checking");
    expect(text("parsing")).toBe("reading the document");
    expect(text("chunking")).toBe("splitting into sections");
    expect(text("embedding")).toBe("embedding");
    expect(text("comparing")).toBe("comparing with the previous version");
    expect(text("indexing")).toBe("saving");
    expect(text("ready")).toBe("ready");
  });

  it("shows embedding progress and an eta when given", () => {
    const job = (progress: unknown) => jobStateText({ state: "embedding", progress } as unknown as JobEvent);
    expect(job({ done: 3, total: 10, eta_s: 12.4 })).toBe("embedding 3/10 (~13s left)");
    expect(job({ done: 0, total: 5, eta_s: 0 })).toBe("embedding 0/5");
    expect(job({ done: 1, total: 5, eta_s: null })).toBe("embedding 1/5");
    expect(jobStateText({ state: "embedding" } as unknown as JobEvent)).toBe("embedding");
  });

  it("surfaces the fixed error message of a failed job, never a stack trace", () => {
    expect(jobStateText({ state: "failed", error: { code: "too_many_pages", message: "too many pages" } } as unknown as JobEvent)).toBe(
      "failed: too many pages",
    );
    expect(jobStateText({ state: "failed", error: { code: "only_code", message: "" } } as unknown as JobEvent)).toBe("failed: only_code");
    expect(jobStateText({ state: "failed" } as unknown as JobEvent)).toBe("failed: unknown error");
  });

  it("tolerates a missing or unknown job object", () => {
    expect(jobStateText(null)).toBe("unknown");
    expect(jobStateText({})).toBe("unknown");
    expect(jobStateText({ state: "some_future_state" })).toBe("some_future_state");
  });
});

// ---------------------------------------------------------------- the queue

interface Rig {
  queue: UploadQueue;
  posts: { url: string; headers: Record<string, string>; form: FormData; signal: AbortSignal | null | undefined }[];
  rows: () => readonly JobRow[];
  terminals: () => number;
  unchanged: () => number;
  announcements: string[];
  watches: { ws: string; job: string; token: string; options: WatchOptions; resolve: (r: WatchResult) => void }[];
  token: {
    siteKeyKnown: boolean;
    siteKeyFailed: boolean;
    siteKeySet: boolean;
    held: string | null;
    waited: number[];
    waitResult: string | null | "never";
    configWaited: number[];
    /** What the config answer turns out to be when the queue waits for it ("never": only an abort ends the wait). */
    configAnswer: "key" | "none" | "failed" | "timeout" | "never";
    /** Runs while the queue waits for the config (a test moves its clock here: the config took that long to answer). */
    onConfigWait: (() => void) | null;
  };
  respond: { handler: (n: number) => Response };
}

const jobId = (n: number) => n.toString(16).padStart(16, "0");

function accepted(n: number): Response {
  return new Response(JSON.stringify({ job_id: jobId(n), document_id: "abc123456789", version: n }), { status: 202 });
}

function rig(options: { deferWatch?: boolean } = {}): Rig {
  const posts: Rig["posts"] = [];
  const watches: Rig["watches"] = [];
  const announcements: string[] = [];
  let latest: readonly JobRow[] = [];
  let terminals = 0;
  let unchanged = 0;
  const token: Rig["token"] = {
    siteKeyKnown: true,
    siteKeyFailed: false,
    siteKeySet: false,
    held: null,
    waited: [],
    waitResult: null,
    configWaited: [],
    configAnswer: "timeout",
    onConfigWait: null,
  };
  const respond = { handler: (n: number): Response => accepted(n) };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      posts.push({
        url: String(input),
        headers: init?.headers as Record<string, string>,
        form: init?.body as FormData,
        signal: init?.signal,
      });
      return respond.handler(posts.length);
    }),
  );
  const tokenSource: TokenSource = {
    siteKeyKnown: () => token.siteKeyKnown,
    siteKeyFailed: () => token.siteKeyFailed,
    waitForSiteKey: (ms, signal) => {
      token.configWaited.push(ms);
      token.onConfigWait?.();
      if (token.configAnswer === "never") {
        return new Promise((resolve) => signal?.addEventListener("abort", () => resolve(false), { once: true }));
      }
      if (token.configAnswer === "key" || token.configAnswer === "none") {
        token.siteKeyKnown = true;
        token.siteKeySet = token.configAnswer === "key";
      }
      if (token.configAnswer === "failed") token.siteKeyFailed = true;
      return Promise.resolve(token.siteKeyKnown);
    },
    siteKeySet: () => token.siteKeySet,
    consume: () => {
      const used = token.held;
      token.held = null;
      return used;
    },
    waitForToken: (ms, signal) => {
      token.waited.push(ms);
      if (token.waitResult === "never") {
        return new Promise((resolve) => signal?.addEventListener("abort", () => resolve(null), { once: true }));
      }
      return Promise.resolve(token.waitResult);
    },
  };
  const watch = (ws: string, job: string, tok: string, opts: WatchOptions) =>
    new Promise<WatchResult>((resolve) => {
      watches.push({ ws, job, token: tok, options: opts, resolve });
      if (options.deferWatch !== true) {
        opts.onJob({ job_id: job, state: "ready", document_id: "abc123456789", version: 1 });
        resolve({ kind: "terminal", job: { job_id: job, state: "ready", document_id: "abc123456789", version: 1 } });
      }
    });
  const queue = new UploadQueue({
    token: tokenSource,
    watch,
    onRows: (rows) => {
      latest = rows;
    },
    onTerminal: () => {
      terminals += 1;
    },
    onUnchanged: () => {
      unchanged += 1;
    },
    onAnnounce: (text) => announcements.push(text),
  });
  queue.attach();
  return { queue, posts, rows: () => latest, terminals: () => terminals, unchanged: () => unchanged, announcements, watches, token, respond };
}

const file = (name: string, body = "x") => new File([body], name, { type: "text/plain" });
const flush = async () => {
  for (let i = 0; i < 200; i += 1) await Promise.resolve();
};

describe("UploadQueue: one file at a time (R34, workspace.test upload_flow 5)", () => {
  it("posts the next file only after the previous file's job reached its end", async () => {
    const r = rig({ deferWatch: true });
    r.queue.enqueue(SESSION, [file("a.txt"), file("b.txt")], "");
    await flush();
    expect(r.posts).toHaveLength(1);
    expect(r.watches).toHaveLength(1);
    r.watches[0].options.onJob({ job_id: r.watches[0].job, state: "parsing", document_id: "abc123456789", version: 1 });
    await flush();
    expect(r.posts).toHaveLength(1); // still only the first: its job has not ended
    r.watches[0].resolve({ kind: "terminal", job: { job_id: r.watches[0].job, state: "ready", document_id: "abc123456789", version: 1 } });
    await flush();
    expect(r.posts).toHaveLength(2);
    expect(r.watches).toHaveLength(2);
    r.watches[1].resolve({ kind: "terminal", job: { job_id: r.watches[1].job, state: "ready", document_id: "abc123456789", version: 1 } });
    await flush();
    expect(r.queue.busy).toBe(false);
  });

  it("starts exactly one watcher per job and exactly one POST per file", async () => {
    const r = rig();
    r.queue.enqueue(SESSION, [file("a.txt"), file("b.txt"), file("c.txt")], "");
    await flush();
    expect(r.posts).toHaveLength(3);
    expect(r.watches.map((w) => w.job)).toEqual([jobId(1), jobId(2), jobId(3)]);
    expect(new Set(r.watches.map((w) => w.job)).size).toBe(3);
  });

  it("keeps each dropped file's own outcome in its own row, even for the same file name", async () => {
    const r = rig();
    r.queue.enqueue(SESSION, [file("same.txt"), file("same.txt"), file("other.txt")], "");
    await flush();
    const rows = r.rows();
    expect(rows.map((row) => [row.name, row.text, row.tone])).toEqual([
      ["same.txt", "ready", "ok"],
      ["same.txt", "ready", "ok"],
      ["other.txt", "ready", "ok"],
    ]);
    expect(new Set(rows.map((row) => row.key)).size).toBe(3);
  });

  it("joins a later enqueue to the queue that is already running", async () => {
    const r = rig({ deferWatch: true });
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    r.queue.enqueue(SESSION, [file("b.txt")], "");
    await flush();
    expect(r.posts).toHaveLength(1);
    r.watches[0].resolve({ kind: "terminal", job: { job_id: r.watches[0].job, state: "ready", document_id: "d", version: 1 } });
    await flush();
    expect(r.posts).toHaveLength(2);
  });
});

describe("UploadQueue: what is sent", () => {
  it("posts the file, the title cut to 120 characters and the target document, with the workspace token in the header only", async () => {
    const r = rig();
    r.queue.enqueue(SESSION, [file(`${"a".repeat(200)}.pdf`, "hello")], "abc123456789");
    await flush();
    const post = r.posts[0];
    expect(post.url).toBe(`/api/workspace/${WS}/documents`);
    expect(post.url).not.toContain(TOKEN);
    expect(post.headers).toEqual({ "X-Workspace-Token": TOKEN });
    expect((post.form.get("title") as string).length).toBe(120);
    expect(post.form.get("document_id")).toBe("abc123456789");
    expect((post.form.get("file") as File).size).toBe(5);
  });

  it("sends no document_id for a new document", async () => {
    const r = rig();
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.posts[0].form.has("document_id")).toBe(false);
  });

  it("watches with the same workspace, job id and token as the upload", async () => {
    const r = rig();
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect([r.watches[0].ws, r.watches[0].job, r.watches[0].token]).toEqual([WS, jobId(1), TOKEN]);
  });
});

describe("UploadQueue: the bot-check token (R33(2), upload_flow 7 to 11)", () => {
  it("sends no token header and never waits when no site key is set", async () => {
    const r = rig();
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.token.waited).toEqual([]);
    expect(r.posts[0].headers).toEqual({ "X-Workspace-Token": TOKEN });
  });

  it("uses the token it holds at once, without waiting, and sends it as X-Turnstile-Token", async () => {
    const r = rig();
    r.token.siteKeySet = true;
    r.token.held = "held-token";
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.token.waited).toEqual([]);
    expect(r.posts[0].headers).toEqual({ "X-Workspace-Token": TOKEN, "X-Turnstile-Token": "held-token" });
  });

  it("waits up to 15 seconds for a token when a site key is set and none is held, then sends that token", async () => {
    const r = rig();
    r.token.siteKeySet = true;
    r.token.waitResult = "refreshed-token";
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(TOKEN_WAIT_MS).toBe(15000);
    expect(r.token.waited).toEqual([15000]);
    expect(r.posts[0].headers["X-Turnstile-Token"]).toBe("refreshed-token");
  });

  it("fails closed when the token never comes: the old row text, no POST, and the next file still gets its turn", async () => {
    const r = rig();
    r.token.siteKeySet = true;
    r.token.waitResult = null;
    r.queue.enqueue(SESSION, [file("a.txt"), file("b.txt")], "");
    await flush();
    expect(BOT_TIMEOUT_TEXT).toBe("bot check did not complete in time \u2014 try again");
    expect(r.posts).toHaveLength(0);
    expect(r.token.waited).toEqual([15000, 15000]);
    expect(r.rows().map((row) => [row.text, row.tone])).toEqual([
      [BOT_TIMEOUT_TEXT, "err"],
      [BOT_TIMEOUT_TEXT, "err"],
    ]);
  });

  it("R8 (owner 2026-11-08): a failed /api/config says so, never 'no key': its own row text, no POST, no wait, whether or not a token is held", async () => {
    const r = rig();
    r.token.siteKeyKnown = false;
    r.token.siteKeyFailed = true;
    r.token.held = "held-token";
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(CONFIG_FAILED_TEXT).toBe("could not load the bot check \u2014 reload the page and try again");
    expect(r.posts).toHaveLength(0);
    expect(r.token.waited).toEqual([]);
    expect(r.token.configWaited).toEqual([]);
    expect(r.token.held).toBe("held-token");
    expect(r.rows().map((row) => [row.text, row.tone, row.terminal])).toEqual([[CONFIG_FAILED_TEXT, "err", true]]);
  });

  it("R8 (owner 2026-11-08): while /api/config is still loading the upload waits for it within 15 s, then sends the held token", async () => {
    const r = rig();
    r.token.siteKeyKnown = false;
    r.token.configAnswer = "key";
    r.token.held = "held-token";
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.token.configWaited).toEqual([TOKEN_WAIT_MS]);
    expect(r.posts).toHaveLength(1);
    expect(r.posts[0].headers["X-Turnstile-Token"]).toBe("held-token");
  });

  it("R8: the config wait and the token wait share one 15 s budget: a config that answers at 10 s leaves 5 s for the token", async () => {
    let now = 1_000_000;
    const clock = vi.spyOn(Date, "now").mockImplementation(() => now);
    try {
      const r = rig();
      r.token.siteKeyKnown = false;
      r.token.configAnswer = "key";
      r.token.onConfigWait = () => {
        now += 10_000;
      };
      r.token.waitResult = null;
      r.queue.enqueue(SESSION, [file("a.txt")], "");
      await flush();
      expect(r.token.configWaited).toEqual([TOKEN_WAIT_MS]);
      expect(r.token.waited).toEqual([TOKEN_WAIT_MS - 10_000]);
      expect(r.posts).toHaveLength(0);
      expect(r.rows().map((row) => [row.text, row.tone])).toEqual([[BOT_TIMEOUT_TEXT, "err"]]);
    } finally {
      clock.mockRestore();
    }
  });

  it("R8: a config that settles on 'none' while the upload waits sends it with no token header", async () => {
    const r = rig();
    r.token.siteKeyKnown = false;
    r.token.configAnswer = "none";
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.posts).toHaveLength(1);
    expect(r.posts[0].headers).toEqual({ "X-Workspace-Token": TOKEN });
  });

  it("R8: a config still unknown after the wait is never taken for none: the old timeout text, no POST", async () => {
    const r = rig();
    r.token.siteKeyKnown = false;
    r.token.held = "held-token";
    r.token.configAnswer = "timeout";
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.token.configWaited).toEqual([TOKEN_WAIT_MS]);
    expect(r.posts).toHaveLength(0);
    expect(r.rows().map((row) => [row.text, row.tone])).toEqual([[BOT_TIMEOUT_TEXT, "err"]]);
  });

  it("R8: a config that fails while the upload waits ends with the config-failure text, not the timeout text", async () => {
    const r = rig();
    r.token.siteKeyKnown = false;
    r.token.configAnswer = "failed";
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.posts).toHaveLength(0);
    expect(r.rows().map((row) => [row.text, row.tone])).toEqual([[CONFIG_FAILED_TEXT, "err"]]);
  });

  it("R8: stops waiting for the config when the queue is aborted, and says nothing", async () => {
    const r = rig();
    r.token.siteKeyKnown = false;
    r.token.configAnswer = "never";
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    r.queue.abortSession();
    await flush();
    expect(r.posts).toHaveLength(0);
    expect(r.rows()).toEqual([]);
  });

  it("never sends an empty token header: a second file waits out a slow re-solve instead", async () => {
    const r = rig();
    r.token.siteKeySet = true;
    r.token.held = "first";
    r.token.waitResult = "refreshed-token";
    r.queue.enqueue(SESSION, [file("a.txt"), file("b.txt")], "");
    await flush();
    expect(r.posts.map((p) => p.headers["X-Turnstile-Token"])).toEqual(["first", "refreshed-token"]);
  });

  it("stops waiting for a token when the queue is aborted, and says nothing", async () => {
    const r = rig();
    r.token.siteKeySet = true;
    r.token.waitResult = "never";
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    r.queue.abortSession();
    await flush();
    expect(r.posts).toHaveLength(0);
    expect(r.rows()).toEqual([]);
  });
});

describe("UploadQueue: row texts (the legacy words)", () => {
  it("shows 'uploading' while the POST is out, then 'queued', then each job state", async () => {
    const r = rig({ deferWatch: true });
    let release: (value: Response) => void = () => {};
    r.respond.handler = () => {
      throw new Error("not used");
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>((resolve) => (release = resolve))),
    );
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.rows()[0].text).toBe("uploading\u2026");
    release(accepted(1));
    await flush();
    expect(r.rows()[0].text).toBe("queued");
    r.watches[0].options.onJob({ job_id: r.watches[0].job, state: "embedding", document_id: "d", version: 1, progress: { done: 1, total: 4, eta_s: 3 } });
    await flush();
    expect(r.rows()[0]).toMatchObject({ text: "embedding 1/4 (~3s left)", tone: "", terminal: false });
    r.watches[0].options.onJob({ job_id: r.watches[0].job, state: "failed", document_id: "d", version: null, error: { code: "x", message: "too many pages" } });
    await flush();
    expect(r.rows()[0]).toMatchObject({ text: "failed: too many pages", tone: "err", terminal: true });
  });

  it("shows 'network error' when the POST cannot be sent, and carries on with the next file", async () => {
    const r = rig();
    r.respond.handler = (n) => {
      if (n === 1) throw new TypeError("offline");
      return accepted(n);
    };
    r.queue.enqueue(SESSION, [file("a.txt"), file("b.txt")], "");
    await flush();
    expect(r.rows().map((row) => [row.text, row.tone])).toEqual([
      ["network error", "err"],
      ["ready", "ok"],
    ]);
  });

  it("shows the server's detail for a refusal, unchanged", async () => {
    const r = rig();
    r.respond.handler = () =>
      new Response(JSON.stringify({ detail: "The service is busy processing another upload \u2014 try again in a moment." }), {
        status: 429,
        statusText: "Too Many Requests",
      });
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.rows()[0]).toMatchObject({ text: "The service is busy processing another upload \u2014 try again in a moment.", tone: "err", terminal: true });
    expect(r.watches).toHaveLength(0);
    expect(r.terminals()).toBe(0);
  });

  it("shows 'unchanged (still vN)' for an identical re-upload, starts no watcher and reloads the documents", async () => {
    const r = rig();
    r.respond.handler = () => new Response(JSON.stringify({ unchanged: true, document_id: "abc123456789", version: 2 }), { status: 200 });
    r.queue.enqueue(SESSION, [file("a.txt")], "abc123456789");
    await flush();
    expect(r.rows()[0]).toMatchObject({ text: "unchanged (still v2)", tone: "ok", terminal: true });
    expect(r.watches).toHaveLength(0);
    expect(r.unchanged()).toBe(1);
  });

  it("treats an accepted upload with no usable job id as one it cannot watch", async () => {
    const r = rig();
    r.respond.handler = () => new Response(JSON.stringify({ document_id: "d" }), { status: 202 });
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.rows()[0]).toMatchObject({ text: "could not watch this upload", tone: "err" });
    expect(r.watches).toHaveLength(0);
    expect(r.terminals()).toBe(1);
  });

  it("announces each terminal state once, and no other state", async () => {
    const r = rig({ deferWatch: true });
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    r.watches[0].options.onJob({ job_id: r.watches[0].job, state: "parsing", document_id: "d", version: 1 });
    expect(r.announcements).toEqual([]);
    r.watches[0].options.onJob({ job_id: r.watches[0].job, state: "ready", document_id: "d", version: 1 });
    expect(r.announcements).toEqual(["a.txt: ready"]);
  });
});

describe("UploadQueue: how a watch ends (upload_flow 14 to 18 and 21 at the queue level)", () => {
  const outcomes: [string, WatchResult, string][] = [
    ["a job that is gone", { kind: "gone" }, "could not watch this upload"],
    ["a watch refused with a status it does not retry", { kind: "gave_up", reason: "status", status: 403 }, "could not watch this upload"],
    ["a watch that ran out of attempts", { kind: "gave_up", reason: "attempts", status: 503 }, "connection lost while watching this upload \u2014 check its status later"],
    ["a frame over the buffer limit", { kind: "gave_up", reason: "overflow", status: 200 }, "connection lost while watching this upload \u2014 check its status later"],
  ];

  it.each(outcomes)("%s ends in the legacy row text, signals once, and the queue goes on", async (_name, result, text) => {
    const r = rig({ deferWatch: true });
    r.queue.enqueue(SESSION, [file("a.txt"), file("b.txt")], "");
    await flush();
    r.watches[0].resolve(result);
    await flush();
    expect(r.rows()[0]).toMatchObject({ text, tone: "err", terminal: true });
    expect(r.terminals()).toBe(1);
    expect(r.posts).toHaveLength(2);
  });

  it("signals once for a terminal job (the evidence of the old version may be outdated), not once per event", async () => {
    const r = rig({ deferWatch: true });
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    for (const state of ["received", "parsing", "embedding"] as const) {
      r.watches[0].options.onJob({ job_id: r.watches[0].job, state, document_id: "d", version: 1 });
    }
    expect(r.terminals()).toBe(0);
    r.watches[0].options.onJob({ job_id: r.watches[0].job, state: "ready", document_id: "d", version: 1 });
    expect(r.terminals()).toBe(0);
    r.watches[0].resolve({ kind: "terminal", job: { job_id: r.watches[0].job, state: "ready", document_id: "d", version: 1 } });
    await flush();
    expect(r.terminals()).toBe(1);
  });

  it("signals for a failed job too", async () => {
    const r = rig({ deferWatch: true });
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    r.watches[0].resolve({ kind: "terminal", job: { job_id: r.watches[0].job, state: "failed", document_id: "d", version: null } });
    await flush();
    expect(r.terminals()).toBe(1);
  });

  it("survives a watch that throws (a failure of the page's own handler), says the connection was lost, signals and goes on", async () => {
    const r = rig();
    const queue = new UploadQueue({
      token: {
        siteKeyKnown: () => true,
        siteKeyFailed: () => false,
        waitForSiteKey: () => Promise.resolve(true),
        siteKeySet: () => false,
        consume: () => null, waitForToken: () => Promise.resolve(null) },
      watch: () => Promise.reject(new Error("handler blew up")),
      onRows: (rows) => {
        latest = rows;
      },
      onTerminal: () => {
        thrown += 1;
      },
      onUnchanged: () => {},
      onAnnounce: () => {},
    });
    let latest: readonly JobRow[] = [];
    let thrown = 0;
    queue.attach();
    queue.enqueue(SESSION, [file("a.txt"), file("b.txt")], "");
    await flush();
    expect(latest.map((row) => row.text)).toEqual([
      "connection lost while watching this upload \u2014 check its status later",
      "connection lost while watching this upload \u2014 check its status later",
    ]);
    expect(thrown).toBe(2);
    expect(r.posts).toHaveLength(2);
  });
});

describe("UploadQueue: teardown and the workspace changing (R34, StrictMode)", () => {
  it("aborts the live watcher and drops the files still waiting when the workspace changes, and posts nothing to the new one", async () => {
    const r = rig({ deferWatch: true });
    r.queue.enqueue(SESSION, [file("a.txt"), file("b.txt")], "");
    await flush();
    const signal = r.watches[0].options.signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    r.queue.abortSession();
    expect(signal.aborted).toBe(true);
    r.watches[0].resolve({ kind: "aborted" });
    await flush();
    expect(r.posts).toHaveLength(1);
    expect(r.rows()).toEqual([]);
    expect(r.terminals()).toBe(0); // an abort is not a terminal state
    expect(r.queue.busy).toBe(false);
  });

  it("is ready for the next workspace's uploads after an abort (the controller is re-armed)", async () => {
    const r = rig();
    r.queue.abortSession();
    r.queue.enqueue(OTHER, [file("n.txt")], "");
    await flush();
    expect(r.posts).toHaveLength(1);
    expect(r.posts[0].url).toBe(`/api/workspace/${OTHER_WS}/documents`);
    expect(r.posts[0].headers["X-Workspace-Token"]).toBe("tok-other");
  });

  it("a stale pass never writes into the rows of the new workspace", async () => {
    const r = rig({ deferWatch: true });
    r.queue.enqueue(SESSION, [file("old.txt")], "");
    await flush();
    r.queue.abortSession();
    r.queue.enqueue(OTHER, [file("new.txt")], "");
    await flush();
    r.watches[0].options.onJob({ job_id: r.watches[0].job, state: "ready", document_id: "d", version: 1 });
    r.watches[0].resolve({ kind: "aborted" });
    await flush();
    expect(r.rows().map((row) => row.name)).toEqual(["new.txt"]);
  });

  it("survives a StrictMode attach, detach, attach: an upload after the remount still goes out", async () => {
    const r = rig();
    r.queue.detach();
    r.queue.attach();
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    expect(r.posts).toHaveLength(1);
    expect(r.posts[0].signal?.aborted).toBe(false);
  });

  it("aborts the live watcher and the POST on detach, and ignores files enqueued while detached", async () => {
    const r = rig({ deferWatch: true });
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    const signal = r.watches[0].options.signal as AbortSignal;
    r.queue.detach();
    expect(signal.aborted).toBe(true);
    r.queue.enqueue(SESSION, [file("b.txt")], "");
    await flush();
    expect(r.posts).toHaveLength(1);
  });

  it("does not report a network error for a POST that was aborted on purpose", async () => {
    const r = rig();
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }),
    );
    r.queue.enqueue(SESSION, [file("a.txt")], "");
    await flush();
    r.queue.abortSession();
    await flush();
    expect(r.rows()).toEqual([]);
  });
});
