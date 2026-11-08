import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SPA_PAGES } from "@/lib/pages.generated";
import { AppRoutes, PAGE_COMPONENTS, routePath } from "../routes";

function at(url: string, basename = "") {
  return render(
    <MemoryRouter basename={basename} initialEntries={[`${basename}${url}`]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

const URL_OF = (page: string) => page.replace("{ticker}", "NVDA");

afterEach(() => vi.restoreAllMocks());

describe("the route table is built from the generated SPA_PAGES", () => {
  it("maps every page path to a component, so a new page without one cannot compile or pass", () => {
    expect(Object.keys(PAGE_COMPONENTS).sort()).toEqual([...SPA_PAGES].sort());
  });

  it.each([
    ["/", ""],
    ["/company/{ticker}/", "company/:ticker"],
    ["/workspace/", "workspace"],
    ["/freshness/", "freshness"],
    ["/method/", "method"],
  ])("%s becomes the router path %j", (page, expected) => {
    expect(routePath(page)).toBe(expected);
  });
});

describe("every page renders one h1 in the main landmark and names the document", () => {
  it.each(SPA_PAGES.map((p) => [p, URL_OF(p)]))("%s", (_page, url) => {
    at(url);
    const headings = screen.getAllByRole("heading", { level: 1 });
    expect(headings).toHaveLength(1);
    expect(screen.getByRole("main")).toContainElement(headings[0]);
    expect(document.title).toBe(`${headings[0].textContent} · semigraph`);
  });

  it("under the /v2 basename the same pages answer at /v2/...", () => {
    at("/workspace/", "/v2");
    expect(screen.getByRole("heading", { level: 1, name: "Workspace" })).toBeInTheDocument();
  });
});

describe("the company page validates its router parameter and makes no request for a bad one", () => {
  it("shows the company for a ticker of the generated list", () => {
    at("/company/NVDA/");
    expect(screen.getByRole("heading", { level: 1, name: "Nvidia (NVDA)" })).toBeInTheDocument();
  });

  it.each(["XXXX", "nvda", "..%2F..%2Fapi%2Fadmin%2Fstate", "NVDA%0A", "%3Cscript%3E", "Samsung"])(
    "the not-found view for %s, with no request",
    (ticker) => {
      const fetchSpy = vi.spyOn(globalThis, "fetch");
      at(`/company/${ticker}/`);
      expect(screen.getByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
      // the shell's own GET /api/freshness (the freshness line, R26) is not a request the page made for the ticker
      const asked = fetchSpy.mock.calls.map(([input]) => String(input)).filter((url) => !url.includes("/api/freshness"));
      expect(asked).toEqual([]);
    },
  );
});

describe("an unknown path renders the not-found view", () => {
  it.each(["/nope/", "/api/nope", "/company/", "/workspace/extra/"])("%s", (url) => {
    at(url);
    expect(screen.getByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(document.title).toBe("Page not found · semigraph");
    expect(screen.getByRole("link", { name: "Back to the start page" })).toHaveAttribute("href", "/");
  });
});
