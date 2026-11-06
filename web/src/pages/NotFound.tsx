import { Link } from "react-router";
import { PageHeading } from "@/components/Layout";

/** What an address that is not a page shows (the server answers the SPA for any extension-less /v2/ path, R9c). */
export function NotFound() {
  return (
    <>
      <PageHeading title="Page not found" />
      <p className="mb-4 text-muted-foreground">There is nothing at this address.</p>
      <Link className="text-primary underline underline-offset-4" to="/">
        Back to the start page
      </Link>
    </>
  );
}
