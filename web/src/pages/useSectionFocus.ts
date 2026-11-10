// docs/specs/m5b-redesign.md RD21: a hash the router reports (the page opened at one, Back, Forward, a typed hash) focuses its
// section's heading once the sections are on the page, unless focus is in the evidence panel. The hash is matched against the
// fixed list of section ids only (lib/sectionHash.ts). Layout keys route focus on the pathname, so a hash never triggers it.
import { useEffect, useRef } from "react";
import { useLocation } from "react-router";
import { EVIDENCE_PANEL_ID } from "@/components/useEvidenceDrawer";
import { focusSection, sectionOf, SECTION_IDS } from "@/lib/sectionHash";

const focusIsInThePanel = (): boolean => document.getElementById(EVIDENCE_PANEL_ID)?.contains(document.activeElement) ?? false;

/** Nobody has placed focus yet: it is on the body, or on the page's h1, where route focus puts it (A7). */
const focusIsUnplaced = (): boolean => {
  const active = document.activeElement;
  return active === null || active === document.body || active.tagName === "H1";
};

/** Focus the section the router's hash names, once `ready` (the sections are rendered). The hash the page opened at moves focus
 *  only when nobody has placed it while the dossier loaded (R3 review A2); a later hash (Back, Forward, a typed one) always does,
 *  unless focus is in the evidence panel. */
export function useSectionFocus(ready: boolean, ids: readonly string[] = SECTION_IDS): void {
  const { hash } = useLocation();
  const first = useRef(true);
  useEffect(() => {
    if (!ready) return;
    const opening = first.current;
    first.current = false;
    const id = sectionOf(hash, ids);
    if (id === null || focusIsInThePanel()) return;
    if (opening && !focusIsUnplaced()) return;
    focusSection(id);
  }, [hash, ready, ids]);
}
