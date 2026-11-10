import { Link } from "react-router";
import { PageHeading } from "@/components/Layout";
import { deck } from "@/copy/deck";

/** What an address that is not a page shows (the server answers the SPA for any extension-less /v2/ path, R9c). */
export function NotFound() {
  const w = deck.notFound;
  return (
    <>
      <PageHeading title={w.title} />
      <p className="mb-4 text-muted-foreground">{w.text}</p>
      <Link className="text-primary underline underline-offset-4" to="/">
        {w.back}
      </Link>
    </>
  );
}
