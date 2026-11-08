import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router";
import { PageHeading } from "@/components/Layout";
import { type DossierResult, fetchDossier } from "@/lib/company";
import { FILERS } from "@/lib/pages.generated";
import { CompanySwitcher, DossierBlocks } from "./CompanyBlocks";
import { NotFound } from "./NotFound";

const MSG_LOADING = "loading\u2026";

/** The not-found view after a dossier 404. The company's own h1 (which may have had focus) is gone, so the new h1 takes the
 *  focus unless the user has put it somewhere else since. */
function DossierNotFound() {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const active = document.activeElement;
    if (active === null || active === document.body) box.current?.querySelector("h1")?.focus();
  }, []);
  return (
    <div ref={box}>
      <NotFound />
    </div>
  );
}

function CompanyPage({ ticker, name }: { ticker: string; name: string }) {
  const [result, setResult] = useState<DossierResult | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchDossier(ticker, controller.signal).then(
      (next) => {
        if (!controller.signal.aborted) setResult(next);
      },
      () => undefined, // only an abort rejects: the page is gone or the ticker changed, nothing to show
    );
    return () => controller.abort();
  }, [ticker]);

  if (result?.kind === "notFound") return <DossierNotFound />;
  const message = result === null ? MSG_LOADING : result.kind === "error" ? result.detail : "";
  return (
    <>
      <PageHeading title={`${name} (${ticker})`} />
      <output className="mb-4 block min-h-6 text-muted-foreground">
        {message}
      </output>
      <CompanySwitcher current={ticker} />
      {result?.kind === "ok" ? <DossierBlocks dossier={result.dossier} /> : null}
    </>
  );
}

// R25: the router-decoded :ticker is untrusted, so a value that is not one of the generated filers renders the not-found view
// and nothing is fetched for it. The key resets the page state when the switcher moves to another company.
export function Company() {
  const { ticker } = useParams();
  const filer = FILERS.find((f) => f.ticker === ticker);
  if (!filer) return <NotFound />;
  return <CompanyPage key={filer.ticker} ticker={filer.ticker} name={filer.name} />;
}
