import { Fragment, useId } from "react";
import { deck } from "@/copy/deck";
import { filingText, ruleText } from "@/lib/sinceView";
import { SAFE_REL } from "@/lib/safeUrl";
import type { SinceFiling, SinceRule } from "@/pages/Freshness.lib";

// docs/specs/m2a-answer-integrity.md P4: the rows of "Since our data". Each row is a block <li> (not flex or grid: NVDA drops a plain
// space between flex items) holding the sentence as ONE text node, then its link on its own line. The link's name starts with its
// visible words, a no-break space and the row's number (a plain space between inline items can be dropped), so two links with the same
// words differ; the sentence is its description, and stays visible as plain text.

interface RowProps {
  id: string;
  sentence: string;
  href: string | null;
  linkText: string;
  /** The row's accession number or document number: data, not words; it makes two links with the same words differ. */
  number: string;
}

function Row({ id, sentence, href, linkText, number }: RowProps) {
  const sentenceId = `${id}-text`;
  return (
    <li className="block border-b border-border py-2 [overflow-wrap:anywhere]">
      <span id={sentenceId}>{sentence}</span>
      {href === null ? null : (
        <a aria-describedby={sentenceId} className="mt-1 block text-primary underline underline-offset-4" href={href} target="_blank" rel={SAFE_REL}>
          {linkText}
          <span className="sr-only">{`\u00a0${number}`}</span>
        </a>
      )}
    </li>
  );
}

const showing = "block py-2 text-sm text-muted-foreground";

/** The filings, in the order given, with "showing 5 of M" after the last row of a company that has more than it shows. */
export function FilingRows({ rows, totals, labelledBy }: { rows: SinceFiling[]; totals: Record<string, number>; labelledBy?: string }) {
  const base = useId();
  const tickers = [...new Set(rows.map((r) => r.ticker))];
  return (
    <ul aria-labelledby={labelledBy} className="m-0 list-none p-0">
      {tickers.map((ticker) => {
        const group = rows.filter((r) => r.ticker === ticker);
        const total = Object.hasOwn(totals, ticker) ? totals[ticker] : 0;
        return (
          <Fragment key={ticker}>
            {group.map((r) => (
              <Row key={r.accession_no} id={`${base}-${r.accession_no}`} sentence={filingText(r)} href={r.href} linkText={deck.evidence.link.filing} number={r.accession_no} />
            ))}
            {total > group.length ? <li className={showing}>{deck.since.showingOf(group.length, total)}</li> : null}
          </Fragment>
        );
      })}
    </ul>
  );
}

export function RuleRows({ rows, labelledBy }: { rows: SinceRule[]; labelledBy: string }) {
  const base = useId();
  return (
    <ul aria-labelledby={labelledBy} className="m-0 list-none p-0">
      {rows.map((r) => (
        <Row key={r.document_number} id={`${base}-${r.document_number}`} sentence={ruleText(r)} href={r.href} linkText={deck.evidence.link.rule} number={r.document_number} />
      ))}
    </ul>
  );
}
