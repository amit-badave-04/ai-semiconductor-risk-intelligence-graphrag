// R22 / R31: the state transitions of one ask, as pure functions. The AskProvider accumulates a stream through `applyEvent`, and
// closes it with `finishState`. A function that throws here is a PAGE failure (the stream client reports it as `failed`, never as
// "done"). Nothing in this file touches the DOM or the network.
import type { StreamEvent } from "@/lib/events";
import { PAGE_FAILED_STATUS, STREAM_CUT_STATUS, type AskOutcome } from "@/lib/sse";
import { CLAUSE_NO_TOKEN, CLAUSE_TOKEN_EXPIRED, STATUS_NETWORK } from "@/copy/deck";
import { FORBIDDEN_STATUS } from "@/pages/Ask.constants";
import { MAX_STEPS_SHOWN } from "@/lib/steps";
import { IDLE_ASK_STATE, type AskState } from "./useAsk";

/** The state of an ask that has just been sent: the answer sequence moves on and everything of the old answer is gone. */
export function startState(previous: AskState, question: string, strategy: string): AskState {
  return { ...IDLE_ASK_STATE, status: "streaming", seq: previous.seq + 1, question, strategy };
}

/** Take one validated stream event in. A `done` replaces the streamed text by the answer; an `error` is kept as the status line. */
export function applyEvent(state: AskState, event: StreamEvent): AskState {
  switch (event.event) {
    case "retrieval":
      return { ...state, retrieval: event };
    case "step":
      return state.steps.length >= MAX_STEPS_SHOWN ? state : { ...state, steps: [...state.steps, event] };
    case "escalated":
      return { ...state, escalated: event };
    case "delta":
      return { ...state, text: state.text + event.text };
    case "done":
      return { ...state, text: event.answer, done: event };
    case "error":
      return { ...state, statusText: event.detail };
  }
}

export interface FinishInput {
  outcome: AskOutcome;
  /** The request never reached the server (the fetch itself rejected) and the visitor did not stop it. */
  networkError: boolean;
  /** A site key is set (the 403 clause applies). */
  siteKeySet: boolean;
  /** The ask carried a bot-check token (picks which clause of the 403 is true). */
  tokenHeld: boolean;
}

/** The true state once the call is over, whichever way it ended. The status text never says more than what happened. */
export function finishState(state: AskState, input: FinishInput): AskState {
  const { outcome, networkError, siteKeySet, tokenHeld } = input;
  if (outcome.refusal !== null) {
    const clause = outcome.refusal.status === FORBIDDEN_STATUS && siteKeySet ? (tokenHeld ? CLAUSE_TOKEN_EXPIRED : CLAUSE_NO_TOKEN) : "";
    return { ...state, status: "ended", refusal: outcome.refusal, statusText: outcome.refusal.detail + clause };
  }
  if (outcome.aborted) return { ...state, status: "aborted", statusText: null };
  if (networkError) return { ...state, status: "failed", statusText: STATUS_NETWORK };
  if (outcome.ended) return { ...state, status: "ended" };
  return { ...state, status: "failed", statusText: outcome.status ?? (outcome.failed ? PAGE_FAILED_STATUS : STREAM_CUT_STATUS) };
}
