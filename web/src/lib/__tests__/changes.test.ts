import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchChanges, parseChangeReport } from "../changes";

// R28, S2, S4, S11 (docs/specs/m5b-frontend.md): the what-changed report as data. Ported from tests/ui/workspace.test.mjs 30 to 36
// (the rendering half is pages/__tests__/workspaceChanges.test.tsx). The report is server data about an UPLOADED document, so it
// is untrusted: only the fields the view shows are kept, a chip id must match the generated grammar, nothing is ever a URL.

const WS = "0123456789abcdef0123456789abcdef";
const TOKEN = "tok-abc";
const SESSION = { id: WS, token: TOKEN, expires_at: null };
const DOC_CHUNK = "doc:0123456789ab:v2:0003";

function json(body: unknown, status = 200, statusText = ""): Response {
  return new Response(JSON.stringify(body), { status, statusText, headers: { "content-type": "application/json" } });
}

function stubFetch(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => handler(String(input), init));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("parseChangeReport", () => {
  it("shows a headline alone when the unit has no surviving passage (30)", () => {
    const report = parseChangeReport({ items_compared: true, unchanged_count: 1, changed: [{ headline: "Market Outlook" }] });
    expect(report).toMatchObject({ kind: "compared", changed: [{ label: "Market Outlook", passages: [] }] });
  });

  it("keeps each passage's quote and its doc: citation id (31)", () => {
    const report = parseChangeReport({
      items_compared: true,
      unchanged_count: 0,
      changed: [{ headline: "Market Outlook", passages: [{ quote: "shrink next year", chunk_id: DOC_CHUNK }] }],
    });
    expect(report).toMatchObject({ changed: [{ passages: [{ quote: "shrink next year", chunkId: DOC_CHUNK }] }] });
  });

  it("keeps an untrusted headline and quote as plain strings, for a text node (32)", () => {
    const report = parseChangeReport({
      items_compared: true,
      added: [{ headline: "<b>eek</b>", passages: [{ quote: "<script>bad</script>", chunk_id: DOC_CHUNK }] }],
    });
    expect(report).toMatchObject({ added: [{ label: "<b>eek</b>", passages: [{ quote: "<script>bad</script>" }] }] });
  });

  it("lists minor_rewordings apart from changed and never folds them into the unchanged count (33)", () => {
    const report = parseChangeReport({
      items_compared: true,
      unchanged_count: 2,
      changed: [{ headline: "Legal Proceedings", passages: [] }],
      minor_rewordings: [{ headline: "Company History" }],
    });
    expect(report).toMatchObject({ unchanged: 2, changed: [{ label: "Legal Proceedings" }], minor: ["Company History"] });
  });

  it("has no minor rewordings when the key is absent (34)", () => {
    const report = parseChangeReport({ items_compared: true, unchanged_count: 3, added: [], removed: [], changed: [] });
    expect(report).toMatchObject({ kind: "compared", unchanged: 3, minor: [], skipped: 0 });
  });

  it("counts the sections the negation check skipped (35)", () => {
    const base = { items_compared: true, unchanged_count: 1 };
    expect(parseChangeReport({ ...base, negation_check_skipped: 2 })).toMatchObject({ skipped: 2 });
    expect(parseChangeReport({ ...base, negation_check_skipped: 0 })).toMatchObject({ skipped: 0 });
    expect(parseChangeReport(base)).toMatchObject({ skipped: 0 });
    expect(parseChangeReport({ ...base, negation_check_skipped: "9" })).toMatchObject({ skipped: 9 });
    expect(parseChangeReport({ ...base, negation_check_skipped: "many" })).toMatchObject({ skipped: 0 });
  });

  it("reports the not-compared reason when items_compared is false (36)", () => {
    expect(parseChangeReport({ items_compared: false, not_compared_reason: "identical_content" })).toEqual({
      kind: "unavailable",
      reason: "identical_content",
    });
    expect(parseChangeReport({ items_compared: false })).toEqual({ kind: "unavailable", reason: "unknown reason" });
    expect(parseChangeReport(null)).toEqual({ kind: "unavailable", reason: "unknown reason" });
    expect(parseChangeReport("x")).toEqual({ kind: "unavailable", reason: "unknown reason" });
  });

  it("labels a unit by headline, then older id, then newer id, then '(untitled)'", () => {
    const report = parseChangeReport({
      items_compared: true,
      changed: [{ headline: "H", older_unit_id: "o" }, { older_unit_id: "o1" }, { newer_unit_id: "n1" }, {}, null, 5],
    });
    expect(report.kind === "compared" && report.changed.map((i) => i.label)).toEqual(["H", "o1", "n1", "(untitled)", "(untitled)", "(untitled)"]);
  });

  it("drops a chip id that does not match the generated grammar (S4), an empty or non-string quote, and a passage left with neither", () => {
    const report = parseChangeReport({
      items_compared: true,
      changed: [
        {
          headline: "x",
          passages: [
            { quote: "q1", chunk_id: "../../api/admin" },
            { quote: "", chunk_id: `${DOC_CHUNK}\n` },
            { quote: 5, chunk_id: DOC_CHUNK.toUpperCase() },
            { quote: "q4", chunk_id: 7 },
            { quote: "q5", chunk_id: DOC_CHUNK },
            "not a passage",
            { quote: null, chunk_id: null },
          ],
        },
      ],
    });
    expect(report.kind === "compared" && report.changed[0].passages).toEqual([
      { quote: "q1", chunkId: null },
      { quote: "q4", chunkId: null },
      { quote: "q5", chunkId: DOC_CHUNK },
    ]);
  });

  it("keeps only the fields the view shows and takes a non-number unchanged count as 0", () => {
    const report = parseChangeReport({
      items_compared: true,
      unchanged_count: "lots",
      added: [{ headline: "a", url: "https://evil.example", html: "<img onerror=x>" }],
      extra: "x",
    });
    expect(report).toEqual({ kind: "compared", unchanged: 0, added: [{ label: "a", passages: [] }], removed: [], changed: [], minor: [], skipped: 0 });
  });

  it("takes a list that is not a list as empty", () => {
    expect(parseChangeReport({ items_compared: true, added: "x", removed: 5, changed: { a: 1 }, minor_rewordings: "y" })).toMatchObject({
      added: [],
      removed: [],
      changed: [],
      minor: [],
    });
  });
});

