// R22: GET /api/stats as the Ask page uses it: the strategy option (agent_enabled), the privacy line (tracing), the limits hint and
// the cost note (limits, models), the badge of a live answer (models.llm), and whether uploads are on (uploads_enabled). The legacy
// page reloaded it after every answer (index.html loadStats) so a paused service or a spent budget shows at once; the AskProvider
// does the same through `refreshStats`. A small external store because three places need it (the Ask page, the AskProvider and,
// for its own two reload points, the workspace provider) and none of them owns the others. One request in flight at a time;
// a failure leaves the last good value (or none): the page works without stats, with the agent option off.
import { useSyncExternalStore } from "react";

export const STATS_URL = "/api/stats";

export interface Stats {
  agent_enabled?: unknown;
  tracing?: unknown;
  uploads_enabled?: unknown;
  paused?: unknown;
  models?: { llm?: unknown; escalation?: unknown };
  limits?: { per_ip?: unknown; per_ip_per_day?: unknown; max_queries_per_day?: unknown };
}

let current: Stats | null = null;
let inflight: Promise<void> | null = null;

const listeners = new Set<() => void>();

const isDict = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);

function publish(next: Stats | null): void {
  current = next;
  for (const listener of Array.from(listeners)) listener();
}

/** The last stats that loaded, or null. */
export function getStats(): Stats | null {
  return current;
}

/** Load /api/stats. Never rejects; joins a request already in flight. */
export function refreshStats(fetchImpl: typeof fetch = fetch): Promise<void> {
  if (inflight !== null) return inflight;
  inflight = (async () => {
    try {
      const response = await fetchImpl(STATS_URL);
      if (!response.ok) return;
      const body: unknown = await response.json();
      if (isDict(body)) publish(body as Stats);
    } catch {
      // the page works without statistics: keep what there is
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

/** Tests only. */
export function resetStatsStore(): void {
  inflight = null;
  publish(null);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => void listeners.delete(listener);
}

/** The stats, re-rendering when they change. */
export function useStats(): Stats | null {
  return useSyncExternalStore(subscribe, getStats, () => null);
}
