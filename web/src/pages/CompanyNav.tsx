// docs/specs/m5b-redesign.md RD21: one row of in-page links to the Company page's sections. On a phone the row scrolls sideways
// and never wraps. Following a link focuses the section's heading (a click focuses it even when the hash does not change); a
// hash the router reports (the page opened at one, Back, Forward, a typed hash) focuses it once the sections are on the page,
// unless focus is in the evidence panel. A hash change never triggers route focus (Layout keys that on the pathname only).
import { deck } from "@/copy/deck";
import { focusSection, SECTION_IDS, type SectionId } from "@/lib/sectionHash";

const LABELS: Record<SectionId, string> = {
  "key-numbers": deck.company.nav.keyNumbers,
  "government-rules": deck.company.nav.rules,
  relationships: deck.company.nav.relationships,
  filings: deck.company.nav.filings,
  "risk-changes": deck.company.nav.changes,
};

export function CompanyNav() {
  return (
    <nav aria-label={deck.company.nav.label} className="mb-6 border-b border-border">
      <ul className="m-0 flex list-none flex-nowrap gap-1 overflow-x-auto p-0 whitespace-nowrap">
        {SECTION_IDS.map((id) => (
          <li key={id} className="shrink-0">
            <a
              href={`#${id}`}
              onClick={() => focusSection(id)}
              className="inline-flex min-h-11 items-center rounded-md px-3 text-sm font-semibold text-primary hover:underline"
            >
              {LABELS[id]}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
