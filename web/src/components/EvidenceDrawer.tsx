// R24 / A5 / S4 / S11: the one evidence drawer. It ports openDrawer / closeDrawer / openSeq / chipPlace of the legacy page. It is a
// non-modal complementary landmark: closed it is `inert` and `aria-hidden` (out of the tab order and the accessibility tree); open,
// focus moves to the named close button, Escape closes, the page stays operable (no focus trap, nothing made inert). It sits above
// the router (the chips of the what-changed view open the same one) and below the WorkspaceProvider, whose signals it follows:
// a new `generation` closes it and clears the cache, a new `docEvidenceEpoch` drops the cached `doc:` evidence (EvidenceCache).
// The body (EvidenceBody.tsx, docs/specs/m5b-redesign.md RD17, RD18) renders every value as a text node; the one link is safeUrl's.
import type { ReactNode } from "react";
import { EVIDENCE_CLOSE_LABEL, EVIDENCE_CLOSE_TITLE, EVIDENCE_LABEL } from "@/pages/Ask.copy";
import { EvidenceBody } from "./EvidenceBody";
import { useDrawerController } from "./useDrawerController";
import { EVIDENCE_PANEL_ID, EvidenceDrawerContext } from "./useEvidenceDrawer";

export function EvidenceDrawerProvider({ children }: { children: ReactNode }) {
  const { api, content, quotes, aside, closeButton } = useDrawerController();
  const { openId, close } = api;
  return (
    <EvidenceDrawerContext.Provider value={api}>
      {children}
      <aside
        ref={aside}
        id={EVIDENCE_PANEL_ID}
        aria-label={EVIDENCE_LABEL}
        {...(openId !== null ? {} : { inert: true, "aria-hidden": true, hidden: true })}
        // Until R2 (docs/specs/m5b-redesign.md RD16, a sticky column): on wide screens it starts below the one-row header (lg:h-14
        // in Layout.tsx), so the header links stay usable while evidence is open.
        className="fixed top-0 right-0 z-10 h-full w-[min(560px,100%)] overflow-auto border-l border-border bg-card p-5 shadow-lg lg:top-14 lg:h-[calc(100%-3.5rem)]"
      >
        <button
          ref={closeButton}
          type="button"
          aria-label={EVIDENCE_CLOSE_LABEL}
          title={EVIDENCE_CLOSE_TITLE}
          onClick={() => close()}
          className="float-right cursor-pointer border-0 bg-transparent text-xl text-muted-foreground"
        >
          {"\u00d7"}
        </button>
        {openId !== null && <EvidenceBody id={openId} content={content} quotes={quotes} />}
      </aside>
    </EvidenceDrawerContext.Provider>
  );
}
