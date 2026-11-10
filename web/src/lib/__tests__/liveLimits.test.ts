import { describe, expect, it } from "vitest";
import { deck } from "@/copy/deck";
import { DEFAULT_LIMITS, liveLimits } from "../askStatus";

// docs/specs/m5b-redesign.md RD4a: the cost line reads its numbers from /api/stats.limits (per_ip "5 per 10 min" and
// per_ip_per_day 20); until stats load, or when they are missing or malformed, the approved defaults (decision D-R5).

describe("liveLimits", () => {
  it("reads the per-address window and the daily cap from the stats", () => {
    expect(liveLimits({ limits: { per_ip: "3 per 15 min", per_ip_per_day: 12 } })).toEqual({ perWindow: 3, windowMinutes: 15, perDay: 12 });
  });

  it("uses the approved defaults until stats load", () => {
    expect(liveLimits(null)).toEqual(DEFAULT_LIMITS);
    expect(DEFAULT_LIMITS).toEqual({ perWindow: 5, windowMinutes: 10, perDay: 20 });
  });

  it("keeps each default whose field is missing or unreadable, never a zero or NaN", () => {
    expect(liveLimits({ limits: { per_ip: "five per ten", per_ip_per_day: "20" } })).toEqual(DEFAULT_LIMITS);
    expect(liveLimits({ limits: { per_ip: "0 per 10 min", per_ip_per_day: -1 } })).toEqual(DEFAULT_LIMITS);
    expect(liveLimits({ limits: { per_ip_per_day: 2.5 } })).toEqual(DEFAULT_LIMITS);
    expect(liveLimits({ limits: { per_ip_per_day: 30 } })).toEqual({ ...DEFAULT_LIMITS, perDay: 30 });
    expect(liveLimits({})).toEqual(DEFAULT_LIMITS);
  });

  it("reads a daily cap of 0 as no daily cap, and the cost line then names none", () => {
    const limits = liveLimits({ limits: { per_ip: "5 per 10 min", per_ip_per_day: 0 } });
    expect(limits).toEqual({ perWindow: 5, windowMinutes: 10, perDay: null });
    expect(deck.ask.costLine(limits)).toBe("Free to use, up to 5 live questions every 10 minutes.");
  });

  it("gives the approved sentence with the defaults", () => {
    expect(deck.ask.costLine(liveLimits(null))).toBe("Free to use, up to 5 live questions every 10 minutes (20 a day).");
  });
});
