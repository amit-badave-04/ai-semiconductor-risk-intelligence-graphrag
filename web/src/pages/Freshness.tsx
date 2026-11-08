import { type ReactNode, useEffect, useState } from "react";
import { PageHeading } from "@/components/Layout";
import { loadFreshness } from "@/lib/freshnessStore";
import { type Freshness as FreshnessData, type PendingFiling, freshnessLine, relativeTime } from "./Freshness.lib";

// R26: every field of GET /api/freshness (monitor.py status_payload, and the "disabled" payload of monitor_routes.py) is
// shown or listed here as omitted, for the I4 classification.
//   shown:   status, snapshot_as_of, checked_at, last_error_at, next_check_at, pending_count, pending_filings,
//            federal_register, unresolved (and the header line, built from status, checked_at, last_error_at,
//            snapshot_as_of and pending_count)
//   omitted: configured and enabled (status says disabled or unconfigured), duration_s (an internal timing)
// A row whose value is null is left out rather than shown empty. Everything from the server is a text node.

const MSG_LOADING = "loading\u2026";

interface Loaded {
  freshness: FreshnessData;
  /** The clock at the moment the payload arrived, so the relative times do not move on a re-render. */
  now: number;
}

type State = { kind: "loading" } | { kind: "error"; detail: string } | ({ kind: "ok" } & Loaded);

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <li className="flex flex-wrap gap-x-3 border-b border-border py-2">
      <span className="min-w-40 font-semibold">{label}</span>
      <span>{children}</span>
    </li>
  );
}

function when(iso: string, now: number): string {
  return `${relativeTime(iso, now)} (${iso})`;
}

function pendingText(p: PendingFiling): string {
  const head = [p.ticker, p.form].filter((v): v is string => v !== null).join(" ");
  const filed = p.filing_date === null ? head : `${head} filed ${p.filing_date}`;
  return p.accession_no === null ? filed : `${filed} \u00b7 ${p.accession_no}`;
}

function Fields({ freshness: f, now }: Loaded) {
  const fr = f.federal_register;
  return (
    <ul aria-label="Freshness fields" className="m-0 list-none p-0">
      {f.status === null ? null : <Row label="Status">{f.status}</Row>}
      {f.snapshot_as_of === null ? null : <Row label="Data as of">{f.snapshot_as_of}</Row>}
      {f.checked_at === null ? null : <Row label="Last checked">{when(f.checked_at, now)}</Row>}
      {f.last_error_at === null ? null : <Row label="Last failed check">{when(f.last_error_at, now)}</Row>}
      {f.next_check_at === null ? null : <Row label="Next check">{when(f.next_check_at, now)}</Row>}
      {f.pending_count === null ? null : <Row label="Filings pending">{String(f.pending_count)}</Row>}
      {f.pending_filings.length === 0 ? null : (
        <li className="py-2">
          <ul aria-label="Pending filings" className="m-0 list-none space-y-1 p-0 pl-4">
            {f.pending_filings.map((p, i) => (
              <li key={`${p.accession_no ?? ""}-${i}`}>{pendingText(p)}</li>
            ))}
          </ul>
        </li>
      )}
      {fr === null ? null : (
        <Row label="Federal Register">{`in graph ${fr.graph_count} \u00b7 live ${fr.live_count} \u00b7 new ${fr.new_since}`}</Row>
      )}
      {f.unresolved.length === 0 ? null : (
        <Row label="Unresolved">
          {f.unresolved.map((t, i) => (
            <span key={`${t}-${i}`} className="mr-2">
              {t}
            </span>
          ))}
        </Row>
      )}
    </ul>
  );
}

export function Freshness() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    void loadFreshness(controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      setState(result.kind === "ok" ? { kind: "ok", freshness: result.freshness, now: Date.now() } : result);
    });
    return () => controller.abort(); // the request itself stops only when the header does not want it either
  }, []);

  const message = state.kind === "loading" ? MSG_LOADING : state.kind === "error" ? state.detail : "";
  return (
    <>
      <PageHeading title="Freshness" />
      <output className="mb-4 block min-h-6 text-muted-foreground">
        {message}
      </output>
      {state.kind === "ok" ? (
        <>
          <p className="mb-4">{freshnessLine(state.freshness, state.now)}</p>
          <Fields freshness={state.freshness} now={state.now} />
        </>
      ) : null}
    </>
  );
}
