// The parts of the Ask page that have a job of their own: the examples panel, the notes under the answer, and the boundary that
// turns a failure of the page while it renders an answer into an honest status. All text is rendered as text nodes (S2, S11).
import { Component, useEffect, useState, type ReactNode } from "react";
import { groupExamples, costNote } from "@/lib/askStatus";
import type { Stats } from "@/lib/statsStore";
import { tracingNote } from "@/lib/steps";
import { SAFE_REL } from "@/lib/safeUrl";
import { EXAMPLES_LOADING, FOOTER_AFTER_COST, FOOTER_END, FOOTER_LEAD, FOOTER_LINK_HREF, FOOTER_LINK_TEXT, NO_EXAMPLES } from "./Ask.copy";
import { RELIABILITY, type Segment } from "./Method.copy";

/** A run of copy: plain text, bold runs and the static links of the pinned copy (opened in a new tab, safely). */
export function Segments({ segments }: { segments: readonly Segment[] }) {
  return (
    <>
      {segments.map((s, i) =>
        s.href !== undefined ? (
          <a key={i} className="text-primary underline underline-offset-4" href={s.href} target="_blank" rel={SAFE_REL}>
            {s.text}
          </a>
        ) : s.bold ? (
          <b key={i}>{s.text}</b>
        ) : (
          s.text
        ),
      )}
    </>
  );
}

type Groups = ReturnType<typeof groupExamples>;

async function loadExamples(): Promise<Groups> {
  try {
    const response = await fetch("/api/examples");
    return response.ok ? groupExamples(await response.json()) : [];
  } catch {
    return [];
  }
}

export interface ExamplesPanelProps {
  /** A workspace is open (R23) or an answer is streaming: an example would be a paid live call, or is ignored. */
  disabled: boolean;
  onPick: (question: string) => void;
}

/** The saved example questions, grouped by type. An example is asked on the hybrid path by its click (the caller's handler). */
export function ExamplesPanel({ disabled, onPick }: ExamplesPanelProps) {
  const [groups, setGroups] = useState<Groups | null>(null);
  useEffect(() => {
    let current = true;
    void loadExamples().then((loaded) => current && setGroups(loaded));
    return () => {
      current = false;
    };
  }, []);
  return (
    <div id="examples" className="flex flex-col items-start gap-1.5">
      {groups === null && <span className="text-sm text-muted-foreground">{EXAMPLES_LOADING}</span>}
      {groups?.length === 0 && <div className="text-sm text-muted-foreground">{NO_EXAMPLES}</div>}
      {groups?.map((group) => (
        <div key={group.type} className="flex flex-col items-start gap-1.5">
          <div data-example-type="" className="mt-2 text-xs tracking-wide text-muted-foreground uppercase">
            {group.type}
          </div>
          {group.questions.map((question, index) => (
            <button
              key={index}
              type="button"
              disabled={disabled}
              onClick={() => onPick(question)}
              className="cursor-pointer rounded-lg border border-transparent bg-muted px-2.5 py-2 text-left text-sm hover:border-primary disabled:cursor-not-allowed disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-ring"
            >
              {question}
            </button>
          ))}
        </div>
      ))}
    </div>
  );
}

/** What stays under every answer: the cost note, the privacy line of the agent, and how reliable change claims are. */
export function AskNotes({ stats }: { stats: Stats | null }) {
  const privacy = tracingNote(stats);
  return (
    <footer className="mt-6 text-xs text-muted-foreground">
      <p>
        {FOOTER_LEAD}
        <span id="costNote">{costNote(stats?.models)}</span>
        {FOOTER_AFTER_COST}
        <a className="text-primary underline underline-offset-4" href={FOOTER_LINK_HREF} target="_blank" rel={SAFE_REL}>
          {FOOTER_LINK_TEXT}
        </a>
        {FOOTER_END}
      </p>
      {privacy !== "" && (
        <p id="tracingNote" className="mt-2.5">
          {privacy}
        </p>
      )}
      <p id="reliability" className="mt-2.5">
        <Segments segments={RELIABILITY} />
      </p>
    </footer>
  );
}

interface BoundaryProps {
  /** What to show when rendering `children` threw. */
  fallback: ReactNode;
  /** Called once per failure, after it was caught. */
  onError: () => void;
  /** When this changes after a failure, the children are tried again. The boundary is NOT remounted for it: remounting the answer on
   *  every streamed delta would drop the keyboard focus of a chip. */
  resetKey?: string | number;
  children: ReactNode;
}

interface BoundaryState {
  failed: boolean;
  resetKey: string | number | undefined;
}

/** A page failure while rendering an answer or its checks is caught here, never left to blank the page, and reported to the page. */
export class RenderBoundary extends Component<BoundaryProps, BoundaryState> {
  override state: BoundaryState = { failed: false, resetKey: this.props.resetKey };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  /** A new resetKey after a failure lifts it (the children are tried again); the boundary itself is never remounted. */
  static getDerivedStateFromProps(props: BoundaryProps, state: BoundaryState): Partial<BoundaryState> | null {
    if (props.resetKey === state.resetKey) return null;
    return state.failed ? { failed: false, resetKey: props.resetKey } : { resetKey: props.resetKey };
  }

  override componentDidCatch(): void {
    this.props.onError();
  }

  override render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
