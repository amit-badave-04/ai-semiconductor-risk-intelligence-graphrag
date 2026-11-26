// M2 R19 (decision 009, the llm-council wording): one upload's card under its row: what we found in the file (preflight), "keyword
// search is ready", the progress of preparing search by meaning with its stop button, and the end or stopped line. Everything the
// server sent as text (form, section labels) is rendered as escaped text. The polite live region carries only the keyword-ready
// line, the start of the preparation, a progress line at most at each 25% step and at most once a minute, and the end line, so a
// screen reader is not interrupted at every progress event.
import { useEffect, useId, useRef, useState } from "react";
import { deck } from "@/copy/deck";
import { ANNOUNCE_START, announceStep, type AnnounceState, type JobView } from "@/lib/jobView";

const C = deck.jobs.card;

interface Props {
  view: JobView;
  /** The row ended (failed, connection lost...) without a final outcome: nothing is running any more. */
  terminal?: boolean;
  /** The id of the element holding the file's name; the stop button is described by it. */
  nameId?: string;
  /** Resolves false (or rejects) when the stop did not reach the server: the button is given back. */
  onCancel: () => Promise<boolean | void> | void;
}

function Preflight({ view }: { view: JobView }) {
  const pre = view.preflight;
  if (pre === null) return null;
  const eta = view.embedding?.etaS ?? null;
  return (
    <div data-preflight="">
      <p className="m-0 font-medium">{C.preflightHeading}</p>
      <p className="m-0">{C.passagesHelp}</p>
      <ul className="m-0 list-none p-0">
        <li>{C.form(pre.form)}</li>
        <li>{C.passages(pre.chunks)}</li>
        {view.outcome === null && <li>{eta !== null ? C.wait(eta) : C.waitUnknown}</li>}
      </ul>
      {pre.sections.length > 0 ? (
        <p className="m-0 mt-1">
          {C.sectionsFound}: <bdi>{pre.sections.join(", ")}</bdi>
        </p>
      ) : (
        <p className="m-0 mt-1">{C.noSections}</p>
      )}
    </div>
  );
}

function Progress({ view, nameId, onCancel }: Pick<Props, "view" | "nameId" | "onCancel">) {
  const { embedding } = view;
  const [stopping, setStopping] = useState(false);
  const helpId = useId();
  const stop = (): void => {
    if (stopping) return; // aria-disabled, not disabled: keyboard focus stays on the button
    setStopping(true);
    void Promise.resolve()
      .then(onCancel)
      .then((ok) => {
        if (ok === false) setStopping(false);
      })
      .catch(() => setStopping(false));
  };
  return (
    <div data-job-running="">
      <p className="m-0">{embedding === null ? C.waiting : C.progress(embedding.done, embedding.total)}</p>
      {embedding !== null && embedding.total > 0 && (
        <div
          role="progressbar"
          aria-label={C.progressName}
          aria-valuemin={0}
          aria-valuemax={embedding.total}
          aria-valuenow={Math.min(embedding.done, embedding.total)}
          className="mt-1 h-1.5 w-full max-w-xs overflow-hidden rounded bg-muted"
        >
          <div className="h-full bg-foreground" style={{ width: `${Math.min(100, (embedding.done / embedding.total) * 100)}%` }} />
        </div>
      )}
      <button
        type="button"
        onClick={stop}
        aria-disabled={stopping ? "true" : undefined}
        aria-describedby={nameId === undefined ? helpId : `${helpId} ${nameId}`}
        className="mt-1 rounded-md border border-input px-2 py-1 text-sm hover:bg-muted aria-disabled:opacity-70"
      >
        {stopping ? C.stopping : C.cancel}
      </button>
      <span id={helpId} className="sr-only">
        {C.cancelHelp}
      </span>
    </div>
  );
}

/** The note the screen reader hears while preparing: throttled by `announceStep`, which needs the clock. */
function useAnnouncement(view: JobView): string {
  const state = useRef<AnnounceState>(ANNOUNCE_START);
  const [note, setNote] = useState("");
  useEffect(() => {
    state.current = announceStep(state.current, view, Date.now());
    setNote(state.current.note);
  }, [view]);
  return note;
}

export function UploadJobCard({ view, terminal = false, nameId, onCancel }: Props) {
  const note = useAnnouncement(view);
  if (!view.keywordReady) return null;
  const running = view.outcome === null && !terminal;
  return (
    <div data-job-card="" className="mt-1 w-full space-y-2 text-sm text-muted-foreground">
      <Preflight view={view} />
      <div aria-live="polite" data-job-progress="">
        <p className="m-0 font-medium text-foreground">{C.keywordReady}</p>
        {running && note !== "" && <p className="sr-only">{note}</p>}
        {view.outcome !== null && <p className="m-0">{C.summary(view.outcome.embeddedChunks, view.outcome.keywordOnlyChunks, view.outcome.stopped)}</p>}
      </div>
      {running && <Progress view={view} nameId={nameId} onCancel={onCancel} />}
    </div>
  );
}
