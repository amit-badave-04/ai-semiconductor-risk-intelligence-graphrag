import { useEffect, useRef } from "react";
import { Link, Outlet, useLocation } from "react-router";
import { deck } from "@/copy/deck";
import { loadShellFreshness } from "@/lib/freshnessStore";
import { Nav } from "./Nav";
import { TurnstileWidget } from "./TurnstileWidget";
import { useEvidenceDrawer } from "./useEvidenceDrawer";

const SITE_NAME = "semigraph";

interface PageHeadingProps {
  title: string;
  /** The tab's name when the h1 is a sentence (the Ask page's "Ask about 13 chip and AI companies." is "Ask"). */
  documentTitle?: string;
  /** The display size of the type scale (RD1: 44 px), for the Ask page's headline only. */
  display?: boolean;
}

/** The page's h1. It names the document (A7) and takes focus, via {@link Layout}, when the route changes. */
export function PageHeading({ title, documentTitle = title, display = false }: PageHeadingProps) {
  useEffect(() => {
    document.title = `${documentTitle} \u00b7 ${SITE_NAME}`;
  }, [documentTitle]);
  return (
    <h1
      tabIndex={-1}
      className={`mb-4 font-semibold tracking-tight ${display ? "text-2xl font-bold sm:text-3xl" : "text-2xl"}`}
    >
      {title}
    </h1>
  );
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
        {deck.shell.skip}
      </a>
      <header className="border-b border-border bg-card">
        <div className="header-row mx-auto flex max-w-[1100px] flex-wrap items-center gap-x-8 px-5 lg:h-14 lg:flex-nowrap">
          <Link to="/" className="inline-flex min-h-11 items-center text-xl font-bold tracking-tight text-foreground">
            {SITE_NAME}
          </Link>
          <Nav />
        </div>
      </header>
      <main id="main" ref={main} className="mx-auto max-w-[1100px] px-5 pt-6 pb-16">
        <Outlet />
      </main>
      <TurnstileWidget />
    </>
  );
}
