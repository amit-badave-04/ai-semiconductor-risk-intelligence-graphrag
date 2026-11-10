import { describe, expect, it } from "vitest";
import { hedgesIn } from "../hedge";

// docs/specs/m5b-redesign.md RD13: which pinned change-claim wordings an answer holds (the facts file's HEDGES, pinned against
// the server's own wording and the hand-check counts by tests/test_web_method_copy.py).

describe("hedgesIn", () => {
  it("finds each change-claim wording in any case and across extra whitespace", () => {
    expect(hedgesIn("Two risk factors **no longer appear as a separate risk factor**.")).toEqual([]);
    expect(hedgesIn("The cybersecurity item No Longer Appears As A Separate\nRisk Factor in 2025.")).toEqual(["removed"]);
    expect(hedgesIn("No matching risk factor found in the earlier filing for the AI item.")).toEqual(["unmatchedNew"]);
    expect(hedgesIn("Passages whose wording was not found in the newer filing include ...")).toEqual(["passageRemoved"]);
  });

  it("lists each group once, in a fixed order", () => {
    const text = "no matching risk factor found in the earlier filing; no longer appears as a separate risk factor; again no longer appears as a separate risk factor";
    expect(hedgesIn(text)).toEqual(["removed", "unmatchedNew"]);
  });

  it("is not set off by the added-passage wording, the short labels on their own, or plain prose", () => {
    expect(hedgesIn("passages whose wording was not found in the older filing")).toEqual([]);
    expect(hedgesIn("Not matched. Reworded. The company removed a risk factor.")).toEqual([]);
    expect(hedgesIn("")).toEqual([]);
  });
});
