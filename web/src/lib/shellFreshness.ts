// docs/specs/m5b-redesign.md RD3: the header's freshness line, worded from GET /api/freshness (monitor.py status_payload) in
// plain words, with one date style in UTC (lib/format.ts). A pure function over the untrusted payload; the result is plain text.
import { deck } from "@/copy/deck";
import { formatDate } from "./format";

const words = deck.freshness;

/** The line, or "" before /api/freshness has answered (`failed` is true when the request failed or answered an error). */
export function shellFreshness(freshness: unknown, failed: boolean): string {
  if (failed) return words.unavailable;
  if (typeof freshness !== "object" || freshness === null) return "";
  const f = freshness as Record<string, unknown>;
  const date = typeof f.snapshot_as_of === "string" ? formatDate(f.snapshot_as_of) : "";
  if (f.status === "error" || f.status === "stale") {
    return date === "" ? words.checkFailed : `${words.dataAsOf(date)}; ${words.checkFailedAfterDate}`;
  }
  if (typeof f.checked_at !== "string" || f.checked_at === "") return words.unavailable;
  const head = date === "" ? words.dateUnknown : words.dataAsOf(date);
  const pending = f.pending_count;
  return typeof pending === "number" && Number.isInteger(pending) && pending > 0 ? `${head}, ${words.newer(pending)}` : head;
}
