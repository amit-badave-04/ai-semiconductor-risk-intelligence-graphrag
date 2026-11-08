import { useEffect, useRef } from "react";
import { Outlet, useLocation } from "react-router";
import { loadShellFreshness, useShellFreshness } from "@/lib/freshnessStore";
import { freshnessLine } from "@/pages/Freshness.lib";
import { Nav } from "./Nav";
import { TurnstileWidget } from "./TurnstileWidget";
import { useEvidenceDrawer } from "./useEvidenceDrawer";

const SITE_NAME = "semigraph";

/** The page's h1. It names the document (A7) and takes focus, via {@link Layout}, when the route changes. */
export function PageHeading({ title }: { title: string }) {
  useEffect(() => {
    document.title = `${title} · ${SITE_NAME}`;
  }, [title]);
  return (
    <h1 tabIndex={-1} className="mb-4 text-2xl font-semibold tracking-tight outline-none focus-visible:ring-2 focus-visible:ring-ring">
      {title}
    </h1>
  );
}

/** R26: the freshness line of the header, read from GET /api/freshness and worded by freshnessLine exactly as the legacy header
 *  words it (index.html:507-520, :639). The height is reserved so the line's arrival moves nothing; a failed request reads as
 *  the legacy page words it ("freshness check unavailable"); before an answer there is no text. Plain text, no live region. */
function ShellFreshness() {
  const { freshness, failed, at: now } = useShellFreshness(); // the clock of the arrival: the words never mix an old clock with new data
  const text = freshness === null && !failed ? "" : freshnessLine(freshness, now); // no payload: the legacy "freshness check unavailable"
  return <p id="shell-freshness" className="m-0 min-h-5 basis-full text-sm text-muted-foreground">{text}</p>;
}

/**
 * The app shell: skip link, banner, a named navigation, the main landmark (A7) and the one Turnstile host (R33(3)). On a route
 * change the evidence drawer closes WITHOUT taking focus back (A5) and focus moves to the new page's h1, which wins (A7); the
 * first render leaves both alone.
 */
export function Layout() {
  const { pathname } = useLocation();
  const previous = useRef(pathname);
  const main = useRef<HTMLElement>(null);
  const { close } = useEvidenceDrawer();

  // One /api/freshness request per page load: the store joins a request already in flight (StrictMode's second effect, the Freshness page).
  useEffect(() => loadShellFreshness(), []);

  useEffect(() => {
    if (previous.current === pathname) return;
    previous.current = pathname;
    close({ restoreFocus: false });
    main.current?.querySelector("h1")?.focus();
  }, [pathname, close]);

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="border-b border-border bg-card">
        <div className="mx-auto flex max-w-[1100px] flex-wrap items-center gap-x-6 gap-y-2 px-5 py-3">
          <p className="m-0 text-lg font-semibold tracking-tight">
            <span className="text-primary">semigraph</span>
          </p>
          <Nav />
          <ShellFreshness />
        </div>
      </header>
      <main id="main" ref={main} className="mx-auto max-w-[1100px] px-5 pt-6 pb-16">
        <Outlet />
      </main>
      <TurnstileWidget />
    </>
  );
}
