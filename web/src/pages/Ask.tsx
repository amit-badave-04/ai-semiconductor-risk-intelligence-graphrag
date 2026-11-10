import { useCallback, useEffect, useState, type KeyboardEvent } from "react";
import { ReadWithCare, SourcesList } from "@/components/AnswerNotes";
import { AnswerSample } from "@/components/AnswerSample";
import { AnswerMeta } from "@/components/Badges";
import { AnswerText } from "@/components/Chips";
import { PageHeading } from "@/components/Layout";
import { Steps } from "@/components/Steps";
import { WhatWasChecked } from "@/components/WhatWasChecked";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { deck } from "@/copy/deck";
import { liveLimits, statusView, type StatusLine } from "@/lib/askStatus";
import { depthOptions, type Depth } from "@/lib/depth";
import { ANSWER_HEADING_ID } from "@/lib/focusTargets";
import { FILERS } from "@/lib/pages.generated";
import { PAGE_FAILED_STATUS } from "@/lib/sse";
import { getStats, refreshStats, useStats, type Stats } from "@/lib/statsStore";
import { agentEnabled } from "@/lib/steps";
import { useAsk, type AskContextValue } from "@/state/useAsk";
import { useWorkspace } from "@/state/useWorkspace";
import { MAX_QUESTION_CHARS } from "./Ask.copy";
import { CompaniesCovered, CostLine, Grounding, MoreOptions, Starters } from "./AskFirstScreen";
import { AskNotes, RenderBoundary } from "./AskParts";
import { AGENT_HINT } from "./Method.copy";

// R22 / R23 / R31, laid out by docs/specs/m5b-redesign.md RD4 to RD9. The ask and its stream live in the AskProvider above the
// router; this page only reads them and starts an ask from an event handler (a click, Ctrl or Cmd with Enter), never from an
// effect. Depth offers `agent` only while /api/stats says so and, with a workspace open, only Standard (`hybrid`). The status line
// (A3) is derived from the ask state and is true on every exit; the answer text is not a live region.

const EMPTY_NOTE_ID = "q-empty";

interface ControlsProps {
  question: string;
  onQuestion: (value: string) => void;
  streaming: boolean;
  onSubmit: () => void;
  emptyShown: boolean;
}

/** RD4: the labelled question box with the Ask button beside it, and RD8's note when Ask was pressed with nothing typed. Ctrl or
 *  Cmd with Enter asks from the box (only there: elsewhere the same keys open a link in a new tab, for example). */
function AskControls({ question, onQuestion, streaming, onSubmit, emptyShown }: ControlsProps) {
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !event.nativeEvent.isComposing) {
      event.preventDefault();
      onSubmit();
    }
  };
  return (
    <>
      {/* the box shows the 3 px focus ring (RD1) while the textarea inside it has focus */}
      <div className="flex flex-col gap-3 rounded-xl border border-input bg-card p-3 has-[textarea:focus-visible]:outline-3 has-[textarea:focus-visible]:outline-offset-2 has-[textarea:focus-visible]:outline-ring has-[textarea:focus-visible]:outline-solid sm:flex-row sm:items-end">
        <Textarea
          id="q"
          value={question}
          maxLength={MAX_QUESTION_CHARS}
          placeholder={deck.ask.placeholder}
          onChange={(e) => onQuestion(e.target.value)}
          onKeyDown={onKeyDown}
          aria-keyshortcuts="Control+Enter Meta+Enter"
          aria-invalid={emptyShown || undefined}
          aria-describedby={emptyShown ? EMPTY_NOTE_ID : undefined}
          className="min-h-20 border-0 bg-transparent text-lg shadow-none focus-visible:outline-none md:text-lg dark:bg-transparent"
        />
        {/* hover keeps the full primary colour (a lighter one drops white text under 4.5:1) and adds an underline */}
        {/* focusable while an answer streams (aria-disabled), so a keyboard user's focus is not dropped to the page */}
        <Button
          id="ask"
          type="button"
          disabled={streaming}
          focusableWhenDisabled
          onClick={onSubmit}
          className="h-11 px-6 text-base hover:bg-primary hover:underline aria-disabled:opacity-60"
        >
          {deck.ask.askButton}
        </Button>
      </div>
      {emptyShown && (
        <p id={EMPTY_NOTE_ID} className="mt-2 text-sm font-semibold text-destructive">
          {deck.ask.emptyQuestion}
        </p>
      )}
    </>
  );
}

const ANSWER_CLASSES =
  "mt-2.5 leading-relaxed [&_h3]:mt-3.5 [&_h3]:mb-1.5 [&_h3]:text-base [&_h3]:font-semibold [&_li]:my-0.5 [&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5";

/** The status line of the form (A3, A10: next to the Ask button); a new announceKey puts the line into the live region as a new
 *  node, so a repeated message is announced again. */
function StatusOutput({ line, announceKey }: { line: StatusLine; announceKey: number }) {
  return (
    <output id="status" aria-live="polite" className={`mt-3 block min-h-5 text-sm ${line.isError ? "text-destructive" : "text-muted-foreground"}`}>
      <span key={announceKey}>{line.text}</span>
    </output>
  );
}

interface AnswerProps {
  ask: AskContextValue;
  stats: Stats | null;
  renderFailed: boolean;
  onRenderError: () => void;
}

/** RD10: the answer, headed by its question (the focus target, A5), with the steps, the text and the footer; a failure of the
 *  page while rendering them is caught. */
