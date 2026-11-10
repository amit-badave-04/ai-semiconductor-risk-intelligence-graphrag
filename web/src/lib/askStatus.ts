// R22: the sentences of the Ask page that are computed from data, as pure functions (ports of index.html's retrievalStatus, limitsText,
// costNote and examplesHtml's grouping) and the one status line of an ask, derived from the AskState. Everything is plain text.
import type { AskState } from "@/state/useAsk";
import { escalationStatus, shortModel } from "./answerMeta";
import type { RetrievalEvent } from "./events";
import type { Limits } from "@/copy/deck";
import type { Stats } from "./statsStore";

// the legacy status projection's words (index.html; pinned by tests/test_web_copy_deck.py), moved here from pages/Ask.copy.ts
export const STATUS_RETRIEVING = "retrieving from the graph\u2026";
export const STATUS_AGENT = "the research agent is choosing lookups\u2026";
export const STATUS_GENERATING = " \u2014 generating\u2026";
export const STATUS_CACHED = "served from cache \u2014 no model call";
export const STATUS_DONE = "done";
export const NO_COMPANY_ANCHOR = "no company detected; defaulting to Nvidia";
export const COST_NOTE_DEFAULT = "one or two model calls each";

type Dict = Record<string, unknown>;
const dictOf = (value: unknown): Dict => (typeof value === "object" && value !== null ? (value as Dict) : {});

/** "anchors: Nvidia, TSMC \u00b7 12 relationships \u00b7 ..." from the retrieval event (the counts the server sent, nothing more). */
export function retrievalStatus(event: Pick<RetrievalEvent, "anchors" | "counts" | "anchor_defaulted"> | null | undefined): string {
  const counts = dictOf(event?.counts);
  const names = event?.anchors ? Object.keys(event.anchors) : [];
  const parts = [`anchors: ${event?.anchor_defaulted ? NO_COMPANY_ANCHOR : names.join(", ") || "none"}`];
  const add = (n: unknown, label: string) => {
    if (typeof n === "number") parts.push(`${n} ${label}`);
  };
  add(counts.edges, "relationships");
  add(counts.metrics, "XBRL metrics");
  add(counts.risks, "active risk disclosures");
  add(counts.temporal, "risk-change items");
  add(counts.chunks, "excerpts");
  return parts.join(" \u00b7 ");
}

/** The hint next to the Ask button: the per-address limit and the models. "" when the service reported neither. */
export function limitsText(stats: Stats | null): string {
  const limits = dictOf(stats?.limits);
  const models = dictOf(stats?.models);
  const parts: string[] = [];
  if (limits.per_ip) parts.push(`limit: ${String(limits.per_ip)} per address`);
  if (models.llm) {
    const escalation = models.escalation
      ? ` (drafts are checked before they are shown; ${shortModel(models.escalation)} answers change-over-time questions and re-answers rejected drafts)`
      : "";
    parts.push(`model: ${shortModel(models.llm)}${escalation}`);
  }
  return parts.join(" \u00b7 ");
}

/** docs/specs/m5b-redesign.md RD4a, decision D-R5: the approved defaults, equal to the server's Settings defaults
 *  (rate_limit_questions, rate_limit_window_seconds / 60, paid_per_ip_per_day; tests/test_web_copy_deck.py checks them). */
export const DEFAULT_LIMITS: Limits = { perWindow: 5, windowMinutes: 10, perDay: 20 };

const PER_IP = /^(\d+) per (\d+) min$/;
const positive = (n: number): boolean => Number.isInteger(n) && n > 0;

/** The limits of the cost line: /api/stats.limits per_ip ("5 per 10 min") and per_ip_per_day, each field falling back to its
 *  approved default when it is missing or unreadable (never a NaN, negative or fractional number on the page). */
export function liveLimits(stats: Stats | null): Limits {
  const limits = dictOf(stats?.limits);
  const window = typeof limits.per_ip === "string" ? PER_IP.exec(limits.per_ip) : null;
  const perWindow = window ? Number(window[1]) : NaN;
  const windowMinutes = window ? Number(window[2]) : NaN;
  const perDay = limits.per_ip_per_day;
  return {
    perWindow: positive(perWindow) && positive(windowMinutes) ? perWindow : DEFAULT_LIMITS.perWindow,
    windowMinutes: positive(perWindow) && positive(windowMinutes) ? windowMinutes : DEFAULT_LIMITS.windowMinutes,
    // 0 is the server's "no per-address daily cap" (config.py paid_per_ip_per_day): the cost line then names none
    perDay: perDay === 0 ? null : typeof perDay === "number" && positive(perDay) ? perDay : DEFAULT_LIMITS.perDay,
  };
}

