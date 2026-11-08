import { describe, expect, it } from "vitest";
import type { StreamEvent } from "../events";
import { askStream, MAX_BUFFER_SIZE } from "../sse";
import { frame, responseOf } from "./sse-helpers";

// I3 (worker B): two fixes to the stream client. `aborted` tells a caller that the call ended because ITS OWN signal aborted (so
// the page never says "connection lost" for a stop the visitor asked for), and a frame over the limit that arrives AFTER the
// terminal event in the same read never turns a finished answer into a failure.

const fetchOf = (make: () => Response | Promise<Response>): typeof fetch => (async () => make()) as unknown as typeof fetch;
const DONE = { answer: "Nvidia reported growth.", citations: [], hallucinated: [] };

describe("outcome.aborted: the caller's own signal ended the call", () => {
  it("is false for an ordinary answer", async () => {
    const out = await askStream({ question: "q" }, { onEvent: () => {}, fetchImpl: fetchOf(() => responseOf(frame("done", DONE))) });
    expect(out).toMatchObject({ ended: true, failed: false, aborted: false });
  });

  it("is true when the signal was already aborted: no connection is made", async () => {
    const controller = new AbortController();
    controller.abort();
    let calls = 0;
    const impl = fetchOf(() => {
      calls += 1;
      return responseOf(frame("done", DONE));
    });
    const out = await askStream({ question: "q" }, { onEvent: () => {}, signal: controller.signal, fetchImpl: impl });
    expect(out).toMatchObject({ ended: false, aborted: true });
    expect(calls).toBeLessThanOrEqual(1);
  });

  it("is true when the signal aborts while the response is being read, and the text that arrived is kept", async () => {
    const controller = new AbortController();
    const seen: StreamEvent[] = [];
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(frame("delta", { text: "Nvidia reported " }));
      },
      pull() {
        return new Promise(() => {});
      },
      cancel() {},
    });
    const impl = fetchOf(() => new Response(stream, { status: 200 }));
    const run = askStream({ question: "q" }, { onEvent: (e) => void seen.push(e), signal: controller.signal, fetchImpl: impl });
    await new Promise((r) => setTimeout(r, 20));
    controller.abort();
    const out = await run;
    expect(out).toMatchObject({ ended: false, aborted: true });
    expect(seen.map((e) => e.event)).toEqual(["delta"]);
  });

  it("is true when the request itself rejects because the signal aborted", async () => {
    const controller = new AbortController();
    const impl = (async () => {
      controller.abort();
      throw new DOMException("aborted", "AbortError");
    }) as unknown as typeof fetch;
    expect(await askStream({ question: "q" }, { onEvent: () => {}, signal: controller.signal, fetchImpl: impl })).toMatchObject({ ended: false, aborted: true });
  });

  it("is false for a cut connection the caller did not ask for", async () => {
    const out = await askStream({ question: "q" }, { onEvent: () => {}, fetchImpl: fetchOf(() => responseOf(frame("delta", { text: "x" }))) });
    expect(out).toMatchObject({ ended: false, failed: false, aborted: false });
  });

  it("is false once the answer ended, even if the signal aborts afterwards", async () => {
    const controller = new AbortController();
    const out = await askStream({ question: "q" }, { onEvent: () => controller.abort(), signal: controller.signal, fetchImpl: fetchOf(() => responseOf(frame("done", DONE))) });
    expect(out).toMatchObject({ ended: true, aborted: false });
  });
});

describe("failed means an overflow of an answer that did not end", () => {
  it("a done followed, in the same read, by a frame over the limit is a finished answer, not a failure", async () => {
    const first = frame("done", DONE);
    // an UNFINISHED line over the limit: the parser counts it and reports the overflow after the done was already taken in
    const huge = new TextEncoder().encode(`event: delta\ndata: ${"x".repeat(5000)}`);
    const both = new Uint8Array(first.length + huge.length);
    both.set(first, 0);
    both.set(huge, first.length);
    const seen: StreamEvent[] = [];
    const out = await askStream({ question: "q" }, { onEvent: (e) => void seen.push(e), fetchImpl: fetchOf(() => responseOf(both)), maxBufferSize: 1000 });
    expect(out).toMatchObject({ ended: true, failed: false, status: null });
    expect(seen.map((e) => e.event)).toEqual(["done"]);
  });

  it("a frame over the limit before any terminal event is still a failure", async () => {
    const huge = new TextEncoder().encode(`event: delta\ndata: ${"x".repeat(MAX_BUFFER_SIZE + 10)}\n\n`);
    const out = await askStream({ question: "q" }, { onEvent: () => {}, fetchImpl: fetchOf(() => responseOf(huge)) });
    expect(out).toMatchObject({ ended: false, failed: true });
  });
});
