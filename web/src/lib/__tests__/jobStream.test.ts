import { describe, expect, it } from "vitest";
import type { JobEvent } from "../events";
import { watchJob, type WatchOptions } from "../jobStream";
import { MAX_BUFFER_SIZE } from "../sse";
import { fixtureBytes, fixtureEvents } from "./fixtures";

// R19 (docs/specs/m5b-frontend.md): one watcher per job, X-Workspace-Token only, retries on 429, 5xx and an early end only after
// the previous response was released, a bounded number of attempts, and a 404 means the job is gone.
const WS = "c".repeat(32);
const JOB = "00112233445566ff";
const TOKEN = "secret-workspace-token";

interface Plan {
  status?: number;
  bytes?: Uint8Array; // streamed body; the stream closes after it (an early end when it holds no terminal event)
  throws?: boolean;
}

/** A fetch that follows `plans` in order and counts how many of its responses are open at once: a response is open from the
 *  fetch until its body has been read to the end or cancelled. */
function server(plans: Plan[]) {
  const stats = { fetches: 0, open: 0, maxOpen: 0, urls: [] as string[], inits: [] as (RequestInit | undefined)[] };
  const fetchImpl = (async (url: string | URL | Request, init?: RequestInit) => {
    stats.fetches += 1;
    stats.urls.push(String(url));
    stats.inits.push(init);
    const plan = plans.shift() ?? { status: 200, bytes: new Uint8Array() };
    if (plan.throws) {
      throw new TypeError("Failed to fetch");
    }
    stats.open += 1;
    stats.maxOpen = Math.max(stats.maxOpen, stats.open);
    let released = false;
    const release = () => {
      if (!released) {
        released = true;
        stats.open -= 1;
      }
    };
    let sent = false;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent || plan.bytes === undefined) {
          release();
          controller.close();
        } else {
          sent = true;
          controller.enqueue(plan.bytes);
        }
      },
      cancel: release,
    }, { highWaterMark: 0 }); // pull runs only when the body is read: an unread response stays open until it is cancelled
    return new Response(stream, { status: plan.status ?? 200 });
  }) as unknown as typeof fetch;
  return { fetchImpl, stats };
}

function watch(plans: Plan[], extra: Partial<WatchOptions> = {}) {
  const { fetchImpl, stats } = server(plans);
  const jobs: JobEvent[] = [];
  const sleeps: number[] = [];
  const result = watchJob(WS, JOB, TOKEN, {
    onJob: (job) => void jobs.push(job),
    fetchImpl,
    sleep: async (ms) => void sleeps.push(ms),
    ...extra,
  });
  return { result, jobs, sleeps, stats };
}

describe("watchJob over the recorded job fixtures", () => {
  it.each([
    ["job_ready", "ready"],
    ["job_failed", "failed"],
  ] as const)("%s: delivers every event of the sidecar and ends terminal (%s)", async (name, state) => {
    const run = watch([{ bytes: fixtureBytes(name) }]);
    const result = await run.result;
    expect(run.jobs).toEqual(fixtureEvents(name));
    expect(result).toMatchObject({ kind: "terminal", job: { state } });
    expect(run.stats.fetches).toBe(1);
  });

  it("sends a GET with X-Workspace-Token as the only credential, and the token never appears in the URL", async () => {
    const run = watch([{ bytes: fixtureBytes("job_ready") }]);
    await run.result;
    expect(run.stats.urls).toEqual([`/api/workspace/${WS}/jobs/${JOB}`]);
    const init = run.stats.inits[0];
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.headers).toEqual({ "X-Workspace-Token": TOKEN });
    expect(init?.body).toBeUndefined();
    expect(run.stats.urls[0]).not.toContain(TOKEN);
  });

  it("puts valid ids into the path as they are", async () => {
    const { fetchImpl, stats } = server([{ status: 404 }]);
    await watchJob(WS, JOB, TOKEN, { onJob: () => undefined, fetchImpl, sleep: async () => undefined });
    expect(stats.urls[0]).toBe(`/api/workspace/${WS}/jobs/${JOB}`);
  });

  // Pre-push security review LOW: encodeURIComponent leaves "." alone, so a "." or ".." id would resolve as a path segment and
  // send the token to another same-origin /api path. Ids outside the server's grammar (32 / 16 lower-case hex) make no request.
  it.each([
    ["..", JOB],
    [".", JOB],
    ["", JOB],
    [WS, ".."],
    [WS, "."],
    [WS, ""],
    ["a/b", JOB],
    [WS, "c?d"],
    ["C".repeat(32), JOB],
    [WS, JOB.toUpperCase()],
    ["c".repeat(31), JOB],
    [WS, "0".repeat(17)],
  ])("makes no request for workspace %j / job %j and reports it gone", async (ws, job) => {
    const { fetchImpl, stats } = server([{ status: 404 }]);
    const result = await watchJob(ws, job, TOKEN, { onJob: () => undefined, fetchImpl, sleep: async () => undefined });
    expect(result).toEqual({ kind: "gone" });
    expect(stats.urls).toEqual([]);
  });

  it("ignores frames that are not valid job events", async () => {
    const noise = new TextEncoder().encode(
      ': ping\n\nevent: job\ndata: nope\n\nevent: job\ndata: {"job_id":"j","state":"done","document_id":"d","version":1}\n\nevent: other\ndata: {}\n\n',
    );
    const merged = new Uint8Array([...noise, ...fixtureBytes("job_failed")]);
    const run = watch([{ bytes: merged }]);
    await run.result;
    expect(run.jobs).toEqual(fixtureEvents("job_failed"));
  });
});