/** The cost note of the footer. */
export function costNote(models: unknown): string {
  const m = dictOf(models);
  const answering = shortModel(m.llm);
  const escalation = shortModel(m.escalation);
  if (answering && escalation && answering !== escalation) {
    return `one ${answering} call each, plus a second ${escalation} call when the draft fails its checks; change-over-time questions go straight to ${escalation}`;
  }
  if (answering) return `one ${answering} call each`;
  return COST_NOTE_DEFAULT;
}

export interface Example {
  type: string;
  question: string;
}

/** The saved examples grouped by type, in the order the types first appear. An example without a string question is dropped. */
export function groupExamples(payload: unknown): { type: string; questions: string[] }[] {
  const groups = new Map<string, string[]>();
  const examples = dictOf(payload).examples;
  for (const item of Array.isArray(examples) ? examples : []) {
    const e = dictOf(item);
    if (typeof e.question !== "string") continue;
    const type = String(e.type ?? "");
    groups.set(type, [...(groups.get(type) ?? []), e.question]);
  }
  return Array.from(groups, ([type, questions]) => ({ type, questions }));
}

export interface StatusLine {
  text: string;
  /** The line is an error or a refusal (shown in the error colour). */
  isError: boolean;
}

/** The status line of the current answer, true on every exit: progress while it streams, then done, cached, an error or a refusal. */
export function statusLine(ask: Pick<AskState, "status" | "strategy" | "retrieval" | "escalated" | "done" | "statusText">): StatusLine {
  if (ask.statusText !== null) return { text: ask.statusText, isError: true };
  if (ask.status === "streaming") {
    if (ask.escalated !== null) return { text: escalationStatus(ask.escalated), isError: false };
    if (ask.retrieval !== null) return { text: retrievalStatus(ask.retrieval) + STATUS_GENERATING, isError: false };
    return { text: ask.strategy === "agent" ? STATUS_AGENT : STATUS_RETRIEVING, isError: false };
  }
  if (ask.status === "ended" && ask.done !== null) return { text: ask.done.cached ? STATUS_CACHED : STATUS_DONE, isError: false };
  return { text: "", isError: false };
}

// ------------------------------------------------------------------ docs/specs/m5b-redesign.md RD10: the status line as codes

export type StatusCode = "idle" | "error" | "retrieving" | "agent" | "retrieval" | "escalated" | "cachedSaved" | "cachedEarlier" | "done";

export interface RetrievalParams {
  anchors: string[];
  defaulted: boolean;
  passages: number;
  figures: number;
  relationships: number;
  risks: number;
  changes: number;
}

export interface StatusView {
  code: StatusCode;
  isError: boolean;
  params?: { text: string } | RetrievalParams;
}

const count = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0);

function retrievalParams(event: Pick<RetrievalEvent, "anchors" | "counts" | "anchor_defaulted">): RetrievalParams {
  const counts = dictOf(event.counts);
  return {
    anchors: Object.keys(dictOf(event.anchors)),
    defaulted: event.anchor_defaulted === true,
    passages: count(counts.chunks),
    figures: count(counts.metrics),
    relationships: count(counts.edges),
    risks: count(counts.risks),
    changes: count(counts.temporal),
  };
}

/** The status of the current answer as a code (the deck words it): the same states as statusLine, in plain words. */
export function statusView(ask: Pick<AskState, "status" | "strategy" | "retrieval" | "escalated" | "done" | "statusText">): StatusView {
  if (ask.statusText !== null) return { code: "error", isError: true, params: { text: ask.statusText } };
  if (ask.status === "streaming") {
    if (ask.escalated !== null) return { code: "escalated", isError: false };
    if (ask.retrieval !== null) return { code: "retrieval", isError: false, params: retrievalParams(ask.retrieval) };
    return { code: ask.strategy === "agent" ? "agent" : "retrieving", isError: false };
  }
  if (ask.status === "ended" && ask.done !== null) {
    if (!ask.done.cached) return { code: "done", isError: false };
    return { code: (ask.done as { source?: unknown }).source === "benchmark" ? "cachedSaved" : "cachedEarlier", isError: false };
  }
  return { code: "idle", isError: false };
}
