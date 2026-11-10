// docs/specs/m5b-redesign.md RD13: which pinned change-claim wordings (pages/Method.copy.ts HEDGES) an answer holds. A plain,
// case-insensitive substring test after whitespace normalisation; the answer is untrusted text and no RegExp is built from it.
import { HEDGES } from "@/pages/Method.copy";

export type HedgeGroup = keyof typeof HEDGES;

const ORDER: readonly HedgeGroup[] = ["removed", "unmatchedNew", "passageRemoved"];
const squash = (text: string): string => text.replace(/\s+/g, " ").toLowerCase();

/** The hedge groups whose wording the text holds, each once, in a fixed order. */
export function hedgesIn(text: string): HedgeGroup[] {
  const haystack = squash(text);
  return ORDER.filter((group) => haystack.includes(HEDGES[group].phrase));
}
