// R24 / A5 / S4 / S11: the one evidence drawer. It ports openDrawer / closeDrawer / openSeq / chipPlace of the legacy page. It is a
// non-modal complementary landmark: closed it is `inert` and `aria-hidden` (out of the tab order and the accessibility tree); open,
// focus moves to the named close button, Escape closes, the page stays operable (no focus trap, nothing made inert). It sits above
// the router (the chips of the what-changed view open the same one) and below the WorkspaceProvider, whose signals it follows:
// a new `generation` closes it and clears the cache, a new `docEvidenceEpoch` drops the cached `doc:` evidence (EvidenceCache).
// Evidence text is a text node; the one link is the safeUrl anchor.
import type { ReactNode } from "react";
import { chipLabel } from "@/lib/citations";
import { evidenceView } from "@/lib/evidenceView";
import { SAFE_REL } from "@/lib/safeUrl";
import {
  EVIDENCE_CLOSE_LABEL,
  EVIDENCE_CLOSE_TITLE,
  EVIDENCE_ERROR_PREFIX,
  EVIDENCE_ID_PREFIX,
  EVIDENCE_LABEL,
  EVIDENCE_LINK_ARROW,
  EVIDENCE_LOADING,
} from "@/pages/Ask.copy";
import { Badge } from "./Badges";
import { useDrawerController, type Content } from "./useDrawerController";
import { EvidenceDrawerContext } from "./useEvidenceDrawer";

function DrawerBody({ id, content }: { id: string; content: Content }) {
  if (content.phase !== "loaded") {
    const text = content.phase === "loading" ? EVIDENCE_LOADING : EVIDENCE_ERROR_PREFIX + content.message;
    return (
      <>
        <h3 className="text-base font-semibold">{`${EVIDENCE_LABEL} \u00b7 ${chipLabel(id)}`}</h3>
        <pre className="my-2 rounded-md border border-border bg-background p-3 text-sm whitespace-pre-wrap">{text}</pre>
      </>
    );
  }
  const view = evidenceView(id, content.payload);
  return (
    <>
      <h3 className="text-base font-semibold">{view.title}</h3>
      <div className="text-xs text-muted-foreground">
        {view.note}
        <div>{EVIDENCE_ID_PREFIX + view.id}</div>
      </div>
      {(view.fresh.badges.length > 0 || view.fresh.hint !== "") && (
        <div className="my-2">
          {view.fresh.badges.map((badge, index) => (
            <Badge key={index} badge={badge} />
          ))}
          {view.fresh.hint !== "" && <div className="mt-0.5 text-xs text-muted-foreground">{view.fresh.hint}</div>}
        </div>
      )}
      {view.facts.length > 0 && (
        <dl className="my-2.5 grid grid-cols-[max-content_1fr] gap-x-3.5 gap-y-1 text-sm">
          {view.facts.map((fact) => (
            <div key={fact.label} className="contents">
              <dt className="text-muted-foreground">{fact.label}</dt>
              <dd className="m-0 break-words">{fact.value}</dd>
            </div>
          ))}
        </dl>
      )}
      {view.text !== "" && <pre className="my-2 rounded-md border border-border bg-background p-3 text-sm whitespace-pre-wrap">{view.text}</pre>}
      {view.link !== null && (
        <p>
          <a className="text-primary underline underline-offset-4" href={view.link.href} target="_blank" rel={SAFE_REL}>
            {view.link.label + EVIDENCE_LINK_ARROW}
          </a>
        </p>
      )}
    </>
  );
}

export function EvidenceDrawerProvider({ children }: { children: ReactNode }) {
  const { api, content, aside, closeButton } = useDrawerController();
  const { openId, close } = api;
  return (
    <EvidenceDrawerContext.Provider value={api}>
      {children}
      <aside
        ref={aside}
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
          className="float-right cursor-pointer border-0 bg-transparent text-xl text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          {"\u00d7"}
        </button>
        {openId !== null && <DrawerBody id={openId} content={content} />}
      </aside>
    </EvidenceDrawerContext.Provider>
  );
}
