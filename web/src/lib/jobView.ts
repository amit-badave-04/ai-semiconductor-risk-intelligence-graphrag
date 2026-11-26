// M2 R19 (docs/specs/m2-search.md, decision 009): what the upload card shows about one job, folded from its events. A reconnect
// replays the job's log from its start, so the fold is idempotent: an earlier event again never moves the view backwards (after ready nothing changes; embedding progress only grows).
import { deck } from "@/copy/deck";
import type { JobEvent, SearchLabel } from "./events";

export interface Preflight {
  /** The detected form ("10-K"...), or null when the server did not recognise it. */
  form: string | null;
  /** The labels of the sections found, as the server detected them (text from an upload: render escaped, never as HTML). */
  sections: readonly string[];
  sectionsMessage: string;
  chunks: number;
  pendingChunks: number;
}

export interface Embedding {
  done: number;
  total: number;
  /** Seconds the server estimates are left, or null before it can measure. */
  etaS: number | null;
}

export interface Outcome {
  embeddedChunks: number;
  keywordOnlyChunks: number;
  stopped: string | null;
  search: SearchLabel | null;
}

export interface JobView {
  /** "keyword_ready" has arrived: questions about this document can be asked. */
  keywordReady: boolean;
  preflight: Preflight | null;
  /** Null until the first `embedding` event: a job whose embedding has not started is waiting for its turn. */
  embedding: Embedding | null;
  outcome: Outcome | null;
}

export const EMPTY_VIEW: JobView = { keywordReady: false, preflight: null, embedding: null, outcome: null };

function preflightOf(job: JobEvent): Preflight {
  return {
    form: job.form ?? null,
    sections: job.sections ?? [],
    sectionsMessage: job.sections_message ?? "",
    chunks: job.chunks ?? 0,
    pendingChunks: job.pending_chunks ?? 0,
  };
}

/** The view after `job`; a new object, the old one untouched. */
export function foldJob(view: JobView | null, job: JobEvent): JobView {
  const prev = view ?? EMPTY_VIEW;
  if (prev.outcome !== null) return prev; // ready is the end: a replayed earlier event changes nothing
  switch (job.state) {
    case "keyword_ready":
      return { ...prev, keywordReady: true, preflight: preflightOf(job) };
    case "embedding": {
      if (job.progress === undefined) return prev;
      const { done, total, eta_s: eta } = job.progress;
      if (prev.embedding !== null && prev.embedding.total === total && done < prev.embedding.done) return prev;
      return { ...prev, embedding: { done, total, etaS: eta } };
    }
    case "ready":
      return {
        keywordReady: true,
        preflight: prev.preflight ?? preflightOf(job),
        embedding: prev.embedding,
        outcome: {
          embeddedChunks: job.embedded_chunks ?? 0,
          keywordOnlyChunks: job.keyword_only_chunks ?? 0,
          stopped: job.stopped ?? null,
          search: job.search ?? null,
        },
      };
    default:
      return prev;
  }
}

/** What the screen reader is told while search by meaning is prepared (a polite live region): the start, then a progress line
 *  at most at each 25% step and at most once a minute. The end or stopped line is not here: it is the card's own outcome. */
export interface AnnounceState {
  note: string;
  started: boolean;
  /** The highest 25% step already spoken (0, 25, 50 or 75). */
  milestone: number;
  /** When a note was last spoken (ms). */
  at: number;
}

export const ANNOUNCE_START: AnnounceState = { note: "", started: false, milestone: 0, at: Number.NEGATIVE_INFINITY };

const PROGRESS_MIN_GAP_MS = 60_000;
const MILESTONE_STEP = 25;
const LAST_MILESTONE = 75;

/** The next announcement state for `view` at `nowMs`; the same object when nothing new is to be said. */
export function announceStep(state: AnnounceState, view: JobView, nowMs: number): AnnounceState {
  if (view.outcome !== null) return state.note === "" ? state : { ...state, note: "" };
  const { embedding } = view;
  if (embedding === null) return state;
  if (!state.started) return { note: deck.jobs.card.started, started: true, milestone: 0, at: nowMs };
  if (embedding.total <= 0) return state;
  const step = Math.min(LAST_MILESTONE, Math.floor((embedding.done / embedding.total) * (100 / MILESTONE_STEP)) * MILESTONE_STEP);
  if (step <= state.milestone || nowMs - state.at < PROGRESS_MIN_GAP_MS) return state;
  return { note: deck.jobs.card.progress(embedding.done, embedding.total), started: true, milestone: step, at: nowMs };
}
