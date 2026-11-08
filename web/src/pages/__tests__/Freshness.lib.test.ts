import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchFreshness, freshnessLine, parseFreshness, relativeTime } from "../Freshness.lib";

// R26 (docs/specs/m5b-frontend.md): the port of index.html's relativeTime/freshnessLine (index.html:489-518) with the cases
// of tests/ui/workspace.test.mjs:96-170, plus the payload parser and client of the page.

const NOW = Date.parse("2026-09-29T12:00:00Z");

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("relativeTime of a time still ahead (next_check_at)", () => {
  it("counts forward instead of saying just now", () => {
    expect(relativeTime("2026-09-29T12:20:00Z", NOW)).toBe("in 20 min");
    expect(relativeTime("2026-09-29T15:00:00Z", NOW)).toBe("in 3 h");
    expect(relativeTime("2026-10-01T12:00:00Z", NOW)).toBe("in 2 d");
  });
  it("keeps just now for the last few seconds on either side", () => {
    expect(relativeTime("2026-09-29T12:00:05Z", NOW)).toBe("just now");
  });
});

describe("relativeTime (tests/ui/workspace.test.mjs:98-110)", () => {
  it("reports just now, minutes, hours and days ago", () => {
    expect(relativeTime("2026-09-29T11:59:55Z", NOW)).toBe("just now");
    expect(relativeTime("2026-09-29T11:45:00Z", NOW)).toBe("15 min ago");
    expect(relativeTime("2026-09-29T09:00:00Z", NOW)).toBe("3 h ago");
    expect(relativeTime("2026-09-27T12:00:00Z", NOW)).toBe("2 d ago");
  });

  it("tolerates a missing or malformed timestamp", () => {
    expect(relativeTime(null, Date.now())).toBe("unknown");
    expect(relativeTime("not-a-date", Date.now())).toBe("unknown");
  });
});

describe("freshnessLine (tests/ui/workspace.test.mjs:111-170)", () => {
  it("reports data-as-of, checked-when and pending count", () => {
    const f = { snapshot_as_of: "2026-09-24", checked_at: "2026-09-29T11:45:00Z", pending_count: 3 };
    expect(freshnessLine(f, NOW)).toBe("Data as of 2026-09-24 \u00b7 checked 15 min ago \u00b7 3 filings pending");
  });

  it("uses the singular for exactly one pending filing", () => {
    const f = { snapshot_as_of: "2026-09-24", checked_at: "2026-09-29T11:45:00Z", pending_count: 1 };
    expect(freshnessLine(f, NOW)).toMatch(/1 filing pending$/);
  });

  it("says the data freshness is unknown when the snapshot date is missing", () => {
    const f = { checked_at: "2026-09-29T11:45:00Z", pending_count: 0 };
    expect(freshnessLine(f, NOW)).toBe("Data freshness unknown \u00b7 checked 15 min ago \u00b7 0 filings pending");
  });

  it("leaves the pending count out when it is not a number", () => {
    const f = { snapshot_as_of: "2026-09-24", checked_at: "2026-09-29T11:45:00Z" };
    expect(freshnessLine(f, NOW)).toBe("Data as of 2026-09-24 \u00b7 checked 15 min ago");
  });

  it("says the check is unavailable when nothing has ever run", () => {
    expect(freshnessLine({}, Date.now())).toBe("freshness check unavailable");
    expect(freshnessLine(null, Date.now())).toBe("freshness check unavailable");
    expect(freshnessLine("text", Date.now())).toBe("freshness check unavailable");
    expect(freshnessLine({ status: "unconfigured" }, Date.now())).toBe("freshness check unavailable");
  });

  it("says unavailable for disabled and never, the same as unconfigured", () => {
    expect(freshnessLine({ status: "disabled", checked_at: null, pending_count: 0 }, Date.now())).toBe("freshness check unavailable");
    expect(freshnessLine({ status: "never", checked_at: null, pending_count: 0 }, Date.now())).toBe("freshness check unavailable");
  });

  it("says the check failed and omits the pending count when the status is error", () => {
    const f = { status: "error", checked_at: "2026-09-29T11:45:00Z", pending_count: 3, snapshot_as_of: "2026-09-24" };
    expect(freshnessLine(f, NOW)).toBe("freshness check failed 15 min ago");
  });

  it("says the check failed for a stale status too", () => {
    const f = { status: "stale", checked_at: "2026-09-27T12:00:00Z", pending_count: 0 };
    expect(freshnessLine(f, NOW)).toBe("freshness check failed 2 d ago");
  });

  it("uses last_error_at when no good check has ever landed (C3 item 6)", () => {
    const f = { status: "error", checked_at: null, last_error_at: "2026-09-29T11:45:00Z", pending_count: 0 };
    expect(freshnessLine(f, NOW)).toBe("freshness check failed 15 min ago");
  });

  it("prefers last_error_at over an older checked_at when the status is error", () => {
    const f = { status: "error", checked_at: "2026-09-20T00:00:00Z", last_error_at: "2026-09-29T11:45:00Z" };
    expect(freshnessLine(f, NOW)).toBe("freshness check failed 15 min ago");
  });

  it("falls back to a bare failed message when neither timestamp is known", () => {
    expect(freshnessLine({ status: "error", checked_at: null, last_error_at: null }, Date.now())).toBe("freshness check failed");
  });

  it("ignores last_error_at for a stale status (only an aging good check)", () => {
    const f = { status: "stale", checked_at: "2026-09-27T12:00:00Z", last_error_at: "2026-09-01T00:00:00Z" };
    expect(freshnessLine(f, NOW)).toBe("freshness check failed 2 d ago");
  });
});

