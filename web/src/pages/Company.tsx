import { useParams } from "react-router";
import { PageHeading } from "@/components/Layout";
import { FILERS } from "@/lib/pages.generated";
import { NotFound } from "./NotFound";

// Stub: the dossier is worker C's, I2. What stays here is the rule of R25: the router-decoded :ticker is untrusted, so a
// value that is not one of the generated filers renders the not-found view and nothing is fetched for it.
export function Company() {
  const { ticker } = useParams();
  const filer = FILERS.find((f) => f.ticker === ticker);
  if (!filer) return <NotFound />;
  return <PageHeading title={`${filer.name} (${filer.ticker})`} />;
}
