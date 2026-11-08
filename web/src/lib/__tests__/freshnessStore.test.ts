import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadFreshness, loadShellFreshness, resetFreshnessStore } from "../freshnessStore";

// R26: one /api/freshness request is shared by the header and the Freshness page; it stops only when nobody wants it.

const BODY = { status: "ok", checked_at: "2026-11-08T10:00:00Z", snapshot_as_of: "2026-09-30", pending_count: 0 };

function pendingFetch() {
  const signals: AbortSignal[] = [];
  const fn = vi.fn((_u: RequestInfo | URL, init?: RequestInit) => {
    const signal = init?.signal as AbortSignal;
    signals.push(signal);
    return new Promise<Response>((resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("a", "AbortError")));
      (fn as unknown as { release: () => void }).release = () => resolve(new Response(JSON.stringify(BODY), { status: 200 }));
    });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, signals, release: () => (fn as unknown as { release: () => void }).release() };
}

beforeEach(() => resetFreshnessStore());
afterEach(() => vi.unstubAllGlobals());

describe("freshnessStore", () => {
  it("joins a request in flight: two loads, one GET of /api/freshness", async () => {
    const { fn, release } = pendingFetch();
    const first = loadFreshness();
    const second = loadFreshness();
    release();
    expect((await first).kind).toBe("ok");
    expect(await second).toEqual(await first);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn.mock.calls[0]?.[0]).toBe("/api/freshness");
  });

  it("a page that lets go does not abort the request the header still wants", async () => {
    const { signals, release } = pendingFetch();
    loadShellFreshness();
    const page = new AbortController();
    const pageResult = loadFreshness(page.signal);
    page.abort();
    expect(signals[0]?.aborted).toBe(false);
    release();
    expect((await pageResult).kind).toBe("ok");
  });

  it("aborts the request when the only consumer lets go, and the next load starts a new one", async () => {
    const { fn, signals } = pendingFetch();
    const page = new AbortController();
    const result = loadFreshness(page.signal);
    page.abort();
    expect(signals[0]?.aborted).toBe(true);
    expect((await result).kind).toBe("error");
    void loadFreshness();
    expect(fn).toHaveBeenCalledTimes(2);
  });
});
