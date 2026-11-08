// Test doubles for the stream readers: a fetch that answers with scripted bytes, a cut, or a clean end.
import type { AskOutcome } from "../sse";

export const encoder = new TextEncoder();
export const frame = (event: string, data: unknown): Uint8Array =>
  encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

export const CUT = Symbol("cut"); // the next read() rejects, the way a reset or aborted connection does
export const END = Symbol("end"); // the next read() reports a clean end of the stream
export type Step = Uint8Array | typeof CUT | typeof END;

export interface ScriptedReader {
  read(): Promise<ReadableStreamReadResult<Uint8Array>>;
  cancel(): Promise<void>;
  reads: () => number;
  cancelled: () => boolean;
}

export function scripted(steps: Step[]): ScriptedReader {
  const remaining = [...steps];
  let reads = 0;
  let cancelled = false;
  return {
    reads: () => reads,
    cancelled: () => cancelled,
    cancel: async () => {
      cancelled = true;
    },
    read: async () => {
      reads += 1;
      const step = remaining.shift() ?? END;
      if (step === CUT) {
        throw new TypeError("network error");
      }
      if (step === END) {
        return { done: true, value: undefined };
      }
      return { done: false, value: step };
    },
  };
}

/** A fetch whose response is OK and whose body is `reader`. `calls` records every request. */
export function fetchOf(reader: ScriptedReader | null, calls: { url: string; init: RequestInit | undefined }[] = []) {
  const impl = (async (url: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return { ok: true, status: 200, statusText: "OK", body: reader === null ? null : { getReader: () => reader } };
  }) as unknown as typeof fetch;
  return { fetchImpl: impl, calls };
}

/** A real Response over a ReadableStream that hands out `bytes` in pieces of `size` (the whole body when 0). */
export function responseOf(bytes: Uint8Array, size = 0, init: ResponseInit = { status: 200 }): Response {
  const pieces: Uint8Array[] = [];
  const step = size > 0 ? size : bytes.length;
  for (let at = 0; at < bytes.length; at += step) {
    pieces.push(bytes.slice(at, at + step));
  }
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      const next = pieces.shift();
      if (next === undefined) {
        controller.close();
      } else {
        controller.enqueue(next);
      }
    },
  });
  return new Response(stream, init);
}

export function outcomeKinds(outcome: AskOutcome): string {
  return `${outcome.ended ? "ended" : "open"}/${outcome.failed ? "failed" : "ok"}`;
}
