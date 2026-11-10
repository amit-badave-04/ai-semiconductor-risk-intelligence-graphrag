// R17 (docs/specs/m5b-frontend.md): the /api/ask stream reader. A fetch POST read through eventsource-parser with an explicit,
// bounded buffer. The contract is docs/v2/M5A_SSE_CONTRACT.md. Four rules:
//   * a POST is paid for, so it is NEVER retried: a cut stream, a clean early end and a page failure all end the call;
//   * `ended` (a done or error event arrived AND the handler took it in without throwing) is kept apart from `failed` (the
//     page itself threw, or a frame went over the buffer limit), so the status returned is always the true one;
//   * unknown events, comments, pings, data that is not JSON and payloads of the wrong shape are ignored;
//   * the parser never buffers without limit: eventsource-parser has no default cap, so one is set here.
import { deck } from "@/copy/deck";
import { createParser, type ParseError } from "eventsource-parser";
import { decodeEvent, isTerminal, type StreamEvent } from "./events";
import { detailOf } from "./refusal";

export const ASK_URL = "/api/ask";

/** The most characters the parser may hold for one unfinished line or event. Sized against `done_max_size.sse` (the server's
 *  largest answer, 2,400 tokens, plus citations and checks: about 17 KB on the wire): 256 KiB is about fifteen times that, so a
 *  real answer never trips it while a runaway frame is stopped. Pinned against the contract doc by a test. */
export const MAX_BUFFER_SIZE = 256 * 1024;

// the words live in the deck (docs/specs/m5b-redesign.md section 4); the names stay for the callers and their tests
export const STREAM_CUT_STATUS = deck.ask.streamCut;
export const PAGE_FAILED_STATUS = deck.ask.pageFailed;

export interface AskBody {
  question: string;
  strategy?: string;
  workspace_id?: string;
  as_of?: string;
  turnstile_token?: string;
}

export interface AskOptions {
  /** Called for each validated event, in order. It may throw: that is a page failure, and the call ends as `failed`. */
  onEvent: (event: StreamEvent) => void;
  signal?: AbortSignal;
  /** Extra request headers (X-Workspace-Token for a workspace ask). */
  headers?: Record<string, string>;
  fetchImpl?: typeof fetch;
  maxBufferSize?: number;
}

export interface Refusal {
  status: number;
  detail: string;
}

export interface AskOutcome {
  /** A done or error event arrived and its handler returned. */
  ended: boolean;
  /** The page threw while taking an event in, or a frame went over the buffer limit, or the response had no body. */
  failed: boolean;
  /** The status text to show when the stream did not end itself; null when `ended` (the page owns the status). For a
   *  refusal, the response's JSON detail. */
  status: string | null;
  /** Set when the server refused before any stream (a non-OK response), through `detailOf`. */
  refusal: Refusal | null;
  /** The error a handler threw, when `failed` came from one. */
  thrown?: unknown;
  /** The call ended because the CALLER's own signal aborted (and no terminal event had arrived): the visitor stopped it, or the
   *  page unloaded. Such a call is neither a cut connection nor a page failure, and the caller must not word it as either. */
  aborted: boolean;
}

function outcome(parts: Partial<AskOutcome> & Pick<AskOutcome, "ended" | "failed">): AskOutcome {
  const { ended, failed } = parts;
  const status = parts.status !== undefined ? parts.status : ended ? null : failed ? PAGE_FAILED_STATUS : STREAM_CUT_STATUS;
  return { refusal: null, aborted: false, ...parts, status };
}

/** Mark an outcome as aborted when the caller's own signal is what ended it (never after a terminal event). */
function withAbort(result: AskOutcome, signal: AbortSignal | undefined): AskOutcome {
  return signal?.aborted === true && !result.ended ? { ...result, aborted: true } : result;
}

async function release(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<void> {
  try {
    await reader.cancel();
  } catch {
    // the connection is already gone: nothing left to release
  }
}

/** Read one answer. Resolves, never rejects: every way out is described by the outcome. */
export async function askStream(body: AskBody, options: AskOptions): Promise<AskOutcome> {
  return withAbort(await askOnce(body, options), options.signal);
}

async function askOnce(body: AskBody, options: AskOptions): Promise<AskOutcome> {
  const { onEvent, signal, headers, fetchImpl = fetch, maxBufferSize = MAX_BUFFER_SIZE } = options;
  let response: Response;
  try {
    response = await fetchImpl(ASK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
      signal,
    });
  } catch {
    return outcome({ ended: false, failed: false }); // no connection, or an abort: nothing arrived
  }
  if (!response.ok) {
    const detail = await detailOf(response);
    return outcome({ ended: false, failed: false, status: detail, refusal: { status: response.status, detail } });
  }
  if (response.body === null) {
    return outcome({ ended: false, failed: true });
  }
  return read(response.body.getReader(), onEvent, signal, maxBufferSize);
}

interface ReadState {
  ended: boolean;
  overflowed: boolean;
}

/** The parser for one answer: it feeds validated events to onEvent and records, in state, that a terminal event was taken in
 *  or that a frame went over the limit. A throw of onEvent propagates through feed(): a page failure. */
function askParser(state: ReadState, onEvent: (event: StreamEvent) => void, maxBufferSize: number) {
  return createParser({
    maxBufferSize,
    onEvent: (message) => {
      if (state.ended || state.overflowed) {
        return;
      }
      if (message.data.length > maxBufferSize) {
        state.overflowed = true; // the parser's own count never sees a whole frame that arrived in one read (S6)
        return;
      }
      const event = decodeEvent(message.event, message.data);
      if (event === null) {
        return;
      }
      onEvent(event);
      state.ended = isTerminal(event);
    },
    onError: (error: ParseError) => {
      state.overflowed = state.overflowed || error.type === "max-buffer-size-exceeded";
    },
  });
}

async function read(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (event: StreamEvent) => void,
  signal: AbortSignal | undefined,
  maxBufferSize: number,
): Promise<AskOutcome> {
  const decoder = new TextDecoder();
  const state: ReadState = { ended: false, overflowed: false };
  let failed = false;
  let thrown: unknown;
  const parser = askParser(state, onEvent, maxBufferSize);
  // A pending read must not outlive the caller's abort: cancelling the reader settles it (a real fetch errors the read itself).
  const onAbort = () => void release(reader);
  signal?.addEventListener("abort", onAbort);
  try {
    while (!state.ended && !state.overflowed) {
      if (signal?.aborted) {
        break;
      }
      let step: ReadableStreamReadResult<Uint8Array>;
      try {
        step = await reader.read();
      } catch {
        break; // a reset or aborted stream: reported as a cut below
      }
      if (step.done) {
        break;
      }
      parser.feed(decoder.decode(step.value, { stream: true }));
    }
    failed = state.overflowed && !state.ended; // a frame over the limit is a failure of the stream, never a silent drop (but not of an answer that already ended)
  } catch (error) {
    failed = true; // the handler, the decoder or the parser threw
    thrown = error;
  } finally {
    signal?.removeEventListener("abort", onAbort);
    await release(reader); // the terminated parser is never fed again; the connection is let go
  }
  return outcome({ ended: state.ended, failed, ...(thrown === undefined ? {} : { thrown }) });
}
