// docs/specs/m5b-redesign.md RD4, SR3, SR9: a specimen source stamp in the Ask page's grounding sentence. A button that opens a
// click popover (WCAG 1.4.13 then does not apply) with a real example of that kind of source; focus moves in, and one Escape
// closes the popover only: while it is open a capture listener on the window handles Escape first and marks it handled, so
// the evidence drawer (which ignores a handled Escape) stays open, wherever focus is. Every value is rendered as a text node.
import { Popover } from "@base-ui/react/popover";
import { ChartNoAxesColumn, FileText, Landmark, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { deck } from "@/copy/deck";
import { SAFE_REL } from "@/lib/safeUrl";
import { specimenEvidence } from "@/lib/specimenCache";
import { SPECIMENS, specimenView, type SpecimenKind, type SpecimenView } from "@/lib/specimens";

const STAMPS: Record<SpecimenKind, { icon: LucideIcon; tone: string }> = {
  passage: { icon: FileText, tone: "border-brand-line bg-brand-fill text-primary" },
  number: { icon: ChartNoAxesColumn, tone: "border-brand-line bg-brand-fill text-primary" },
  rule: { icon: Landmark, tone: "border-rule-line bg-rule-fill text-rule" },
};

type Loaded = { kind: "loading" } | { kind: "ready"; view: SpecimenView } | { kind: "failed" };

/** The example of the current opening: each opening starts from "loading" (a failure is never shown again while a retry runs);
 *  a success comes from the page's cache at once. */
function useSpecimen(kind: SpecimenKind, opening: number): Loaded {
  const [result, setResult] = useState<{ opening: number; loaded: Loaded }>({ opening: 0, loaded: { kind: "loading" } });
  useEffect(() => {
    if (opening === 0) return;
    let current = true;
    void specimenEvidence(SPECIMENS[kind]).then((outcome) => {
      if (!current) return;
      const view = outcome.ok ? specimenView(kind, outcome.payload) : null;
      setResult({ opening, loaded: view === null ? { kind: "failed" } : { kind: "ready", view } });
    });
    return () => {
      current = false;
    };
  }, [kind, opening]);
  return result.opening === opening ? result.loaded : { kind: "loading" };
}

/** SR9: while open, Escape anywhere closes this popover first and is marked handled for every later listener. */
function useEscapeFirst(open: boolean, close: () => void) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || event.isComposing) return;
      event.preventDefault();
      close();
    };
    // window, capture phase: it runs before every listener on the document, Base UI's own included (which may close the popover
    // synchronously and so remove a document listener before it runs)
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [open, close]);
}

function Example({ loaded }: { loaded: Loaded }) {
  const w = deck.specimen;
  if (loaded.kind === "loading") return <p className="m-0 text-muted-foreground">{w.loading}</p>;
  if (loaded.kind === "failed") return <p className="m-0 text-muted-foreground">{w.failed}</p>;
  const { view } = loaded;
  return (
    <>
      {view.label !== undefined && (
        <p className="m-0 mb-1 inline-flex items-center gap-1 rounded-sm border border-rule-line bg-rule-fill px-1.5 text-xs font-semibold text-rule">
          <Landmark aria-hidden="true" className="size-3.5" />
          {view.label}
        </p>
      )}
      <Popover.Title className="m-0 text-base font-semibold">{view.heading}</Popover.Title>
      <p className="m-0 mt-0.5 text-muted-foreground">{view.meta}</p>
      {view.figure !== undefined && (
        <p className="m-0 mt-2">
          <span className="text-xl font-semibold">{view.figure.compact}</span>{" "}
          <span className="text-muted-foreground">{view.figure.exact}</span>
        </p>
      )}
      {view.quote !== undefined && (
        <blockquote className="m-0 mt-2 border-l-4 border-mark-edge pl-3 font-serif text-base leading-relaxed">
          {"\u201c"}
          {view.quote}
          {"\u201d"}
        </blockquote>
      )}
      {view.note !== undefined && <p className="m-0 mt-2 text-muted-foreground">{view.note}</p>}
      {view.link !== undefined && (
        <a href={view.link.href} target="_blank" rel={SAFE_REL} className="mt-2 inline-block text-primary underline underline-offset-4">
          {view.link.text}
        </a>
      )}
    </>
  );
}

export function SpecimenStamp({ kind }: { kind: SpecimenKind }) {
  const [open, setOpen] = useState(false);
  const [opening, setOpening] = useState(0);
  const loaded = useSpecimen(kind, opening);
  const close = useCallback(() => setOpen(false), []);
  useEscapeFirst(open, close);
  const onOpenChange = (next: boolean) => {
    if (next) setOpening((n) => n + 1);
    setOpen(next);
  };
  const { icon: Icon, tone } = STAMPS[kind];
  return (
    <Popover.Root open={open} onOpenChange={onOpenChange}>
      <Popover.Trigger
        className={`inline-flex min-h-[24px] cursor-pointer items-center gap-1 rounded-md border px-1.5 align-baseline font-semibold whitespace-nowrap hover:underline ${tone}`}
      >
        <Icon aria-hidden="true" className="size-4 shrink-0" />
        {deck.ask.stamp[kind]}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Positioner sideOffset={8} collisionPadding={16} className="z-20">
          {/* named in every state (loading, failed, ready); once ready the example's title names it */}
          <Popover.Popup aria-label={deck.specimen.dialogName(deck.ask.stamp[kind])} className="w-[min(26rem,calc(100vw-2rem))] rounded-lg border border-border-strong bg-card p-4 text-sm text-card-foreground shadow-lg">
            <Example loaded={loaded} />
            <div className="mt-3 text-right">
              <Popover.Close className="inline-flex min-h-11 items-center rounded-md px-3 font-semibold hover:bg-muted">
                {deck.specimen.close}
              </Popover.Close>
            </div>
          </Popover.Popup>
        </Popover.Positioner>
      </Popover.Portal>
    </Popover.Root>
  );
}
