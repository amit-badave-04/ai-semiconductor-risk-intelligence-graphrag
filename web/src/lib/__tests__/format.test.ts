import { describe, expect, it } from "vitest";
import { compactMoney, exactMoney, fiscalYearLabel, formatDate, paragraphNumber } from "../format";

// docs/specs/m5b-redesign.md RD3 (one date style, in UTC), RD11 (paragraph numbers), RD22 (figures in the reported currency,
// compact with the exact value kept, fiscal-year labels). Pure functions; the server's values are untrusted strings and numbers.

describe("formatDate: one date style, in UTC", () => {
  it("formats an ISO day as 'Mon D, YYYY' whatever the machine's time zone", () => {
    expect(formatDate("2024-02-21")).toBe("Feb 21, 2024");
    expect(formatDate("2026-09-24")).toBe("Sep 24, 2026");
  });

  it("formats a timestamp by its UTC day, so a late-evening US time does not move the day", () => {
    expect(formatDate("2026-11-10T03:49:59.945788+00:00")).toBe("Nov 10, 2026");
    expect(formatDate("2026-01-25T23:30:00Z")).toBe("Jan 25, 2026");
  });

  it("reads a timestamp with no offset as UTC, so a browser west of UTC does not move the day or the fiscal year", () => {
    const saved = process.env.TZ;
    process.env.TZ = "America/New_York";
    try {
      expect(formatDate("2026-02-03T23:30:00")).toBe("Feb 3, 2026");
      expect(fiscalYearLabel("2025-12-31T22:00:00")?.short).toBe("FY2025");
      expect(formatDate("2026-11-10T03:49:59+05:30")).toBe("Nov 9, 2026");
    } finally {
      process.env.TZ = saved;
    }
  });

  it("returns an empty string for null, empty or unreadable input, never 'Invalid Date'", () => {
    expect(formatDate(null)).toBe("");
    expect(formatDate("")).toBe("");
    expect(formatDate("not a date")).toBe("");
    expect(formatDate("2026-13-45")).toBe("");
  });
});

describe("compactMoney and exactMoney: the reported currency, never a default dollar", () => {
  it("compacts US dollars to one decimal", () => {
    expect(compactMoney(215938000000, "USD")).toBe("$215.9B");
    expect(compactMoney(18497000000, "USD")).toBe("$18.5B");
    expect(compactMoney(-267000000, "USD")).toBe("-$267M");
  });

  it("uses the currency the company reported", () => {
    expect(compactMoney(2894307000000, "TWD")).toBe("NT$2.9T");
    expect(compactMoney(28262900000, "EUR")).toBe("€28.3B");
  });

  it("keeps the exact value with grouping and no rounding", () => {
    expect(exactMoney(215938000000, "USD")).toBe("$215,938,000,000");
    expect(exactMoney(-267000000, "USD")).toBe("-$267,000,000");
  });

  it("shows a value whose unit is not a currency as a plain number with its unit", () => {
    expect(compactMoney(42000, "shares")).toBe("42K shares");
    expect(exactMoney(42000, "shares")).toBe("42,000 shares");
  });

  it("returns an empty string for a missing or non-finite value, and reads a numeric string", () => {
    expect(compactMoney(null, "USD")).toBe("");
    expect(compactMoney(Number.NaN, "USD")).toBe("");
    expect(compactMoney("130497000000", "USD")).toBe("$130.5B");
    expect(exactMoney("abc", "USD")).toBe("");
  });

  it("never treats an unknown currency code as dollars, and keeps the code it was given", () => {
    expect(compactMoney(1500000, "XYZ")).toBe("1.5M XYZ");
    expect(exactMoney(1500000, "XYZ")).toBe("1,500,000 XYZ");
    expect(compactMoney(1500000, null)).toBe("1.5M");
  });

  it("never shows a minus sign on a value that rounds to zero", () => {
    expect(compactMoney(-0, "USD")).toBe("$0");
    expect(compactMoney(-0.04, "USD")).toBe("$0");
    expect(exactMoney(-0, "USD")).toBe("$0");
  });
});

describe("paragraphNumber: no leading zeros", () => {
  it("drops leading zeros and keeps zero itself", () => {
    expect(paragraphNumber("0162")).toBe("162");
    expect(paragraphNumber("0007")).toBe("7");
    expect(paragraphNumber("0000")).toBe("0");
    expect(paragraphNumber("1234")).toBe("1234");
  });

  it("returns the input unchanged when it is not all digits", () => {
    expect(paragraphNumber("12a")).toBe("12a");
  });
});

describe("fiscalYearLabel: the fiscal year named by the year it ends, with the end date", () => {
  it("labels a period end", () => {
    expect(fiscalYearLabel("2026-01-25")).toEqual({ short: "FY2026", long: "FY2026 (ended Jan 25, 2026)" });
    expect(fiscalYearLabel("2025-06-30")).toEqual({ short: "FY2025", long: "FY2025 (ended Jun 30, 2025)" });
  });

  it("returns null for an unreadable period end", () => {
    expect(fiscalYearLabel(null)).toBeNull();
    expect(fiscalYearLabel("2026-02-30")).toBeNull();
  });
});
