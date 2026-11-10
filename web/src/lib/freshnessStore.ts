// R26: GET /api/freshness for the shell header, as the legacy header reads it (index.html:639), once per page load. The Freshness
// page asks through `loadFreshness` too, so a deep link to it joins the header's request instead of making a second one. A
// failed request is worded by the header's lib/shellFreshness.ts (docs/specs/m5b-redesign.md RD3: "Data status unavailable").
import { useSyncExternalStore } from "react";
import { type Freshness, type FreshnessResult, fetchFreshness } from "@/pages/Freshness.lib";

/** The freshness payload with the instant it arrived: a line worded "N min ago" uses the clock of its own data. `failed` is a
 *  request that came back as an error. */
export interface FreshnessClock {
  freshness: Freshness | null;
  failed: boolean;
  at: number;
}

const EMPTY: FreshnessClock = { freshness: null, failed: false, at: 0 };
let snapshot: FreshnessClock = EMPTY;
interface Shared {
  request: Promise<FreshnessResult>;
  controller: AbortController;
  /** Consumers that still want the answer; a consumer that gave no signal never lets go. */
  holders: number;
}
let inflight: Shared | null = null;
const listeners = new Set<() => void>();

const MSG_ABORTED = "request cancelled";

/** Load /api/freshness, joining a request already in flight. Never rejects. With a `signal`, aborting it lets go of the request,
 *  and the request itself is aborted once no consumer still wants it (the header never lets go). */
export function loadFreshness(signal?: AbortSignal): Promise<FreshnessResult> {
  if (inflight === null) {
    const controller = new AbortController();
    const shared: Shared = {
      controller,
      holders: 0,
      request: fetchFreshness(controller.signal)
        .catch((): FreshnessResult => ({ kind: "error", detail: MSG_ABORTED })) // only an abort rejects
        .finally(() => {
          if (inflight === shared) inflight = null;
        }),
    };
    inflight = shared;
  }
  const shared = inflight;
  shared.holders += 1;
  signal?.addEventListener("abort", () => {
    shared.holders -= 1;
    if (shared.holders > 0) return;
    if (inflight === shared) inflight = null;
    shared.controller.abort();
  }, { once: true });
  return shared.request;
}

/** The shell's one load per page load: publishes what arrives to the header. */
export function loadShellFreshness(): void {
  void loadFreshness().then((result) => {
    snapshot = result.kind === "ok" ? { freshness: result.freshness, failed: false, at: Date.now() } : { freshness: null, failed: true, at: Date.now() };
    for (const listener of Array.from(listeners)) listener();
  });
}

/** Tests only. */
export function resetFreshnessStore(): void {
  inflight = null;
  snapshot = EMPTY;
  for (const listener of Array.from(listeners)) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

/** The header's freshness and the instant it arrived, re-rendering when they change. */
export function useShellFreshness(): FreshnessClock {
  return useSyncExternalStore(subscribe, () => snapshot, () => EMPTY);
}
