import { type ReactNode, useEffect, useId, useState } from "react";
import { FilingRows, RuleRows } from "@/components/SinceRows";
import { PageHeading } from "@/components/Layout";
import { deck } from "@/copy/deck";
import { formatDate } from "@/lib/format";
import { loadFreshness } from "@/lib/freshnessStore";
import { shellFreshness } from "@/lib/shellFreshness";
import { filingsPart, partOf, sinceState } from "@/lib/sinceView";
import { type Freshness as FreshnessData, type PendingFiling, relativeTime } from "./Freshness.lib";

// R26 and docs/specs/m5b-redesign.md RD27: every field of GET /api/freshness (monitor.py status_payload, and the "disabled" payload
// of monitor_routes.py) is shown or listed here as omitted, in plain words through the deck.
//   shown:   status, snapshot_as_of, checked_at, last_error_at, next_check_at, pending_count, pending_filings,
//            federal_register, unresolved (and the summary line, the header's own: lib/shellFreshness.ts)
//            since_data (M2a P4): the "Since our data" section after the list, as its own region
//   omitted: configured and enabled (status says disabled or unconfigured), duration_s (an internal timing), and in since_data
//            the counts `dropped` (they only decide whether a subsection says "nothing new") and the list's `rules_more` is shown as "20+"
// A row whose value is null is left out rather than shown empty. Dates are in words, in UTC. Everything from the server is a
// text node.

const w = deck.freshness.page;
const since = deck.since;

interface Loaded {
  freshness: FreshnessData;
  /** The clock at the moment the payload arrived, so the relative times do not move on a re-render. */
  now: number;
}

type State = { kind: "loading" } | { kind: "error"; detail: string } | ({ kind: "ok" } & Loaded);

/** N3 (the owner's NVDA pass): the label ends in a no-break space, which no layout drops; between two flex items a plain space is
 *  dropped, and NVDA read "ChecksWorking". */
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <li className="flex flex-wrap gap-x-3 border-b border-border py-2">
      <span className="min-w-48 font-semibold">{`${label}\u00a0`}</span>
      <span className="min-w-0 break-words">{children}</span>
    </li>
  );
}

const when = (iso: string, now: number): string => w.when(relativeTime(iso, now), formatDate(iso));

const pendingText = (p: PendingFiling): string =>
  w.pendingFiling({ ticker: p.ticker, form: p.form, filed: formatDate(p.filing_date), accession: p.accession_no });

function Fields({ freshness: f, now }: Loaded) {
  const fr = f.federal_register;
  const r = w.rows;
  return (
    <ul aria-label={w.list} className="m-0 list-none p-0">
      {f.status === null ? null : <Row label={r.checks}>{w.status(f.status)}</Row>}
      {f.snapshot_as_of === null ? null : <Row label={r.dataAsOf}>{formatDate(f.snapshot_as_of)}</Row>}
      {f.checked_at === null ? null : <Row label={r.lastChecked}>{when(f.checked_at, now)}</Row>}
      {f.last_error_at === null ? null : <Row label={r.lastFailed}>{when(f.last_error_at, now)}</Row>}
      {f.next_check_at === null ? null : <Row label={r.nextCheck}>{when(f.next_check_at, now)}</Row>}
      {f.pending_count === null ? null : <Row label={r.pending}>{String(f.pending_count)}</Row>}
      {f.pending_filings.length === 0 ? null : (
        <li className="py-2">
          <ul aria-label={w.pendingList} className="m-0 list-none space-y-1 p-0 pl-4">
            {f.pending_filings.map((p, i) => (
              <li key={`${p.accession_no ?? ""}-${i}`} className="break-words">
                {pendingText(p)}
              </li>
            ))}
          </ul>
        </li>
      )}
      {fr === null ? null : <Row label={r.rules}>{w.rules(fr.graph_count, fr.live_count, fr.new_since)}</Row>}
      {f.unresolved.length === 0 ? null : (
        <Row label={r.unresolved}>{f.unresolved.join(", ") /* one text run: as spans with a margin, NVDA read "TSMASML" */}</Row>
      )}
    </ul>
  );
}

const H3 = "mt-4 mb-1 text-base font-semibold";

/** M2a P4, after the details list and never inside it. See lib/sinceView.ts for which parts show. */
function SinceSection({ freshness }: { freshness: FreshnessData }) {
  const base = useId();
  const state = sinceState(freshness);
  if (state.kind === "hidden") return null;
  const heading = `${base}-since`;
  const filingsHeading = `${base}-filings`;
  const rulesHeading = `${base}-rules`;
  let body: ReactNode = <p className="m-0 text-muted-foreground">{since.notChecked}</p>;
  if (state.kind === "data") {
    const { data } = state;
    const filings = filingsPart(data, freshness.unresolved);
    const rules = partOf(data.rules, data.dropped.rules);
    if (filings.kind === "omitted" && rules.kind === "omitted") return null; // no bare heading
    const nothing = <p className="m-0 text-muted-foreground">{since.nothingNew}</p>;
    body = (
      <>
        {filings.kind === "omitted" ? null : (
          <>
            <h3 id={filingsHeading} className={H3}>{since.filingsHeading(formatDate(data.since))}</h3>
            {filings.kind === "rows" ? <FilingRows rows={filings.rows} totals={data.filing_totals} labelledBy={filingsHeading} /> : nothing}
          </>
        )}
        {rules.kind === "omitted" ? null : (
          <>
            <h3 id={rulesHeading} className={H3}>{since.rulesHeading(formatDate(data.rules_since))}</h3>
            {rules.kind === "rows" ? (
              <>
                <RuleRows rows={rules.rows} labelledBy={rulesHeading} />
                <p className="mt-2 mb-0 text-muted-foreground">{since.notLinked(rules.rows.length, data.rules_more)}</p>
              </>
            ) : (
              nothing
            )}
          </>
        )}
      </>
    );
  }
  return (
    <section aria-labelledby={heading} className="mt-8">
      <h2 id={heading} className="mb-2 text-lg font-semibold">{since.heading}</h2>
      {body}
    </section>
  );
}

export function Freshness() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    void loadFreshness(controller.signal).then((result) => {
      if (controller.signal.aborted || result.kind === "aborted") return; // nobody wants it any more: nothing to show
      setState(result.kind === "ok" ? { kind: "ok", freshness: result.freshness, now: Date.now() } : result);
    });
    return () => controller.abort(); // the request itself stops only when the header does not want it either
  }, []);

  const message = state.kind === "loading" ? w.loading : state.kind === "error" ? w.failed(state.detail) : "";
  return (
    <>
      <PageHeading title="Freshness" />
      <p className="mt-0 mb-2 max-w-[46rem]">{w.intro}</p>
      <output className="mb-4 block min-h-6 text-muted-foreground">
        {message}
      </output>
      {state.kind === "ok" ? (
        <>
          <p className="mb-4 font-semibold">{shellFreshness(state.freshness, false)}</p>
          <Fields freshness={state.freshness} now={state.now} />
          <SinceSection freshness={state.freshness} />
        </>
      ) : null}
    </>
  );
}
