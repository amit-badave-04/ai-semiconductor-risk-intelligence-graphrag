// docs/specs/m5b-redesign.md R3: the Company page's views (RD19 the reports in words, RD20 at a glance, RD22 key numbers, RD24
// relationships), pure functions over the parsed dossier (lib/company.ts), worded by copy/deck.ts company. Server values are
// untrusted strings: they are passed through as text, never used as an object key outside a Map, never turned into markup.
import type { Edge, Filing, Metric } from "./company";
import { fiscalYearLabel, formatDate } from "./format";

/** The four metrics the server reports, in the order the page shows them (an unknown one follows, by its raw name). */
export const METRIC_ORDER = ["revenue", "net_income", "rnd", "capex"] as const;
export type KnownMetric = (typeof METRIC_ORDER)[number];
/** D-R2: the page shows the company's four latest fiscal years. */
export const MAX_FISCAL_COLUMNS = 4;
/** The three figures At a glance shows (RD20). */
const GLANCE_METRICS: readonly KnownMetric[] = ["revenue", "net_income", "rnd"];
/** How long after its fiscal year ends an annual report is taken to be about that year (RD19). */
const ANNUAL_REPORT_WINDOW_DAYS = 180;
const DAY_MS = 86_400_000;

export interface FiscalColumn {
  periodEnd: string;
  /** "FY2026" */
  short: string;
  /** "FY2026 (ended Jan 25, 2026)" */
  long: string;
}

export interface Cell {
  value: number | string | null;
  unit: string | null;
  cite: string | null;
}

export interface NumberRow {
  metric: string;
  /** One of METRIC_ORDER (the deck has its name and meaning); false for a name the server added. */
  known: boolean;
  /** One per column, null where the company reported nothing for that year. */
  cells: (Cell | null)[];
  /** "FY2012" for a metric with rows, none of them in these years; null otherwise. */
  latestHeld: string | null;
  /** The company has no row at all for this metric. */
  absent: boolean;
}

export interface GlanceFigure {
  metric: KnownMetric;
  latest: Cell | null;
  prior: Cell | null;
  latestHeld: string | null;
  absent: boolean;
}

const time = (day: string | null): number | null => {
  const label = day === null ? null : fiscalYearLabel(day);
  return label === null || day === null ? null : Date.parse(`${day.slice(0, 10)}T00:00:00Z`);
};

function rowsByMetric(metrics: readonly Metric[]): Map<string, Metric[]> {
  const by = new Map<string, Metric[]>();
  for (const row of metrics) {
    if (row.metric === null || time(row.period_end) === null) continue;
    by.set(row.metric, [...(by.get(row.metric) ?? []), row]);
  }
  return by;
}

const newest = (rows: readonly Metric[]): string =>
  rows.reduce((best, r) => ((time(r.period_end) ?? 0) > (time(best) ?? 0) ? (r.period_end as string) : best), rows[0].period_end as string);

/** The company's newest fiscal years, at most MAX_FISCAL_COLUMNS, newest first: the year ends of the metrics that reach the
 *  company's newest year (so a metric whose rows stopped years ago never adds a column). */
export function fiscalColumns(metrics: readonly Metric[]): FiscalColumn[] {
  const by = rowsByMetric(metrics);
  if (by.size === 0) return [];
  const latest = Math.max(...[...by.values()].map((rows) => time(newest(rows)) ?? 0));
  const ends = new Set<string>();
  for (const rows of by.values()) {
    if (time(newest(rows)) !== latest) continue;
    for (const r of rows) ends.add(r.period_end as string);
  }
  return [...ends]
    .sort((a, b) => (time(b) ?? 0) - (time(a) ?? 0))
    .slice(0, MAX_FISCAL_COLUMNS)
    .map((periodEnd) => {
      const label = fiscalYearLabel(periodEnd);
      return { periodEnd, short: label?.short ?? "", long: label?.long ?? "" };
    });
}

const cellOf = (row: Metric): Cell => ({ value: row.value, unit: row.unit, cite: row.cite });

function rowFor(metric: string, rows: readonly Metric[], columns: readonly FiscalColumn[], known: boolean): NumberRow {
  const cells = columns.map((col) => {
    const hit = rows.find((r) => r.period_end === col.periodEnd);
    return hit === undefined ? null : cellOf(hit);
  });
  const empty = cells.every((c) => c === null);
  const latestHeld = empty && rows.length > 0 ? (fiscalYearLabel(newest(rows))?.short ?? null) : null;
  return { metric, known, cells, latestHeld, absent: rows.length === 0 };
}

