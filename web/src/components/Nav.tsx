import { Link, useMatch } from "react-router";
import { FILERS } from "@/lib/pages.generated";

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
      <Link
        to={to}
        aria-current={active ? "page" : undefined}
        className="rounded-md px-3 py-2 text-sm hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none aria-[current=page]:bg-muted aria-[current=page]:font-semibold"
      >
        {label}
      </Link>
    </li>
  );
}

/** The main navigation: one link per page. "Company" opens the first filer; the company switcher lives on that page. */
export function Nav() {
  const firstFiler = FILERS[0].ticker;
  return (
    <nav aria-label="Main">
      <ul className="m-0 flex list-none flex-wrap gap-1 p-0">
        <NavItem to="/" label="Ask" match="/" />
        <NavItem to={`/company/${firstFiler}/`} label="Company" match="/company/*" end={false} />
        <NavItem to="/workspace/" label="Workspace" match="/workspace" />
        <NavItem to="/freshness/" label="Freshness" match="/freshness" />
        <NavItem to="/method/" label="Method & Evidence" match="/method" />
      </ul>
    </nav>
  );
}
