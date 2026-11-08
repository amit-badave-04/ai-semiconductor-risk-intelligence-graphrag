// R26 (docs/specs/m5b-frontend.md): the port of the legacy page's freshness header line (index.html:489-518, tests in
// tests/ui/workspace.test.mjs:96-170) and the client and parser of GET /api/freshness. Worker C1, I2.
import { detailOf } from "@/lib/refusal";

const MS_PER_MINUTE = 60000;
const MINUTES_PER_HOUR = 60;
const HOURS_PER_DAY = 24;
// The legacy page's words for a failed request (index.html:680).
const MSG_NETWORK = "network error \u2014 is the service online?";

export function relativeTime(iso: string | null | undefined, now: number): string {
  const then = iso ? new Date(iso).getTime() : Number.NaN;
  if (!Number.isFinite(then)) return "unknown";
  const signed = Math.round((now - then) / MS_PER_MINUTE);
  const minutes = Math.abs(signed);
  if (minutes < 1) return "just now";
  const span = spanOf(minutes);
  return signed < 0 ? `in ${span}` : `${span} ago`; // a time still ahead (next_check_at) counts forward
}

function spanOf(minutes: number): string {
  if (minutes < MINUTES_PER_HOUR) return `${minutes} min`;
  const hours = Math.round(minutes / MINUTES_PER_HOUR);
  return hours < HOURS_PER_DAY ? `${hours} h` : `${Math.round(hours / HOURS_PER_DAY)} d`;
}

/** The page's single freshness line. `now` (ms) is a parameter so this stays pure. error/stale is decided FIRST, before the
 *  checked_at test (C3 item 6): the first check a monitor ever makes can fail before any good check has landed. */
export function freshnessLine(freshness: unknown, now: number): string {
  const f = (typeof freshness === "object" && freshness !== null ? freshness : {}) as Record<string, unknown>;
  const checkedAt = typeof f.checked_at === "string" ? f.checked_at : null;
  const lastErrorAt = typeof f.last_error_at === "string" ? f.last_error_at : null;
  if (f.status === "error" || f.status === "stale") {
    const at = f.status === "error" && lastErrorAt ? lastErrorAt : checkedAt;
    return at ? `freshness check failed ${relativeTime(at, now)}` : "freshness check failed";
  }
  if (!checkedAt) return "freshness check unavailable";
  const snapshot = typeof f.snapshot_as_of === "string" && f.snapshot_as_of !== "" ? f.snapshot_as_of : null;
  const parts = [snapshot ? `Data as of ${snapshot}` : "Data freshness unknown", `checked ${relativeTime(checkedAt, now)}`];
  if (typeof f.pending_count === "number") parts.push(`${f.pending_count} filing${f.pending_count === 1 ? "" : "s"} pending`);
  return parts.join(" \u00b7 ");
}

export interface PendingFiling {
  ticker: string | null;
  form: string | null;
  filing_date: string | null;
  accession_no: string | null;
  period_of_report: string | null;
}

export interface Freshness {
  status: string | null;
  checked_at: string | null;
  snapshot_as_of: string | null;
  last_error_at: string | null;
  next_check_at: string | null;
  pending_count: number | null;
  pending_filings: PendingFiling[];
  federal_register: { graph_count: number; live_count: number; new_since: number } | null;
  unresolved: string[];
}

export type FreshnessResult = { kind: "ok"; freshness: Freshness } | { kind: "error"; detail: string };

type Row = Record<string, unknown>;

function isRow(value: unknown): value is Row {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function federalRegister(value: unknown): Freshness["federal_register"] {
  if (!isRow(value)) return null;
  const { graph_count: graph, live_count: live, new_since: fresh } = value;
  if (typeof graph !== "number" || typeof live !== "number" || typeof fresh !== "number") return null;
  return { graph_count: graph, live_count: live, new_since: fresh };
}

/** Narrow an unknown body to the fields the page shows (configured, enabled and duration_s are not shown); null when it is
 *  not an object. */
export function parseFreshness(body: unknown): Freshness | null {
  if (!isRow(body)) return null;
  return {
    status: str(body.status),
    checked_at: str(body.checked_at),
    snapshot_as_of: str(body.snapshot_as_of),
    last_error_at: str(body.last_error_at),
    next_check_at: str(body.next_check_at),
    pending_count: typeof body.pending_count === "number" ? body.pending_count : null,
    pending_filings: (Array.isArray(body.pending_filings) ? body.pending_filings : []).filter(isRow).map((r) => ({
      ticker: str(r.ticker),
      form: str(r.form),
      filing_date: str(r.filing_date),
      accession_no: str(r.accession_no),
      period_of_report: str(r.period_of_report),
    })),
    federal_register: federalRegister(body.federal_register),
    unresolved: (Array.isArray(body.unresolved) ? body.unresolved : []).filter((v): v is string => typeof v === "string"),
  };
}

/** GET /api/freshness (an absolute path). An abort rejects; every other failure is a result. */
export async function fetchFreshness(signal?: AbortSignal): Promise<FreshnessResult> {
  let response: Response;
  try {
    response = await fetch("/api/freshness", { signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    return { kind: "error", detail: MSG_NETWORK };
  }
  if (!response.ok) return { kind: "error", detail: await detailOf(response) };
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // not JSON: reported below
  }
  const freshness = parseFreshness(body);
  if (freshness === null) return { kind: "error", detail: response.statusText || `HTTP ${response.status}` };
  return { kind: "ok", freshness };
}
