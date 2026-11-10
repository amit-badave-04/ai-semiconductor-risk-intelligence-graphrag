// docs/specs/m5b-redesign.md RD7: the Depth choices of "More options" and the strategy each sends (Standard = hybrid, Deep research
// = agent, Basic text search = vector). R23: with a workspace open only Standard; Deep research only while /api/stats says the
// agent is on.
import { deck } from "@/copy/deck";

export type Depth = "hybrid" | "agent" | "vector";

export interface DepthOption {
  value: Depth;
  /** the short name shown in the "Depth: ..." summary */
  name: string;
  label: string;
  hint: string;
}

const o = deck.options;
const STANDARD: DepthOption = { value: "hybrid", name: o.standard, label: o.standardLabel, hint: o.standardHint };
const DEEP: DepthOption = { value: "agent", name: o.deep, label: o.deep, hint: o.deepHint };
const BASIC: DepthOption = { value: "vector", name: o.basic, label: o.basic, hint: o.basicHint };

/** The depths on offer (R23: with a workspace open only Standard; Deep research only while /api/stats says the agent is on). */
export function depthOptions(agentOn: boolean, workspaceOpen: boolean): DepthOption[] {
  if (workspaceOpen) return [STANDARD];
  return agentOn ? [STANDARD, DEEP, BASIC] : [STANDARD, BASIC];
}
