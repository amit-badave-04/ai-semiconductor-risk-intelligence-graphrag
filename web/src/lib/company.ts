// R25 (docs/specs/m5b-frontend.md): the company dossier client (a filer ticker only, encoded in the path). Worker C1, I2.
// The router-decoded :ticker is untrusted: a value that is not one of the generated filers is never fetched. The parser keeps
// only the fields the page shows, so an unknown field of the response never reaches a component, and it never keeps a
// data-supplied URL (S2: the page has no href that comes from data).
import { CHUNK_ID_RE, FR_ID_RE, XBRL_ID_RE } from "@/lib/ids.generated";
import { FILERS, type Ticker } from "@/lib/pages.generated";
import { detailOf } from "@/lib/refusal";

export interface Filing {
  accession_no: string | null;
  form: string | null;
  filing_date: string | null;
  is_current: boolean | null;
  status: string | null;
  superseded_by: string | null;
}

export interface Metric {
  metric: string | null;
  value: number | string | null;
  unit: string | null;
  period_start: string | null;
  period_end: string | null;
  /** The `xbrl:` citation id of this fact, or null when its parts do not form one. */
  cite: string | null;
}

export interface ActiveRisk {
  risk_id: string | null;
  summary: string | null;
  category: string | null;
  last_evidenced_at: string | null;
}

export interface Edge {
  source: string | null;
  relation: string | null;
  target: string | null;
  status: string | null;
  quote: string | null;
  /** Only ids that match the chunk-id grammar. */
  chunk_ids: string[];
}

export interface Rule extends Edge {
  rule_id: string | null;
  date: string | null;
  kind: string | null;
  /** The `fr:` citation id of the rule, or null when rule_id does not form one. */
  cite: string | null;
}

export interface Dossier {
  company: { ticker: string; name: string; cik: number | null };
  dataAsOf: string | null;
  filings: Filing[];
  metrics: Metric[];
  activeRisks: ActiveRisk[];
  edges: Edge[];
  rules: Rule[];
}

export type DossierResult =
  | { kind: "ok"; dossier: Dossier }
  | { kind: "notFound" }
  | { kind: "error"; detail: string };

// The legacy page's words for a failed request (index.html:680).
const MSG_NETWORK = "network error \u2014 is the service online?";

const FILER_TICKERS: ReadonlySet<string> = new Set(FILERS.map((f) => f.ticker));

export function isFilerTicker(value: unknown): value is Ticker {
  return typeof value === "string" && FILER_TICKERS.has(value);
}

/** The one place the dossier URL is built: an absolute /api path (never the router basename), the ticker encoded. */
export function dossierUrl(ticker: string): string {
  return `/api/company/${encodeURIComponent(ticker)}/dossier`;
}

type Row = Record<string, unknown>;

function isRow(value: unknown): value is Row {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function bool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function rows(value: unknown): Row[] {
  return Array.isArray(value) ? value.filter(isRow) : [];
}

function chunkIds(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string" && CHUNK_ID_RE.test(v)) : [];
}

function metricCite(cik: unknown, metric: string | null, periodEnd: string | null): string | null {
  if (typeof cik !== "number" || metric === null || periodEnd === null) return null;
  const id = `xbrl:${cik}:${metric}:${periodEnd}`;
  return XBRL_ID_RE.test(id) ? id : null;
}

function ruleCite(ruleId: string | null): string | null {
  if (ruleId === null) return null;
  return [ruleId, `fr:${ruleId}`].find((id) => FR_ID_RE.test(id)) ?? null;
}

function toEdge(row: Row): Edge {
  return {
    source: str(row.source),
    relation: str(row.relation),
    target: str(row.target),
    status: str(row.status),
    quote: str(row.quote),
    chunk_ids: chunkIds(row.chunk_ids),
  };
}

/** Narrow an unknown response body to a Dossier; null when it has no company. Unknown fields are dropped, a missing or
 *  mistyped block is empty. */
export function parseDossier(body: unknown): Dossier | null {
  if (!isRow(body) || !isRow(body.company)) return null;
  const c = body.company;
  const ticker = str(c.ticker);
  const name = str(c.name);
  if (ticker === null || name === null) return null;
  const cik = typeof c.cik === "number" ? c.cik : null;
  return {
    company: { ticker, name, cik },
    dataAsOf: str(body.data_as_of),
    filings: rows(body.filings).map((r) => ({
      accession_no: str(r.accession_no),
      form: str(r.form),
      filing_date: str(r.filing_date),
      is_current: bool(r.is_current),
      status: str(r.status),
      superseded_by: str(r.superseded_by),
    })),
    metrics: rows(body.metrics).map((r) => {
      const metric = str(r.metric);
      const periodEnd = str(r.period_end);
      const value = typeof r.value === "number" || typeof r.value === "string" ? r.value : null;
      return {
        metric,
        value,
        unit: str(r.unit),
        period_start: str(r.period_start),
        period_end: periodEnd,
        cite: metricCite(r.cik ?? cik, metric, periodEnd),
      };
    }),
    activeRisks: rows(body.active_risks).map((r) => ({
      risk_id: str(r.risk_id),
      summary: str(r.summary),
      category: str(r.category),
      last_evidenced_at: str(r.last_evidenced_at),
    })),
    edges: rows(body.edges).map(toEdge),
    rules: rows(body.rules).map((r) => {
      const ruleId = str(r.rule_id);
      return { ...toEdge(r), rule_id: ruleId, date: str(r.date), kind: str(r.kind), cite: ruleCite(ruleId) };
    }),
  };
}

/** GET one filer's dossier. No request at all for a ticker that is not a generated filer. An abort rejects (the caller
 *  discards it); every other failure is a result. */
export async function fetchDossier(ticker: string, signal?: AbortSignal): Promise<DossierResult> {
  if (!isFilerTicker(ticker)) return { kind: "notFound" };
  let response: Response;
  try {
    response = await fetch(dossierUrl(ticker), { signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    return { kind: "error", detail: MSG_NETWORK };
  }
  if (response.status === 404) return { kind: "notFound" };
  if (!response.ok) return { kind: "error", detail: await detailOf(response) };
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { kind: "error", detail: response.statusText || `HTTP ${response.status}` };
  }
  const dossier = parseDossier(body);
  if (dossier === null) return { kind: "error", detail: response.statusText || `HTTP ${response.status}` };
  return { kind: "ok", dossier };
}
