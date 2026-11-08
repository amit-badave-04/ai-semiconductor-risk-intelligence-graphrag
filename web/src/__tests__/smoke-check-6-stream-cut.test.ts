import { createParser } from "eventsource-parser";
import { describe, expect, it } from "vitest";
import type { StreamEvent } from "@/lib/events";
import { askStream, MAX_BUFFER_SIZE, STREAM_CUT_STATUS } from "@/lib/sse";

// Smoke check 6 of docs/specs/m5b-frontend.md R2, repointed at the shipped client (I2). The read-loop half of
// tests/ui/stream_cut.test.mjs is ported in full as web/src/lib/__tests__/sse.stream-cut.test.ts (16 cases), so this file no
// longer carries a read loop or a buffer constant of its own: it keeps only the two STACK facts the port does not state, both
// through the real askStream and MAX_BUFFER_SIZE under vitest 5.1.0 + jsdom 30.1.3:
//   * a real ReadableStream whose pull errors (the shape of a reset connection) is a cut, with the text that arrived kept;
//   * the parser has no limit of its own (the reason R17 sets MAX_BUFFER_SIZE), and a frame well past 64 KiB still fits.
// The DOM half (statuses written into #status, the Ask button, the checks footer) stays for I3.

const encoder = new TextEncoder();
const sse = (event: string, data: unknown) => encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

function fetchOfStream(stream: ReadableStream<Uint8Array>): typeof fetch {
  return (async () => new Response(stream, { status: 200 })) as unknown as typeof fetch;
}

describe("smoke check 6: stack facts under the shipped askStream (R17, S6)", () => {
  it("a real ReadableStream whose third pull errors is reported as a cut and keeps the text that arrived", async () => {
    const queue = [sse("retrieval", { anchors: {}, counts: {} }), sse("delta", { text: "Nvidia reported " })];
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        const next = queue.shift();
        if (next === undefined) {
          controller.error(new TypeError("network error"));
        } else {
          controller.enqueue(next);
        }
      },
    });
    const events: StreamEvent[] = [];
    const outcome = await askStream(
      { question: "q" },
      { onEvent: (event) => void events.push(event), fetchImpl: fetchOfStream(stream) },
    );
    expect(outcome).toMatchObject({ ended: false, failed: false, status: STREAM_CUT_STATUS });
    expect(events.map((e) => e.event)).toEqual(["retrieval", "delta"]);
    expect(events[1]).toMatchObject({ text: "Nvidia reported " });
  });

  it("the parser has no default limit, and MAX_BUFFER_SIZE lets a 200,000-character frame through", async () => {
    const seen: string[] = [];
    const bare = createParser({ onEvent: (message) => seen.push(message.data), onError: (error) => seen.push(error.type) });
    bare.feed(`data: ${"y".repeat(200_000)}`);
    bare.feed("\n\n");
    expect(seen).toHaveLength(1);
    expect(seen[0]).toHaveLength(200_000);

    expect(MAX_BUFFER_SIZE).toBeGreaterThan(200_000);
    const events: StreamEvent[] = [];
    const answer = "z".repeat(200_000);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(sse("done", { answer, citations: [], hallucinated: [] }));
        controller.close();
      },
    });
    const outcome = await askStream({ question: "q" }, { onEvent: (e) => void events.push(e), fetchImpl: fetchOfStream(stream) });
    expect(outcome).toMatchObject({ ended: true, failed: false });
    expect(events).toHaveLength(1);
  });
});
