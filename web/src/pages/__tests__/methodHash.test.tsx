import { act, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { MemoryRouter, useNavigate } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetFreshnessStore } from "@/lib/freshnessStore";
import { AppRoutes } from "../../routes";

// docs/specs/m5b-redesign.md RD21/SR10 on the Method page and the R2 review's A3: "How reliable change claims are" (Read with
// care, under an answer) opens Method at its reliability section with focus on that heading, not on the h1. Layout's route
// focus honours a hash that names a section registered for the new page when that heading is already there; anything else is
// the h1, as before (A7). The hash is matched against a fixed list, never used as a selector.

const go: { to: ((url: string) => void) | null } = { to: null };
function Probe() {
  const navigate = useNavigate();
  useEffect(() => {
    go.to = (url) => void navigate(url);
  }, [navigate]);
  return null;
}

function mount(url = "/") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Probe />
      <AppRoutes />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  resetFreshnessStore();
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 404 })));
});
afterEach(() => vi.unstubAllGlobals());

const reliability = () => screen.getByRole("heading", { level: 2, name: "How reliable are change claims?" });
const h1 = () => screen.getByRole("heading", { level: 1 });

describe("the Method page's sections and the hash (RD21, A3)", () => {
  it("a route change to /method/#method-reliability focuses that section's heading, not the h1", async () => {
    mount("/workspace/");
    act(() => go.to?.("/method/#method-reliability"));
    await waitFor(() => expect(reliability()).toHaveFocus());
  });

  it("a route change to /method/ with no hash focuses the h1, as before (A7)", async () => {
    mount("/workspace/");
    act(() => go.to?.("/method/"));
    await waitFor(() => expect(h1()).toHaveFocus());
    expect(h1()).toHaveTextContent("Method & Evidence");
  });

  it.each(["#nope", '#method-reliability"]', "#<img>", "#key-numbers", "#main"])("a route change with %s focuses the h1 and throws nothing", async (hash) => {
    mount("/workspace/");
    act(() => go.to?.(`/method/${hash}`));
    await waitFor(() => expect(h1()).toHaveFocus());
  });

  it("a page opened at the section focuses it, and a later hash change moves focus to the other section", async () => {
    mount("/method/#method-reliability");
    await waitFor(() => expect(reliability()).toHaveFocus());
    act(() => go.to?.("/method/#method-checks"));
    await waitFor(() => expect(screen.getByRole("heading", { level: 2, name: "What the checks mean" })).toHaveFocus());
  });

  it("both section headings take focus programmatically", () => {
    mount("/method/");
    for (const heading of screen.getAllByRole("heading", { level: 2 })) expect(heading).toHaveAttribute("tabindex", "-1");
  });
});
