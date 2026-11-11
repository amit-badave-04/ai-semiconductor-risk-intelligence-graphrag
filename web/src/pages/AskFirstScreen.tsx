// docs/specs/m5b-redesign.md RD4 to RD9: the parts of the Ask page a reader meets before asking: the grounding sentence with its
// three source stamps, the cost line, "More options" (Depth), the six starters with "Show all examples", and the covered
// companies. All copy comes from copy/deck.ts; every server value is rendered as a text node (S2).
import { Collapsible } from "@base-ui/react/collapsible";
import { ChevronRight } from "lucide-react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { SpecimenStamp } from "@/components/SpecimenStamp";
import { deck } from "@/copy/deck";
import { FILERS } from "@/lib/pages.generated";
import { type Depth, type DepthOption } from "@/lib/depth";
import { STARTERS, splitExamples, type Example, type StarterGroup, type StarterJob } from "@/lib/starters";

// ------------------------------------------------------------------ RD4: the grounding sentence and its source stamps

export function Grounding() {
  const w = deck.ask;
  return (
    <p className="mb-6 max-w-[46rem] text-lg leading-relaxed text-muted-foreground">
      {w.groundingLead}
      <SpecimenStamp kind="passage" />
      {w.groundingBetween}
      <SpecimenStamp kind="number" />
      {w.groundingBeforeLast}
      <SpecimenStamp kind="rule" />
      {w.groundingEnd}
    </p>
  );
}

// ------------------------------------------------------------------ RD4a: the cost line

export function CostLine({ costLine }: { costLine: string }) {
  const w = deck.ask;
  return (
    <p className="mt-3 max-w-[46rem] text-sm text-muted-foreground">
      <span className="font-semibold text-foreground">{costLine}</span> {w.examplesFree} {w.notAdvice}{" "}
      <Link to="/method/" className="text-primary underline underline-offset-4">
        {w.howLink}
      </Link>
    </p>
  );
}

// ------------------------------------------------------------------ RD7: More options, Depth

const o = deck.options;

interface MoreOptionsProps {
  options: DepthOption[];
  depth: Depth;
  onDepth: (depth: Depth) => void;
  workspaceOpen: boolean;
  /** shown under the group when the chosen depth has a note of its own (the agent's hint) */
  note?: ReactNode;
}

function DepthRadio({ option, checked, onDepth }: { option: DepthOption; checked: boolean; onDepth: (d: Depth) => void }) {
  const id = useId();
  return (
    <div className="flex items-start gap-3 py-1.5">
      <input
        id={id}
        type="radio"
        name="depth"
        value={option.value}
        checked={checked}
        onChange={() => onDepth(option.value)}
        aria-describedby={`${id}-hint`}
        className="mt-1 size-5 shrink-0 accent-primary"
      />
      <div>
        <label htmlFor={id} className="font-semibold">
          {option.label}
        </label>
        <p id={`${id}-hint`} className="m-0 text-sm text-muted-foreground">
          {option.hint}
        </p>
      </div>
    </div>
  );
}

export function MoreOptions({ options, depth, onDepth, workspaceOpen, note }: MoreOptionsProps) {
  const current = options.find((opt) => opt.value === depth) ?? options[0];
  return (
    <Collapsible.Root className="mt-2">
      <Collapsible.Trigger className="group inline-flex min-h-11 items-center gap-1.5 font-semibold">
        <ChevronRight aria-hidden="true" className="size-4 group-data-[panel-open]:rotate-90" />
        {/* N4: one inline span, so the space is rendered (as two flex items NVDA read "More optionsDepth: Standard") */}
        <span>
          {o.toggle} <span className="font-normal text-muted-foreground">{o.summary(current.name)}</span>
        </span>
      </Collapsible.Trigger>
      <Collapsible.Panel className="mt-1 mb-2 pl-6">
        <fieldset className="m-0 border-0 p-0">
          <legend className="mb-1 text-sm font-semibold">{o.legend}</legend>
          {options.map((option) => (
            <DepthRadio key={option.value} option={option} checked={option.value === current.value} onDepth={onDepth} />
          ))}
        </fieldset>
        {workspaceOpen && <p className="mt-1 text-sm text-muted-foreground">{o.workspaceOnlyStandard}</p>}
        {note}
      </Collapsible.Panel>
    </Collapsible.Root>
  );
}

// ------------------------------------------------------------------ RD5: starters and every example

type Loaded = { kind: "loading" } | { kind: "loaded"; payload: unknown } | { kind: "failed" };

/** GET /api/examples once; a failure is said as one, not as "no examples" (the page works without them). */
function useExamples(): Loaded {
  const [state, setState] = useState<Loaded>({ kind: "loading" });
  useEffect(() => {
    let current = true;
    const load = async (): Promise<Loaded> => {
      try {
        const response = await fetch("/api/examples");
        return response.ok ? { kind: "loaded", payload: (await response.json()) as unknown } : { kind: "failed" };
      } catch {
        return { kind: "failed" };
      }
    };
    void load().then((next) => current && setState(next));
    return () => {
      current = false;
    };
  }, []);
  return state;
}

/** Why the questions cannot be asked now: a workspace is open (R23: each would be a paid live call; the buttons are disabled and
 *  say why), or an answer is streaming (busy: aria-disabled, so a keyboard user's focus stays on the pressed button). */
export type StartersOff = "workspace" | "busy" | null;

