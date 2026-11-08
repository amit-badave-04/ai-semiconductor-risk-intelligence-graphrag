// R17 (docs/specs/m5b-frontend.md): the typed events of the /api/ask stream and of the upload-job stream, validated by type
// guards and never by casts. The wire is the contract in docs/v2/M5A_SSE_CONTRACT.md; the byte fixtures in tests/data/sse are
// its examples. A guard returns false for a frame of the wrong shape (a non-JSON payload, a string where a number belongs, a
// non-finite number): the reader then ignores the frame, so a malformed event can never reach the page as if it were valid.
// Unknown fields are allowed and ignored; unknown event names are not decoded at all (decodeEvent returns null).

export interface RetrievalEvent {
  event: "retrieval";
  anchors: Record<string, number>;
  counts: Record<string, number>;
  /** True when no company was detected in the question and the server defaulted to Nvidia. */
  anchor_defaulted?: boolean;
}

export interface StepEvent {
  event: "step";
  n: number;
  tool: string;
  summary: string;
  ok: boolean;
}

export interface DeltaEvent {
  event: "delta";
  text: string;
}

export interface EscalatedEvent {
  event: "escalated";
  reasons: string[];
  from: string;
  to: string;
}

export interface Usage {
  prompt_tokens: number;
  completion_tokens: number;
}

export interface AgentInfo {
  elapsed_s: number;
  model_calls?: number;
  fallback_reason?: string | null;
}

export interface WorkspaceInfo {
  id_hash: string;
  doc_chunks: number;
  stale_citations: string[];
  suspicious: boolean;
}

interface DoneBase {
  event: "done";
  answer: string;
  citations: string[];
  hallucinated: string[];
}

/** The answer served from the cache: no usage, no checks, no model call. */
export interface CachedDoneEvent extends DoneBase {
  cached: true;
  source?: string;
}

/** The answer of a live ask. `checks` is passed through to the checks footer (its own parser lives with the footer). */
export interface LiveDoneEvent extends DoneBase {
  cached?: false;
  /** Routing fields the answer badge reads (index.html answerBadge): which model answered and how it got there. */
  answered_by?: string | null;
  routed?: string | null; // "cheap" or "strong"
  escalated?: boolean;
  escalation_reasons?: string[];
  finish_reason?: string | null;
  usage?: Usage | null; // null when the provider reported none
  cost_usd?: number | null;
  checks?: Record<string, unknown>;
  agent?: AgentInfo;
  workspace?: WorkspaceInfo;
}

export type DoneEvent = CachedDoneEvent | LiveDoneEvent;

export interface ErrorEvent {
  event: "error";
  detail: string;
}

export type StreamEvent = RetrievalEvent | StepEvent | DeltaEvent | EscalatedEvent | DoneEvent | ErrorEvent;

export const JOB_STATES = [
  "received",
  "validating",
  "parsing",
  "chunking",
  "embedding",
  "comparing",
  "indexing",
  "ready",
  "failed",
] as const;
export type JobState = (typeof JOB_STATES)[number];

export interface JobProgress {
  done: number;
  total: number;
  eta_s: number | null;
}

export interface JobEvent {
  job_id: string;
  state: JobState;
  document_id: string;
  version: number | null;
  progress?: JobProgress;
  chunks?: number;
  units?: number;
  items_compared?: boolean;
  not_compared_reason?: string | null;
  suspicious?: boolean;
  error?: { code: string; message: string };
}

type Dict = Record<string, unknown>;

function isDict(value: unknown): value is Dict {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === "string";
}

function isNumberRecord(value: unknown): value is Record<string, number> {
  return isDict(value) && Object.values(value).every(isFiniteNumber);
}

/** Absent, or present and passing `check`: an optional field of the wrong type fails the whole event. */
function optional(value: Dict, key: string, check: (item: unknown) => boolean): boolean {
  return !(key in value) || check(value[key]);
}

export function isRetrievalEvent(value: unknown): value is RetrievalEvent {
  return (
    isDict(value) &&
    isNumberRecord(value.anchors) &&
    isNumberRecord(value.counts) &&
    optional(value, "anchor_defaulted", (item) => typeof item === "boolean")
  );
}

export function isStepEvent(value: unknown): value is StepEvent {
  return (
    isDict(value) &&
    isFiniteNumber(value.n) &&
    typeof value.tool === "string" &&
    typeof value.summary === "string" &&
    typeof value.ok === "boolean"
  );
}

export function isDeltaEvent(value: unknown): value is DeltaEvent {
  return isDict(value) && typeof value.text === "string";
}

export function isEscalatedEvent(value: unknown): value is EscalatedEvent {
  return (
    isDict(value) && isStringArray(value.reasons) && typeof value.from === "string" && typeof value.to === "string"
  );
}

