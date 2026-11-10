import { useCallback, useEffect, useRef, useState } from "react";
import { AnswerMeta } from "@/components/Badges";
import { AnswerText } from "@/components/Chips";
import { PageHeading } from "@/components/Layout";
import { Steps } from "@/components/Steps";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { limitsText, statusLine } from "@/lib/askStatus";
import { ANSWER_HEADING_ID } from "@/lib/focusTargets";
import { PAGE_FAILED_STATUS } from "@/lib/sse";
import { getStats, refreshStats, useStats, type Stats } from "@/lib/statsStore";
import { agentEnabled } from "@/lib/steps";
import { useAsk, type AskContextValue } from "@/state/useAsk";
import { useWorkspace } from "@/state/useWorkspace";
import {
  AGENT_OPTION_LABEL,
  ASK_BUTTON,
  ASK_HEADING,
  EXAMPLES_HEADING,
  HYBRID_LABEL,
  MAX_QUESTION_CHARS,
  QUESTION_LABEL,
  QUESTION_PLACEHOLDER,
  STRATEGY_TITLE,
  VECTOR_LABEL,
} from "./Ask.copy";
import { AskNotes, ExamplesPanel, RenderBoundary, Segments } from "./AskParts";
import { AGENT_HINT, WITHDRAWN } from "./Method.copy";

// R22 / R23 / R31: the Ask page. The ask and its stream live in the AskProvider above the router; this page only reads them and
// starts an ask from an event handler (a click, Ctrl or Cmd with Enter), never from an effect. The strategy select offers `agent`
// only while /api/stats says so and, with a workspace open, only `hybrid`. The status line (A3) is derived from the ask state and is
// true on every exit; the answer text is not a live region.

interface Option {
  value: string;
  label: string;
}

/** The strategies on offer: hybrid always; vector and agent only without a workspace (a workspace ask on them is a guaranteed 400). */
function strategyOptions(stats: Stats | null, workspaceOpen: boolean): Option[] {
  const options = [{ value: "hybrid", label: HYBRID_LABEL }];
  if (workspaceOpen) return options;
  options.push({ value: "vector", label: VECTOR_LABEL });
  if (agentEnabled(stats)) options.push({ value: "agent", label: AGENT_OPTION_LABEL });
  return options;
}

function useCtrlEnter(submit: () => void) {
  const latest = useRef(submit);
  useEffect(() => {
    latest.current = submit;
  });
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") latest.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);
}

interface ControlsProps {
  question: string;
  onQuestion: (value: string) => void;
  options: Option[];
  strategy: string;
  onStrategy: (value: string) => void;
  streaming: boolean;
  onSubmit: () => void;
  stats: Stats | null;
}

