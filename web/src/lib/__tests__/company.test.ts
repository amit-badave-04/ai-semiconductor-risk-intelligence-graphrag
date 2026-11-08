import { afterEach, describe, expect, it, vi } from "vitest";
import { dossierUrl, fetchDossier, isFilerTicker, parseDossier } from "../company";
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

  it("reports a 200 that is not a JSON object as an error, never as data", async () => {
    stubFetch(new Response("<html>not json</html>", { status: 200, statusText: "OK" }));
    const result = await fetchDossier("NVDA");
    expect(result.kind).toBe("error");
    stubFetch(json([1, 2, 3]));
    expect((await fetchDossier("NVDA")).kind).toBe("error");
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

  it("builds a rule's citation id from a bare Federal Register number too", () => {
    const dossier = parseDossier({
      company: { ticker: "NVDA", name: "Nvidia", cik: 1 },
      rules: [{ target: "A rule", rule_id: "2026-01234" }],
    });
    expect(dossier?.rules[0]?.cite).toBe("fr:2026-01234");
  });
});