describe("watchJob retries (never two open GETs at once)", () => {
  it("a 404 means gone: no retry, no sleep", async () => {
    const run = watch([{ status: 404 }]);
    expect(await run.result).toEqual({ kind: "gone" });
    expect(run.stats.fetches).toBe(1);
    expect(run.sleeps).toEqual([]);
  });

  it.each([429, 500, 503])("a %i is retried after its response was released, then succeeds", async (status) => {
    const run = watch([{ status }, { bytes: fixtureBytes("job_ready") }]);
    expect(await run.result).toMatchObject({ kind: "terminal" });
    expect(run.stats.fetches).toBe(2);
    expect(run.stats.maxOpen).toBe(1);
    expect(run.sleeps).toHaveLength(1);
  });

  it("an early end (no terminal event) is retried and the replay finishes", async () => {
    const early = new TextEncoder().encode('event: job\ndata: {"job_id":"j","state":"parsing","document_id":"d","version":1}\n\n');
    const run = watch([{ bytes: early }, { bytes: fixtureBytes("job_ready") }]);
    expect(await run.result).toMatchObject({ kind: "terminal", job: { state: "ready" } });
    expect(run.stats.fetches).toBe(2);
    expect(run.stats.maxOpen).toBe(1);
    expect(run.jobs[0]?.state).toBe("parsing"); // delivered before the retry; the replay then repeats earlier states
  });

  it("a rejected fetch is retried", async () => {
    const run = watch([{ throws: true }, { bytes: fixtureBytes("job_failed") }]);
    expect(await run.result).toMatchObject({ kind: "terminal", job: { state: "failed" } });
    expect(run.stats.fetches).toBe(2);
  });

  it("gives up after a bounded number of attempts, with growing waits", async () => {
    const run = watch([{ status: 503 }, { status: 503 }, { status: 503 }, { status: 503 }, { status: 503 }, { status: 503 }], {
      maxAttempts: 4,
    });
    expect(await run.result).toEqual({ kind: "gave_up", reason: "attempts", status: 503 });
    expect(run.stats.fetches).toBe(4);
    expect(run.sleeps).toHaveLength(3);
    expect(run.sleeps).toEqual([...run.sleeps].sort((a, b) => a - b));
    expect(run.stats.maxOpen).toBe(1);
  });

  it.each([400, 401, 403])("a %i is not retried", async (status) => {
    const run = watch([{ status }]);
    expect(await run.result).toEqual({ kind: "gave_up", reason: "status", status });
    expect(run.stats.fetches).toBe(1);
  });

  it("a frame over the buffer limit is not retried", async () => {
    const huge = new TextEncoder().encode(`event: job\ndata: ${"x".repeat(MAX_BUFFER_SIZE + 10)}`);
    const run = watch([{ bytes: huge }]);
    expect(await run.result).toEqual({ kind: "gave_up", reason: "overflow", status: 200 });
    expect(run.stats.fetches).toBe(1);
    expect(run.stats.open).toBe(0);
  });

  it("a complete single-line frame over the limit that arrives in ONE read is not delivered either (S6)", async () => {
    const [ready] = fixtureEvents("job_ready");
    const oversized = new TextEncoder().encode(
      `event: job
data: ${JSON.stringify({ ...ready, pad: "x".repeat(MAX_BUFFER_SIZE + 10) })}

`,
    );
    const run = watch([{ bytes: oversized }]);
    expect(await run.result).toEqual({ kind: "gave_up", reason: "overflow", status: 200 });
    expect(run.jobs).toEqual([]);
    expect(run.stats.fetches).toBe(1);
    expect(run.stats.open).toBe(0);
  });
});

