// R22: the research agent's steps as one plain sentence each (a port of index.html's stepText, agentEnabled and tracingNote). A step
// carries a tool name and a summary (counts, ids, fiscal years); both are server text, rendered as a text node only (S2, S11).
import { TRACING_NOTE } from "@/pages/Method.copy";
import type { StepEvent } from "./events";

export const MAX_STEP_CHARS = 140;
export const MAX_STEPS_SHOWN = 12;

const STEP_VERBS: Readonly<Record<string, readonly [string, string]>> = {
  lookup_company: ["Looked up", " "],
  search_filings: ["Searched the filings", ": "],
  financial_metrics: ["Fetched", " "],
  risk_changes: ["Compared risk disclosures", ": "],
  relationships: ["Checked relationships", ": "],
  active_risks: ["Listed active risks", ": "],
  compute_change: ["Computed", " "],
};

const TOOL_NAME = /^[a-z][a-z0-9_]{0,40}$/;

/** The sentence of one step. Defensive about the shape of what the server sent; the summary is bounded to MAX_STEP_CHARS. */
export function stepText(event: StepEvent | null | undefined): string {
  const ev = (typeof event === "object" && event !== null ? event : {}) as Record<string, unknown>;
  const tool = typeof ev.tool === "string" && TOOL_NAME.test(ev.tool) ? ev.tool : "";
  const known = Object.hasOwn(STEP_VERBS, tool);
  const [verb, joiner] = known ? STEP_VERBS[tool] : [tool ? `Ran ${tool.replace(/_/g, " ")}` : "Ran a tool", ": "];
  const raw = typeof ev.summary === "string" ? ev.summary.replace(/\s+/g, " ").trim() : "";
  const summary = raw.length > MAX_STEP_CHARS ? `${raw.slice(0, MAX_STEP_CHARS)}\u2026` : raw;
  return verb + (summary ? joiner + summary : "") + (ev.ok === false ? " (failed)" : "");
}

/** The agent option is offered only when the service says so with an explicit true. */
export function agentEnabled(stats: unknown): boolean {
  return typeof stats === "object" && stats !== null && (stats as { agent_enabled?: unknown }).agent_enabled === true;
}

/** The privacy line: only when the agent is on and a sample of its questions is really traced; "" otherwise. */
export function tracingNote(stats: unknown): string {
  return agentEnabled(stats) && (stats as { tracing?: unknown }).tracing === true ? TRACING_NOTE : "";
}
