import { type ReactNode, useEffect, useState } from "react";
import { PageHeading } from "@/components/Layout";
import { deck } from "@/copy/deck";
import { formatDate } from "@/lib/format";
import { loadFreshness } from "@/lib/freshnessStore";
import { shellFreshness } from "@/lib/shellFreshness";
import { type Freshness as FreshnessData, type PendingFiling, relativeTime } from "./Freshness.lib";

// R26 and docs/specs/m5b-redesign.md RD27: every field of GET /api/freshness (monitor.py status_payload, and the "disabled" payload
// of monitor_routes.py) is shown or listed here as omitted, in plain words through the deck.
//   shown:   status, snapshot_as_of, checked_at, last_error_at, next_check_at, pending_count, pending_filings,
//            federal_register, unresolved (and the summary line, the header's own: lib/shellFreshness.ts)
//   omitted: configured and enabled (status says disabled or unconfigured), duration_s (an internal timing)
// A row whose value is null is left out rather than shown empty. Dates are in words, in UTC. Everything from the server is a
// text node.

const w = deck.freshness.page;

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
        </>
      ) : null}
    </>
  );
}