/** One row per known metric, in METRIC_ORDER, then any other metric by its raw name. */
export function numberRows(metrics: readonly Metric[], columns: readonly FiscalColumn[]): NumberRow[] {
  const by = rowsByMetric(metrics);
  const knownNames: ReadonlySet<string> = new Set(METRIC_ORDER);
  const others = [...by.keys()].filter((name) => !knownNames.has(name));
  return [
    ...METRIC_ORDER.map((name) => rowFor(name, by.get(name) ?? [], columns, true)),
    ...others.map((name) => rowFor(name, by.get(name) ?? [], columns, false)),
  ];
}

/** Revenue, net income and R&D: the latest column's figure and the prior column's beneath it (RD20). */
export function glance(metrics: readonly Metric[], columns: readonly FiscalColumn[]): GlanceFigure[] {
  const rows = numberRows(metrics, columns.slice(0, 2));
  return GLANCE_METRICS.map((metric) => {
    const row = rows.find((r) => r.metric === metric) as NumberRow;
    return { metric, latest: row.cells[0] ?? null, prior: row.cells[1] ?? null, latestHeld: row.latestHeld, absent: row.absent };
  });
}

export interface ReportLine {
  annual: { form: string; yearEnded: string | null; filed: string } | null;
  quarterly: { form: string; filed: string } | null;
  /** The company files no quarterly report (a 20-F filer): the page says so. */
  annualOnly: boolean;
}

const byNewest = (filings: readonly Filing[]): Filing[] => [...filings].sort((a, b) => (time(b.filing_date) ?? 0) - (time(a.filing_date) ?? 0));

/** RD19: the latest annual report, the fiscal year it covers (the latest year end of its figures within 180 days before it was
 *  filed; none when no figure fits), and the latest quarterly report. Dates are formatted in UTC. */
export function reportLine(filings: readonly Filing[], metrics: readonly Metric[], filer: { annualForm: string; quarterly: boolean }): ReportLine {
  const sorted = byNewest(filings.filter((f) => time(f.filing_date) !== null));
  const annualFiling = sorted.find((f) => f.form === filer.annualForm && f.status === "current");
  let annual: ReportLine["annual"] = null;
  if (annualFiling !== undefined) {
    const filedAt = time(annualFiling.filing_date) as number;
    const ends = metrics
      .map((m) => m.period_end)
      .filter((d): d is string => d !== null && time(d) !== null)
      .filter((d) => {
        const gap = filedAt - (time(d) as number);
        return gap > 0 && gap <= ANNUAL_REPORT_WINDOW_DAYS * DAY_MS;
      })
      .sort((a, b) => (time(b) as number) - (time(a) as number));
    annual = { form: filer.annualForm, yearEnded: ends.length > 0 ? formatDate(ends[0]) : null, filed: formatDate(annualFiling.filing_date) };
  }
  const quarterlyFiling = filer.quarterly ? sorted.find((f) => f.form === "10-Q") : undefined;
  return {
    annual,
    quarterly: quarterlyFiling === undefined ? null : { form: "10-Q", filed: formatDate(quarterlyFiling.filing_date) },
    annualOnly: !filer.quarterly,
  };
}

export interface RelationRow {
  /** The other company; both names when neither side is the page's company. */
  other: string[];
  quote: string | null;
  /** The chip of the passage the quote came from (lib/company.ts quoteChunk), shown beside the quote. */
  quoteChunk: string | null;
  /** The other passages, after it. */
  chips: string[];
}

export interface CompetitorRow {
  name: string;
  chips: string[];
}

export interface RelationshipsView {
  /** Relations with a direction (any relation but competition), listed first. The relation's name is never returned. */
  directional: RelationRow[];
  /** Each competitor once, its chips merged. */
  competitors: CompetitorRow[];
}

const COMPETES = "COMPETES_WITH";

function otherSide(edge: Edge, company: string): string[] {
  const sides = [edge.source, edge.target].filter((s): s is string => s !== null && s !== "");
  const others = sides.filter((s) => s !== company);
  return others.length === sides.length ? sides : others;
}

/** RD24: relationships without a relation name. */
export function relationshipsView(edges: readonly Edge[], company: string): RelationshipsView {
  const directional: RelationRow[] = [];
  const competitors = new Map<string, string[]>();
  for (const edge of edges) {
    const other = otherSide(edge, company);
    if (edge.relation === COMPETES) {
      const name = other.join(", ");
      const chips = competitors.get(name) ?? [];
      competitors.set(name, [...chips, ...edge.chunk_ids.filter((id) => !chips.includes(id))]);
      continue;
    }
    directional.push({ other, quote: edge.quote, quoteChunk: edge.quoteChunk, chips: edge.chunk_ids.filter((id) => id !== edge.quoteChunk) });
  }
  return { directional, competitors: [...competitors].map(([name, chips]) => ({ name, chips })) };
}
