import { readFileSync } from "node:fs";
import path from "node:path";
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

// docs/specs/m2a-answer-integrity.md P4 and S7: since_data is server data about other sites, so each row is validated here.
describe("parseFreshness since_data", () => {
  const sample = JSON.parse(
    readFileSync(path.resolve(import.meta.dirname, "../../../../tests/data/freshness/since_data.sample.json"), "utf-8"),
  ) as Record<string, unknown>;
  const base = { status: "ok", checked_at: "2026-11-21T00:00:00Z" };
  const withSince = (since: unknown) => parseFreshness({ ...base, since_data: since })?.since_data;
  const nvda = (over: Record<string, unknown>) => ({ ...(sample.filings as Record<string, unknown>[])[0], ...over });
  const withFilings = (...rows: unknown[]) => withSince({ ...sample, filings: rows });
  const withRules = (...rows: unknown[]) => withSince({ ...sample, rules: rows });
  const rule = (over: Record<string, unknown>) => ({ ...(sample.rules as Record<string, unknown>[])[0], ...over });
  const filingsOf = (v: ReturnType<typeof withSince>) => (typeof v === "object" && v !== null ? v.filings : []);
  const rulesOf = (v: ReturnType<typeof withSince>) => (typeof v === "object" && v !== null ? v.rules : []);

  it("reads the sample the server's own keys are pinned to", () => {
    const since = withSince(sample);
    expect(since).toMatchObject({ since: "2026-09-24", rules_since: "2026-09-24", rules_more: false, filing_totals: { NVDA: 7, TSM: 1 }, dropped: { filings: 0, rules: 0 } });
    expect(filingsOf(since)).toHaveLength(6);
    expect(filingsOf(since)[0]).toEqual({
      ticker: "NVDA", form: "8-K", filing_date: "2026-11-19", accession_no: "0001045810-26-000020", items: ["2.02", "9.01"],
      href: "https://www.sec.gov/Archives/edgar/data/1045810/000104581026000020/nvda-8k-results.htm",
    });
    expect(rulesOf(since)[0]).toMatchObject({ publication_date: "2026-11-18", title: "Export Administration Regulations: Advanced Computing Items" });
  });

  it("is null for null or missing, and invalid for a body that is not an object, a bad date or a non-array list", () => {
    expect(withSince(null)).toBeNull();
    expect(parseFreshness(base)?.since_data).toBeNull();
    expect(withSince("x")).toBe("invalid");
    expect(withSince([])).toBe("invalid");
    expect(withSince({ ...sample, since: "yesterday" })).toBe("invalid");
    expect(withSince({ ...sample, rules_since: "2026-13-1" })).toBe("invalid");
    expect(withSince({ ...sample, filings: {} })).toBe("invalid");
    expect(withSince({ ...sample, rules: "x" })).toBe("invalid");
  });

  it.each([
    ["a ticker we do not cover", { ticker: "XYZ" }],
    ["a form that is not an event form", { form: "10-K" }],
    ["a bad accession", { accession_no: "0001045810-26-00002" }],
    ["an accession with a non-ASCII digit", { accession_no: "000104581\u0660-26-000020" }],
    ["a bad date", { filing_date: "Nov 19" }],
    ["an item that is not a code", { items: ["2.02", "<b>"] }],
    ["items that are not a list", { items: "2.02" }],
    ["a date that is not a day (Feb 31)", { filing_date: "2026-02-31" }],
    ["a date that is not a day (Apr 31)", { filing_date: "2026-04-31" }],
    ["a date that is not a day (Feb 30)", { filing_date: "2026-02-30" }],
    ["a date that is not a day (Feb 29 of a common year)", { filing_date: "2026-02-29" }],
    ["a date with a trailing newline", { filing_date: "2026-11-19\n" }],
    ["a date with a leading space", { filing_date: " 2026-11-19" }],
    ["a date with a trailing space", { filing_date: "2026-11-19 " }],
    ["a date with an extra character", { filing_date: "2026-11-19x" }],
    ["a date with full-width digits", { filing_date: "\uff12\uff10\uff12\uff16-11-19" }],
    ["a date with Arabic-Indic digits", { filing_date: "\u0662\u0660\u0662\u0666-11-19" }],
    ["an accession with a trailing newline", { accession_no: "0001045810-26-000020\n" }],
    ["an accession with a leading space", { accession_no: " 0001045810-26-000020" }],
    ["an accession with a trailing space", { accession_no: "0001045810-26-000020 " }],
    ["an accession with an extra digit", { accession_no: "0001045810-26-0000200" }],
    ["an item code with a trailing newline", { items: ["2.02\n"] }],
    ["an item code with a leading space", { items: [" 2.02"] }],
    ["an item code with a trailing space", { items: ["2.02 "] }],
    ["an item code with an extra digit", { items: ["2.021"] }],
    ["an item code with a non-ASCII digit", { items: ["2.0\u0662"] }],
  ])("drops a filing row with %s", (_name, over) => {
    expect(filingsOf(withFilings(nvda(over)))).toEqual([]);
  });

  it("keeps a filing dated on a real leap day", () => {
    expect(filingsOf(withFilings(nvda({ filing_date: "2028-02-29" })))[0]).toMatchObject({ filing_date: "2028-02-29" });
  });

  it.each(["2026-02-31", "2026-04-31", "2026-02-30", "2026-11-19\n", "2026-11-19 "])("is invalid for a since or rules_since of %j", (day) => {
    expect(withSince({ ...sample, since: day })).toBe("invalid");
    expect(withSince({ ...sample, rules_since: day })).toBe("invalid");
  });

  it("keeps a 6-K with no items and a row without a primary document", () => {
    const tsm = (sample.filings as Record<string, unknown>[]).find((f) => f.form === "6-K");
    expect(filingsOf(withFilings(tsm))[0]).toMatchObject({ ticker: "TSM", form: "6-K", items: [] });
    expect(filingsOf(withFilings(nvda({ primary_document: null })))).toHaveLength(1);
  });

  it("drops a rule with an empty title, a 301-character title or a bad date, and keeps 300", () => {
    expect(rulesOf(withRules(rule({ title: "" })))).toEqual([]);
    expect(rulesOf(withRules(rule({ title: "x".repeat(301) })))).toEqual([]);
    expect(rulesOf(withRules(rule({ title: 5 })))).toEqual([]);
    expect(rulesOf(withRules(rule({ publication_date: "soon" })))).toEqual([]);
    for (const day of ["2026-02-31", "2026-04-31", "2026-11-18\n", " 2026-11-18", "2026-11-18 ", "2026-11-18x", "\u0662\u0660\u0662\u0666-11-18"]) {
      expect(rulesOf(withRules(rule({ publication_date: day }))), day).toEqual([]);
    }
    expect(rulesOf(withRules(rule({ title: "x".repeat(300) })))).toHaveLength(1);
    // The server caps at 300 characters (code points, federal_register.clean_title): 300 emoji count 300, not 600.
    expect(rulesOf(withRules(rule({ title: "\u{1F600}".repeat(300) })))).toHaveLength(1);
    expect(rulesOf(withRules(rule({ title: "\u{1F600}".repeat(301) })))).toEqual([]);
  });

  it("keeps the document number, and drops a rule whose document number is not the Federal Register's form", () => {
    expect(rulesOf(withRules(rule({})))[0]).toMatchObject({ document_number: "2026-23001" });
    expect(rulesOf(withRules(rule({ document_number: "C1-2026-23001" })))[0]).toMatchObject({ document_number: "C1-2026-23001" });
    for (const bad of [undefined, 5, "", "2026-23001\n", " 2026-23001", "2026-23001 ", "2026-230010x", "2026-123", "\u0662\u0660\u0662\u0666-23001"]) {
      expect(rulesOf(withRules(rule({ document_number: bad }))), String(bad)).toEqual([]);
    }
  });

  it("caps 5 filings per company and 20 rules, and says there are more rules when it trims", () => {
    const many = Array.from({ length: 8 }, (_, i) => nvda({ accession_no: `0001045810-26-0000${String(10 + i)}` }));
    expect(filingsOf(withFilings(...many))).toHaveLength(5);
    const rules = Array.from({ length: 25 }, (_, i) => rule({ title: `Rule ${i}` }));
    const trimmed = withRules(...rules.map((r, i) => ({ ...r, document_number: `2026-${String(10000 + i)}` })));
    expect(rulesOf(trimmed)).toHaveLength(20);
    expect(trimmed).toMatchObject({ rules_more: true });
    expect(withRules(rule({}))).toMatchObject({ rules_more: false });
  });

  it("keeps one row per accession number (the first), counting each extra as dropped, across companies too", () => {
    const tsm = (sample.filings as Record<string, unknown>[]).find((f) => f.ticker === "TSM");
    const since = withFilings(nvda({}), nvda({ filing_date: "2026-11-01" }), { ...tsm, accession_no: "0001045810-26-000020" }, nvda({ accession_no: "0001045810-26-000021" }));
    expect(filingsOf(since).map((f) => [f.ticker, f.accession_no, f.filing_date])).toEqual([
      ["NVDA", "0001045810-26-000020", "2026-11-19"],
      ["NVDA", "0001045810-26-000021", "2026-11-19"],
    ]);
    expect(since).toMatchObject({ dropped: { filings: 2, rules: 0 } });
  });

  it("keeps one rule per document number, counting the repeat as dropped", () => {
    const since = withRules(rule({}), rule({ title: "Again" }));
    expect(rulesOf(since)).toHaveLength(1);
    expect(since).toMatchObject({ dropped: { filings: 0, rules: 1 } });
  });

  it.each([
    ["a script scheme", ["java", "script:alert(1)"].join("")],
    ["http:", "http://www.sec.gov/Archives/edgar/data/1/2/x.htm"],
    ["another host", "https://evil.example/Archives/edgar/data/1/2/x.htm"],
    ["a look-alike host", "https://www.sec.gov.evil.example/x"],
    ["credentials", "https://user:pw@www.sec.gov/Archives/x.htm"],
    ["a port", "https://www.sec.gov:8443/Archives/x.htm"],
    ["a non-string", 5],
  ])("keeps the filing as text with no link for %s", (_name, url) => {
    const row = filingsOf(withFilings(nvda({ url })))[0];
    expect(row.href).toBeNull();
    expect(row.accession_no).toBe("0001045810-26-000020");
  });

  it("refuses a link with a port, and keeps one that names the default port", () => {
    expect(filingsOf(withFilings(nvda({ url: "https://www.sec.gov:8443/x" })))[0].href).toBeNull();
    expect(rulesOf(withRules(rule({ html_url: "https://www.federalregister.gov:4443/x" })))[0].href).toBeNull();
    expect(filingsOf(withFilings(nvda({ url: "https://www.sec.gov:443/x" })))[0].href).toBe("https://www.sec.gov/x");
  });

  it("links a rule only on www.federalregister.gov", () => {
    expect(rulesOf(withRules(rule({ html_url: "https://www.sec.gov/x" })))[0].href).toBeNull();
    expect(rulesOf(withRules(rule({ html_url: "http://www.federalregister.gov/x" })))[0].href).toBeNull();
    expect(rulesOf(withRules(rule({})))[0].href).toMatch(/^https:\/\/www\.federalregister\.gov\//);
  });

  it("reads rules_more only when it is true, dropped counts as numbers, and totals only for our companies", () => {
    const since = withSince({ ...sample, rules_more: "yes", dropped: { filings: "2", rules: 3 }, filing_totals: { NVDA: 9, XYZ: 4, TSM: -1 } });
    expect(since).toMatchObject({ rules_more: false, dropped: { filings: 0, rules: 3 }, filing_totals: { NVDA: 9 } });
  });
});

describe("parseFreshness since_data rows the page drops itself", () => {
  it("counts them as dropped, so an emptied list is never reported as nothing new", () => {
    const sample = JSON.parse(
      readFileSync(path.resolve(import.meta.dirname, "../../../../tests/data/freshness/since_data.sample.json"), "utf-8"),
    ) as { filings: Record<string, unknown>[]; rules: Record<string, unknown>[] };
    const body = { since_data: { ...sample, filings: [{ ...sample.filings[0], ticker: "XYZ" }], rules: [{ ...sample.rules[0], title: "" }] } };
    const since = parseFreshness(body)?.since_data;
    expect(since).toMatchObject({ filings: [], rules: [], dropped: { filings: 1, rules: 1 } });
  });
});
