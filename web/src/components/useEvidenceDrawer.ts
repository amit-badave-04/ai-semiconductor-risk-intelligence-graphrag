// FROZEN CONTRACT (I3 main-first, 2026-11-08): the evidence drawer's API, as worker B's EvidenceDrawerProvider
// (components/EvidenceDrawer.tsx) provides it. Worker B owns the drawer (R24, A5, S4, S11); worker C2's what-changed view uses
// `Chip` (components/Chips.tsx); main's Layout calls `close` when the route changes. The types, the hook and the behaviour below
// do not change without main.
//
// WHO CLEARS THE CACHED EVIDENCE: the drawer, and only the drawer. It sits BELOW WorkspaceProvider, so it reads the workspace's
// signals (state/useWorkspace.ts) and acts on them itself: a new `generation` (the workspace identity changed) closes the drawer
// and clears everything; a new `docEvidenceEpoch` (identity change, or `invalidateDocEvidence()` from a job watcher, whichever
// route is showing) drops the `doc:` entries and refuses to cache a `doc:` fetch that began earlier, which `EvidenceCache`
// (lib/evidenceCache.ts) implements. Nobody else, worker C2's watchers included, calls `dropDocEvidence` or `clearAll`: they call
// `invalidateDocEvidence()` on the workspace context, because from inside WorkspaceProvider this hook is the inert one.
//
// One component owns the drawer (R24) and it sits above the router, so the chips inside what-changed (R28) open the same one.
// Closed, the drawer is `<aside aria-label="Evidence" inert aria-hidden="true">`; open, a non-modal complementary landmark
// (A5): focus moves to the named close button, Escape closes, the page stays operable (no focus trap, nothing made inert).
import { createContext, useContext } from "react";
import type { ChipPlace } from "@/lib/citations";

/** The id of the evidence panel element (the `<aside>`), which every chip names with aria-controls (RD11). */
export const EVIDENCE_PANEL_ID = "evidence-panel";

export interface EvidenceDrawerApi {
  /** The citation id the drawer shows, or null when closed. */
  openId: string | null;
  /**
   * The chip key (data-chip-key) of the occurrence whose evidence is open, or null: that chip, and only that one, says
   * aria-expanded="true" (docs/specs/m5b-redesign.md RD11). An `open` with no opener keeps the previous key. (Additive, R2.)
   */
  openKey: string | null;
  /**
   * Open the drawer for a chip. `id` came from untrusted model text: it is fetched only after an anchored match of the
   * generated grammar (classifyId), with encodeURIComponent, and a `doc:` id with no workspace open makes NO request (S4).
   * `opener` is the chip element, kept so focus can return to it on close (A2: by chip key, even after the answer re-rendered;
   * when the chip is gone, focus goes to the answer heading). A second `open` while open replaces the content (openSeq rule).
   * `quotes` (optional, additive in R2) are the words the answer quoted right before the chip, which the panel highlights where
   * the passage holds them exactly (RD18); they are untrusted text and never reach the request.
   */
  open: (id: string, opener: HTMLElement | null, place: ChipPlace, quotes?: readonly string[]) => void;
  /** Close the drawer. `restoreFocus` defaults to true; a route change passes false (the new page's h1 takes focus, A7). */
  close: (options?: { restoreFocus?: boolean }) => void;
  /** Forget cached evidence of `doc:` ids. The drawer's own reaction to `docEvidenceEpoch`; no other component calls it. */
  dropDocEvidence: () => void;
  /** Close the drawer and forget every cached evidence. The drawer's own reaction to `generation`; no other component calls it. */
  clearAll: () => void;
}

const INERT: EvidenceDrawerApi = {
  openId: null,
  openKey: null,
  open: () => {},
  close: () => {},
  dropDocEvidence: () => {},
  clearAll: () => {},
};

export const EvidenceDrawerContext = createContext<EvidenceDrawerApi>(INERT);

/** The drawer API. Outside a provider it does nothing (keeps component tests simple). */
export function useEvidenceDrawer(): EvidenceDrawerApi {
  return useContext(EvidenceDrawerContext);
}
