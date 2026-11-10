// The parts of the Ask page that have a job of their own: the notes at the foot of the page, and the boundary that
// turns a failure of the page while it renders an answer into an honest status. All text is rendered as text nodes (S2, S11).
import { Component, type ReactNode } from "react";
import { costNote } from "@/lib/askStatus";
import type { Stats } from "@/lib/statsStore";
import { tracingNote } from "@/lib/steps";
import { SAFE_REL } from "@/lib/safeUrl";
import { FOOTER_AFTER_COST, FOOTER_END, FOOTER_LEAD, FOOTER_LINK_HREF, FOOTER_LINK_TEXT } from "./Ask.copy";
import { RELIABILITY, WITHDRAWN, type Segment } from "./Method.copy";

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

/** The foot of the Ask page: the withdrawn-accuracy notice, the cost note, the privacy line of the agent, and how reliable change
 *  claims are (pinned legacy copy; docs/specs/m5b-redesign.md R4 rewords it). */
export function AskNotes({ stats }: { stats: Stats | null }) {
  const privacy = tracingNote(stats);
  return (
    <footer className="mt-12 border-t border-border pt-6 text-xs text-muted-foreground">
      <p>
        <Segments segments={WITHDRAWN} />
      </p>
      <p className="mt-2.5">
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
