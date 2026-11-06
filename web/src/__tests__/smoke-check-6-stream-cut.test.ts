import { createParser, type ParseError } from "eventsource-parser";
import { describe, expect, it } from "vitest";

// Smoke check 6 of docs/specs/m5b-frontend.md R2: "Vitest 5.1 + jsdom 30.1.3 ... port chips/evidence/stream_cut tests". This is
// a PROBE, not the port: it runs the pure read-loop part of tests/ui/stream_cut.test.mjs (the cut, the clean end, how a
// finished answer or a server error keeps its own status, a page failure) against a stand-in for worker A's `sse.ts` (I2) that
// uses the real parser (eventsource-parser 4.1.3) under vitest 5.1.0 and jsdom 30.1.3. It settles whether the stack can host the
// port (streams, TextDecoder and Uint8Array across jsdom's realm, the parser's callbacks, buffer limit and terminated state).
// What is NOT here: the DOM half of the old tests (statuses written into #status, the Ask button), chips and evidence: those
// come with the components in I2 and I3, and the full port of the 16 tests is theirs.

const STREAM_CUT_STATUS = "connection lost before the answer finished: what is shown is incomplete and was not checked. Please ask again.";
const PAGE_FAILED_STATUS = "the page could not show this answer completely: what is shown may be incomplete and was not checked. Please ask again.";
const MAX_BUFFER = 64 * 1024;

const encoder = new TextEncoder();
const sse = (event: string, data: unknown) => encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
const RETRIEVAL = sse("retrieval", { anchors: { Nvidia: 1045810 }, counts: { chunks: 8 } });
const DELTA = sse("delta", { text: "Nvidia reported " });
const DONE = sse("done", { answer: "Nvidia reported growth.", cached: true });
const DONE_LIVE = sse("done", { answer: "Nvidia reported growth.", cached: false });
const SERVER_ERROR = sse("error", { detail: "The service is paused for today." });
const CUT = Symbol("cut"); // the next read() rejects, the way a reset or aborted connection does
const END = Symbol("end"); // the next read() reports a clean end of the stream

type Step = Uint8Array | typeof CUT | typeof END;
interface Reader {
  read(): Promise<ReadableStreamReadResult<Uint8Array>>;
}

function scripted(steps: Step[]): Reader & { reads: () => number } {
  const remaining = [...steps];
  let reads = 0;
  return {
    reads: () => reads,
    read: async () => {
      reads += 1;
      const step = remaining.shift() ?? END;
      if (step === CUT) throw new TypeError("network error");
      if (step === END) return { done: true, value: undefined };
      return { done: false, value: step };
    },
  };
}

interface Outcome {
  status: string;
  ended: boolean;
  failed: boolean;
  text: string;
  footer: boolean;
  thrown: unknown;
  overflowed: boolean;
}

interface Hooks {
  render?: (text: string) => void; // a page-level function the event handler calls (it may throw, as a rendering bug would)
  footer?: () => void;
  maxBufferSize?: number | undefined;
}

/** The read loop of index.html's ask(), over the real parser: `ended` only once a done or error event was shown completely,
 *  `failed` when the page itself threw, and the status is always the true one. */
