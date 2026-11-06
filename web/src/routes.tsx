import type { ComponentType } from "react";
import { Route, Routes } from "react-router";
import { Layout } from "@/components/Layout";
import { SPA_PAGES } from "@/lib/pages.generated";
import { Ask } from "@/pages/Ask";
import { Company } from "@/pages/Company";
import { Freshness } from "@/pages/Freshness";
import { Method } from "@/pages/Method";
import { NotFound } from "@/pages/NotFound";
import { Workspace } from "@/pages/Workspace";

export type PagePath = (typeof SPA_PAGES)[number];

/** One component per generated page path: a page added to the server's SPA_PAGES without a component here does not compile. */
export const PAGE_COMPONENTS: Record<PagePath, ComponentType> = {
  "/": Ask,
  "/company/{ticker}/": Company,
  "/workspace/": Workspace,
  "/freshness/": Freshness,
  "/method/": Method,
};

/** The server's page path as a router path: "/company/{ticker}/" becomes "company/:ticker", "/" becomes "" (the index). */
export function routePath(page: string): string {
  return page.replace(/^\/+|\/+$/g, "").replace(/\{(\w+)\}/g, ":$1");
}

/** The route tree, built from the generated page list; any other path renders the not-found view inside the shell. */
export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        {SPA_PAGES.map((page) => {
          const Page = PAGE_COMPONENTS[page];
          const path = routePath(page);
          return path === "" ? (
            <Route key={page} index element={<Page />} />
          ) : (
            <Route key={page} path={path} element={<Page />} />
          );
        })}
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
