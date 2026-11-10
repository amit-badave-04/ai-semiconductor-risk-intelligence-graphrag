// docs/specs/m5b-redesign.md RD13: which change claims an answer makes, by group (pages/Method.copy.ts HEDGES holds each group's
// pinned wording and hand-check counts). R2 review C1 and C2: a group counts when a sentence of the answer makes that claim, in
// the context heading's wording or the one the answer prompt prescribes (src/semigraph/artifacts/prompts/answer.txt), and not
// when the sentence says none was found ("found no risk factor that no longer appears ...") or names the list by its quoted
// heading. The answer is untrusted text: only fixed patterns run over it (no RegExp is built from it), sentence by sentence,
// in time linear in its length.
import type { HEDGES } from "@/pages/Method.copy";

export type HedgeGroup = keyof typeof HEDGES;

const ORDER: readonly HedgeGroup[] = ["removed", "unmatchedNew", "passageRemoved"];

/** Each group's wordings: the context heading's and the prompt's (singular or plural, with or without "was", "whose"). The
 *  passage group is the sentences missing from the NEWER filing only (its counts are about those). */
const CLAIMS: Record<HedgeGroup, RegExp> = {
  removed: /no longer appear(?:s|ing)? (?:as )?(?:a )?separate(?:ly)?/,
  unmatchedNew: /no matching risk factor (?:was |were )?found in the earlier filing/,
  passageRemoved: /wording (?:[a-z0-9' -]{0,40}? )?(?:was |is )?not found in the (?!older|earlier)/,
};

/** Before the wording in its sentence: the answer says no such item was found. */
const NONE_BEFORE = ["found no ", "no such ", "not listed", "none ", "did not find", "no risk factor was found", "there is no ", "there are no ", "there were no "];
/** After the wording in its sentence: the claim is made of nothing ("... for any items; none were listed"). */
const NONE_AFTER = ["for any item", "none were listed", "none was listed", "none found"];
/** A sentence ends at . ! ? followed by white space, at a blank line, or where a list item starts; any other line break is a
 *  space (a sentence may wrap). */
const SENTENCES = /(?<=[.!?])\s+|\n\s*\n|\n(?=[ \t]*(?:[-*+]|\d+\.)\s)/;
/** How far before and after the wording (in characters) a cue that says none was found is looked for. */
const CUE_WINDOW_CHARS = 300;

const normalise = (text: string): string => text.replace(/[\u201c\u201d]/g, '"').toLowerCase();
const flatten = (sentence: string): string => sentence.replace(/\s+/g, " ");

/** Whether the wording at `at` (of `length` characters) in `sentence` makes the claim. */
function claims(sentence: string, at: number, length: number): boolean {
  const before = sentence.slice(Math.max(0, at - CUE_WINDOW_CHARS), at);
  if (/"\s*$/.test(before)) return false; // the list's quoted heading, named, not a claim
  if (NONE_BEFORE.some((cue) => before.includes(cue))) return false;
  const after = sentence.slice(at + length, at + length + CUE_WINDOW_CHARS);
  return !NONE_AFTER.some((cue) => after.includes(cue));
}

/** The groups whose claim the text makes, each once, in a fixed order. */
export function hedgesIn(text: string): HedgeGroup[] {
  const found = new Set<HedgeGroup>();
  for (const raw of normalise(text).split(SENTENCES)) {
    const sentence = flatten(raw);
    for (const group of ORDER) {
      if (found.has(group)) continue;
      for (const match of sentence.matchAll(new RegExp(CLAIMS[group].source, "g"))) {
        if (claims(sentence, match.index, match[0].length)) {
          found.add(group);
          break;
        }
      }
    }
  }
  return ORDER.filter((group) => found.has(group));
}
