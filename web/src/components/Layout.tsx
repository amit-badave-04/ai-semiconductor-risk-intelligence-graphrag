import { useEffect, useRef } from "react";
import { Outlet, useLocation } from "react-router";
import { Nav } from "./Nav";

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

/**
 * The app shell: skip link, banner, a named navigation and the main landmark (A7). On a route change focus moves to the new
 * page's h1; the first render leaves focus alone.
 */
export function Layout() {
  const { pathname } = useLocation();
  const previous = useRef(pathname);
  const main = useRef<HTMLElement>(null);

  useEffect(() => {
    if (previous.current === pathname) return;
    previous.current = pathname;
    main.current?.querySelector("h1")?.focus();
  }, [pathname]);

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
        </div>
      </header>
      <main id="main" ref={main} className="mx-auto max-w-[1100px] px-5 pt-6 pb-16">
        <Outlet />
      </main>
    </>
  );
}
