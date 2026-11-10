import { afterEach, describe, expect, it, vi } from "vitest";
import { dossierUrl, fetchDossier, fetchRiskChanges, isFilerTicker, parseDossier, parseRiskChanges, riskChangesUrl } from "../company";
import { FILERS } from "../pages.generated";

// built from pieces so no script URL literal sits in the source
const SCRIPT_URL = ["java", "script:alert(1)"].join("");

// R25 (docs/specs/m5b-frontend.md): the company dossier client. The router-decoded :ticker is untrusted: it is fetched only
// when it is one of the generated filers, and always through encodeURIComponent.

const CHUNK = "0001045810-26-000021:I.1:0320";
const XBRL_VALUE = 1000;

function json(body: unknown, status = 200, statusText = ""): Response {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "content-type": "application/json" } });
}

function stubFetch(response: Response | (() => Response)) {
  const fn = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    typeof response === "function" ? response() : response,
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("isFilerTicker", () => {
  it.each(FILERS.map((f) => f.ticker))("accepts the generated filer %s", (ticker) => {
    expect(isFilerTicker(ticker)).toBe(true);
  });

  it.each(["nvda", "XXXX", "", "NVDA ", "..%2F..%2Fapi%2Fadmin%2Fstate", "../x", "constructor", "__proto__"])(
    "rejects %j",
    (value) => {
      expect(isFilerTicker(value)).toBe(false);
    },
  );

  it("rejects a value that is not a string", () => {
    expect(isFilerTicker(undefined)).toBe(false);
    expect(isFilerTicker(7)).toBe(false);
  });
});

describe("dossierUrl", () => {
  it("builds an absolute /api path for a ticker", () => {
    expect(dossierUrl("NVDA")).toBe("/api/company/NVDA/dossier");
  });

  it("encodes every path-significant character of its argument", () => {
    expect(dossierUrl("../x")).toBe("/api/company/..%2Fx/dossier");
    expect(dossierUrl("a?b#c")).toBe("/api/company/a%3Fb%23c/dossier");
  });
});

describe("fetchDossier", () => {
  it.each(["..%2F..%2Fapi%2Fadmin%2Fstate", "../../api/admin/state", "nvda", "XXXX", ""])(
    "makes no request at all for %j and reports not found",
    async (ticker) => {
      const fn = stubFetch(json({}));
      expect(await fetchDossier(ticker)).toEqual({ kind: "notFound" });
      expect(fn).not.toHaveBeenCalled();
    },
  );

  it("GETs the encoded dossier of a filer, passing the abort signal", async () => {
    const fn = stubFetch(json({ company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 } }));
    const controller = new AbortController();
    const result = await fetchDossier("NVDA", controller.signal);
    expect(result.kind).toBe("ok");
    expect(fn).toHaveBeenCalledTimes(1);
    const [url, init] = fn.mock.calls[0] ?? [];
    expect(url).toBe("/api/company/NVDA/dossier");
    expect(init?.signal).toBe(controller.signal);
    expect(init?.method ?? "GET").toBe("GET");
  });

  it("maps a 404 to not found", async () => {
    stubFetch(json({ detail: "unknown ticker" }, 404, "Not Found"));
    expect(await fetchDossier("AMD")).toEqual({ kind: "notFound" });
  });

  it("shows the server's detail for a 429, unchanged", async () => {
    stubFetch(json({ detail: "Too many requests from your address \u2014 please slow down." }, 429, "Too Many Requests"));
    expect(await fetchDossier("NVDA")).toEqual({
      kind: "error",
      detail: "Too many requests from your address \u2014 please slow down.",
    });
  });

  it("falls back to the status text when an error body is not JSON", async () => {
    stubFetch(new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" }));
    expect(await fetchDossier("NVDA")).toEqual({ kind: "error", detail: "Bad Gateway" });
  });

  it("reports a 200 that is not a JSON object as unreadable, never as data", async () => {
    stubFetch(new Response("<html>not json</html>", { status: 200, statusText: "OK" }));
    expect(await fetchDossier("NVDA")).toEqual({ kind: "unreadable" });
    stubFetch(json([1, 2, 3]));
    expect(await fetchDossier("NVDA")).toEqual({ kind: "unreadable" });
  });

  // Owner 2026-11-08 (A2): a 200 whose body parses but is no dossier (no company) is "unreadable" (the page words it); the
  // status text is no longer shown for it, whatever the status text is.
  it("reports a 200 that holds no company as unreadable, with or without a status text", async () => {
    stubFetch(new Response(JSON.stringify({ filings: [] }), { status: 200, statusText: "OK" }));
    expect(await fetchDossier("NVDA")).toEqual({ kind: "unreadable" });
    stubFetch(new Response(JSON.stringify({ company: { ticker: "NVDA" } }), { status: 200, statusText: "" }));
    expect(await fetchDossier("NVDA")).toEqual({ kind: "unreadable" });
  });

  it("reports a network failure with the legacy page's words", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    expect(await fetchDossier("NVDA")).toEqual({
      kind: "error",
      detail: "network error \u2014 is the service online?",
    });
  });

  it("rethrows an abort, so a cancelled request never becomes a result", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("aborted", "AbortError")));
    await expect(fetchDossier("NVDA")).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("parseDossier (a type guard that ignores unknown fields)", () => {
  it("keeps the known fields and drops the unknown ones", () => {
    const dossier = parseDossier({
      company: { ticker: "NVDA", name: "Nvidia", cik: 1045810, extra: "x" },
      data_as_of: "2026-09-30",
      surprise: { a: 1 },
      filings: [
        { accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25", is_current: true, status: "current", superseded_by: null, extra: 1 },
      ],
      metrics: [],
      active_risks: [{ risk_id: "r1", summary: "S", category: "supply", last_evidenced_at: "2026-02-25", extra: 1 }],
      edges: [],
      rules: [],
    });
    expect(dossier).not.toBeNull();
    expect(dossier?.company).toEqual({ ticker: "NVDA", name: "Nvidia", cik: 1045810 });
    expect(dossier?.dataAsOf).toBe("2026-09-30");
    expect(dossier?.filings).toEqual([
      { accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25", is_current: true, status: "current", superseded_by: null },
    ]);
    expect(dossier?.activeRisks).toEqual([{ risk_id: "r1", summary: "S", category: "supply", last_evidenced_at: "2026-02-25" }]);
    expect(JSON.stringify(dossier)).not.toContain("surprise");
    expect(JSON.stringify(dossier)).not.toContain("extra");
  });

  it("treats a missing or mistyped block as empty rather than throwing", () => {
    const dossier = parseDossier({ company: { ticker: "NVDA", name: "Nvidia", cik: 1 }, filings: "nope", metrics: null, edges: 3 });
    expect(dossier?.filings).toEqual([]);
    expect(dossier?.metrics).toEqual([]);
    expect(dossier?.edges).toEqual([]);
    expect(dossier?.rules).toEqual([]);
    expect(dossier?.dataAsOf).toBeNull();
  });

  it("drops rows that are not objects and fields of the wrong type", () => {
    const dossier = parseDossier({
      company: { ticker: "NVDA", name: "Nvidia", cik: 1 },
      active_risks: ["x", null, { risk_id: 5, summary: { a: 1 }, category: "supply", last_evidenced_at: 7 }],
    });
    expect(dossier?.activeRisks).toEqual([{ risk_id: null, summary: null, category: "supply", last_evidenced_at: null }]);
  });

  it("returns null for a body without a company", () => {
    expect(parseDossier({})).toBeNull();
    expect(parseDossier(null)).toBeNull();
    expect(parseDossier({ company: "NVDA" })).toBeNull();
  });

  it("builds a metric's citation id only when it has the xbrl grammar", () => {
    const dossier = parseDossier({
      company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 },
      metrics: [
        { cik: 1045810, metric: "revenue", value: XBRL_VALUE, unit: "USD", period_start: "2025-02-01", period_end: "2026-01-25" },
        { cik: 1045810, metric: "Bad Metric!", value: 1, unit: "USD", period_start: null, period_end: "2026-01-25" },
      ],
    });
    expect(dossier?.metrics[0]?.cite).toBe("xbrl:1045810:revenue:2026-01-25");
    expect(dossier?.metrics[1]?.cite).toBeNull();
  });

  it("keeps only chunk ids that match the citation grammar and never keeps a rule url", () => {
    const dossier = parseDossier({
      company: { ticker: "NVDA", name: "Nvidia", cik: 1 },
      edges: [{ source: "Nvidia", relation: "SUPPLIES_TO", target: "TSMC", status: "Active", quote: "q", chunk_ids: [CHUNK, SCRIPT_URL, 4] }],
      rules: [
        { source: "Nvidia", relation: "AFFECTED_BY", target: "A rule", status: "Active", quote: null, chunk_ids: null, rule_id: "fr:2026-01234", date: "2026-03-01", url: SCRIPT_URL, kind: "Rule", link_source: "federal_register", link_method: "keyword", external: true },
        { source: "Nvidia", relation: "AFFECTED_BY", target: "Another", rule_id: "<img src=x>" },
      ],
    });
    expect(dossier?.edges[0]?.chunk_ids).toEqual([CHUNK]);
    expect(dossier?.rules[0]?.cite).toBe("fr:2026-01234");
    expect(dossier?.rules[1]?.cite).toBeNull();
    expect(JSON.stringify(dossier)).not.toContain(SCRIPT_URL.slice(0, 11));
  });

  it("keeps the chunk a relation's quote came from: its first raw id, only when that id is valid (RD24)", () => {
    // the server sets the quote when the edge is created and appends chunk ids later, so the quote belongs to chunk_ids[0]
    const dossier = parseDossier({
      company: { ticker: "NVDA", name: "Nvidia", cik: 1 },
      edges: [
        { source: "Nvidia", relation: "DEPENDS_ON", target: "TSMC", quote: "q", chunk_ids: [CHUNK, "0001045810-26-000021:I.1A:0002"] },
        { source: "Nvidia", relation: "DEPENDS_ON", target: "SK Hynix", quote: "q", chunk_ids: ["not-a-chunk-id", CHUNK] },
        { source: "Nvidia", relation: "DEPENDS_ON", target: "X", quote: "q", chunk_ids: null },
      ],
    });
    expect(dossier?.edges.map((e) => e.quoteChunk)).toEqual([CHUNK, null, null]);
    expect(dossier?.edges[1]?.chunk_ids).toEqual([CHUNK]);
  });

  it("builds a rule's citation id from a bare Federal Register number too", () => {
    const dossier = parseDossier({
      company: { ticker: "NVDA", name: "Nvidia", cik: 1 },
      rules: [{ target: "A rule", rule_id: "2026-01234" }],
    });
    expect(dossier?.rules[0]?.cite).toBe("fr:2026-01234");
  });
});

// R25 (owner 2026-11-08: yes): the recent risk changes of GET /api/company/{ticker}/risk-changes, through the same ticker gate,
// the same encoding, the same refusal words and the same status-text fallback as the dossier.
const OLD_FILING = { accession_no: "0001045810-25-000023", form: "10-K", filing_date: "2025-02-26" };
const NEW_FILING = { accession_no: "0001045810-26-000021", form: "10-K", filing_date: "2026-02-25" };
const CHANGES = {
  company: { ticker: "NVDA", name: "Nvidia", cik: 1045810 },
  pairs: [
    {
      older: OLD_FILING,
      newer: NEW_FILING,
      compared: true,
      not_compared_reason: null,
      items: [
        { kind: "new", headline: "A new risk", older_item_id: null, newer_item_id: "n1", passages: [] },
        { kind: "dropped", headline: "A gone risk", older_item_id: "o1", newer_item_id: null, passages: [] },
        { kind: "unsettled", headline: "An unsettled risk", older_item_id: null, newer_item_id: null, passages: [] },
        {
          kind: "changed",
          headline: "A reworded risk",
          older_item_id: null,
          newer_item_id: "n2",
          passages: [
            { quote: "old wording", chunk_id: CHUNK, side: "older" },
            { quote: "new wording", chunk_id: "not a chunk id", side: "newer" },
          ],
        },
      ],
    },
    { older: OLD_FILING, newer: NEW_FILING, compared: false, not_compared_reason: "low_text_yield", items: [] },
  ],
};

describe("riskChangesUrl and fetchRiskChanges", () => {
  it("builds an absolute, encoded /api path", () => {
    expect(riskChangesUrl("NVDA")).toBe("/api/company/NVDA/risk-changes");
    expect(riskChangesUrl("a/b?c")).toBe("/api/company/a%2Fb%3Fc/risk-changes");
  });

  it("makes no request for a ticker that is not a generated filer", async () => {
    const fn = stubFetch(json(CHANGES));
    for (const t of ["XXXX", "nvda", "..%2F..%2Fapi%2Fadmin%2Fstate", "constructor"]) {
      expect(await fetchRiskChanges(t)).toEqual({ kind: "notFound" });
    }
    expect(fn).not.toHaveBeenCalled();
  });

  it("GETs the filer's risk changes, passing the abort signal, and parses them", async () => {
    const fn = stubFetch(json(CHANGES));
    const controller = new AbortController();
    const result = await fetchRiskChanges("NVDA", controller.signal);
    expect(fn.mock.calls[0]?.[0]).toBe("/api/company/NVDA/risk-changes");
    expect(fn.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
    expect(result.kind).toBe("ok");
  });

  it("maps a 404 to not found, a refusal to the server's detail, and a network failure to the legacy words", async () => {
    stubFetch(json({ detail: "unknown ticker" }, 404));
    expect(await fetchRiskChanges("NVDA")).toEqual({ kind: "notFound" });
    stubFetch(json({ detail: "Too many requests." }, 429));
    expect(await fetchRiskChanges("NVDA")).toEqual({ kind: "error", detail: "Too many requests." });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    expect(await fetchRiskChanges("NVDA")).toEqual({ kind: "error", detail: "network error \u2014 is the service online?" });
  });

  it("keeps the status-text fallback for a 200 that is no report (no sentence of its own)", async () => {
    stubFetch(new Response("[]", { status: 200, statusText: "OK" }));
    expect(await fetchRiskChanges("NVDA")).toEqual({ kind: "error", detail: "OK" });
    stubFetch(new Response("not json", { status: 200, statusText: "" }));
    expect(await fetchRiskChanges("NVDA")).toEqual({ kind: "error", detail: "HTTP 200" });
  });

  it("rethrows an abort", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("aborted", "AbortError")));
    await expect(fetchRiskChanges("NVDA")).rejects.toThrow();
  });
});

