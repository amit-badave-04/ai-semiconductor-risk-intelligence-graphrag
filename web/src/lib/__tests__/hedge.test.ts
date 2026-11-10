import { describe, expect, it } from "vitest";
import { hedgesIn, type HedgeGroup } from "../hedge";

// docs/specs/m5b-redesign.md RD13: which change-claim wordings an answer makes (the facts file's HEDGES, pinned against the
// server's own wording and the hand-check counts by tests/test_web_method_copy.py). R2 review C1 and C2: a wording counts only
// when the answer makes the claim (not when it says none was found, or names the list by its quoted heading), and the wordings
// the answer prompt itself prescribes (src/semigraph/artifacts/prompts/answer.txt) count as well as the context headings.

describe("hedgesIn", () => {
  it("finds each change-claim wording in any case and across extra whitespace", () => {
    expect(hedgesIn("Two risk factors **no longer appear as separate risk factors**.")).toEqual(["removed"]);
    expect(hedgesIn("The cybersecurity item No Longer Appears As A Separate\nRisk Factor in 2025.")).toEqual(["removed"]);
    expect(hedgesIn("No matching risk factor found in the earlier filing for the AI item.")).toEqual(["unmatchedNew"]);
    expect(hedgesIn("Passages whose wording was not found in the newer filing include ...")).toEqual(["passageRemoved"]);
  });

  it("lists each group once, in a fixed order", () => {
    const text = "No matching risk factor found in the earlier filing. It no longer appears as a separate risk factor. Again, it no longer appears as a separate risk factor.";
    expect(hedgesIn(text)).toEqual(["removed", "unmatchedNew"]);
  });

  it("is not set off by the added-passage wording, the short labels on their own, or plain prose", () => {
    expect(hedgesIn("passages whose wording was not found in the older filing")).toEqual([]);
    expect(hedgesIn("Not matched. Reworded. The company removed a risk factor.")).toEqual([]);
    expect(hedgesIn("A risk factor was not verified as no longer appearing.")).toEqual([]);
    expect(hedgesIn("")).toEqual([]);
  });

  it("counts the wordings the answer prompt prescribes", () => {
    expect(hedgesIn("At least 21 risk factors no longer appear as separate risk factors (parts of their content may be covered inside other risk factors), of which 8 are listed:")).toEqual(["removed"]);
    expect(hedgesIn("No matching risk factor was found in the earlier filing (it is new, or a restructured older risk factor) [x].")).toEqual(["unmatchedNew"]);
    expect(hedgesIn('Its "wording was not found" in the newer filing, and a differently worded version of the same statement may exist.')).toEqual([]); // quoted label only
    expect(hedgesIn("Its exact wording was not found in the newer filing (10-K filed 2026-02-25) [x].")).toEqual(["passageRemoved"]);
  });

  // Every sentence of the saved benchmark answers (src/semigraph/artifacts/examples.json) that holds one of the wordings, with
  // whether it makes the claim. "[id]" stands for a citation.
  const SAVED: readonly (readonly [string, string, readonly HedgeGroup[]])[] = [
    ["R1", "Separately, some sentences from the earlier filing's version of this risk factor, such as the description of the Notified Advanced Computing (NAC) notification process, had their wording not found in the newer filing [id], and a differently worded version of the same statement may exist.", ["passageRemoved"]],
    ["T1", "- At least 1 risk factor no longer appears as a separate risk factor, meaning the text check found no matching text in the newer filing (parts of its content may be covered inside other risk factors):", ["removed"]],
    ["T1", "- The text check found no risk factor that no longer appears as a separate risk factor for this pair.", []],
    ["T1", "So, across the two most recent comparisons, one risk factor from the fiscal year ended January 28, 2024 filing was found to no longer appear as a separate risk factor in the fiscal year ended January 26, 2025 filing, while no such finding exists between the fiscal year ended January 26, 2025 filing and the latest 10-K for the fiscal year ended January 25, 2026 [id].", ["removed"]],
    ["T5", "The text check found no risk factor that no longer appears as a separate risk factor between Meta's 10-K filed 2025-01-30 (covering the fiscal year ended December 31, 2024) and its 10-K filed 2026-01-29 (covering the fiscal year ended December 31, 2025) [id] [id].", []],
    ["T5", "No matching risk factor was found in the earlier filing for any items in this comparison; none were listed under that heading in this dataset.", []],
    ["T7", "Comparing Nvidia's 10-K filed 2024-02-21 (covering the fiscal year ended January 28, 2024) with the 10-K filed 2025-02-26 (covering the fiscal year ended January 26, 2025), the text check found at least 1 risk factor that no longer appears as a separate risk factor, meaning parts of its content may be covered inside other risk factors [id].", ["removed"]],
    ["T7", 'Separately, no risk factor was found under "No matching risk factor found in the earlier filing," so this comparison does not identify any risk factor as newly added between these two filings.', []],
    ["T10", "Separately, the text check found no risk factor that no longer appears as a separate risk factor between these two filings.", []],
    ["T11", "- Separately, the text check found one sentence within this risk factor whose wording from the earlier filing was not found in the newer filing: a sentence about estimated fiscal 2025 capital expenditures as a percentage of revenue [id].", ["passageRemoved"]],
    ["T11", "- The text check's \"No longer appears as a separate risk factor\" list for this filing pair shows none found, so no risk factor from the earlier filing was identified as no longer appearing separately.", []],
    ["T12", 'This risk factor is not listed under "No longer appears as a separate risk factor" for the fiscal year ended January 28, 2024 to fiscal year ended January 26, 2025 comparison; the only item under that heading is a different risk factor concerning indebtedness [id].', []],
    ["T13", "The specific sentence stating that the Notified Advanced Computing (NAC) process has not resulted in approvals for exports of products to customers in China was part of the earlier filing (10-K filed 2025-02-26) and its exact wording was not found in the newer filing (10-K filed 2026-02-25), meaning a differently worded version of the same statement may exist [id].", ["passageRemoved"]],
    ["T14", 'The specific sentence stating that Nvidia "transitioned some operations, including certain testing, validation, and supply and distribution operations out of China and Hong Kong" following the 2022 export controls has its wording not found in the newer filing (the 10-K filed 2026-02-25, accession 0001045810-26-000021) [id].', ["passageRemoved"]],
    ["T15", "The specific UVEU sentence's wording was not found in the fiscal year ended January 25, 2026 10-K, though a differently worded version of the same statement may exist [id].", ["passageRemoved"]],
    ["T15", "This sentence appears among a set of 8 listed passages (of 25 total) from surviving risk factors in the fiscal year ended January 26, 2025 10-K whose wording was not found in the newer fiscal year ended January 25, 2026 10-K [id].", ["passageRemoved"]],
  ];

  it.each(SAVED.map(([id, sentence, groups], i) => [i, id, sentence, groups] as const))("saved answer sentence %i (%s) makes the claims it says", (_i, _id, sentence, groups) => {
    expect(hedgesIn(sentence)).toEqual(groups);
  });

  it("gives no note to an answer that only says none was found (T5, T10) and only the removal note to T7", () => {
    const t5 = `${SAVED[4][1]} ${SAVED[5][1]}`;
    expect(hedgesIn(t5)).toEqual([]);
    expect(hedgesIn(`Intro. ${SAVED[8][1]}`)).toEqual([]);
    expect(hedgesIn(`${SAVED[6][1]} ${SAVED[7][1]}`)).toEqual(["removed"]);
  });

  it("runs in time linear in the length of a long answer", () => {
    const text = "word ".repeat(40_000) + "no longer appears as a separate risk factor.";
    const started = performance.now();
    expect(hedgesIn(text)).toEqual(["removed"]);
    expect(performance.now() - started).toBeLessThan(500);
  });
});
