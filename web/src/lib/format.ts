// docs/specs/m5b-redesign.md RD3, RD11, RD22: how dates, money, paragraph numbers and fiscal years read across the site. Pure
// functions over untrusted server values; every result is plain text (a caller renders it as a text node).

const DATE = new Intl.DateTimeFormat("en-US", { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})/;
const HAS_OFFSET = /(?:Z|[+-]\d{2}:?\d{2})$/i;

/** A Date for an ISO day or timestamp, or null. The day part must be a real calendar day ("2026-02-30" is null). A timestamp
 *  with no offset is read as UTC (never as the browser's local time, which would move the day west of UTC). */
function parseDay(value: string | null | undefined): Date | null {
  if (typeof value !== "string") return null;
  const match = ISO_DAY.exec(value);
  if (!match) return null;
  const [, y, m, d] = match.map(Number);
  const day = new Date(Date.UTC(y, m - 1, d));
  if (day.getUTCFullYear() !== y || day.getUTCMonth() !== m - 1 || day.getUTCDate() !== d) return null;
  if (value.length > 10) {
    const instant = new Date(HAS_OFFSET.test(value) ? value : `${value}Z`);
    return Number.isNaN(instant.getTime()) ? null : instant;
  }
  return day;
}

/** "Sep 24, 2026", formatted in UTC so a US time zone never moves the day; "" for missing or unreadable input. */
export function formatDate(value: string | null | undefined): string {
  const day = parseDay(value);
  return day === null ? "" : DATE.format(day);
}

function toNumber(value: number | string | null | undefined): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

// Every ISO 4217 code this runtime formats (all current browsers and Node have Intl.supportedValuesOf).
const KNOWN_CURRENCIES: ReadonlySet<string> = new Set(
  typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("currency") : [],
);

/** A three-letter ISO currency the runtime knows, or null (never assumed to be dollars). */
function currencyOf(unit: string | null | undefined): string | null {
  return typeof unit === "string" && KNOWN_CURRENCIES.has(unit) ? unit : null;
}

/** The number with its unit when the unit is not a currency the runtime formats (a share count, an unknown code). */
function plainWithUnit(text: string, unit: string | null | undefined): string {
  return typeof unit === "string" && unit.trim() !== "" ? `${text} ${unit}` : text;
}

// A value that rounds to zero is never shown as "-$0".
const SIGN: Intl.NumberFormatOptions = { signDisplay: "negative" };

/** "$215.9B", "NT$2.9T", "-$267M": compact, one decimal, in the reported currency; "" when there is no number. */
export function compactMoney(value: number | string | null | undefined, unit: string | null | undefined): string {
  const n = toNumber(value);
  if (n === null) return "";
  const currency = currencyOf(unit);
  const options: Intl.NumberFormatOptions = { ...SIGN, notation: "compact", maximumFractionDigits: 1 };
  if (currency !== null) return new Intl.NumberFormat("en-US", { ...options, style: "currency", currency }).format(n);
  return plainWithUnit(new Intl.NumberFormat("en-US", options).format(n), unit);
}

/** "$215,938,000,000": the exact value with grouping and no rounding; "" when there is no number. */
export function exactMoney(value: number | string | null | undefined, unit: string | null | undefined): string {
  const n = toNumber(value);
  if (n === null) return "";
  const currency = currencyOf(unit);
  if (currency !== null) {
    return new Intl.NumberFormat("en-US", { ...SIGN, style: "currency", currency, minimumFractionDigits: 0, maximumFractionDigits: 20 }).format(n);
  }
  return plainWithUnit(new Intl.NumberFormat("en-US", { ...SIGN, maximumFractionDigits: 20 }).format(n), unit);
}

/** "0162" reads "162"; a value that is not all digits is returned unchanged. */
export function paragraphNumber(value: string): string {
  return /^\d+$/.test(value) ? String(Number.parseInt(value, 10)) : value;
}

export interface FiscalYearLabel {
  /** "FY2026" */
  short: string;
  /** "FY2026 (ended Jan 25, 2026)" */
  long: string;
}

/** A fiscal year named by the calendar year it ends in, with its end date; null for an unreadable period end. */
export function fiscalYearLabel(periodEnd: string | null | undefined): FiscalYearLabel | null {
  const day = parseDay(periodEnd);
  if (day === null) return null;
  const short = `FY${day.getUTCFullYear()}`;
  return { short, long: `${short} (ended ${DATE.format(day)})` };
}