function AnswerArea({ ask, stats, renderFailed, onRenderError }: AnswerProps) {
  const stale = ask.done !== null && !ask.done.cached ? (ask.done.workspace?.stale_citations ?? []) : [];
  return (
    <section aria-labelledby={ANSWER_HEADING_ID} className="mt-6">
      <h2 id={ANSWER_HEADING_ID} tabIndex={-1} className="m-0 text-2xl font-semibold tracking-tight break-words">
        {ask.question}
      </h2>
      <Steps steps={agentEnabled(stats) ? ask.steps : []} />
      <div id="answer" className={ANSWER_CLASSES}>
        <RenderBoundary key={ask.seq} resetKey={ask.text.length} fallback={<p>{ask.text}</p>} onError={onRenderError}>
          <AnswerText text={ask.text} place="answer" gen={ask.seq} staleIds={stale} />
        </RenderBoundary>
      </div>
      {ask.done !== null && !renderFailed && (
        <div id="meta">
          <RenderBoundary key={ask.seq} fallback={null} onError={onRenderError}>
            {/* RD13 under the answer, not beside each claim (AnswerText's markup is pinned by R21): recorded in the spec Status */}
            <ReadWithCare text={ask.text} />
            <SourcesList text={ask.text} gen={ask.seq} staleIds={stale} />
            <WhatWasChecked done={ask.done} />
            {/* RD14: the legacy badges and the checks hint, for analysts */}
            <details className="mt-3 text-sm">
              <summary className="cursor-pointer font-semibold text-muted-foreground">{deck.checked.technical}</summary>
              <AnswerMeta done={ask.done} defaultModel={stats?.models?.llm} />
            </details>
          </RenderBoundary>
        </div>
      )}
    </section>
  );
}

export function Ask() {
  const ask = useAsk();
  const stats = useStats();
  const { workspace } = useWorkspace();
  const [question, setQuestion] = useState("");
  const [preferred, setPreferred] = useState<Depth>("hybrid");
  const [emptyCount, setEmptyCount] = useState(0);
  const emptyShown = emptyCount > 0;
  const [renderFailedAt, setRenderFailedAt] = useState<number | null>(null);
  const workspaceOpen = workspace !== null;
  const options = depthOptions(agentEnabled(stats), workspaceOpen);
  const strategy = options.some((opt) => opt.value === preferred) ? preferred : "hybrid";
  const streaming = ask.status === "streaming";

  useEffect(() => {
    if (getStats() === null) void refreshStats(); // the shell does not load stats: this page does, once; ask-finish refreshes
  }, []);
  const send = ask.ask;
  const submit = useCallback(() => {
    if (streaming) return; // the answer in progress keeps its status line
    if (question.trim() === "") {
      // RD8: nothing is sent; each empty Ask counts, and the count keys the status text so a repeat is announced again
      setEmptyCount((n) => n + 1);
      return;
    }
    send({ question, strategy });
  }, [send, question, strategy, streaming]);
  const onQuestion = (value: string) => {
    setQuestion(value);
    setEmptyCount(0);
  };
  const pick = (picked: string) => {
    onQuestion(picked);
    setPreferred("hybrid");
    send({ question: picked, strategy: "hybrid" });
  };

  const renderFailed = renderFailedAt === ask.seq && ask.seq > 0;
  // RD10: once a question is asked, the answer is headed by it (keyed on the question, never on the status, which goes back to
  // idle after an abort)
  const answered = ask.seq > 0 && ask.question.trim() !== "";
  const view = statusView(ask);
  const line: StatusLine = emptyShown
    ? { text: deck.ask.emptyQuestion, isError: true }
    : renderFailed && ask.statusText === null
      ? { text: PAGE_FAILED_STATUS, isError: true }
      : { text: deck.status.line(view), isError: view.isError };

  return (
    <>
      <PageHeading title={deck.ask.heading(FILERS.length)} documentTitle={deck.nav.ask} display />
      {/* Two columns from lg: the question form beside RD6's sample, then the starters beside the companies (RD9). One column on a
          phone, in the same order: form, sample, starters, companies. */}
      <div className="grid gap-x-12 gap-y-8 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0">
          <Grounding />
          {/* not a named region: its name would be "Your question" too, the same as the box (one name, one control) */}
          <section>
            <h2 id={answered ? undefined : ANSWER_HEADING_ID} tabIndex={-1} className="mb-2 text-base font-semibold">
              <label htmlFor="q">{deck.ask.questionLabel}</label>
            </h2>
            <AskControls question={question} onQuestion={onQuestion} streaming={streaming} onSubmit={submit} emptyShown={emptyShown} />
            <CostLine costLine={deck.ask.costLine(liveLimits(stats))} />
            <MoreOptions
              options={options}
              depth={strategy}
              onDepth={setPreferred}
              workspaceOpen={workspaceOpen}
              note={
                strategy === "agent" ? (
                  <p id="agentHint" className="mt-1 text-sm text-muted-foreground">
                    {AGENT_HINT}
                  </p>
                ) : null
              }
            />
            <StatusOutput line={line} announceKey={emptyCount} />
          </section>
          {answered && <AnswerArea ask={ask} stats={stats} renderFailed={renderFailed} onRenderError={() => setRenderFailedAt(ask.seq)} />}
        </div>
        {/* RD6: the sample shows what an answer looks like until a real one is on the page */}
        <div className="lg:pt-14">{!answered && <AnswerSample />}</div>
        <div className="min-w-0">
          <Starters off={workspaceOpen ? "workspace" : streaming ? "busy" : null} onPick={pick} />
        </div>
        <CompaniesCovered />
      </div>
      <AskNotes stats={stats} />
    </>
  );
}