describe("parseRiskChanges", () => {
  it("keeps the shown fields, the four kinds, and a citation id only when it is a chunk id", () => {
    const parsed = parseRiskChanges({ ...CHANGES, extra: "dropped" });
    expect(parsed?.pairs).toHaveLength(2);
    const first = parsed?.pairs[0];
    expect(first?.older).toEqual(OLD_FILING);
    expect(first?.compared).toBe(true);
    expect(first?.items.map((i) => i.kind)).toEqual(["new", "dropped", "unsettled", "changed"]);
    expect(first?.items[3]?.passages).toEqual([
      { quote: "old wording", chunkId: CHUNK, side: "older" },
      { quote: "new wording", chunkId: null, side: "newer" },
    ]);
    expect(parsed?.pairs[1]).toMatchObject({ compared: false, reason: "low_text_yield", items: [] });
  });

  it("treats a pair without 'compared: false' as compared, and drops items of an unknown kind or that are not objects", () => {
    const parsed = parseRiskChanges({
      pairs: [{ older: OLD_FILING, newer: NEW_FILING, items: [{ kind: "weird", headline: "x" }, "no", { kind: "new", headline: "ok" }] }],
    });
    expect(parsed?.pairs[0]?.compared).toBe(true);
    expect(parsed?.pairs[0]?.items).toEqual([{ kind: "new", headline: "ok", passages: [] }]);
  });

  it("survives mistyped fields and returns null for a body without a pairs list", () => {
    const parsed = parseRiskChanges({ pairs: [{ older: 3, newer: null, compared: "yes", not_compared_reason: 7, items: "x" }, 5] });
    expect(parsed?.pairs).toHaveLength(1);
    expect(parsed?.pairs[0]?.older).toEqual({ accession_no: null, form: null, filing_date: null });
    expect(parseRiskChanges({})).toBeNull();
    expect(parseRiskChanges(null)).toBeNull();
    expect(parseRiskChanges({ pairs: "x" })).toBeNull();
  });
});
