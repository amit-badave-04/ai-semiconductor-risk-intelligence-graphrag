import { Clock } from "lucide-react";
import { Link, useMatch } from "react-router";
import { deck } from "@/copy/deck";
import { useShellFreshness } from "@/lib/freshnessStore";
import { FILERS } from "@/lib/pages.generated";
import { shellFreshness } from "@/lib/shellFreshness";

// docs/specs/m5b-redesign.md RD3: one named navigation holds the three pages, the freshness line (a link to the Freshness page)
// and "How it works" (the Method page). The current page is marked with aria-current and an underline, not by colour alone.

const LINK =
  "inline-flex min-h-11 items-center border-b-2 border-transparent px-1 text-base font-semibold text-muted-foreground hover:text-foreground aria-[current=page]:border-primary aria-[current=page]:text-foreground";

interface NavItemProps {
  to: string;
  label: string;
  /** The router path whose match marks this item as the current page. */
  match: string;
  end?: boolean;
}

function NavItem({ to, label, match, end = true }: NavItemProps) {
  const active = useMatch({ path: match, end }) !== null;
  return (
    <li>
      <Link to={to} aria-current={active ? "page" : undefined} className={LINK}>
        {label}
      </Link>
    </li>
  );
}

/** The freshness line: no link (and no text) until /api/freshness has answered; its row keeps its height meanwhile. */
function FreshnessLink() {
  const { freshness, failed } = useShellFreshness();
  const text = shellFreshness(freshness, failed);
  const active = useMatch({ path: "/freshness", end: true }) !== null;
  return (
    <li id="shell-freshness" className="min-h-11">
      {text !== "" && (
        <Link
          to="/freshness/"
          title={text}
          aria-current={active ? "page" : undefined}
          className="inline-flex min-h-11 max-w-full items-center gap-1.5 border-b-2 border-transparent text-sm text-muted-foreground hover:text-foreground aria-[current=page]:border-primary aria-[current=page]:text-foreground"
        >
          <Clock aria-hidden="true" className="size-4 shrink-0 text-primary" />
          <span className="sr-only">{deck.freshness.linkPrefix}</span>{" "}
          {/* the one-row header at lg (Layout.tsx lg:h-14) never wraps: a very long line is cut visually, its title and name keep it whole */}
          <span className="lg:max-w-[26rem] lg:truncate">{text}</span>
        </Link>
      )}
    </li>
  );
}

/** The main navigation. "Companies" opens the first filer; the company switcher lives on that page. */
export function Nav() {
  const firstFiler = FILERS[0].ticker;
  const words = deck.nav;
  return (
    <nav aria-label={words.label} className="flex min-w-0 basis-full flex-wrap items-center gap-x-6 md:flex-1 md:basis-auto">
      <ul className="m-0 flex list-none flex-wrap gap-x-5 p-0">
        <NavItem to="/" label={words.ask} match="/" />
        <NavItem to={`/company/${firstFiler}/`} label={words.companies} match="/company/*" end={false} />
        <NavItem to="/workspace/" label={words.workspace} match="/workspace" />
      </ul>
      <ul className="m-0 flex list-none flex-wrap items-center gap-x-5 p-0 md:ml-auto">
        <FreshnessLink />
        <NavItem to="/method/" label={words.howItWorks} match="/method" />
      </ul>
    </nav>
  );
}