async function readAnswer(reader: Reader, hooks: Hooks = {}): Promise<Outcome> {
  const decoder = new TextDecoder();
  const out: Outcome = { status: "retrieving from the graph…", ended: false, failed: false, text: "", footer: false, thrown: undefined, overflowed: false };
  const handle = (event: string | undefined, raw: string) => {
    let data: { text?: string; answer?: string; cached?: boolean; detail?: string };
    try {
      data = JSON.parse(raw) as typeof data;
    } catch {
      return; // not JSON: ignored, never thrown past the client
    }
    if (event === "retrieval") out.status = "retrieved — generating…";
    else if (event === "delta" && typeof data.text === "string") {
      out.text += data.text;
      hooks.render?.(out.text);
    } else if (event === "done" && typeof data.answer === "string") {
      out.text = data.answer;
      hooks.render?.(out.text);
      out.status = data.cached ? "served from cache — no model call" : "done";
      hooks.footer?.();
      out.footer = true;
      out.ended = true; // ended only once shown: a throw above is a page failure
    } else if (event === "error" && typeof data.detail === "string") {
      out.status = data.detail;
      out.ended = true;
    } // anything else (ping, step, an unknown event or field) is ignored
  };
  const parser = createParser({
    maxBufferSize: hooks.maxBufferSize ?? MAX_BUFFER,
    onEvent: (message) => handle(message.event, message.data),
    onError: (error: ParseError) => {
      if (error.type === "max-buffer-size-exceeded") out.overflowed = true;
    },
  });
  try {
    for (;;) {
      let step: ReadableStreamReadResult<Uint8Array>;
      try {
        step = await reader.read();
      } catch {
        break; // a reset or aborted stream: say so below
      }
      if (step.done) break;
      parser.feed(decoder.decode(step.value, { stream: true }));
      if (out.overflowed) {
        out.failed = true; // a frame over the limit is a failure of the stream, never a silent drop
        break; // and the terminated parser is never fed again (its next feed() would throw)
      }
    }
  } catch (error) {
    out.failed = true; // handle(), the decoder or the parser threw
    out.thrown = error;
  } finally {
    if (!out.ended) out.status = out.failed ? PAGE_FAILED_STATUS : STREAM_CUT_STATUS;
  }
  return out;
}

describe("smoke check 6: the stream_cut semantics on the new stack (eventsource-parser under vitest + jsdom)", () => {
  it("a read that rejects mid-answer says the connection was lost and keeps the text that arrived", async () => {
    const out = await readAnswer(scripted([RETRIEVAL, DELTA, CUT]));
    expect(out.status).toBe(STREAM_CUT_STATUS);
    expect(out.status).not.toMatch(/generating/);
    expect(out.ended).toBe(false);
    expect(out.failed).toBe(false);
    expect(out.text).toBe("Nvidia reported ");
    expect(out.footer).toBe(false); // an unfinished answer carries no checks footer
  });

  it("the status never claims the cut answer is complete or checked", async () => {
    const out = await readAnswer(scripted([DELTA, CUT]));
    expect(out.status).toMatch(/incomplete/);
    expect(out.status).toMatch(/not checked/);
  });

  it("a stream that ends cleanly without a done or error event is reported the same way", async () => {
    const out = await readAnswer(scripted([RETRIEVAL, DELTA, END]));
    expect(out.status).toBe(STREAM_CUT_STATUS);
  });

  it("a cut before the first byte is reported too", async () => {
    expect((await readAnswer(scripted([CUT]))).status).toBe(STREAM_CUT_STATUS);
  });

  it.each([
    ["a reset", CUT],
    ["closing", END],
  ] as const)("a finished answer keeps its own status when the stream then ends by %s", async (_name, ending) => {
    const out = await readAnswer(scripted([DELTA, DONE, ending]));
    expect(out.status).toBe("served from cache — no model call");
    expect(out.footer).toBe(true);
    expect(out.text).toBe("Nvidia reported growth.");
  });

  it.each([
    ["a reset", CUT],
    ["closing", END],
  ] as const)("the server's own error detail is kept when the stream then ends by %s", async (_name, ending) => {
    const out = await readAnswer(scripted([RETRIEVAL, SERVER_ERROR, ending]));
    expect(out.status).toBe("The service is paused for today.");
    expect(out.status).not.toMatch(/connection lost/);
    expect(out.ended).toBe(true);
  });

  it("an ordinary answer is unchanged: done status and the checks footer", async () => {
    const out = await readAnswer(scripted([RETRIEVAL, DELTA, DONE_LIVE, END]));
    expect(out.status).toBe("done");
    expect(out.footer).toBe(true);
  });

  it("a failure while the page takes an event in leaves an honest status and the original error", async () => {
    const boom = new Error("boom");
    const out = await readAnswer(scripted([RETRIEVAL, DELTA, DONE, END]), {
      render: () => {
        throw boom;
      },
    });
    expect(out.failed).toBe(true);
    expect(out.status).toBe(PAGE_FAILED_STATUS);
    expect(out.status).toMatch(/not checked/);
    expect(out.thrown).toBe(boom);
  });

  it("a failure after the done text was rendered does not leave 'done' over a footer that was never built", async () => {
    const out = await readAnswer(scripted([DELTA, DONE, END]), {
      footer: () => {
        throw new Error("boom");
      },
    });
    expect(out.status).toBe(PAGE_FAILED_STATUS);
    expect(out.ended).toBe(false);
    expect(out.footer).toBe(false);
  });

  it("a server error event that was shown is not replaced by the page-failure text", async () => {
    const out = await readAnswer(scripted([RETRIEVAL, SERVER_ERROR, END]));
    expect(out.status).toBe("The service is paused for today.");
    expect(out.status).not.toMatch(/could not show/);
  });
});