function isDoneBase(value: unknown): value is Dict {
  return (
    isDict(value) && typeof value.answer === "string" && isStringArray(value.citations) && isStringArray(value.hallucinated)
  );
}

function isUsage(value: unknown): value is Usage {
  return isDict(value) && isFiniteNumber(value.prompt_tokens) && isFiniteNumber(value.completion_tokens);
}

function isAgentInfo(value: unknown): value is AgentInfo {
  return (
    isDict(value) &&
    isFiniteNumber(value.elapsed_s) &&
    optional(value, "model_calls", isFiniteNumber) &&
    optional(value, "fallback_reason", (item) => item === null || typeof item === "string")
  );
}

function isWorkspaceInfo(value: unknown): value is WorkspaceInfo {
  return (
    isDict(value) &&
    typeof value.id_hash === "string" &&
    isFiniteNumber(value.doc_chunks) &&
    isStringArray(value.stale_citations) &&
    typeof value.suspicious === "boolean"
  );
}

export function isCachedDoneEvent(value: unknown): value is CachedDoneEvent {
  return isDoneBase(value) && value.cached === true && optional(value, "source", (item) => typeof item === "string");
}

export function isLiveDoneEvent(value: unknown): value is LiveDoneEvent {
  return (
    isDoneBase(value) &&
    value.cached !== true &&
    optional(value, "cached", (item) => item === false) &&
    optional(value, "answered_by", isNullableString) &&
    optional(value, "routed", isNullableString) &&
    optional(value, "escalated", (item) => typeof item === "boolean") &&
    optional(value, "escalation_reasons", isStringArray) &&
    optional(value, "finish_reason", isNullableString) &&
    optional(value, "usage", (item) => item === null || isUsage(item)) &&
    optional(value, "cost_usd", (item) => item === null || isFiniteNumber(item)) &&
    optional(value, "checks", isDict) &&
    optional(value, "agent", isAgentInfo) &&
    optional(value, "workspace", isWorkspaceInfo)
  );
}

export function isDoneEvent(value: unknown): value is DoneEvent {
  return isCachedDoneEvent(value) || isLiveDoneEvent(value);
}

export function isErrorEvent(value: unknown): value is ErrorEvent {
  return isDict(value) && typeof value.detail === "string";
}

function isJobProgress(value: unknown): value is JobProgress {
  return (
    isDict(value) &&
    isFiniteNumber(value.done) &&
    isFiniteNumber(value.total) &&
    (value.eta_s === null || isFiniteNumber(value.eta_s))
  );
}

function isJobFailure(value: unknown): boolean {
  return isDict(value) && typeof value.code === "string" && typeof value.message === "string";
}

export function isJobState(value: unknown): value is JobState {
  return typeof value === "string" && (JOB_STATES as readonly string[]).includes(value);
}

export function isJobEvent(value: unknown): value is JobEvent {
  return (
    isDict(value) &&
    typeof value.job_id === "string" &&
    isJobState(value.state) &&
    typeof value.document_id === "string" &&
    (value.version === null || isFiniteNumber(value.version)) &&
    optional(value, "progress", isJobProgress) &&
    optional(value, "chunks", isFiniteNumber) &&
    optional(value, "units", isFiniteNumber) &&
    optional(value, "items_compared", (item) => typeof item === "boolean") &&
    optional(value, "not_compared_reason", (item) => item === null || typeof item === "string") &&
    optional(value, "suspicious", (item) => typeof item === "boolean") &&
    optional(value, "error", isJobFailure)
  );
}

export function isTerminal(event: StreamEvent): event is DoneEvent | ErrorEvent {
  return event.event === "done" || event.event === "error";
}

function parseJson(raw: string): unknown {
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return undefined;
  }
}

/** One SSE message (its `event:` name and `data:` payload) as a validated stream event, or null: an unknown name, data that
 *  is not JSON, or a payload of the wrong shape. The name decides the kind, as on the wire. */
export function decodeEvent(name: string | undefined, raw: string): StreamEvent | null {
  const data = parseJson(raw);
  switch (name) {
    case "retrieval":
      return isRetrievalEvent(data) ? { ...data, event: "retrieval" } : null;
    case "step":
      return isStepEvent(data) ? { ...data, event: "step" } : null;
    case "delta":
      return isDeltaEvent(data) ? { ...data, event: "delta" } : null;
    case "escalated":
      return isEscalatedEvent(data) ? { ...data, event: "escalated" } : null;
    case "done":
      return isDoneEvent(data) ? { ...data, event: "done" } : null;
    case "error":
      return isErrorEvent(data) ? { ...data, event: "error" } : null;
    default:
      return null;
  }
}

/** One `event: job` message as a validated job event, or null. */
export function decodeJobEvent(name: string | undefined, raw: string): JobEvent | null {
  if (name !== "job") {
    return null;
  }
  const data = parseJson(raw);
  return isJobEvent(data) ? data : null;
}
