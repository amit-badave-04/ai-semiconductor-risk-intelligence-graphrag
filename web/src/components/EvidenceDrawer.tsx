// R24 / A5 / S4 / S11: the one evidence drawer. It ports openDrawer / closeDrawer / openSeq / chipPlace of the legacy page. It is a
// non-modal complementary landmark: closed it is `inert` and `aria-hidden` (out of the tab order and the accessibility tree); open,
// focus moves to the named close button, Escape closes, the page stays operable (no focus trap, nothing made inert). It sits above
// the router (the chips of the what-changed view open the same one) and below the WorkspaceProvider, whose signals it follows:
// a new `generation` closes it and clears the cache, a new `docEvidenceEpoch` drops the cached `doc:` evidence (EvidenceCache).
// The body (EvidenceBody.tsx, docs/specs/m5b-redesign.md RD17, RD18) renders every value as a text node; the one link is safeUrl's.
// Layout (RD16): a bottom sheet below 900 px, a full-height column from 900 px. While it is open the root element carries
// `data-evidence`, and index.css pads the page by the sheet's height or the column's width, so it covers nothing (WCAG 2.4.11);
// the chip that opened it is then scrolled into view. The header with Close stays put while the evidence scrolls.
import { X } from "lucide-react";
import { useLayoutEffect, type ReactNode } from "react";
import { deck } from "@/copy/deck";
import { EvidenceBody } from "./EvidenceBody";
import { useDrawerController } from "./useDrawerController";
import { EVIDENCE_PANEL_ID, EvidenceDrawerContext } from "./useEvidenceDrawer";

const OPEN_MARK = "data-evidence";

const PANEL =
  "fixed inset-x-0 bottom-0 z-20 flex h-[var(--evidence-sheet)] flex-col rounded-t-lg border-t border-border-strong bg-card shadow-lg " +
  "min-[900px]:inset-x-auto min-[900px]:top-0 min-[900px]:right-0 min-[900px]:h-auto min-[900px]:w-[var(--evidence-column)] " +
  "min-[900px]:rounded-none min-[900px]:border-t-0 min-[900px]:border-l";

/** While the panel is open: mark the root element (the page makes room), then bring the chip that opened it into view. */
function useMakeRoom(isOpen: boolean, openKey: string | null) {
  useLayoutEffect(() => {
    if (!isOpen) return;
    const root = document.documentElement;
    root.setAttribute(OPEN_MARK, "");
    return () => root.removeAttribute(OPEN_MARK);
  }, [isOpen]);
  useLayoutEffect(() => {
    if (!isOpen || openKey === null) return;
    const opener = document.querySelector(`[data-chip-key="${openKey.replace(/["\\]/g, "\\$&")}"]`);
    if (opener !== null && typeof opener.scrollIntoView === "function") opener.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [isOpen, openKey]);
}

export function EvidenceDrawerProvider({ children }: { children: ReactNode }) {
  const { api, content, quotes, aside, closeButton } = useDrawerController();
  const { openId, openKey, close } = api;
  useMakeRoom(openId !== null, openKey);
  return (
    <EvidenceDrawerContext.Provider value={api}>
      {children}
      <aside
        ref={aside}
        id={EVIDENCE_PANEL_ID}
        aria-label={deck.evidence.title}
        {...(openId !== null ? {} : { inert: true, "aria-hidden": true, hidden: true })}
        className={PANEL}
      >
        <div className="flex items-center justify-between gap-4 border-b border-border px-5 py-1">
          <h2 className="m-0 text-base font-semibold">{deck.evidence.title}</h2>
          <button
            ref={closeButton}
            type="button"
            aria-label={deck.evidence.closeName}
            onClick={() => close()}
            className="inline-flex min-h-[44px] min-w-[44px] cursor-pointer items-center gap-1 rounded-md border-0 bg-transparent px-3 font-semibold text-primary hover:underline"
          >
            <X aria-hidden="true" className="size-4" />
            {deck.evidence.close}
          </button>
        </div>
        <div data-evidence-body="" className="min-h-0 flex-1 overflow-auto p-5">
          {openId !== null && <EvidenceBody id={openId} content={content} quotes={quotes} />}
        </div>
      </aside>
    </EvidenceDrawerContext.Provider>
  );
}