interface PickProps {
  off: StartersOff;
  onPick: (question: string) => void;
  /** the id of the sentence that says why the questions are off */
  reasonId?: string;
}

const QUESTION_BUTTON =
  "min-h-11 w-full cursor-pointer py-2.5 text-left hover:text-primary hover:underline disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-60";

function QuestionList({ examples, off, onPick, reasonId }: PickProps & { examples: Example[] }) {
  return (
    <ul className="m-0 list-none border-t border-border p-0">
      {examples.map((e, i) => (
        <li key={`${e.id}-${i}`} className="border-b border-border">
          <button
            type="button"
            disabled={off === "workspace"}
            aria-disabled={off === "busy" || undefined}
            aria-describedby={off === "workspace" ? reasonId : undefined}
            onClick={() => off === null && onPick(e.question)}
            className={QUESTION_BUTTON}
          >
            {e.question}
          </button>
        </li>
      ))}
    </ul>
  );
}

const s = deck.starters;
const JOB_WORDS = {
  company: [s.company, s.companyProof],
  number: [s.number, s.numberProof],
  rules: [s.rules, s.rulesProof],
  changes: [s.changes, s.changesProof],
} as const;

/** Until /api/examples answers, each job shows its heading and an empty row per curated starter, so the page below (the
 *  companies, the footer) does not move when the questions arrive (CLS, RD4). */
const RESERVED: StarterGroup[] = (["company", "number", "rules", "changes"] as StarterJob[]).map((job) => ({
  job,
  examples: STARTERS.filter((st) => st.job === job).map((st) => ({ id: st.id, question: "" })),
}));

function ReservedRows({ count }: { count: number }) {
  return (
    <ul aria-hidden="true" className="m-0 list-none border-t border-border p-0">
      {Array.from({ length: count }, (_, i) => (
        <li key={i} className="min-h-11 border-b border-border" />
      ))}
    </ul>
  );
}

export function Starters({ off, onPick }: Omit<PickProps, "reasonId">) {
  const loaded = useExamples();
  const headingId = useId();
  const reasonId = useId();
  const split = loaded.kind === "loaded" ? splitExamples(loaded.payload) : loaded.kind === "failed" ? { groups: [], byCompany: [] } : null;
  const groups = split?.groups ?? RESERVED;
  const byCompany = split?.byCompany ?? [];
  return (
    <section aria-labelledby={headingId}>
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3">
        <h2 id={headingId} className="m-0 text-xl font-semibold">
          {s.heading}
        </h2>
        <p className="m-0 text-sm text-muted-foreground">{s.note}</p>
      </div>
      {off === "workspace" && (
        <p id={reasonId} className="mb-3 text-sm text-muted-foreground">
          {s.workspaceOff}
        </p>
      )}
      {split === null && <p className="sr-only">{s.loading}</p>}
      {loaded.kind === "failed" && <p className="text-sm text-muted-foreground">{s.failed}</p>}
      {loaded.kind === "loaded" && groups.length === 0 && byCompany.length === 0 && <p className="text-sm text-muted-foreground">{s.none}</p>}
      <div className="grid gap-x-8 gap-y-6 sm:grid-cols-2">
        {groups.map((group) => (
          <div key={group.job}>
            <h3 className="m-0 text-base font-semibold text-primary">{JOB_WORDS[group.job][0]}</h3>
            <p className="mt-0.5 mb-2 text-sm text-muted-foreground">{JOB_WORDS[group.job][1]}</p>
            {split === null ? (
              <ReservedRows count={group.examples.length} />
            ) : (
              <QuestionList examples={group.examples} off={off} onPick={onPick} reasonId={reasonId} />
            )}
          </div>
        ))}
      </div>
      {(split === null || byCompany.length > 0) && (
        <Collapsible.Root className="mt-6" disabled={split === null}>
          <Collapsible.Trigger className="group inline-flex min-h-11 items-center gap-1.5 font-semibold text-primary data-[disabled]:opacity-60">
            <ChevronRight aria-hidden="true" className="size-4 group-data-[panel-open]:rotate-90" />
            {s.showAll}
          </Collapsible.Trigger>
          <Collapsible.Panel id="examples" className="mt-2 grid gap-x-8 gap-y-5 sm:grid-cols-2">
            {byCompany.map((company) => (
              <div key={company.company}>
                <h3 className="m-0 mb-1 text-base font-semibold">{company.company}</h3>
                <QuestionList examples={company.examples} off={off} onPick={onPick} reasonId={reasonId} />
              </div>
            ))}
          </Collapsible.Panel>
        </Collapsible.Root>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ RD9: the covered companies

export function CompaniesCovered() {
  const c = deck.companies;
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="lg:pt-1">
      <h2 id={headingId} className="m-0 text-lg font-semibold">
        {c.heading}
      </h2>
      <p className="mt-1 mb-3 text-sm text-muted-foreground">{c.note}</p>
      <ul className="m-0 grid list-none grid-cols-2 gap-x-6 p-0">
        {FILERS.map((f) => (
          <li key={f.ticker} className="border-b border-border">
            <Link to={`/company/${f.ticker}/`} className="flex min-h-11 items-center justify-between gap-2 hover:text-primary">
              <span>{f.name}</span>
              <span className="text-sm font-semibold text-muted-foreground">{f.ticker}</span>
            </Link>
          </li>
        ))}
      </ul>
      <p className="mt-4 text-sm text-muted-foreground">{c.rulesNote}</p>
    </section>
  );
}
