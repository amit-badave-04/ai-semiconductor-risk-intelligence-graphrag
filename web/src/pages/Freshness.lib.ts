// R26 (docs/specs/m5b-frontend.md): the port of the legacy page's freshness header line (index.html:489-518, tests in
// tests/ui/workspace.test.mjs:96-170) and the client and parser of GET /api/freshness. Worker C1, I2. Its words are in
// copy/deck.ts freshness (docs/specs/m5b-redesign.md R4); the Freshness page itself shows RD3's line.
import { deck, STATUS_NETWORK } from "@/copy/deck";
import { FILERS } from "@/lib/pages.generated";
import { detailOf } from "@/lib/refusal";
import { safeUrl } from "@/lib/safeUrl";

const MS_PER_MINUTE = 60000;
const MINUTES_PER_HOUR = 60;
const HOURS_PER_DAY = 24;
const time = deck.freshness.time;
const legacy = deck.freshness.legacyLine;

export function relativeTime(iso: string | null | undefined, now: number): string {
  const then = iso ? new Date(iso).getTime() : Number.NaN;
  if (!Number.isFinite(then)) return time.unknown;
  const signed = Math.round((now - then) / MS_PER_MINUTE);
  const minutes = Math.abs(signed);
  if (minutes < 1) return time.justNow;
  const span = spanOf(minutes);
  return signed < 0 ? time.ahead(span) : time.ago(span); // a time still ahead (next_check_at) counts forward
}

function spanOf(minutes: number): string {
  if (minutes < MINUTES_PER_HOUR) return time.minutes(minutes);
  const hours = Math.round(minutes / MINUTES_PER_HOUR);
  return hours < HOURS_PER_DAY ? time.hours(hours) : time.days(Math.round(hours / HOURS_PER_DAY));
}

/** The page's single freshness line. `now` (ms) is a parameter so this stays pure. error/stale is decided FIRST, before the
 *  checked_at test (C3 item 6): the first check a monitor ever makes can fail before any good check has landed. */
export function freshnessLine(freshness: unknown, now: number): string {
  const f = (typeof freshness === "object" && freshness !== null ? freshness : {}) as Record<string, unknown>;
  const checkedAt = typeof f.checked_at === "string" ? f.checked_at : null;
  const lastErrorAt = typeof f.last_error_at === "string" ? f.last_error_at : null;
  if (f.status === "error" || f.status === "stale") {
    const at = f.status === "error" && lastErrorAt ? lastErrorAt : checkedAt;
    return at ? legacy.failedAt(relativeTime(at, now)) : legacy.failed;
  }
  if (!checkedAt) return legacy.unavailable;
  const snapshot = typeof f.snapshot_as_of === "string" && f.snapshot_as_of !== "" ? f.snapshot_as_of : null;
  const parts = [snapshot ? legacy.dataAsOf(snapshot) : legacy.dateUnknown, legacy.checked(relativeTime(checkedAt, now))];
  if (typeof f.pending_count === "number") parts.push(legacy.pending(f.pending_count));
  return parts.join(legacy.separator);
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
  since_data: SinceData | null | "invalid";
}

/** "aborted": the request was cancelled because nobody wants it any more; it is never shown (R4 review of the plan, sec #12). */
export type FreshnessResult = { kind: "ok"; freshness: Freshness } | { kind: "error"; detail: string } | { kind: "aborted" };

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

// docs/specs/m2a-answer-integrity.md P4 and S7: "Since our data". The server validates these rows (ingestion/freshness.py,
// federal_register.py); this parses them again, because the page is the last place a string from another site can do harm.
const EVENT_FORMS = ["8-K", "8-K/A", "6-K"] as const;
const FILINGS_PER_COMPANY = 5;
const RULES_SHOWN = 20;
const RULE_TITLE_MAX = 300;
const ISO_DAY = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;
const DOCUMENT_NUMBER = /^(?:C[0-9]-)?[0-9]{4}-[0-9]{4,6}$/; // federal_register.py DOCUMENT_NUMBER_RE
const ACCESSION = /^[0-9]{10}-[0-9]{2}-[0-9]{6}$/;
const ITEM_CODE = /^[0-9]\.[0-9]{2}$/;
const SEC_HOST = "www.sec.gov";
const FEDERAL_REGISTER_HOST = "www.federalregister.gov";

export interface SinceFiling {
  ticker: string;
  form: (typeof EVENT_FORMS)[number];
  filing_date: string;
  accession_no: string;
  items: string[];
  /** The filing on sec.gov, or null when the link was not a plain https link there (the row is then text only). */
  href: string | null;
}

export interface SinceRule {
  /** The Federal Register document number: the rule's identity, and part of its link's accessible name. */
  document_number: string;
  title: string;
  publication_date: string;
  href: string | null;
}

