import { describe, expect, it } from "vitest";
import { watchJob } from "../jobStream";

// R19, R34 (docs/specs/m5b-frontend.md): a retry opens its request only AFTER the previous response was released, so a job never has
// two client watchers (the server allows three per job and answers a fourth with 429). jobStream.test.ts counts open responses
// with real streams, whose cancel() settles at once, so it cannot tell `await release(reader)` from `release(reader)`; here the
// reader's cancel() is slow on purpose and the second fetch must wait for it. There are two release sites: after a response that
// ended early and after a non-OK response.

const WS = "c".repeat(32);
const JOB = "00112233445566ff";
const TOKEN = "secret-workspace-token";

interface Slow {
  response: Response;
  cancelled: () => boolean;
  finishCancel: () => void;
}

/** A response whose reader's cancel() is a promise the test settles by hand. `ok` false is a 503. */
function slowResponse(ok: boolean): Slow {
  let finish: () => void = () => {};
  let cancelled = false;
  const reader = {
    read: () => Promise.resolve({ done: true, value: undefined }),
    cancel: () => {
      cancelled = true;
      return new Promise<void>((resolve) => {
        finish = resolve;
      });
    },
  };
  const response = { ok, status: ok ? 200 : 503, body: { getReader: () => reader } } as unknown as Response;
  return { response, cancelled: () => cancelled, finishCancel: () => finish() };
}

const flush = async () => {
  for (let i = 0; i < 200; i += 1) await Promise.resolve();
};

async function secondFetchWaitsForRelease(ok: boolean): Promise<{ before: number; after: number; result: string }> {
  const slow = slowResponse(ok);
  let fetches = 0;
  const fetchImpl = (async () => {
    fetches += 1;
    return fetches === 1 ? slow.response : ({ ok: false, status: 404, body: null } as unknown as Response);
  }) as typeof fetch;
  const pending = watchJob(WS, JOB, TOKEN, { onJob: () => {}, fetchImpl, sleep: () => Promise.resolve(), maxAttempts: 2 });
  await flush();
  expect(slow.cancelled()).toBe(true);
  const before = fetches; // the first response is still being released
  slow.finishCancel();
  const result = await pending;
  return { before, after: fetches, result: result.kind };
}

describe("watchJob releases a response before it retries", () => {
  it("waits for the reader of a stream that ended early to be released before the next request", async () => {
    const seen = await secondFetchWaitsForRelease(true);
    expect(seen.before).toBe(1);
    expect(seen.after).toBe(2);
    expect(seen.result).toBe("gone");
  });

  it("waits for the body of a non-OK response to be released before the next request", async () => {
    const seen = await secondFetchWaitsForRelease(false);
    expect(seen.before).toBe(1);
    expect(seen.after).toBe(2);
    expect(seen.result).toBe("gone");
  });
});
