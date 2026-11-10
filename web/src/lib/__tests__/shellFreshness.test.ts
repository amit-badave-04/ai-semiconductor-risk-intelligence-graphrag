import { describe, expect, it } from "vitest";
import { shellFreshness } from "../shellFreshness";

// docs/specs/m5b-redesign.md RD3: the header's freshness line, a link to the Freshness page, reads "Data as of <date>, <n> newer
// filings being added", or the unavailable and failed forms in plain words; one date style ("Sep 24, 2026"), in UTC. Before
// /api/freshness answers there is no text (the height is reserved by the caller).

const OK = { status: "ok", checked_at: "2026-11-10T03:00:00Z", snapshot_as_of: "2026-09-24", pending_count: 9 };

describe("shellFreshness", () => {
  it("names the data date and the newer filings being added", () => {
    expect(shellFreshness(OK, false)).toBe("Data as of Sep 24, 2026, 9 newer filings being added");
    expect(shellFreshness({ ...OK, pending_count: 1 }, false)).toBe("Data as of Sep 24, 2026, 1 newer filing being added");
  });

  it("names only the data date when nothing newer is waiting or the count is missing", () => {
    expect(shellFreshness({ ...OK, pending_count: 0 }, false)).toBe("Data as of Sep 24, 2026");
    expect(shellFreshness({ ...OK, pending_count: null }, false)).toBe("Data as of Sep 24, 2026");
  });

  it("says in plain words when the check for newer filings failed, keeping the data date it knows", () => {
    expect(shellFreshness({ ...OK, status: "error" }, false)).toBe("Data as of Sep 24, 2026; the check for newer filings failed");
    expect(shellFreshness({ ...OK, status: "stale", snapshot_as_of: null }, false)).toBe("The check for newer filings failed");
  });

  it("says the data date is unknown when the check ran but recorded no date", () => {
    expect(shellFreshness({ ...OK, snapshot_as_of: "" }, false)).toBe("Data date unknown, 9 newer filings being added");
    expect(shellFreshness({ ...OK, snapshot_as_of: "not a date", pending_count: 0 }, false)).toBe("Data date unknown");
  });

  it("says the status is unavailable when the request failed or no check has run", () => {
    expect(shellFreshness(null, true)).toBe("Data status unavailable");
    expect(shellFreshness({ status: "ok", checked_at: null }, false)).toBe("Data status unavailable");
  });

  it("has no text before the first answer", () => {
    expect(shellFreshness(null, false)).toBe("");
  });

  it("never shows a negative or fractional count", () => {
    expect(shellFreshness({ ...OK, pending_count: -2 }, false)).toBe("Data as of Sep 24, 2026");
    expect(shellFreshness({ ...OK, pending_count: 2.5 }, false)).toBe("Data as of Sep 24, 2026");
  });
});
