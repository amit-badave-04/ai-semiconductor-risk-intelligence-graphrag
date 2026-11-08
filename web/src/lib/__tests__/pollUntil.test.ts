import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { pollUntil } from "../pollUntil";

// A small poll for a value that lives in a ref (the Turnstile gate's config state): no subscription exists to wait on.

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("pollUntil", () => {
  it("answers true at once when the condition already holds, without a timer", async () => {
    expect(await pollUntil(() => true, 1000)).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("answers true as soon as the condition turns true, within the budget", async () => {
    let ready = false;
    const result = pollUntil(() => ready, 1000, undefined, 100);
    await vi.advanceTimersByTimeAsync(250);
    ready = true;
    await vi.advanceTimersByTimeAsync(100);
    expect(await result).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("answers false when the budget runs out", async () => {
    const result = pollUntil(() => false, 1000, undefined, 100);
    await vi.advanceTimersByTimeAsync(1000);
    expect(await result).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("answers false on an abort, also one that came before the call", async () => {
    const controller = new AbortController();
    const result = pollUntil(() => false, 1000, controller.signal, 100);
    await vi.advanceTimersByTimeAsync(150);
    controller.abort();
    expect(await result).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
    expect(await pollUntil(() => false, 1000, controller.signal)).toBe(false);
  });
});
