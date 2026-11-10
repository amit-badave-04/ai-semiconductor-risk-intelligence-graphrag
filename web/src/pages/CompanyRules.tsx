// docs/specs/m5b-redesign.md RD23 and D-R3, docs/decisions/001-r3-company-copy.md (2): the government rules linked to the company,
// in their own section. The notice says what they are (US export-control rules from the Federal Register, linked by keyword
// matching: actions by the government, not statements the company made) and describes the list. Each rule shows its "Government
// rule" stamp, its date and its title, and nothing else: no relation name, no passage chips, and its url never becomes a link (S2).
// A rule whose id fails the grammar keeps its title and date beside the words "Government rule", with no button (SR3).
import { Landmark } from "lucide-react";
import { useId } from "react";
import { deck } from "@/copy/deck";
import { numberOccurrences } from "@/lib/citations";
import { CITE_GROUP, type Rule } from "@/lib/company";
import { formatDate } from "@/lib/format";
import { Block, Cite } from "./CompanySection";

export function GovernmentRules({ rules, companyName }: { rules: Rule[]; companyName: string }) {
  const noticeId = useId();
  const w = deck.company.rules;
  const nths = numberOccurrences(rules.map((r) => (r.cite === null ? [] : [r.cite]))); // a rule listed twice keeps two keys (A2)
  return (
    <Block id="government-rules" title={w.heading}>
      <p id={noticeId} className="mt-0 mb-3 text-sm text-muted-foreground">
        {w.notice}
      </p>
      {rules.length === 0 ? (
        <p className="text-muted-foreground">{w.none(companyName)}</p>
      ) : (
        <ul aria-describedby={noticeId} className="m-0 list-none space-y-3 p-0">
          {rules.map((r, i) => (
            <li key={`${r.rule_id ?? ""}-${i}`} className="space-y-1">
              <p className="m-0 flex flex-wrap items-center gap-2 text-sm">
                {r.cite === null ? (
                  <span className="inline-flex items-center gap-1 font-semibold text-rule">
                    <Landmark aria-hidden="true" className="size-4" />
                    {deck.evidence.ruleLabel}
                  </span>
                ) : (
                  <Cite id={r.cite} group={CITE_GROUP.rules} nth={nths[i][0]} />
                )}
                {r.date !== null && formatDate(r.date) !== "" && <span className="text-muted-foreground">{formatDate(r.date)}</span>}
              </p>
              {r.target !== null && <p className="m-0 break-words">{r.target}</p>}
            </li>
          ))}
        </ul>
      )}
    </Block>
  );
}
