import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CONFIG_URL, loadConfig, parseConfig, resetConfigCache } from "../config";

// R8: GET /api/config once (plus one retry on a failure). A 200 with an empty key is a KNOWN "no key"; a failure is "unknown",
// and "unknown" is never treated as "no key" (a widget that never mounts must not look like a site with no bot check).

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function stubFetch(...answers: (Response | Error)[]) {
  const queue = [...answers];
  const fn = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
    const next = queue.shift() ?? new Error("no more answers");
    if (next instanceof Error) throw next;
    return next;
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

beforeEach(() => resetConfigCache());
afterEach(() => vi.unstubAllGlobals());

describe("parseConfig", () => {
  it("reads a key", () => {
    expect(parseConfig({ turnstile_site_key: "dummy-site-key" })).toEqual({ kind: "key", siteKey: "dummy-site-key" });
  });

  it("reads an empty key as a known none", () => {
    expect(parseConfig({ turnstile_site_key: "" })).toEqual({ kind: "none" });
  });

  it.each([null, undefined, 5, "x", [], {}, { turnstile_site_key: null }, { turnstile_site_key: 5 }, { turnstile_site_key: "a b" },
    { turnstile_site_key: "<script>" }, { turnstile_site_key: "x".repeat(200) }])("reads %j as malformed (null)", (body) => {
    expect(parseConfig(body)).toBeNull();
  });
});

describe("loadConfig", () => {
  it("requests /api/config once and answers key", async () => {
    const fetchMock = stubFetch(json({ turnstile_site_key: "dummy-site-key" }));
    expect(await loadConfig()).toEqual({ kind: "key", siteKey: "dummy-site-key" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(CONFIG_URL);
  });

  it("answers none for an empty key", async () => {
    stubFetch(json({ turnstile_site_key: "" }));
    expect(await loadConfig()).toEqual({ kind: "none" });
  });

  it("caches a settled answer: a second call (StrictMode, a remount) makes no request", async () => {
    const fetchMock = stubFetch(json({ turnstile_site_key: "" }));
    await Promise.all([loadConfig(), loadConfig()]);
    await loadConfig();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries a network error once and then succeeds", async () => {
    const fetchMock = stubFetch(new TypeError("network"), json({ turnstile_site_key: "dummy-site-key" }));
    expect(await loadConfig({ retryDelayMs: 0 })).toEqual({ kind: "key", siteKey: "dummy-site-key" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retries a non-200 once", async () => {
    const fetchMock = stubFetch(json({}, 503), json({ turnstile_site_key: "" }));
    expect(await loadConfig({ retryDelayMs: 0 })).toEqual({ kind: "none" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("after two failures is unknown and failed, never none", async () => {
    const fetchMock = stubFetch(new TypeError("network"), json({}, 500));
    const state = await loadConfig({ retryDelayMs: 0 });
    expect(state).toEqual({ kind: "unknown", failed: true });
    expect(state.kind).not.toBe("none");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("treats a malformed 200 as a failure (retried, then unknown)", async () => {
    const fetchMock = stubFetch(json({ turnstile_site_key: 5 }), new Response("not json", { status: 200 }));
    expect(await loadConfig({ retryDelayMs: 0 })).toEqual({ kind: "unknown", failed: true });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not cache a failure: a later call asks again", async () => {
    const fetchMock = stubFetch(new TypeError("a"), new TypeError("b"), json({ turnstile_site_key: "" }));
    expect((await loadConfig({ retryDelayMs: 0 })).kind).toBe("unknown");
    expect(await loadConfig({ retryDelayMs: 0 })).toEqual({ kind: "none" });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("sends no credentials and no body", async () => {
    const fetchMock = stubFetch(json({ turnstile_site_key: "" }));
    await loadConfig();
    const init = fetchMock.mock.calls[0][1];
    expect(init?.method ?? "GET").toBe("GET");
    expect(init?.body).toBeUndefined();
  });
});