describe("smoke check 6: stack facts the client relies on (R17, S6)", () => {
  it("decodes a multi-byte character split across two chunks (TextDecoder with stream: true, across jsdom's realm)", async () => {
    const bytes = encoder.encode('event: delta\ndata: {"text":"price €5"}\n\n');
    const split = bytes.indexOf(0xe2) + 1; // inside the three-byte euro sign
    const out = await readAnswer(scripted([bytes.slice(0, split), bytes.slice(split), DONE, END]));
    expect(out.status).toBe("served from cache — no model call");
    const midway = await readAnswer(scripted([bytes.slice(0, split), bytes.slice(split), CUT]));
    expect(midway.text).toBe("price €5");
  });

  it("works over a real ReadableStream whose second pull errors (the shape of a reset connection)", async () => {
    const queue: Step[] = [RETRIEVAL, DELTA, CUT];
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        const step = queue.shift() ?? END;
        if (step === CUT) controller.error(new TypeError("network error"));
        else if (step === END) controller.close();
        else controller.enqueue(step);
      },
    });
    const out = await readAnswer(stream.getReader());
    expect(out.text).toBe("Nvidia reported ");
    expect(out.status).toBe(STREAM_CUT_STATUS);
  });

  it("ignores ping comments, unknown events, unknown fields and data that is not JSON", async () => {
    const noise = encoder.encode(
      ": ping\n\nevent: ping\ndata: {}\n\nevent: surprise\ndata: {\"x\":1}\n\nevent: delta\ndata: not json\n\nretry: 5\n\nevent: delta\nfoo: bar\ndata: {\"text\":\"ok\"}\n\n",
    );
    const out = await readAnswer(scripted([noise, DONE, END]));
    expect(out.text).toBe("Nvidia reported growth.");
    expect(out.status).toBe("served from cache — no model call");
  });

  it("ends a frame over the buffer limit as failed, never as a silent drop, and does not feed the terminated parser again", async () => {
    const huge = encoder.encode(`event: delta\ndata: ${"x".repeat(5_000)}`); // no blank line yet: the frame stays buffered
    const reader = scripted([huge, DELTA, DONE, END]);
    const out = await readAnswer(reader, { maxBufferSize: 1_000 });
    expect(out.overflowed).toBe(true);
    expect(out.failed).toBe(true);
    expect(out.status).toBe(PAGE_FAILED_STATUS);
    expect(reader.reads()).toBe(1); // it stopped reading
  });

  it("the parser, once terminated by an overflow, throws on the next feed until reset (so the client must catch it)", () => {
    const seen: string[] = [];
    const parser = createParser({
      maxBufferSize: 10,
      onEvent: (message) => seen.push(message.data),
      onError: (error) => seen.push(error.type),
    });
    parser.feed(`data: ${"y".repeat(50)}`);
    expect(seen).toEqual(["max-buffer-size-exceeded"]);
    expect(() => parser.feed("data: z\n\n")).toThrow(/terminated/);
    parser.reset();
    parser.feed("data: ok\n\n");
    expect(seen).toEqual(["max-buffer-size-exceeded", "ok"]);
  });

  it("has no buffer limit unless one is set (the parser's own default is unbounded, which is why R17 sets one)", () => {
    const seen: string[] = [];
    const parser = createParser({ onEvent: (message) => seen.push(message.data), onError: (error) => seen.push(error.type) });
    parser.feed(`data: ${"y".repeat(200_000)}`);
    parser.feed("\n\n");
    expect(seen).toHaveLength(1);
    expect(seen[0]).toHaveLength(200_000);
  });
});
