import { Link } from "react-router";
import { deck } from "@/copy/deck";
import type { ActiveRisk, Dossier, Filing } from "@/lib/company";
import { formatDate } from "@/lib/format";
import { FILERS } from "@/lib/pages.generated";
import { Block, ScrollRegion } from "./CompanySection";
import { AtAGlance, KeyNumbers } from "./CompanyNumbers";
import { Relationships } from "./CompanyRelationships";
import { GovernmentRules } from "./CompanyRules";

// R25 and docs/specs/m5b-redesign.md R3: every block of the dossier, in the page's fixed order. Everything from the server is a
// text node (S2): no dangerouslySetInnerHTML, no href, src or style comes from data. A citation id of the corpus grammar is the
// shared evidence chip or figure stamp (components/Chips.tsx), a real button that opens the one evidence drawer with the place
// "company" (A1); any other value, a `doc:` id included (it belongs to a workspace), stays text and can cause no request (S4).

const NONE = deck.company.status.none;

const TH = "px-3 py-2 text-left text-sm font-semibold";
const TD = "px-3 py-2 align-top text-sm";
// A cell that holds one long token (an accession number, a 12-digit value, a date, a chip label). overflow-wrap: anywhere (not
// break-word) lowers the cell's minimum width, so the five-column table fits a 390 px screen whatever the font; short words in
// the other cells stay whole.
const TD_LONG = `${TD} [overflow-wrap:anywhere]`;

function text(value: string | number | boolean | null): string {
  return value === null ? "" : String(value);
}

function FilingsBlock({ filings }: { filings: Filing[] }) {
  return (
    <Block id="filings" title={deck.company.filings.heading}>
      {filings.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <ScrollRegion label={deck.company.filings.tableRegion}>
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
                  <td className={TD_LONG}>{formatDate(f.filing_date)}</td>
                  <td className={TD}>{f.status === null ? "" : deck.evidence.status(f.status)}</td>
                  <td className={TD_LONG}>{text(f.accession_no)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </ScrollRegion>
      )}
    </Block>
  );
}

function RisksBlock({ risks }: { risks: ActiveRisk[] }) {
  return (
    <Block id="active-risks" title={deck.company.risks.heading}>
      {/* D-R4: the summaries are semigraph's words; the data has no date for them (the field is never written) */}
      <p className="mt-0 mb-3 text-sm text-muted-foreground">{deck.company.risks.label}</p>
      {risks.length === 0 ? (
        <p className="text-muted-foreground">{NONE}</p>
      ) : (
        <ul className="m-0 list-none space-y-3 p-0">
          {risks.map((r, i) => (
            <li key={`${r.risk_id ?? ""}-${i}`}>
              <p className="m-0">{text(r.summary)}</p>
              {r.category !== null && <p className="m-0 text-sm text-muted-foreground">{r.category}</p>}
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
              className="rounded-md px-3 py-2 text-sm hover:bg-muted aria-[current=page]:bg-muted aria-[current=page]:font-semibold"
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
  // the generated filer name, not the server's (D-R3 names the company; RD24 tells the company's own side of a relation)
  const companyName = FILERS.find((f) => f.ticker === dossier.company.ticker)?.name ?? dossier.company.name;
  return (
    <>
      <AtAGlance metrics={dossier.metrics} rulesListed={dossier.rules.length} />
      <KeyNumbers metrics={dossier.metrics} />
      <GovernmentRules rules={dossier.rules} companyName={companyName} />
      <Relationships edges={dossier.edges} companyName={companyName} />
      <FilingsBlock filings={dossier.filings} />
      <RisksBlock risks={dossier.activeRisks} />
    </>
  );
}
