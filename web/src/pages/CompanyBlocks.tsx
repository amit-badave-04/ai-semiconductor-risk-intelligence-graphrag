import type { ReactNode } from "react";
import { Link } from "react-router";
import { Chip } from "@/components/Chips";
import { classifyId, numberOccurrences } from "@/lib/citations";
import { CITE_GROUP, type ActiveRisk, type Dossier, type Edge, type Filing, type Metric, type Rule } from "@/lib/company";
import { FILERS } from "@/lib/pages.generated";

// R25: every block of the dossier. Everything from the server is a text node (S2): no dangerouslySetInnerHTML, no href, src
// or style comes from data. A citation id of the corpus grammar (chunk, xbrl, fr) is the shared evidence chip (Chips.tsx): a real
// button that opens the one evidence drawer with the place "company" (R25 "filing-backed items open evidence chips", A1). Any
// other value, a `doc:` id included (it belongs to a workspace), stays text with a data-cite attribute and can cause no request (S4).

// The header sentence of index.html:58 that says what the rules are (pinned by tests/test_static_ui.py:54).
const RULES_NOTE_BEFORE = "The links to export-control rules are ";
const RULES_NOTE_PINNED = "keyword matches to external Federal Register rules";
const RULES_NOTE_AFTER = ": outside events, not statements the company made.";

const NONE = "none";

export interface CiteProps {
  /** The citation id from the server. Untrusted: only an id of the corpus grammar becomes a chip. */
  id: string;
  /** The block's group (CITE_GROUP). */
  group: number;
  /** The occurrence of this id within its group, from numberOccurrences. */
  nth: number;
}

export function Cite({ id, group, nth }: CiteProps) {
  const kind = classifyId(id);
  if (kind !== null && kind !== "doc") return <Chip id={id} place="company" gen={group} nth={nth} />;
  return (
    <code data-cite={id} className="rounded bg-muted px-1.5 py-0.5 text-xs break-all">
      {id}
    </code>
  );
}