export interface SinceData {
  since: string;
  rules_since: string;
  filings: SinceFiling[];
  filing_totals: Record<string, number>;
  rules: SinceRule[];
  rules_more: boolean;
  dropped: { filings: number; rules: number };
}

/** A real calendar day written YYYY-MM-DD: Date.UTC rolls 2026-02-31 over to March, so the day must round-trip. */
export function isDay(value: unknown): value is string {
  if (typeof value !== "string" || !ISO_DAY.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10) === value;
}

/** `value` as a link when it is a plain https URL on exactly `host`. */
function hostUrl(value: unknown, host: string): string | null {
  const href = safeUrl(value);
  if (href === null) return null;
  const url = new URL(href);
  return url.hostname === host && url.port === "" ? href : null; // the default :443 normalises to ""
}

function sinceFiling(value: unknown): SinceFiling | null {
  if (!isRow(value)) return null;
  const { ticker, form, filing_date: day, accession_no: accession, items } = value;
  if (typeof ticker !== "string" || !FILERS.some((f) => f.ticker === ticker)) return null;
  if (typeof form !== "string" || !(EVENT_FORMS as readonly string[]).includes(form)) return null;
  if (!isDay(day) || typeof accession !== "string" || !ACCESSION.test(accession)) return null;
  if (!Array.isArray(items) || !items.every((i) => typeof i === "string" && ITEM_CODE.test(i))) return null;
  return { ticker, form: form as SinceFiling["form"], filing_date: day, accession_no: accession, items: items as string[], href: hostUrl(value.url, SEC_HOST) };
}

function sinceRule(value: unknown): SinceRule | null {
  if (!isRow(value)) return null;
  const { title, publication_date: day, document_number: number } = value;
  if (typeof number !== "string" || !DOCUMENT_NUMBER.test(number)) return null;
  if (typeof title !== "string" || title.length < 1 || title.length > RULE_TITLE_MAX || !isDay(day)) return null;
  return { document_number: number, title, publication_date: day, href: hostUrl(value.html_url, FEDERAL_REGISTER_HOST) };
}

function totals(value: unknown): Record<string, number> {
  const out: Record<string, number> = {};
  if (!isRow(value)) return out;
  for (const { ticker } of FILERS) {
    const n = Object.hasOwn(value, ticker) ? value[ticker] : null;
    if (typeof n === "number" && Number.isInteger(n) && n >= 0) out[ticker] = n;
  }
  return out;
}

const count = (value: unknown): number => (typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0);

/** null: no since_data (not checked); "invalid": a body that is not the shape the server sends (the panel is not shown). */
export function parseSinceData(value: unknown): SinceData | null | "invalid" {
  if (value === null || value === undefined) return null;
  if (!isRow(value) || !isDay(value.since) || !isDay(value.rules_since)) return "invalid";
  if (!Array.isArray(value.filings) || !Array.isArray(value.rules)) return "invalid";
  const perCompany = new Map<string, number>();
  const accessions = new Set<string>();
  let unreadableFilings = 0;
  const filings = value.filings.flatMap((raw): SinceFiling[] => {
    const row = sinceFiling(raw);
    if (row === null) {
      unreadableFilings += 1;
      return [];
    }
    if (accessions.has(row.accession_no)) {
      unreadableFilings += 1; // a repeat of an accession number is dropped, and counted
      return [];
    }
    accessions.add(row.accession_no);
    const seen = perCompany.get(row.ticker) ?? 0;
    perCompany.set(row.ticker, seen + 1);
    return seen < FILINGS_PER_COMPANY ? [row] : [];
  });
  const dropped = isRow(value.dropped) ? value.dropped : {};
  const numbers = new Set<string>();
  const rules = value.rules.map(sinceRule).filter((r): r is SinceRule => {
    if (r === null || numbers.has(r.document_number)) return false; // unreadable, or a repeat of a document number: dropped, and counted
    numbers.add(r.document_number);
    return true;
  });
  return {
    since: value.since,
    rules_since: value.rules_since,
    filings,
    filing_totals: totals(value.filing_totals),
    rules: rules.slice(0, RULES_SHOWN),
    rules_more: value.rules_more === true || rules.length > RULES_SHOWN, // a cap made here implies more, too
    dropped: { filings: count(dropped.filings) + unreadableFilings, rules: count(dropped.rules) + (value.rules.length - rules.length) },
  };
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
    since_data: parseSinceData(body.since_data),
  };
}

/** GET /api/freshness (an absolute path). An abort rejects; every other failure is a result. */
export async function fetchFreshness(signal?: AbortSignal): Promise<FreshnessResult> {
  let response: Response;
  try {
    response = await fetch("/api/freshness", { signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    return { kind: "error", detail: STATUS_NETWORK };
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
