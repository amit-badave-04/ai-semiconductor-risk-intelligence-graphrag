import type { ReactNode } from "react";
import { Link } from "react-router";
import type { ActiveRisk, Dossier, Edge, Filing, Metric, Rule } from "@/lib/company";
import { FILERS } from "@/lib/pages.generated";

// R25: every block of the dossier. Everything from the server is a text node (S2): no dangerouslySetInnerHTML, no href, src
// or style comes from data. A citation id is shown as text with a data-cite attribute; the evidence chips and the drawer are
// worker B's (I3) and will take over these elements.

// The header sentence of index.html:58 that says what the rules are (pinned by tests/test_static_ui.py:54).
const RULES_NOTE_BEFORE = "The links to export-control rules are ";
const RULES_NOTE_PINNED = "keyword matches to external Federal Register rules";
const RULES_NOTE_AFTER = ": outside events, not statements the company made.";

const NONE = "none";

export function Cite({ id }: { id: string }) {
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
                <td className={TD}>{m.cite === null ? null : <Cite id={m.cite} />}</td>
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

function Relation({ edge }: { edge: Edge }) {
  return (
    <>
      <p className="m-0 font-medium">
        {text(edge.source)} <span className="text-muted-foreground">{text(edge.relation)}</span> <span>{text(edge.target)}</span>
        {edge.status === null ? "" : ` (${edge.status})`}
      </p>
      {edge.quote === null ? null : <p className="m-0 text-sm">{edge.quote}</p>}
      {edge.chunk_ids.length === 0 ? null : (
        <p className="m-0 flex flex-wrap gap-2">
          {edge.chunk_ids.map((id) => (
            <Cite key={id} id={id} />
          ))}
        </p>
      )}
    </>
  );
}

function EdgesBlock({ edges }: { edges: Edge[] }) {
  return (
    <Block title="Relationships">
      {edges.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <ul className="m-0 list-none space-y-3 p-0">
          {edges.map((e, i) => (
            <li key={`${e.source ?? ""}-${e.relation ?? ""}-${e.target ?? ""}-${i}`}>
              <Relation edge={e} />
            </li>
          ))}
        </ul>
      )}
    </Block>
  );
}

function RulesBlock({ rules }: { rules: Rule[] }) {
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
              <Relation edge={r} />
              <p className="m-0 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                {r.cite === null ? <span>{text(r.rule_id)}</span> : <Cite id={r.cite} />}
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