describe("fetchChanges (GET /api/workspace/{id}/changes)", () => {
  const REPORT = { items_compared: true, unchanged_count: 1, added: [], removed: [], changed: [] };

  it("builds the URL from encoded parts and sends the token in the header only", async () => {
    const fn = stubFetch(() => json(REPORT));
    const result = await fetchChanges(SESSION, "abc123456789", 1, 2);
    expect(result.kind).toBe("ok");
    const [url, init] = fn.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/workspace/${WS}/changes?document_id=abc123456789&from=1&to=2`);
    expect(url).not.toContain(TOKEN);
    expect(init.headers).toEqual({ "X-Workspace-Token": TOKEN });
  });

  it("encodes a document id that is not URL-safe", async () => {
    const fn = stubFetch(() => json(REPORT));
    await fetchChanges(SESSION, "a&b=c/../d", 1, 2);
    expect((fn.mock.calls[0] as [string])[0]).toContain("document_id=a%26b%3Dc%2F..%2Fd&from=1&to=2");
  });

  it("makes no request for an id, a token, a document or a version that is malformed", async () => {
    const fn = stubFetch(() => json(REPORT));
    for (const [session, doc, a, b] of [
      [{ ...SESSION, id: "../x" }, "d", 1, 2],
      [{ ...SESSION, token: "a\nb" }, "d", 1, 2],
      [SESSION, "", 1, 2],
      [SESSION, "d", 0, 2],
      [SESSION, "d", 1.5, 2],
      [SESSION, "d", 1, Number.NaN],
    ] as const) {
      expect((await fetchChanges(session, doc, a, b)).kind).toBe("error");
    }
    expect(fn).not.toHaveBeenCalled();
  });

  it("shows the server's detail for a refusal, and the status text when it has none", async () => {
    stubFetch(() => json({ detail: "no change report for that version pair" }, 404, "Not Found"));
    expect(await fetchChanges(SESSION, "d", 1, 2)).toEqual({ kind: "error", detail: "no change report for that version pair" });
    stubFetch(() => new Response("<html>", { status: 502, statusText: "Bad Gateway" }));
    expect(await fetchChanges(SESSION, "d", 1, 2)).toEqual({ kind: "error", detail: "Bad Gateway" });
  });

  it("reports a network failure and a body that is not JSON in the legacy words", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    expect(await fetchChanges(SESSION, "d", 1, 2)).toEqual({ kind: "error", detail: "network error loading the change report" });
    stubFetch(() => new Response("<html>", { status: 200 }));
    expect(await fetchChanges(SESSION, "d", 1, 2)).toEqual({ kind: "error", detail: "could not load the change report" });
  });

  it("rethrows an abort so a cancelled request never becomes a result", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("aborted", "AbortError")));
    await expect(fetchChanges(SESSION, "d", 1, 2, new AbortController().signal)).rejects.toMatchObject({ name: "AbortError" });
  });
});
