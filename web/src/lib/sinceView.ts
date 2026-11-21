// docs/specs/m2a-answer-integrity.md P4: what the "Since our data" panel shows, decided from the freshness payload. Pure; every
// sentence is the deck's (copy/deck.ts since).
import { deck } from "@/copy/deck";
import { formatDate } from "@/lib/format";
import { FILERS } from "@/lib/pages.generated";
import type { Freshness, SinceData, SinceFiling, SinceRule } from "@/pages/Freshness.lib";

const s = deck.since;

export type SinceState = { kind: "hidden" } | { kind: "notChecked" } | { kind: "data"; data: SinceData };

/** hidden: a body the page does not recognise ("invalid"); notChecked: the monitor has no lists (whatever its status); else the lists. */
export function sinceState(freshness: Freshness): SinceState {
  const since = freshness.since_data;
  if (since === "invalid") return { kind: "hidden" };
  return since === null ? { kind: "notChecked" } : { kind: "data", data: since };
}

export type Part<T> = { kind: "rows"; rows: T[] } | { kind: "empty" } | { kind: "omitted" };

/** Rows; or "nothing new" when none were left out; or nothing at all when rows were dropped as unreadable (never claim "nothing"). */
export function partOf<T>(rows: T[], dropped: number): Part<T> {
  if (rows.length > 0) return { kind: "rows", rows };
  return dropped === 0 ? { kind: "empty" } : { kind: "omitted" };
}

/** The filings part of the panel: all rows, or one company's (`ticker`). When there are no rows it is "nothing new" only if the server
 *  checked everything it was asked to: a row dropped as unreadable, or a company the server could not check (`unresolved`: it
 *  contributes no rows and no totals), leaves the part out instead. */
export function filingsPart(data: SinceData, unresolved: readonly string[], ticker?: string): Part<SinceFiling> {
  const rows = ticker === undefined ? data.filings : filingsFor(data, ticker);
  const unchecked = ticker === undefined ? unresolved.length > 0 : unresolved.includes(ticker);
  return partOf(rows, data.dropped.filings + (unchecked ? 1 : 0));
}

/** That company's rows. */
export function filingsFor(data: SinceData, ticker: string): SinceFiling[] {
  return data.filings.filter((f) => f.ticker === ticker);
}

export function filingText(row: SinceFiling): string {
  const company = FILERS.find((f) => f.ticker === row.ticker)?.name ?? row.ticker;
  return s.filingRow({ company, form: row.form, date: formatDate(row.filing_date), labels: row.items.map(s.itemLabel) });
}

export function ruleText(rule: SinceRule): string {
  return s.ruleRow(formatDate(rule.publication_date), rule.title);
}
