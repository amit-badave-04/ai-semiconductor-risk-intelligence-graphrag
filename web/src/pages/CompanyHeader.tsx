// docs/specs/m5b-redesign.md RD19: under the company's name and ticker, its latest annual and quarterly reports in words, or for a
// 20-F filer that it files no quarterly 10-Q, and the data date (formatted in UTC). Every value is a text node.
import { deck } from "@/copy/deck";
import type { Dossier } from "@/lib/company";
import { reportLine } from "@/lib/companyView";
import { formatDate } from "@/lib/format";

export interface FilerFacts {
  annualForm: string;
  quarterly: boolean;
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