describe("parseFreshness", () => {
  const DISABLED = {
    checked_at: null, snapshot_as_of: null, last_error_at: null, next_check_at: null, pending_count: 0, pending_filings: [],
    federal_register: null, unresolved: [], duration_s: null, configured: false, enabled: false, status: "disabled",
  };

  it("reads the disabled payload", () => {
    const f = parseFreshness(DISABLED);
    expect(f).toMatchObject({ status: "disabled", checked_at: null, pending_count: 0, pending_filings: [], federal_register: null, unresolved: [] });
  });

  it("keeps the known fields of a full payload and drops unknown ones", () => {
    const f = parseFreshness({
      ...DISABLED,
      status: "ok",
      checked_at: "2026-09-29T11:45:00Z",
      snapshot_as_of: "2026-09-24",
      next_check_at: "2026-09-29T17:45:00Z",
      pending_count: 1,
      pending_filings: [{ ticker: "NVDA", cik: 1, form: "10-Q", filing_date: "2026-09-20", accession_no: "0001-26-1", period_of_report: "2026-07-27", extra: 1 }, "x"],
      federal_register: { graph_count: 10, live_count: 12, new_since: 2, extra: 1 },
      unresolved: ["TSM", 5],
      surprise: true,
    });
    expect(f?.pending_filings).toEqual([{ ticker: "NVDA", form: "10-Q", filing_date: "2026-09-20", accession_no: "0001-26-1", period_of_report: "2026-07-27" }]);
    expect(f?.federal_register).toEqual({ graph_count: 10, live_count: 12, new_since: 2 });
    expect(f?.unresolved).toEqual(["TSM"]);
    expect(JSON.stringify(f)).not.toContain("surprise");
  });

  it("returns null for anything that is not an object", () => {
    expect(parseFreshness(null)).toBeNull();
    expect(parseFreshness([])).toBeNull();
    expect(parseFreshness("x")).toBeNull();
  });

  it("types a wrong-typed field as absent", () => {
    const f = parseFreshness({ status: 3, checked_at: 5, pending_count: "2", federal_register: { graph_count: "x" } });
    expect(f?.status).toBeNull();
    expect(f?.checked_at).toBeNull();
    expect(f?.pending_count).toBeNull();
    expect(f?.federal_register).toBeNull();
  });
});

describe("fetchFreshness", () => {
  it("GETs /api/freshness and parses it", async () => {
    const fn = vi.fn(async (_u: RequestInfo | URL, _i?: RequestInit) => new Response(JSON.stringify({ status: "disabled" }), { status: 200 }));
    vi.stubGlobal("fetch", fn);
    const controller = new AbortController();
    const result = await fetchFreshness(controller.signal);
    expect(result).toMatchObject({ kind: "ok", freshness: { status: "disabled" } });
    expect(fn.mock.calls[0]?.[0]).toBe("/api/freshness");
    expect(fn.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
  });

  it("shows the server's detail for a refusal and the legacy words for a network failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Too many requests" }), { status: 429, statusText: "x" })));
    expect(await fetchFreshness()).toEqual({ kind: "error", detail: "Too many requests" });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("down")));
    expect(await fetchFreshness()).toEqual({ kind: "error", detail: "network error \u2014 is the service online?" });
  });

  it("is an error when a 200 is not a JSON object, and rethrows an abort", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>", { status: 200, statusText: "OK" })));
    expect((await fetchFreshness()).kind).toBe("error");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("a", "AbortError")));
    await expect(fetchFreshness()).rejects.toMatchObject({ name: "AbortError" });
  });
});
