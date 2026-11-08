// A bounded poll for a condition kept in a ref (the Turnstile gate's /api/config state), which has no subscription to wait on.
const DEFAULT_INTERVAL_MS = 100;

/** Resolve true as soon as `done()` holds; false after `ms` milliseconds, or at once on an abort (also one that came earlier). */
export function pollUntil(done: () => boolean, ms: number, signal?: AbortSignal, intervalMs: number = DEFAULT_INTERVAL_MS): Promise<boolean> {
  if (done()) return Promise.resolve(true);
  if (signal?.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    const started = Date.now();
    const finish = (value: boolean) => {
      clearInterval(timer);
      signal?.removeEventListener("abort", onAbort);
      resolve(value);
    };
    const onAbort = () => finish(false);
    const timer = setInterval(() => {
      if (done()) finish(true);
      else if (Date.now() - started >= ms) finish(false);
    }, intervalMs);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}
