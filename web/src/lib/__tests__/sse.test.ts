import { describe, expect, it } from "vitest";
import { createParser } from "eventsource-parser";
import type { StreamEvent } from "../events";
import { ASK_URL, askStream, MAX_BUFFER_SIZE, PAGE_FAILED_STATUS, STREAM_CUT_STATUS } from "../sse";
import { ASK_FIXTURES, fixtureBytes, fixtureEvents, fixtureRefusals } from "./fixtures";
import { CUT, END, fetchOf, frame, outcomeKinds, responseOf, scripted, type Step } from "./sse-helpers";

const BODY = { question: "How exposed is Nvidia to TSMC?", strategy: "hybrid" };

function collect() {
  const events: StreamEvent[] = [];
  return { events, onEvent: (event: StreamEvent) => void events.push(event) };
}

describe("askStream over the recorded byte fixtures (R15, R17)", () => {
  const pieces = [0, 1, 13, 4096];
  for (const name of ASK_FIXTURES) {
    it.each(pieces)(`${name}: decodes to the Python sidecar when the body arrives in pieces of %i bytes`, async (size) => {
      const { events, onEvent } = collect();
      const fetchImpl = (async () => responseOf(fixtureBytes(name), size)) as unknown as typeof fetch;
      const outcome = await askStream(BODY, { onEvent, fetchImpl });
      expect(events).toEqual(fixtureEvents(name));
      expect(outcome).toMatchObject({ ended: true, failed: false, status: null, refusal: null });
    });
  }

  it("a sized-up frame still fits: the largest done is far below MAX_BUFFER_SIZE", () => {
    const largest = Math.max(...ASK_FIXTURES.map((name) => fixtureBytes(name).length));
    expect(largest * 8).toBeLessThan(MAX_BUFFER_SIZE);
    expect(MAX_BUFFER_SIZE).toBe(256 * 1024);
  });

  it("a frame over the limit fails the stream instead of being dropped, and stops reading", async () => {
    const { events, onEvent } = collect();
    // an unterminated frame (no blank line yet) stays buffered: that is what the limit bounds
    const huge = new TextEncoder().encode(`event: delta\ndata: ${"x".repeat(5000)}`);
    const reader = scripted([huge, frame("delta", { text: "x" }), END]);
    const { fetchImpl } = fetchOf(reader);
    const outcome = await askStream(BODY, { onEvent, fetchImpl, maxBufferSize: 1000 });
    expect(outcome).toMatchObject({ ended: false, failed: true, status: PAGE_FAILED_STATUS });
    expect(events).toEqual([]);
    expect(reader.reads()).toBe(1);
    expect(reader.cancelled()).toBe(true);
  });

  it("a complete single-line frame over the limit that arrives in ONE read still fails the stream (S6)", async () => {
    const { events, onEvent } = collect();
    // a valid done plus a field the guards ignore: were it let through it would be rendered as an answer
    const oversized = frame("done", { ...fixtureEvents("done_max_size")[0], pad: "x".repeat(MAX_BUFFER_SIZE + 10) });
    const reader = scripted([oversized, END]);
    const { fetchImpl } = fetchOf(reader);
    const outcome = await askStream(BODY, { onEvent, fetchImpl });
    expect(outcome).toMatchObject({ ended: false, failed: true, status: PAGE_FAILED_STATUS });
    expect(events).toEqual([]);
    expect(reader.cancelled()).toBe(true);
  });

  it("a frame whose data is exactly the limit is still delivered, one character more is not", async () => {
    const done = fixtureEvents("done_max_size")[0];
    const limit = JSON.stringify(done).length;
    const run = async (maxBufferSize: number) => {
      const { events, onEvent } = collect();
      const { fetchImpl } = fetchOf(scripted([frame("done", done), END]));
      return { outcome: await askStream(BODY, { onEvent, fetchImpl, maxBufferSize }), events };
    };
    const exact = await run(limit);
    expect(exact.outcome).toMatchObject({ ended: true, failed: false });
    expect(exact.events).toHaveLength(1);
    const over = await run(limit - 1);
    expect(over.outcome).toMatchObject({ ended: false, failed: true });
    expect(over.events).toEqual([]);
  });

  it("a real-size done split across reads fits under MAX_BUFFER_SIZE but not under a limit of 8,000 characters", async () => {
    const bytes = fixtureBytes("done_max_size");
    const fits = collect();
    const ok = await askStream(BODY, {
      onEvent: fits.onEvent,
      fetchImpl: (async () => responseOf(bytes, 1024)) as unknown as typeof fetch,
    });
    expect(ok.ended).toBe(true);
    expect(fits.events).toHaveLength(1);
    const tight = await askStream(BODY, {
      onEvent: () => undefined,
      fetchImpl: (async () => responseOf(bytes, 1024)) as unknown as typeof fetch,
      maxBufferSize: 8000,
    });
    expect(tight).toMatchObject({ ended: false, failed: true });
  });
});