describe("watchJob abort and failures of the page", () => {
  it("an abort before the first request makes none", async () => {
    const controller = new AbortController();
    controller.abort();
    const run = watch([{ bytes: fixtureBytes("job_ready") }], { signal: controller.signal });
    expect(await run.result).toEqual({ kind: "aborted" });
    expect(run.stats.fetches).toBe(0);
  });

  it("an abort while waiting to retry ends the watch without another request", async () => {
    const controller = new AbortController();
    const run = watch([{ status: 503 }, { bytes: fixtureBytes("job_ready") }], {
      signal: controller.signal,
      sleep: async () => controller.abort(),
    });
    expect(await run.result).toEqual({ kind: "aborted" });
    expect(run.stats.fetches).toBe(1);
    expect(run.stats.open).toBe(0);
  });

  it("an abort while reading releases the response", async () => {
    const controller = new AbortController();
    const first = new TextEncoder().encode('event: job\ndata: {"job_id":"j","state":"parsing","document_id":"d","version":1}\n\n');
    const run = watch([{ bytes: first }], {
      signal: controller.signal,
      onJob: () => controller.abort(),
    });
    expect(await run.result).toEqual({ kind: "aborted" });
    expect(run.stats.open).toBe(0);
  });

  it("an onJob that throws rejects the watch with that error, after the response was released", async () => {
    const boom = new Error("boom");
    const run = watch([{ bytes: fixtureBytes("job_ready") }], {
      onJob: () => {
        throw boom;
      },
    });
    await expect(run.result).rejects.toBe(boom);
    expect(run.stats.open).toBe(0);
  });

  it("uses the global fetch and a real timer by default", async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = (async () => {
      calls += 1;
      return new Response("", { status: 404 });
    }) as typeof fetch;
    try {
      expect(await watchJob(WS, JOB, TOKEN, { onJob: () => undefined })).toEqual({ kind: "gone" });
      expect(calls).toBe(1);
    } finally {
      globalThis.fetch = original;
    }
  });

  it("the default sleep waits, and wakes early on an abort", async () => {
    const original = globalThis.fetch;
    const controller = new AbortController();
    let calls = 0;
    globalThis.fetch = (async () => {
      calls += 1;
      queueMicrotask(() => controller.abort());
      return new Response("", { status: 503 });
    }) as typeof fetch;
    try {
      const started = Date.now();
      const result = await watchJob(WS, JOB, TOKEN, { onJob: () => undefined, signal: controller.signal });
      expect(result).toEqual({ kind: "aborted" });
      expect(Date.now() - started).toBeLessThan(400); // the first wait is longer than that: the abort woke it
      expect(calls).toBe(1);
    } finally {
      globalThis.fetch = original;
    }
  });
});

describe("watchJob delivers nothing after the terminal frame, even in the same read", () => {
  it("a ready frame followed by another job frame in ONE chunk delivers only up to ready", async () => {
    const ready = fixtureEvents("job_ready").at(-1) as Record<string, unknown>;
    const enc = new TextEncoder();
    const job = (data: unknown) => enc.encode(`event: job\ndata: ${JSON.stringify(data)}\n\n`);
    const late = { job_id: "j", state: "indexing", document_id: "d", version: 1 };
    const run = watch([{ bytes: new Uint8Array([...job(ready), ...job(late)]) }]);
    const result = await run.result;
    expect(run.jobs.map((j) => j.state)).toEqual(["ready"]);
    expect(result).toMatchObject({ kind: "terminal", job: { state: "ready" } });
  });
});
