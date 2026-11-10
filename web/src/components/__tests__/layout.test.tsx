import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../../routes";

function at(url: string, basename = "") {
  return render(
    <MemoryRouter basename={basename} initialEntries={[`${basename}${url}`]}>
      <AppRoutes />
    </MemoryRouter>,
  );
}

describe("the app shell landmarks (A7)", () => {
  it("has a banner, a named navigation and a main landmark", () => {
    at("/");
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toBeInTheDocument();
  });

  it("starts with a skip link to the main landmark", async () => {
    const user = userEvent.setup();
    at("/");
    await user.tab();
    const skip = screen.getByRole("link", { name: "Skip to content" });
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute("href", "#main");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
  });

  it("lists the pages under the basename (RD3: Ask, Companies, Workspace, the freshness line, How it works) and marks the current one", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 503 }))); // the freshness line: "Data status unavailable"
    at("/workspace/", "/v2");
    const nav = within(screen.getByRole("navigation", { name: "Main" }));
    await nav.findByRole("link", { name: "Data status unavailable" });
    const links = nav.getAllByRole("link");
    // React Router joins the basename and "/" without a trailing slash; the server answers /v2 with a redirect to /v2/ (R9e).
    expect(links.map((l) => [l.textContent, l.getAttribute("href")])).toEqual([
      ["Ask", "/v2"],
      ["Companies", "/v2/company/NVDA/"],
      ["Workspace", "/v2/workspace/"],
      ["Data status unavailable", "/v2/freshness/"],
      ["How it works", "/v2/method/"],
    ]);
    expect(links.filter((l) => l.getAttribute("aria-current") === "page").map((l) => l.textContent)).toEqual(["Workspace"]);
    vi.unstubAllGlobals();
  });

  it("marks Companies current for any company page", () => {
    at("/company/AMD/");
    const nav = within(screen.getByRole("navigation", { name: "Main" }));
    expect(nav.getByRole("link", { name: "Companies" })).toHaveAttribute("aria-current", "page");
    expect(nav.getByRole("link", { name: "Ask" })).not.toHaveAttribute("aria-current");
  });

  it("links the brand to the Ask page, outside the main navigation", () => {
    at("/method/");
    const brand = screen.getByRole("link", { name: "semigraph" });
    expect(brand).toHaveAttribute("href", "/");
    expect(within(screen.getByRole("navigation", { name: "Main" })).queryByRole("link", { name: "semigraph" })).toBeNull();
  });
});

describe("focus on a route change (A7)", () => {
  it("does not move focus on the first render", () => {
    at("/");
    expect(document.body).toHaveFocus();
  });

  it("moves focus to the new page's h1 when a nav link is followed", async () => {
    const user = userEvent.setup();
    at("/");
    await user.click(screen.getByRole("link", { name: "How it works" }));
    const heading = screen.getByRole("heading", { level: 1, name: "Method & Evidence" });
    expect(heading).toHaveFocus();
    expect(heading).toHaveAttribute("tabindex", "-1");
    expect(document.title).toBe("Method & Evidence · semigraph");
  });

  it("keeps the focus where the user put it when nothing navigated", async () => {
    const user = userEvent.setup();
    at("/method/");
    await user.tab();
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveFocus();
  });
});