/** The question box, the strategy select, the Ask button and the notes that belong to them (A4: every control has its label). */
function AskControls({ question, onQuestion, options, strategy, onStrategy, streaming, onSubmit, stats }: ControlsProps) {
  return (
    <>
      <Label htmlFor="q" className="sr-only">
        {QUESTION_LABEL}
      </Label>
      <Textarea id="q" value={question} maxLength={MAX_QUESTION_CHARS} placeholder={QUESTION_PLACEHOLDER} onChange={(e) => onQuestion(e.target.value)} className="min-h-22" />
      <div className="mt-2.5 flex flex-wrap items-center gap-2.5">
        <Label htmlFor="strategy" className="sr-only">
          {STRATEGY_TITLE}
        </Label>
        <select
          id="strategy"
          title={STRATEGY_TITLE}
          value={strategy}
          onChange={(e) => onStrategy(e.target.value)}
          className="max-w-full rounded-lg border border-input bg-background px-2.5 py-2 text-sm"
        >
          {options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <Button id="ask" type="button" disabled={streaming} onClick={onSubmit} className="hover:bg-primary hover:underline">
          {ASK_BUTTON}
        </Button>
        <span className="text-xs text-muted-foreground">{limitsText(stats)}</span>
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground">
        <Segments segments={WITHDRAWN} />
      </p>
      {agentEnabled(stats) && strategy === "agent" && (
        <p id="agentHint" className="mt-1.5 text-xs text-muted-foreground">
          {AGENT_HINT}
        </p>
      )}
    </>
  );
}

const ANSWER_CLASSES =
  "mt-2.5 leading-relaxed [&_h3]:mt-3.5 [&_h3]:mb-1.5 [&_h3]:text-base [&_h3]:font-semibold [&_li]:my-0.5 [&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5";

interface AnswerProps {
  ask: AskContextValue;
  stats: Stats | null;
  renderFailed: boolean;
  onRenderError: () => void;
}

/** The status line, the steps, the answer text and the footer of the answer; a failure of the page while rendering them is caught. */
function AnswerArea({ ask, stats, renderFailed, onRenderError }: AnswerProps) {
  const line = renderFailed && ask.statusText === null ? { text: PAGE_FAILED_STATUS, isError: true } : statusLine(ask);
  const stale = ask.done !== null && !ask.done.cached ? (ask.done.workspace?.stale_citations ?? []) : [];
  return (
    <>
      <output id="status" aria-live="polite" className={`mt-3 block min-h-5 text-sm ${line.isError ? "text-destructive" : "text-muted-foreground"}`}>
        {line.text}
      </output>
      <Steps steps={agentEnabled(stats) ? ask.steps : []} />
      <div id="answer" className={ANSWER_CLASSES}>
        <RenderBoundary key={ask.seq} resetKey={ask.text.length} fallback={<p>{ask.text}</p>} onError={onRenderError}>
          <AnswerText text={ask.text} place="answer" gen={ask.seq} staleIds={stale} />
        </RenderBoundary>
      </div>
      {ask.done !== null && !renderFailed && (
        <div id="meta">
          <RenderBoundary key={ask.seq} fallback={null} onError={onRenderError}>
            <AnswerMeta done={ask.done} defaultModel={stats?.models?.llm} />
          </RenderBoundary>
        </div>
      )}
    </>
  );
}

const CARD = "mb-4 rounded-xl bg-card p-5 ring-1 ring-foreground/10";

export function Ask() {
  const ask = useAsk();
  const stats = useStats();
  const { workspace } = useWorkspace();
  const [question, setQuestion] = useState("");
  const [preferred, setPreferred] = useState("hybrid");
  const [renderFailedAt, setRenderFailedAt] = useState<number | null>(null);
  const workspaceOpen = workspace !== null;
  const options = strategyOptions(stats, workspaceOpen);
  const strategy = options.some((o) => o.value === preferred) ? preferred : "hybrid";
  const streaming = ask.status === "streaming";

  useEffect(() => {
    if (getStats() === null) void refreshStats(); // the shell does not load stats: this page does, once; ask-finish refreshes
  }, []);
  const send = ask.ask;
  const submit = useCallback(() => send({ question, strategy }), [send, question, strategy]);
  useCtrlEnter(submit);
  const pick = (picked: string) => {
    setQuestion(picked);
    setPreferred("hybrid");
    send({ question: picked, strategy: "hybrid" });
  };

  return (
    <>
      <PageHeading title="Ask" />
      <section aria-labelledby="examples-heading" className={CARD}>
        <h2 id="examples-heading" className="mb-2 text-base font-semibold">
          {EXAMPLES_HEADING}
        </h2>
        <ExamplesPanel disabled={workspaceOpen || streaming} onPick={pick} />
      </section>
      <section aria-labelledby={ANSWER_HEADING_ID} className={CARD}>
        <h2 id={ANSWER_HEADING_ID} tabIndex={-1} className="mb-2 text-base font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">
          {ASK_HEADING}
        </h2>
        <AskControls question={question} onQuestion={setQuestion} options={options} strategy={strategy} onStrategy={setPreferred} streaming={streaming} onSubmit={submit} stats={stats} />
        <AnswerArea ask={ask} stats={stats} renderFailed={renderFailedAt === ask.seq && ask.seq > 0} onRenderError={() => setRenderFailedAt(ask.seq)} />
      </section>
      <AskNotes stats={stats} />
    </>
  );
}
