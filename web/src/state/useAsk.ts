// FROZEN CONTRACT (I3 main-first, 2026-11-08): the Ask state, as worker B's AskProvider (state/AskProvider.tsx) will provide it
// and the Ask page, the live status region and the steps timeline read it. Worker B implements the provider; the types, the
// hook and the behaviour promised in the JSDoc below do not change without main.
//
// R31: the Ask state lives above the router, so a route change neither aborts the stream nor loses the answer; the client
// aborts only on a deliberate user action (`abort`) or page unload. An ask starts only from an event handler, never from an
// effect (StrictMode re-runs effects: that would POST twice and spend twice), and the POST is never retried.
import { createContext, useContext } from "react";
import type { DoneEvent, EscalatedEvent, RetrievalEvent, StepEvent } from "@/lib/events";
import type { Refusal } from "@/lib/sse";

/**
 * - idle: nothing asked yet (or `reset`);
 * - streaming: a POST is open;
 * - ended: a done or error event arrived and was taken in (the server finished; an `error` event shows in `statusText`);
 * - failed: the page could not show the answer completely (a frame over the buffer limit, a handler threw);
 * - aborted: the visitor stopped it (`abort`) or the page unloaded.
 * A refused request (403, 429, 503...) and a cut connection end as `ended: false, failed: false`; `refusal` / `statusText` say why.
 */
export type AskStatus = "idle" | "streaming" | "ended" | "failed" | "aborted";

export interface AskRequest {
  question: string;
  /** "hybrid", "vector" or "agent"; "" or absent is the server default. "agent" is offered only when /api/stats says so. */
  strategy?: string;
}

export interface AskState {
  status: AskStatus;
  /**
   * The answer sequence: 0 before the first ask, then +1 for every ask that starts. It only ever increases (`reset` keeps it).
   * Chip keys are `place:seq:id:occurrence` (see chipKey), so streaming appends keep a chip's identity (A2) and a new answer
   * never reuses a key of the old one.
   */
  seq: number;
  /** The question of the current answer ("" when idle). */
  question: string;
  /** The strategy that was sent for the current answer ("" when the default). */
  strategy: string;
  retrieval: RetrievalEvent | null;
  /** The agent steps so far, in order (the timeline caps itself: 12 steps, 140 characters). */
  steps: readonly StepEvent[];
  escalated: EscalatedEvent | null;
  /** The streamed text so far (deltas appended); once `done` arrived, `done.answer`. Never HTML: render it as text nodes. */
  text: string;
  /** The terminal `done` event, once it arrived. */
  done: DoneEvent | null;
  /** Set when the server refused before any stream (non-OK response). The 403 clause of index.html:681 is part of `statusText`
   *  when a site key is set. */
  refusal: Refusal | null;
  /**
   * The one line for the status region (A3: role="status") and the status text next to the control (A10), or null:
   * a server `error` event's detail, a refusal's detail (plus the 403 clause), STREAM_CUT_STATUS or PAGE_FAILED_STATUS from
   * lib/sse.ts, or the aborted text. Null while streaming and after a clean `done`.
   */
  statusText: string | null;
}

export interface AskContextValue extends AskState {
  /**
   * Start an ask. CALL ONLY FROM AN EVENT HANDLER (click, Ctrl/Cmd+Enter), never from an effect. Ignored (no request) while
   * `status` is "streaming" and for an empty question. Sends exactly one POST /api/ask, at once with the current Turnstile token
   * or none (R33(1); the client never holds an ask back), consumes the token and resets the widget, adds the open workspace
   * (workspace_id, as_of, X-Workspace-Token header) when there is one, and reloads /api/stats at the legacy four points.
   */
  ask: (request: AskRequest) => void;
  /** Stop the open stream (a deliberate user action): the status becomes "aborted". No effect when nothing streams. */
  abort: () => void;
  /** Clear the answer back to the idle state (not `seq`). Aborts nothing: call `abort` first if a stream is open. */
  reset: () => void;
}

export const IDLE_ASK_STATE: AskState = {
  status: "idle",
  seq: 0,
  question: "",
  strategy: "",
  retrieval: null,
  steps: [],
  escalated: null,
  text: "",
  done: null,
  refusal: null,
  statusText: null,
};

const INERT: AskContextValue = { ...IDLE_ASK_STATE, ask: () => {}, abort: () => {}, reset: () => {} };

export const AskContext = createContext<AskContextValue>(INERT);

/** The Ask state. Outside a provider it is idle and its functions do nothing (keeps component tests simple). */
export function useAsk(): AskContextValue {
  return useContext(AskContext);
}
