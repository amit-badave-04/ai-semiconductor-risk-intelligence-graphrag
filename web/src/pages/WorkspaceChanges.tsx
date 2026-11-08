// R28, A1, S2, S11 (docs/specs/m5b-frontend.md): the what-changed view of the workspace page (index.html:491-530, 947-976). The
// report is data about an uploaded document: every headline and quote is a TEXT NODE, a chip appears only for an id that the
// parser (lib/changes.ts) matched against the generated grammar, and the only elements are p, ul, li, h3 and the chip's button
// inside one plain wrapper (the S2 allow-list of what-changed text). The chips are worker B's `Chip`, used through its frozen
// props; its drawer is the one above the router.
import { Fragment, useMemo } from "react";
import { Chip } from "@/components/Chips";
import type { ChangeItem, ChangeReport } from "@/lib/changes";
import {
  SECTION_ADDED,
  SECTION_CHANGED,
  SECTION_MINOR,
  SECTION_REMOVED,
  comparedHeader,
  firstVersion,
  loadingChanges,
  notCompared,
  skippedNote,
} from "./Workspace.copy";

export type ChangesView =
  | { kind: "idle" }
  | { kind: "loading"; older: number; newer: number }
  | { kind: "first"; newer: number }
  | { kind: "error"; detail: string }
  /** `seq` counts the reports shown, so a chip of a newer report never has the key of an older one's (A2). */
  | { kind: "report"; older: number; newer: number; report: ChangeReport; seq: number };

interface PlacedPassage {
  quote: string | null;
  chip: { id: string; nth: number } | null;
}
interface PlacedItem {
  label: string;
  passages: PlacedPassage[];
}
interface Section {
  title: string;
  items: PlacedItem[];
}

const LEFT_QUOTE = "\u201c";
const RIGHT_QUOTE = "\u201d";

/** The sections to show, with each chip's occurrence among the chips of its id counted across the whole report. */
function sectionsOf(report: Extract<ChangeReport, { kind: "compared" }>): { sections: Section[]; minor: string[] } {
  const seen = new Map<string, number>();
  const place = (items: ChangeItem[]): PlacedItem[] =>
    items.map((item) => ({
      label: item.label,
      passages: item.passages.map((p) => {
        if (p.chunkId === null) return { quote: p.quote, chip: null };
        const nth = seen.get(p.chunkId) ?? 0;
        seen.set(p.chunkId, nth + 1);
        return { quote: p.quote, chip: { id: p.chunkId, nth } };
      }),
    }));
  const all: [string, ChangeItem[]][] = [
    [SECTION_ADDED, report.added],
    [SECTION_REMOVED, report.removed],
    [SECTION_CHANGED, report.changed],
  ];
  const sections = all.filter(([, items]) => items.length > 0).map(([title, items]) => ({ title, items: place(items) }));
  return { sections, minor: report.minor };
}

function ItemView({ item, seq }: { item: PlacedItem; seq: number }) {
  return (
    <li>
      {item.label}
      {item.passages.map((passage, index) => (
        <p key={index} className="my-1">
          {passage.quote !== null ? `${LEFT_QUOTE}${passage.quote}${RIGHT_QUOTE}` : null}
          {passage.chip !== null ? (
            <Fragment>
              {passage.quote !== null ? " " : null}
              <Chip id={passage.chip.id} place="changes" gen={seq} nth={passage.chip.nth} />
            </Fragment>
          ) : null}
        </p>
      ))}
    </li>
  );
}

function ComparedView({ older, newer, report, seq }: { older: number; newer: number; report: Extract<ChangeReport, { kind: "compared" }>; seq: number }) {
  const { sections, minor } = useMemo(() => sectionsOf(report), [report]);
  return (
    <>
      <p className="text-sm text-muted-foreground">{comparedHeader(older, newer, report.unchanged)}</p>
      {sections.map((section) => (
        <Fragment key={section.title}>
          <h3 className="mt-3 text-sm font-semibold">{`${section.title} (${section.items.length})`}</h3>
          <ul className="ml-5 list-disc">
            {section.items.map((item, index) => (
              <ItemView key={index} item={item} seq={seq} />
            ))}
          </ul>
        </Fragment>
      ))}
      {minor.length > 0 ? (
        <>
          <h3 className="mt-3 text-sm font-semibold">{`${SECTION_MINOR} (${minor.length})`}</h3>
          <ul className="ml-5 list-disc">
            {minor.map((label, index) => (
              <li key={index}>{label}</li>
            ))}
          </ul>
        </>
      ) : null}
      {report.skipped > 0 ? <p className="text-sm text-muted-foreground">{skippedNote(report.skipped)}</p> : null}
    </>
  );
}

export function WorkspaceChanges({ view }: { view: ChangesView }) {
  if (view.kind === "idle") return null;
  let body;
  if (view.kind === "loading") body = <p className="text-sm text-muted-foreground">{loadingChanges(view.older, view.newer)}</p>;
  else if (view.kind === "first") body = <p className="text-sm text-muted-foreground">{firstVersion(view.newer)}</p>;
  else if (view.kind === "error") body = <p className="text-sm text-destructive">{view.detail}</p>;
  else if (view.report.kind === "unavailable") {
    body = <p className="text-sm text-muted-foreground">{notCompared(view.older, view.newer, view.report.reason)}</p>;
  } else body = <ComparedView older={view.older} newer={view.newer} report={view.report} seq={view.seq} />;
  return <div className="mt-3">{body}</div>;
}
