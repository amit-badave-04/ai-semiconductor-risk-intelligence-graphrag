// docs/specs/m5b-redesign.md RD21 and SR10: in-page sections (the Company page's, the Method page's). A URL hash is untrusted: it
// is compared with a fixed list of ids and only a match is looked up, with getElementById. It is never decoded, never used as a
// selector, and anything else is ignored.

/** The Company page's sections the navigation links to, in page order. */
export const SECTION_IDS = ["key-numbers", "government-rules", "relationships", "filings", "risk-changes"] as const;
export type SectionId = (typeof SECTION_IDS)[number];
/** The Method page's sections (R2 review A3: "How reliable change claims are" opens the reliability section). */
export const METHOD_SECTION_IDS = ["method-checks", "method-reliability"] as const;
/** Every section a hash may name when a route opens (components/Layout.tsx route focus). */
export const ROUTE_SECTION_IDS: readonly string[] = [...SECTION_IDS, ...METHOD_SECTION_IDS];

/** The section of `ids` a hash names ("#filings"), or null for anything else. */
export function sectionOf<T extends string>(hash: string, ids: readonly T[] = SECTION_IDS as unknown as readonly T[]): T | null {
  const id = hash.startsWith("#") ? hash.slice(1) : hash;
  return (ids as readonly string[]).includes(id) ? (id as T) : null;
}

/** Focus the heading of a section (its id is on the heading); false when it is not on the page. */
export function focusSection(id: string): boolean {
  const heading = document.getElementById(id);
  if (heading === null) return false;
  heading.focus();
  return true;
}
