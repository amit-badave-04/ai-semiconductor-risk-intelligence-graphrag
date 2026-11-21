import { Link } from "react-router";
import { FilingRows } from "@/components/SinceRows";
import { deck } from "@/copy/deck";
import { useShellFreshness } from "@/lib/freshnessStore";
import { filingsPart, sinceState } from "@/lib/sinceView";
import { Block } from "./CompanySection";

// docs/specs/m2a-answer-integrity.md P4: this company's new 8-K, 8-K/A and 6-K filings since the data, and one line about new BIS
// rules that links to Freshness. It reads the shell's one /api/freshness answer (no request of its own), so it shows nothing until
// that has arrived, after a failed request, or for a body it does not recognise.

const since = deck.since;

export function CompanySince({ ticker }: { ticker: string }) {
  const { freshness } = useShellFreshness();
  if (freshness === null) return null;
  const state = sinceState(freshness);
  if (state.kind === "hidden") return null;
  if (state.kind === "notChecked") {
    return (
      <Block id="since-our-data" title={since.companyHeading}>
        <p className="text-muted-foreground">{since.notChecked}</p>
      </Block>
    );
  }
  const { data } = state;
  const filings = filingsPart(data, freshness.unresolved, ticker);
  if (filings.kind === "omitted") return null; // not even the BIS line alone under "New filings since our data"
  const hasRules = data.rules.length > 0;
  return (
    <Block id="since-our-data" title={since.companyHeading}>
      {filings.kind === "rows" ? <FilingRows rows={filings.rows} totals={data.filing_totals} /> : null}
      {filings.kind === "empty" ? <p className="text-muted-foreground">{since.nothingNew}</p> : null}
      {hasRules ? (
        <p className="mt-3 mb-0">
          <Link className="text-primary underline underline-offset-4" to="/freshness/">
            {since.companyRules(data.rules.length, data.rules_more)}
          </Link>
        </p>
      ) : null}
    </Block>
  );
}
