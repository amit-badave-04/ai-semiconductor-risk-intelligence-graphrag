// R19 (docs/specs/m5b-frontend.md): one watcher of an upload job. GET /api/workspace/{ws}/jobs/{job_id} with X-Workspace-Token
// (the token is a header, never part of the URL) streams `event: job` frames until `ready` or `failed`.
//   * the server allows three live watchers per job and answers a fourth with 429, so the client keeps AT MOST ONE open GET per
//     watch: a retry starts only after the previous response was released (never two in flight);
//   * 429, 5xx, a rejected fetch and an early end (the stream closed before a terminal state) are retried, at most
//     `maxAttempts` requests in all, with growing waits; a 404 means the job is gone; any other status is not retried;
//   * a reconnect replays the job's log from its start (the server sends every event of the job to a new watcher), so `onJob`
//     may see an earlier state again: a consumer must treat states idempotently.
// Queue orchestration (several jobs, the polling fallback) is jobs.ts, I3.
import { createParser, type ParseError } from "eventsource-parser";
import { decodeJobEvent, type JobEvent } from "./events";
import { MAX_BUFFER_SIZE } from "./sse";

export type WatchResult =
  | { kind: "terminal"; job: JobEvent }
  | { kind: "gone" }
  | { kind: "aborted" }
  | { kind: "gave_up"; reason: "attempts" | "status" | "overflow"; status: number };

export interface WatchOptions {
  onJob: (job: JobEvent) => void;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  /** Resolves after `ms`, or at once when `signal` aborts. */
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
  maxAttempts?: number;
}

export const MAX_ATTEMPTS = 5;
export const RETRY_DELAYS_MS = [500, 1000, 2000, 4000];

// The server's id grammar: a workspace id is secrets.token_hex(16) checked by uploads.WORKSPACE_ID_RE, a job id is
// secrets.token_hex(8) (uploads/jobs.py). Anything else (".", "..", a slash, upper case) is never put into a path, so the token
// can only ever go to this job's route: encodeURIComponent alone leaves "." as a path segment the URL parser resolves.
const WORKSPACE_ID_RE = /^[0-9a-f]{32}$/;
const JOB_ID_RE = /^[0-9a-f]{16}$/;

const DEFAULT_SLEEP = (ms: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve) => {
    if (signal?.aborted === true) {
      resolve();
      return;
    }
    const done = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal?.addEventListener("abort", done, { once: true });
  });

const isRetryable = (status: number): boolean => status === 429 || status >= 500;

async function release(reader: ReadableStreamDefaultReader<Uint8Array> | null | undefined): Promise<void> {
  try {
    await reader?.cancel();
  } catch {
    // already closed
  }
}

type Attempt = { kind: "retry"; status: number } | WatchResult;

async function attemptOnce(
  url: string,
  token: string,
  options: WatchOptions,
  fetchImpl: typeof fetch,
): Promise<Attempt> {
  let response: Response;
  try {
    response = await fetchImpl(url, { headers: { "X-Workspace-Token": token }, signal: options.signal });
  } catch {
    return { kind: "retry", status: 0 };
  }
  if (!response.ok || response.body === null) {
    await release(response.body?.getReader());
    if (response.status === 404) {
      return { kind: "gone" };
    }
    return isRetryable(response.status)
      ? { kind: "retry", status: response.status }
      : { kind: "gave_up", reason: "status", status: response.status };
  }
  return readJobs(response, options);
}

interface JobReadState {
  terminal: JobEvent | null;
  overflowed: boolean;
}

/** The parser for one job stream: it feeds validated job events to onJob and records, in state, the terminal job or an
 *  overflow. A throw of onJob propagates through feed(): the page's own failure, released by the caller and rethrown. */
function jobParser(state: JobReadState, onJob: (job: JobEvent) => void) {
  return createParser({
    maxBufferSize: MAX_BUFFER_SIZE,
    onEvent: (message) => {
      if (state.overflowed || state.terminal !== null) {
        return;
      }
      if (message.data.length > MAX_BUFFER_SIZE) {
        state.overflowed = true; // the parser's own count never sees a whole frame that arrived in one read (S6)
        return;
      }
      const job = decodeJobEvent(message.event, message.data);
      if (job === null) {
        return;
      }
      onJob(job);
      if (job.state === "ready" || job.state === "failed") {
        state.terminal = job;
      }
    },
    onError: (error: ParseError) => {
      state.overflowed = state.overflowed || error.type === "max-buffer-size-exceeded";
    },
  });
}

async function readJobs(response: Response, options: WatchOptions): Promise<Attempt> {
  const reader = (response.body as ReadableStream<Uint8Array>).getReader();
  const decoder = new TextDecoder();
  const state: JobReadState = { terminal: null, overflowed: false };
  const parser = jobParser(state, options.onJob);
  try {
    while (state.terminal === null && !state.overflowed && options.signal?.aborted !== true) {
      let step: ReadableStreamReadResult<Uint8Array>;
      try {
        step = await reader.read();
      } catch {
        break; // a reset or an abort
      }
      if (step.done) {
        break;
      }
      parser.feed(decoder.decode(step.value, { stream: true }));
    }
  } finally {
    await release(reader); // before any retry: never two open responses
  }
  if (state.terminal !== null) {
    return { kind: "terminal", job: state.terminal };
  }
  if (state.overflowed) {
    return { kind: "gave_up", reason: "overflow", status: response.status };
  }
  return options.signal?.aborted === true ? { kind: "aborted" } : { kind: "retry", status: response.status };
}

export async function watchJob(
  workspaceId: string,
  jobId: string,
  token: string,
  options: WatchOptions,
): Promise<WatchResult> {
  const { signal, fetchImpl = fetch, sleep = DEFAULT_SLEEP, maxAttempts = MAX_ATTEMPTS } = options;
  if (!WORKSPACE_ID_RE.test(workspaceId) || !JOB_ID_RE.test(jobId)) {
    return { kind: "gone" }; // no such job can exist: no request at all
  }
  const url = `/api/workspace/${workspaceId}/jobs/${jobId}`;
  let lastStatus = 0;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    if (signal?.aborted === true) {
      return { kind: "aborted" };
    }
    const result = await attemptOnce(url, token, options, fetchImpl);
    if (result.kind !== "retry") {
      return result;
    }
    lastStatus = result.status;
    if (attempt < maxAttempts) {
      await sleep(RETRY_DELAYS_MS[Math.min(attempt - 1, RETRY_DELAYS_MS.length - 1)] ?? 0, signal);
    }
  }
  return signal?.aborted === true
    ? { kind: "aborted" }
    : { kind: "gave_up", reason: "attempts", status: lastStatus };
}
