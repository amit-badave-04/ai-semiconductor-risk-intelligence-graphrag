// docs/specs/m5b-redesign.md RD21: a hash the router reports (the page opened at one, Back, Forward, a typed hash) focuses its
// section's heading once the sections are on the page, unless focus is in the evidence panel. The hash is matched against the
// fixed list of section ids only (lib/sectionHash.ts). Layout keys route focus on the pathname, so a hash never triggers it.
import { useEffect } from "react";
import { useLocation } from "react-router";
import { EVIDENCE_PANEL_ID } from "@/components/useEvidenceDrawer";
import { focusSection, sectionOf } from "@/lib/sectionHash";

const focusIsInThePanel = (): boolean => document.getElementById(EVIDENCE_PANEL_ID)?.contains(document.activeElement) ?? false;

/** Focus the section the router's hash names, once `ready` (the sections are rendered). */
export function useSectionFocus(ready: boolean): void {
  const { hash } = useLocation();
  useEffect(() => {
    if (!ready) return;
    const id = sectionOf(hash);
    if (id !== null && !focusIsInThePanel()) focusSection(id);
  }, [hash, ready]);
}
