// docs/specs/m5b-redesign.md RD21 and SR10: the Company page's in-page sections. A URL hash is untrusted: it is compared with a
// fixed list of ids and only a match is looked up, with getElementById. It is never decoded, never used as a selector, and
// anything else is ignored.

/** The sections the navigation links to, in page order. */
export const SECTION_IDS = ["key-numbers", "government-rules", "relationships", "filings", "risk-changes"] as const;
export type SectionId = (typeof SECTION_IDS)[number];

/** The section a hash names ("#filings"), or null for anything else. */
export function sectionOf(hash: string): SectionId | null {
  const id = hash.startsWith("#") ? hash.slice(1) : hash;
  return (SECTION_IDS as readonly string[]).includes(id) ? (id as SectionId) : null;
}

/** Focus the heading of a section (its id is on the heading); false when it is not on the page. */
export function focusSection(id: SectionId): boolean {
  const heading = document.getElementById(id);
  if (heading === null) return false;
  heading.focus();
  return true;
}
