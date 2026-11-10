// docs/specs/m5b-redesign.md RD19: under the company's name and ticker, its latest annual and quarterly reports in words, or for a
// 20-F filer that it files no quarterly 10-Q, and the data date (formatted in UTC). Every value is a text node.
import { Link } from "react-router";
import { deck } from "@/copy/deck";
import type { Dossier } from "@/lib/company";
import { reportLine } from "@/lib/companyView";
import { formatDate } from "@/lib/format";
import { focusSection } from "@/lib/sectionHash";

export interface FilerFacts {
  annualForm: string;
  quarterly: boolean;
}

/** RD19's actions: ask about the company (Ask opens with the approved question in the box, nothing is sent) and compare its
 *  filings (the Risk changes section). The ticker is the generated filer's; the router state carries nothing else. */
export function CompanyActions({ ticker, name }: { ticker: string; name: string }) {
  const w = deck.company.header;
  const action = "inline-flex min-h-[24px] items-center text-sm font-semibold text-primary underline underline-offset-4";
  return (
    <p className="mt-0 mb-4 flex flex-wrap gap-x-6 gap-y-2">
      <Link to="/" state={{ askAbout: ticker }} className={action}>
        {w.askAbout(name)}
      </Link>
      <a href="#risk-changes" onClick={() => focusSection("risk-changes")} className={action}>
        {w.compare}
      </a>
    </p>
  );
}

export function CompanyReports({ dossier, filer }: { dossier: Dossier; filer: FilerFacts }) {
  const w = deck.company.header;
  const line = reportLine(dossier.filings, dossier.metrics, filer);
  const dataAsOf = formatDate(dossier.dataAsOf);
  return (
    <div className="mb-4 space-y-0.5 text-sm">
      {line.annual !== null && <p className="m-0">{w.annual(line.annual)}</p>}
      {line.quarterly !== null && <p className="m-0">{w.quarterly(line.quarterly)}</p>}
      {line.annualOnly && <p className="m-0">{w.annualOnly}</p>}
      {dataAsOf !== "" && <p className="m-0 text-muted-foreground">{w.dataAsOf(dataAsOf)}</p>}
    </div>
  );
}