describe("askStream request and outcome", () => {
  it("POSTs the body as JSON to /api/ask with the caller's headers and signal, once", async () => {
    const controller = new AbortController();
    const { fetchImpl, calls } = fetchOf(scripted([frame("delta", { text: "x" }), END]));
    await askStream(BODY, {
      onEvent: () => undefined,
      fetchImpl,
      signal: controller.signal,
      headers: { "X-Workspace-Token": "tok" },
    });
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(ASK_URL);
    expect(calls[0]?.init).toMatchObject({ method: "POST", signal: controller.signal });
    expect(calls[0]?.init?.headers).toEqual({ "Content-Type": "application/json", "X-Workspace-Token": "tok" });
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual(BODY);
  });

  it("never re-sends a POST after a cut, a clean early end or a page failure", async () => {
    const cases: Step[][] = [[CUT], [END], [frame("delta", { text: "x" }), CUT]];
    for (const steps of cases) {
      const { fetchImpl, calls } = fetchOf(scripted(steps));
      const outcome = await askStream(BODY, { onEvent: () => undefined, fetchImpl });
      expect(outcome.status).toBe(STREAM_CUT_STATUS);
      expect(calls).toHaveLength(1);
    }
    const failing = fetchOf(scripted([frame("delta", { text: "x" }), END]));
    await askStream(BODY, {
      onEvent: () => {
        throw new Error("boom");
      },
      fetchImpl: failing.fetchImpl,
    });
    expect(failing.calls).toHaveLength(1);
  });

  it("reports a fetch that rejects (no connection, an abort) as a cut, never as a page failure", async () => {
    const fetchImpl = (async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;
    expect(await askStream(BODY, { onEvent: () => undefined, fetchImpl })).toMatchObject({
      ended: false,
      failed: false,
      status: STREAM_CUT_STATUS,
    });
    const aborted = new AbortController();
    const abortingFetch = (async (_url: unknown, init?: RequestInit) => {
      aborted.abort();
      init?.signal?.throwIfAborted();
      return new Response("");
    }) as unknown as typeof fetch;
    const outcome = await askStream(BODY, { onEvent: () => undefined, fetchImpl: abortingFetch, signal: aborted.signal });
    expect(outcome.status).toBe(STREAM_CUT_STATUS);
  });

  it("an abort between reads ends the stream as a cut and releases the reader", async () => {
    const controller = new AbortController();
    const reader = scripted([frame("delta", { text: "x" }), END]);
    const { fetchImpl } = fetchOf(reader);
    const { events, onEvent } = collect();
    const outcome = await askStream(BODY, {
      onEvent: (event) => {
        onEvent(event);
        controller.abort();
      },
      fetchImpl,
      signal: controller.signal,
    });
    expect(events).toHaveLength(1);
    expect(outcome).toMatchObject({ ended: false, failed: false, status: STREAM_CUT_STATUS });
    expect(reader.cancelled()).toBe(true);
  });

  it("stops at the terminal event: nothing after a done is delivered or read", async () => {
    const reader = scripted([
      frame("done", { answer: "a", citations: [], hallucinated: [] }),
      frame("delta", { text: "late" }),
      END,
    ]);
    const { fetchImpl } = fetchOf(reader);
    const { events, onEvent } = collect();
    const outcome = await askStream(BODY, { onEvent, fetchImpl });
    expect(events.map((e) => e.event)).toEqual(["done"]);
    expect(outcome.ended).toBe(true);
    expect(reader.reads()).toBe(1);
    expect(reader.cancelled()).toBe(true);
  });

  it("sets ended only after the handler returned for a terminal event", async () => {
    const failing = fetchOf(scripted([frame("error", { detail: "x" }), END]));
    const thrown = await askStream(BODY, {
      onEvent: () => {
        throw new Error("boom");
      },
      fetchImpl: failing.fetchImpl,
    });
    expect(thrown).toMatchObject({ ended: false, failed: true, status: PAGE_FAILED_STATUS });
    const fine = fetchOf(scripted([frame("error", { detail: "x" }), END]));
    expect((await askStream(BODY, { onEvent: () => undefined, fetchImpl: fine.fetchImpl })).ended).toBe(true);
  });

  it("ignores pings, unknown events and fields, and data that is not JSON or the wrong shape", async () => {
    const noise = new TextEncoder().encode(
      ': ping\n\nevent: ping\ndata: {}\n\nevent: surprise\ndata: {"x":1}\n\nevent: delta\ndata: not json\n\nretry: 5\n\n' +
        'event: delta\ndata: {"text":5}\n\nevent: delta\nfoo: bar\ndata: {"text":"ok"}\n\n',
    );
    const { events, onEvent } = collect();
    const { fetchImpl } = fetchOf(scripted([noise, frame("done", { answer: "a", citations: [], hallucinated: [] }), END]));
    const outcome = await askStream(BODY, { onEvent, fetchImpl });
    expect(events.map((e) => e.event)).toEqual(["delta", "done"]);
    expect(outcomeKinds(outcome)).toBe("ended/ok");
  });

  it("decodes a multi-byte character split across two chunks", async () => {
    const bytes = new TextEncoder().encode('event: delta\ndata: {"text":"price \u20ac5"}\n\n');
    const split = bytes.indexOf(0xe2) + 1;
    const { events, onEvent } = collect();
    const { fetchImpl } = fetchOf(scripted([bytes.slice(0, split), bytes.slice(split), END]));
    await askStream(BODY, { onEvent, fetchImpl });
    expect(events).toEqual([{ event: "delta", text: "price \u20ac5" }]);
  });

  it("handles multi-line data and CRLF-free comment frames per the spec grammar", async () => {
    const wire = new TextEncoder().encode('event: delta\ndata: {"text":\ndata: "two lines"}\n\n');
    const { events, onEvent } = collect();
    const { fetchImpl } = fetchOf(scripted([wire, END]));
    await askStream(BODY, { onEvent, fetchImpl });
    expect(events).toEqual([{ event: "delta", text: "two lines" }]);
  });
});

describe("askStream refusals (R20): the server's JSON detail, no body read, no retry", () => {
  it.each(fixtureRefusals().map((r) => [r.name, r] as const))("%s", async (_name, refusal) => {
    const calls: unknown[] = [];
    const fetchImpl = (async () => {
      calls.push(1);
      const headers: Record<string, string> = { "content-type": "application/json" };
      if (refusal.retry_after !== null) {
        headers["retry-after"] = refusal.retry_after;
      }
      return new Response(JSON.stringify({ detail: refusal.detail }), { status: refusal.status, headers });
    }) as unknown as typeof fetch;
    const { events, onEvent } = collect();
    const outcome = await askStream(BODY, { onEvent, fetchImpl });
    expect(outcome).toMatchObject({
      ended: false,
      failed: false,
      status: refusal.detail,
      refusal: { status: refusal.status, detail: refusal.detail },
    });
    expect(events).toEqual([]);
    expect(calls).toHaveLength(1);
  });

  it("an upstream HTML page is never rendered: the status text stands in", async () => {
    const fetchImpl = (async () =>
      new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" })) as unknown as typeof fetch;
    const outcome = await askStream(BODY, { onEvent: () => undefined, fetchImpl });
    expect(outcome.status).toBe("Bad Gateway");
    expect(outcome.refusal).toEqual({ status: 502, detail: "Bad Gateway" });
  });
});

describe("the parser contract the client relies on (eventsource-parser 4.1.3)", () => {
  it("terminates on overflow and throws on the next feed until reset", () => {
    const seen: string[] = [];
    const parser = createParser({
      maxBufferSize: 10,
      onEvent: (message) => seen.push(message.data),
      onError: (error) => seen.push(error.type),
    });
    parser.feed(`data: ${"y".repeat(50)}`);
    expect(seen).toEqual(["max-buffer-size-exceeded"]);
    expect(() => parser.feed("data: z\n\n")).toThrow(/terminated/);
  });
});

describe("nothing after the terminal event is delivered, even in the same read", () => {
  it("a done and a delta in ONE chunk deliver only the done", async () => {
    const { events, onEvent } = collect();
    const done = fixtureEvents("cached_done")[0] as Record<string, unknown>;
    const one = new Uint8Array([...frame("done", done), ...frame("delta", { text: "late" })]);
    const { fetchImpl } = fetchOf(scripted([one, END]));
    const outcome = await askStream(BODY, { onEvent, fetchImpl });
    expect(events.map((e) => e.event)).toEqual(["done"]);
    expect(outcome).toMatchObject({ ended: true, failed: false });
  });
});
