// R25 (docs/specs/m5b-frontend.md): the company dossier client (a filer ticker only, encoded in the path). Worker C1, I2.
// The router-decoded :ticker is untrusted: a value that is not one of the generated filers is never fetched. The parser keeps
// only the fields the page shows, so an unknown field of the response never reaches a component, and it never keeps a
// data-supplied URL (S2: the page has no href that comes from data).
import { deck } from "@/copy/deck";
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

/** The Company page's chip groups (R25): a chip's key is place + group + id + occurrence, unique on the page (A2). */
export const CITE_GROUP = { metrics: 1, edges: 2, rules: 3, changes: 4 } as const;

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
  /** The passage the quote came from: the first raw chunk id when it matches the grammar, else null (docs/specs/m5b-redesign.md
   *  RD24; the server sets the quote when the edge is created and appends later passages after it). */
  quoteChunk: string | null;
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
  /** A 200 whose body is no dossier (the page words it: Company.copy MSG_COMPANY_UNREADABLE). */
  | { kind: "unreadable" }
  | { kind: "error"; detail: string };

/** The kinds of change the server reports for a risk item (retrieval/dossier.py _KIND_MAP). */
export type RiskKind = "new" | "changed" | "dropped" | "unsettled";

export interface FilingRef {
  accession_no: string | null;
  form: string | null;
  filing_date: string | null;
}

export interface RiskPassage {
  quote: string | null;
  /** Only an id that matches the chunk-id grammar. */
  chunkId: string | null;
  side: "older" | "newer" | null;
}

export interface RiskItem {
  kind: RiskKind;
  headline: string | null;
  passages: RiskPassage[];
}

export interface RiskPair {
  older: FilingRef;
  newer: FilingRef;
  /** False only when the server says so: the pair could not be compared, and `reason` says why. */
  compared: boolean;
  reason: string | null;
  items: RiskItem[];
}

export interface RiskChanges {
  pairs: RiskPair[];
}

export type RiskChangesResult =
  | { kind: "ok"; changes: RiskChanges }
  | { kind: "notFound" }
  | { kind: "error"; detail: string };

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

function quoteChunk(value: unknown): string | null {
  const first: unknown = Array.isArray(value) ? value[0] : undefined;
  return typeof first === "string" && CHUNK_ID_RE.test(first) ? first : null;
}

function toEdge(row: Row): Edge {
  return {
    source: str(row.source),
    relation: str(row.relation),
    target: str(row.target),
    status: str(row.status),
    quote: str(row.quote),
    chunk_ids: chunkIds(row.chunk_ids),
    quoteChunk: quoteChunk(row.chunk_ids),
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

type Fetched =
  | { kind: "ok"; body: unknown; fallback: string }
  | { kind: "notFound" }
  | { kind: "error"; detail: string };

/** GET one company route as JSON. An abort rejects (the caller discards it); every other failure is a result. `fallback` is the
 *  status text (or "HTTP <status>") the risk changes show for a 200 whose body is no usable report; the dossier words it itself. */
async function getJson(url: string, signal?: AbortSignal): Promise<Fetched> {
  let response: Response;
  try {
    response = await fetch(url, { signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    return { kind: "error", detail: deck.company.network };
  }
  if (response.status === 404) return { kind: "notFound" };
  const fallback = response.statusText || `HTTP ${response.status}`;
  if (!response.ok) return { kind: "error", detail: await detailOf(response) };
  try {
    return { kind: "ok", body: await response.json(), fallback };
  } catch {
    return { kind: "ok", body: undefined, fallback }; // a 200 that is not JSON is no report: the caller's parse says so
  }
}

/** GET one filer's dossier. No request at all for a ticker that is not a generated filer. An abort rejects (the caller
 *  discards it); every other failure is a result. */
export async function fetchDossier(ticker: string, signal?: AbortSignal): Promise<DossierResult> {
  if (!isFilerTicker(ticker)) return { kind: "notFound" };
  const got = await getJson(dossierUrl(ticker), signal);
  if (got.kind !== "ok") return got;
  const dossier = parseDossier(got.body);
  return dossier === null ? { kind: "unreadable" } : { kind: "ok", dossier };
}

/** The one place the risk-changes URL is built: an absolute /api path, the ticker encoded. */
export function riskChangesUrl(ticker: string): string {
  return `/api/company/${encodeURIComponent(ticker)}/risk-changes`;
}

const RISK_KINDS: ReadonlySet<string> = new Set<RiskKind>(["new", "changed", "dropped", "unsettled"]);

function toFilingRef(value: unknown): FilingRef {
  const row = isRow(value) ? value : {};
  return { accession_no: str(row.accession_no), form: str(row.form), filing_date: str(row.filing_date) };
}

function toRiskPassage(row: Row): RiskPassage {
  const chunk = typeof row.chunk_id === "string" && CHUNK_ID_RE.test(row.chunk_id) ? row.chunk_id : null;
  const side = row.side === "older" || row.side === "newer" ? row.side : null;
  return { quote: str(row.quote), chunkId: chunk, side };
}

function toRiskItem(row: Row): RiskItem | null {
  if (typeof row.kind !== "string" || !RISK_KINDS.has(row.kind)) return null;
  return { kind: row.kind as RiskKind, headline: str(row.headline), passages: rows(row.passages).map(toRiskPassage) };
}

function toRiskPair(row: Row): RiskPair {
  return {
    older: toFilingRef(row.older),
    newer: toFilingRef(row.newer),
    compared: row.compared !== false,
    reason: str(row.not_compared_reason),
    items: rows(row.items).map(toRiskItem).filter((i): i is RiskItem => i !== null),
  };
}

/** Narrow an unknown body to the risk changes; null when it has no pairs list. Unknown fields and items of an unknown kind are
 *  dropped, and a passage keeps a citation id only when it matches the chunk-id grammar (S2: nothing here is a URL). */
export function parseRiskChanges(body: unknown): RiskChanges | null {
  if (!isRow(body) || !Array.isArray(body.pairs)) return null;
  return { pairs: rows(body.pairs).map(toRiskPair) };
}

/** GET one filer's recent risk changes. Same gate as {@link fetchDossier}: no request for a ticker that is not a generated filer. */
export async function fetchRiskChanges(ticker: string, signal?: AbortSignal): Promise<RiskChangesResult> {
  if (!isFilerTicker(ticker)) return { kind: "notFound" };
  const got = await getJson(riskChangesUrl(ticker), signal);
  if (got.kind !== "ok") return got;
  const changes = parseRiskChanges(got.body);
  return changes === null ? { kind: "error", detail: got.fallback } : { kind: "ok", changes };
}