export function Block({ title, children }: { title: string; children: ReactNode }) {
  const id = `block-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
  return (
    <section aria-labelledby={id} className="mb-8">
      <h2 id={id} className="mb-2 text-lg font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

const TH = "px-3 py-2 text-left text-sm font-semibold";
const TD = "px-3 py-2 align-top text-sm";

function text(value: string | number | boolean | null): string {
  return value === null ? "" : String(value);
}

function FilingsBlock({ filings }: { filings: Filing[] }) {
  return (
    <Block title="Filings">
      {filings.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <table className="w-full border-collapse break-words">
          <thead>
            <tr className="border-b border-border">
              <th scope="col" className={TH}>Form</th>
              <th scope="col" className={TH}>Filed</th>
              <th scope="col" className={TH}>Status</th>
              <th scope="col" className={TH}>Accession</th>
            </tr>
          </thead>
          <tbody>
            {filings.map((f, i) => (
              <tr key={`${f.accession_no ?? ""}-${i}`} className="border-b border-border">
                <td className={TD}>{text(f.form)}</td>
                <td className={TD}>{text(f.filing_date)}</td>
                <td className={TD}>{text(f.status)}</td>
                <td className={TD}>{text(f.accession_no)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Block>
  );
}

function MetricsBlock({ metrics }: { metrics: Metric[] }) {
  const nths = numberOccurrences(metrics.map((m) => (m.cite === null ? [] : [m.cite])));
  return (
    <Block title="Metrics">
      {metrics.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <table className="w-full border-collapse break-words">
          <thead>
            <tr className="border-b border-border">
              <th scope="col" className={TH}>Metric</th>
              <th scope="col" className={TH}>Value</th>
              <th scope="col" className={TH}>Unit</th>
              <th scope="col" className={TH}>Period end</th>
              <th scope="col" className={TH}>Source</th>
            </tr>
          </thead>
          <tbody>
            {metrics.map((m, i) => (
              <tr key={`${m.metric ?? ""}-${m.period_end ?? ""}-${i}`} className="border-b border-border">
                <td className={TD}>{text(m.metric)}</td>
                <td className={TD}>{text(m.value)}</td>
                <td className={TD}>{text(m.unit)}</td>
                <td className={TD}>{text(m.period_end)}</td>
                <td className={TD}>{m.cite === null ? null : <Cite id={m.cite} group={CITE_GROUP.metrics} nth={nths[i][0]} />}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Block>
  );
}

function RisksBlock({ risks }: { risks: ActiveRisk[] }) {
  return (
    <Block title="Active risks">
      {risks.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <ul className="m-0 list-none space-y-3 p-0">
          {risks.map((r, i) => (
            <li key={`${r.risk_id ?? ""}-${i}`}>
              <p className="m-0">{text(r.summary)}</p>
              <p className="m-0 text-sm text-muted-foreground">
                {text(r.category)}
                {r.last_evidenced_at === null ? "" : ` \u00b7 ${r.last_evidenced_at}`}
              </p>
            </li>
          ))}
        </ul>
      )}
    </Block>
  );
}

interface RelationProps {
  edge: Edge;
  group: number;
  /** The occurrence number of each of edge.chunk_ids, in order. */
  nths: readonly number[];
}

function Relation({ edge, group, nths }: RelationProps) {
  return (
    <>
      <p className="m-0 font-medium">
        {text(edge.source)} <span className="text-muted-foreground">{text(edge.relation)}</span> <span>{text(edge.target)}</span>
        {edge.status === null ? "" : ` (${edge.status})`}
      </p>
      {edge.quote === null ? null : <p className="m-0 text-sm">{edge.quote}</p>}
      {edge.chunk_ids.length === 0 ? null : (
        <p className="m-0 flex flex-wrap gap-2">
          {edge.chunk_ids.map((id, j) => (
            <Cite key={`${id}:${nths[j]}`} id={id} group={group} nth={nths[j]} />
          ))}
        </p>
      )}
    </>
  );
}

function EdgesBlock({ edges }: { edges: Edge[] }) {
  const nths = numberOccurrences(edges.map((e) => e.chunk_ids));
  return (
    <Block title="Relationships">
      {edges.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <ul className="m-0 list-none space-y-3 p-0">
          {edges.map((e, i) => (
            <li key={`${e.source ?? ""}-${e.relation ?? ""}-${e.target ?? ""}-${i}`}>
              <Relation edge={e} group={CITE_GROUP.edges} nths={nths[i]} />
            </li>
          ))}
        </ul>
      )}
    </Block>
  );
}

function RulesBlock({ rules }: { rules: Rule[] }) {
  // A rule's chunk ids, then its own cite: one count for the whole block.
  const nths = numberOccurrences(rules.map((r) => (r.cite === null ? r.chunk_ids : [...r.chunk_ids, r.cite])));
  return (
    <Block title="Federal Register rules">
      <p className="mb-3 text-sm text-muted-foreground">
        {RULES_NOTE_BEFORE}
        <b>{RULES_NOTE_PINNED}</b>
        {RULES_NOTE_AFTER}
      </p>
      {rules.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <ul className="m-0 list-none space-y-3 p-0">
          {rules.map((r, i) => (
            <li key={`${r.rule_id ?? ""}-${r.target ?? ""}-${i}`}>
              <Relation edge={r} group={CITE_GROUP.rules} nths={nths[i]} />
              <p className="mt-2 mb-0 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                {r.cite === null ? <span>{text(r.rule_id)}</span> : <Cite id={r.cite} group={CITE_GROUP.rules} nth={nths[i][r.chunk_ids.length]} />}
                {r.date === null ? null : <span>{r.date}</span>}
                {r.kind === null ? null : <span>{r.kind}</span>}
              </p>
            </li>
          ))}
        </ul>
      )}
    </Block>
  );
}

/** The company switcher: the 13 filers as links. */
export function CompanySwitcher({ current }: { current: string }) {
  return (
    <nav aria-label="Companies" className="mb-6">
      <ul className="m-0 flex list-none flex-wrap gap-1 p-0">
        {FILERS.map((f) => (
          <li key={f.ticker}>
            <Link
              to={`/company/${f.ticker}/`}
              aria-current={f.ticker === current ? "page" : undefined}
              className="rounded-md px-3 py-2 text-sm hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none aria-[current=page]:bg-muted aria-[current=page]:font-semibold"
            >
              {`${f.name} (${f.ticker})`}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export function DossierBlocks({ dossier }: { dossier: Dossier }) {
  return (
    <>
      {dossier.dataAsOf === null ? null : <p className="mb-6 text-sm text-muted-foreground">{`Data as of ${dossier.dataAsOf}`}</p>}
      <FilingsBlock filings={dossier.filings} />
      <MetricsBlock metrics={dossier.metrics} />
      <RisksBlock risks={dossier.activeRisks} />
      <EdgesBlock edges={dossier.edges} />
      <RulesBlock rules={dossier.rules} />
    </>
  );
}
